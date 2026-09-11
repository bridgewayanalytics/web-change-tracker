"""
Backfill document extraction + QA eval for rows extracted before 2026-08-19.

Before that date, the document extraction agent ran WITHOUT before/after HTML context
(commit d4dbb5e added HTML injection on 2026-08-19). The QA agent running today DOES
receive that HTML from S3, creating an unfair comparison. This script:

  1. Finds all pre-Aug-19 document extraction rows in document_extractions_table.jsonl
  2. For each unique agent_call_id:
       a. Fetches before/after HTML from S3 (same snapshots the QA agent uses)
       b. Re-runs the document extraction agent WITH HTML context
       c. Replaces the old extraction rows in document_extractions_table.jsonl
       d. Deletes stale QA eval results for this agent_call_id
       e. Runs QA eval on the new extraction rows
  3. Writes updated JSONLs back to S3

Usage (run in ECS or locally with AWS + OpenAI creds):
  python scripts/backfill_html_extraction_and_eval.py [--limit N] [--dry-run] [--skip-eval]

Flags:
  --limit N      Process at most N unique agent_call_ids (default: all)
  --dry-run      Fetch HTML and log what would happen; no S3 writes, no agent calls
  --skip-eval    Re-run extraction only; skip QA eval step
  --call-ids X   Comma-separated list of specific agent_call_ids to process
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("backfill_html_extraction")

_BUCKET = (
    os.environ.get("CHANGELOG_BUCKET", "").strip()
    or os.environ.get("BUBBLE_ARTIFACT_BUCKET", "").strip()
    or "web-change-tracker-prod-artifacts-815039343351"
)
_EXTRACTIONS_KEY = "alerts/document_extractions_table.jsonl"
_ALERTS_KEY = "alerts/alerts_table.jsonl"
_DOC_EVAL_KEY = "alerts/doc_eval_results_table.jsonl"
_HTML_CUTOFF = "2026-08-19"  # commit d4dbb5e — HTML added to extraction agent


def _s3():
    import boto3
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _load_jsonl(client, key: str) -> list[dict]:
    try:
        body = client.get_object(Bucket=_BUCKET, Key=key)["Body"].read().decode("utf-8")
        rows = []
        for line in body.split("\n"):
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return rows
    except Exception as e:
        log.warning("Could not load %s: %s", key, e)
        return []


def _put_jsonl(client, key: str, rows: list[dict], dry_run: bool, label: str) -> None:
    if dry_run:
        log.info("[DRY RUN] Would write %d rows to %s", len(rows), key)
        return
    body = "\n".join(json.dumps(r, default=str) for r in rows)
    client.put_object(
        Bucket=_BUCKET,
        Key=key,
        Body=body.encode("utf-8"),
        ContentType="application/x-ndjson",
        Metadata={"source": label},
    )
    log.info("Wrote %d rows to s3://%s/%s", len(rows), _BUCKET, key)


def _load_secrets() -> None:
    try:
        from bubble.ssm_loader import load_openai_env_from_ssm, load_db_env_from_ssm
        load_openai_env_from_ssm()
        load_db_env_from_ssm()
        log.info("Loaded secrets from SSM")
    except Exception as e:
        log.warning("SSM loader failed (may be fine locally): %s", e)
    if not os.environ.get("OPENAI_API_KEY"):
        log.warning("OPENAI_API_KEY not set — agent calls will fail")


def _fetch_html(run_id: str, target_id: str, run_timestamp) -> tuple[str, str]:
    """Fetch before/after HTML from S3 — same call as doc_eval_agent uses."""
    try:
        from storage.page_change_s3 import fetch_page_html
        if isinstance(run_timestamp, str):
            from datetime import datetime, timezone
            run_timestamp = datetime.fromisoformat(run_timestamp).timestamp()
        before, after = fetch_page_html(run_id, target_id, run_timestamp)
        return before, after
    except Exception as e:
        log.warning("Could not fetch HTML for run_id=%s target_id=%s: %s", run_id, target_id, e)
        return "", ""


def run(
    limit: int | None = None,
    dry_run: bool = False,
    skip_eval: bool = False,
    call_ids_filter: set[str] | None = None,
) -> None:
    os.environ.setdefault("PAGE_CHANGE_AGENT_ENABLED", "true")
    os.environ.setdefault("PGVECTOR_ENABLED", "true")
    os.environ.setdefault("AWS_REGION", "us-east-1")
    # page_change_s3.fetch_page_html reads this bucket var (same bucket as changelog)
    os.environ.setdefault("PAGE_CHANGE_SNAPSHOT_BUCKET", _BUCKET)
    os.environ.setdefault("HTML_SNAPSHOT_BUCKET", _BUCKET)

    _load_secrets()

    client = _s3()

    log.info("=== Loading tables from S3 ===")
    all_extractions = _load_jsonl(client, _EXTRACTIONS_KEY)
    all_alerts = _load_jsonl(client, _ALERTS_KEY)
    all_eval = _load_jsonl(client, _DOC_EVAL_KEY)
    log.info(
        "Loaded: %d extraction rows, %d alert rows, %d eval rows",
        len(all_extractions), len(all_alerts), len(all_eval),
    )

    # Index alerts by agent_call_id for fast lookup
    alert_by_call_id: dict[str, dict] = {}
    for a in all_alerts:
        cid = a.get("agent_call_id", "")
        if cid and cid not in alert_by_call_id:
            alert_by_call_id[cid] = a

    # Find pre-Aug-19 extraction rows
    pre_rows = [
        r for r in all_extractions
        if (r.get("run_timestamp") or "") < _HTML_CUTOFF
        and r.get("library_item_url", "").strip().lower() not in ("", "n/a")
        and r.get("extraction_source") != "transcript"
    ]
    log.info("Pre-%s extraction rows: %d", _HTML_CUTOFF, len(pre_rows))

    # Group by agent_call_id
    by_call: dict[str, list[dict]] = {}
    for r in pre_rows:
        cid = r.get("agent_call_id", "")
        if cid:
            by_call.setdefault(cid, []).append(r)

    log.info("Unique agent_call_ids to process: %d", len(by_call))

    if call_ids_filter:
        by_call = {k: v for k, v in by_call.items() if k in call_ids_filter}
        log.info("Filtered to %d specified call_id(s)", len(by_call))

    call_ids = list(by_call.keys())
    if limit:
        call_ids = call_ids[:limit]
        log.info("Applying --limit %d → processing %d call_ids", limit, len(call_ids))

    if not call_ids:
        log.info("Nothing to process. Exiting.")
        return

    # Track what we produce
    new_extraction_rows: list[dict] = []
    processed_call_ids: set[str] = set()
    skipped_no_html: list[str] = []
    failed_extraction: list[str] = []

    from bubble.document_agent import extract_document_data
    from storage.alert_s3 import _build_doc_extraction_rows

    for i, cid in enumerate(call_ids, 1):
        group = by_call[cid]
        first = group[0]

        run_id = str(first.get("run_id") or "")
        target_id = str(first.get("target_id") or "")
        run_timestamp = first.get("run_timestamp") or ""
        source_url = str(first.get("source_url") or "")
        config_hash = str(first.get("config_hash") or "")

        # Unique library_item_urls for this call
        urls_seen: set[str] = set()
        items: list[dict] = []
        for r in group:
            url = r.get("library_item_url", "")
            if url and url not in urls_seen:
                urls_seen.add(url)
                items.append({
                    "preliminary_title": r.get("library_item_title") or r.get("document_title") or "",
                    "url": url,
                    "file_name": r.get("library_item_file_name") or "",
                })

        alert_row = alert_by_call_id.get(cid)
        log.info(
            "[%d/%d] agent_call_id=%s  run=%s  target=%s  docs=%d  alert_found=%s",
            i, len(call_ids), cid[:20], run_id, target_id, len(items), alert_row is not None,
        )

        # Fetch HTML (same source as QA agent)
        before_html, after_html = _fetch_html(run_id, target_id, run_timestamp)
        if not after_html:
            log.warning("  -> No HTML available for run_id=%s target_id=%s — SKIPPING", run_id, target_id)
            skipped_no_html.append(cid)
            continue

        log.info(
            "  -> HTML: before=%d chars, after=%d chars",
            len(before_html), len(after_html),
        )

        if dry_run:
            log.info("  [DRY RUN] Would re-extract %d document(s) with HTML", len(items))
            for item in items:
                log.info("    doc: %s — %s", item["preliminary_title"][:60], item["url"][-50:])
            continue

        # Re-run document extraction WITH HTML for each document
        doc_extractions: list[dict] = []
        for item in items:
            doc_name = item["preliminary_title"]
            doc_url = item["url"]
            log.info("  -> Extracting: %s", doc_name[:70])
            try:
                results = extract_document_data(
                    document_name=doc_name,
                    document_url=doc_url,
                    alert_context=alert_row,
                    before_html=before_html,
                    after_html=after_html,
                )
                if results:
                    for result_dict in results:
                        doc_extractions.append({"item": item, "extraction": result_dict})
                    log.info("     -> %d agenda item(s) extracted", len(results))
                else:
                    log.warning("     -> No output from extraction agent")
                    failed_extraction.append(f"{cid}|{doc_url}")
            except Exception as e:
                log.error("     -> Extraction FAILED: %s", e)
                failed_extraction.append(f"{cid}|{doc_url}")

        if not doc_extractions:
            log.warning("  -> No extractions produced for %s — keeping original rows", cid[:20])
            continue

        rows = _build_doc_extraction_rows(
            doc_extractions,
            run_id=run_id,
            run_timestamp_iso=run_timestamp,
            target_id=target_id,
            source_url=source_url,
            agent_call_id=cid,
            config_hash=config_hash,
        )
        # Stamp rerun timestamp
        for r in rows:
            r["last_rerun_at"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())

        log.info("  -> Built %d new extraction row(s)", len(rows))
        new_extraction_rows.extend(rows)
        processed_call_ids.add(cid)

    # --- Write updated document_extractions_table.jsonl ---
    if not dry_run and processed_call_ids:
        log.info("=== Updating document_extractions_table.jsonl ===")
        # Keep rows not being replaced (different call_id OR post-cutoff)
        surviving = [
            r for r in all_extractions
            if r.get("agent_call_id") not in processed_call_ids
        ]
        combined = surviving + new_extraction_rows
        combined.sort(key=lambda r: str(r.get("run_timestamp") or ""), reverse=True)
        _put_jsonl(client, _EXTRACTIONS_KEY, combined, dry_run, "backfill-html-extraction")
        log.info(
            "Extractions: %d kept + %d new = %d total (replaced %d call_ids)",
            len(surviving), len(new_extraction_rows), len(combined), len(processed_call_ids),
        )

    # --- QA eval step ---
    if not skip_eval and not dry_run and processed_call_ids:
        log.info("=== Running QA eval on %d re-extracted call_ids ===", len(processed_call_ids))

        # Delete stale eval results for these call_ids
        surviving_eval = [
            r for r in all_eval
            if r.get("agent_call_id") not in processed_call_ids
        ]
        removed_eval = len(all_eval) - len(surviving_eval)
        if removed_eval:
            log.info("Removed %d stale QA eval entries before re-eval", removed_eval)
            _put_jsonl(client, _DOC_EVAL_KEY, surviving_eval, dry_run, "backfill-html-extraction-pre-eval")

        from eval.run_doc_eval import run as run_eval
        log.info("Running QA eval for %d call_ids...", len(processed_call_ids))
        try:
            eval_rows = run_eval(agent_call_ids=list(processed_call_ids))
            log.info("QA eval complete: %d eval row(s) written", len(eval_rows))
        except Exception as e:
            log.error("QA eval failed: %s", e)

    # --- Final summary ---
    log.info("=== Summary ===")
    log.info("  Processed: %d call_ids", len(processed_call_ids))
    log.info("  Skipped (no HTML in S3): %d", len(skipped_no_html))
    log.info("  Extraction failures: %d", len(failed_extraction))
    if skipped_no_html:
        log.info("  No-HTML call_ids: %s", skipped_no_html[:5])
    if failed_extraction:
        log.info("  Failed: %s", failed_extraction[:5])
    if dry_run:
        log.info("  [DRY RUN] No S3 writes made.")


def main():
    parser = argparse.ArgumentParser(description="Backfill doc extraction + QA eval with HTML context")
    parser.add_argument("--limit", type=int, default=None, help="Max call_ids to process")
    parser.add_argument("--dry-run", action="store_true", help="No writes, no agent calls")
    parser.add_argument("--skip-eval", action="store_true", help="Skip QA eval step")
    parser.add_argument("--call-ids", type=str, default=None, help="Comma-separated call_ids to process")
    args = parser.parse_args()

    call_ids_filter = None
    if args.call_ids:
        call_ids_filter = {x.strip() for x in args.call_ids.split(",") if x.strip()}

    run(
        limit=args.limit,
        dry_run=args.dry_run,
        skip_eval=args.skip_eval,
        call_ids_filter=call_ids_filter,
    )


if __name__ == "__main__":
    main()
