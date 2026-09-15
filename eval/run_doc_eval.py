"""
Document extraction QA evaluation pipeline entry point.

Usage:
  python -m eval.run_doc_eval                        # evaluate last 20 eligible rows
  python -m eval.run_doc_eval --limit 10             # evaluate last N rows
  python -m eval.run_doc_eval --agent-call-ids a,b   # evaluate specific rows by agent_call_id
  python -m eval.run_doc_eval --dry-run              # print selected rows, no agent calls

Eligible rows: most recent rows from document_extractions_table.jsonl,
excluding transcript rows (extraction_source == "transcript") and rows
with no real library_item_url.
"""

import argparse
import json
import logging
import os
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)

_DEFAULT_LIMIT = 20
_DOC_EXTRACTIONS_KEY = "alerts/document_extractions_table.jsonl"
_ALERTS_KEY = "alerts/alerts_table.jsonl"
_RESULTS_KEY = "alerts/doc_eval_results_table.jsonl"
_DEFAULT_BUCKET = "web-change-tracker-prod-artifacts-815039343351"


def _get_bucket() -> str:
    return (
        os.environ.get("CHANGELOG_BUCKET", "").strip()
        or os.environ.get("BUBBLE_ARTIFACT_BUCKET", "").strip()
        or _DEFAULT_BUCKET
    )


def _s3_client():
    import boto3
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _load_alert_lookup() -> dict[tuple[str, str], dict]:
    """Return a dict keyed by (agent_call_id, library_item_url) → alert row.
    Handles compound (semicolon-separated) library_item_url values by also
    indexing each individual URL so single-URL doc extraction rows match.
    """
    bucket = _get_bucket()
    client = _s3_client()
    lookup: dict[tuple[str, str], dict] = {}
    try:
        resp = client.get_object(Bucket=bucket, Key=_ALERTS_KEY)
        lines = resp["Body"].read().decode("utf-8").strip().split("\n")
    except Exception as e:
        log.warning("Could not load alerts_table.jsonl for alert context: %s", e)
        return lookup
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            cid = row.get("agent_call_id", "")
            url = row.get("library_item_url", "") or ""
            if not cid:
                continue
            lookup[(cid, url)] = row
            # Also index individual URLs from semicolon-separated compound values
            if ";" in url:
                for single_url in url.split(";"):
                    single_url = single_url.strip()
                    if single_url:
                        lookup[(cid, single_url)] = row
        except json.JSONDecodeError:
            continue
    return lookup


def _load_doc_rows(agent_call_ids: list[str] | None, limit: int = _DEFAULT_LIMIT, library_item_url: str | None = None) -> list[dict]:
    # `limit` is only used for the agent_call_ids path; bulk path selects all eligible rows.
    bucket = _get_bucket()
    client = _s3_client()
    try:
        resp = client.get_object(Bucket=bucket, Key=_DOC_EXTRACTIONS_KEY)
        lines = resp["Body"].read().decode("utf-8").strip().split("\n")
    except Exception as e:
        log.error("Failed to load document_extractions_table.jsonl: %s", e)
        return []

    all_rows: list[dict] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            all_rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if agent_call_ids is not None:
        result = [r for r in all_rows if r.get("agent_call_id") in agent_call_ids]
        if library_item_url:
            result = [r for r in result if r.get("library_item_url") == library_item_url]
        # Deduplicate by (library_item_url, eval_row_key) — old and new format rows for the
        # same agenda item within the same document can coexist in the JSONL; keep the most
        # recent. Include URL so rows from different documents with the same std_id don't collapse.
        from eval.doc_eval_agent import make_doc_eval_row_key
        dedup: dict[tuple, dict] = {}
        for row in result:
            key = (row.get("library_item_url", ""), make_doc_eval_row_key(row))
            existing = dedup.get(key)
            if existing is None or (row.get("run_timestamp", "") > existing.get("run_timestamp", "")):
                dedup[key] = row
        result = list(dedup.values())
        log.info("Selected %d row(s) by agent_call_id (after dedup)", len(result))
        return result

    # Bulk path: evaluate all eligible rows up to the upper bound (most recently
    # synced-to-Bubble alert), skipping rows already QA'd and transcript rows.

    # --- Upper bound: run_timestamp of the most recently synced alert ---
    upper_bound: str | None = None
    try:
        alerts_resp = client.get_object(Bucket=bucket, Key=_ALERTS_KEY)
        for line in alerts_resp["Body"].read().decode("utf-8").split("\n"):
            if not line.strip():
                continue
            try:
                a = json.loads(line)
                if a.get("bubble_sync_status") == "synced":
                    ts = a.get("run_timestamp", "")
                    if ts and (upper_bound is None or ts > upper_bound):
                        upper_bound = ts
            except json.JSONDecodeError:
                continue
    except Exception as e:
        log.warning("Could not load alerts_table.jsonl for upper bound: %s", e)

    if upper_bound:
        log.info("Bulk doc eval upper bound (latest bubble sync): %s", upper_bound)
    else:
        log.info("No synced alerts found; evaluating all eligible rows")

    # --- Already-QA'd rows ---
    # Track by doc_extraction_id (unique per doc, new rows) or agent_call_id (old rows).
    # Old eval results that predate doc_extraction_id are tracked by agent_call_id so that
    # old multi-doc calls (same agent_call_id, different docs) still get skipped correctly.
    already_evald: set[str] = set()
    try:
        eval_resp = client.get_object(Bucket=bucket, Key=_RESULTS_KEY)
        for line in eval_resp["Body"].read().decode("utf-8").split("\n"):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
                evald_id = r.get("doc_extraction_id") or r.get("agent_call_id") or ""
                if evald_id:
                    already_evald.add(evald_id)
            except json.JSONDecodeError:
                continue
    except Exception as e:
        log.warning("Could not load doc_eval_results for dedup: %s", e)

    # --- Filter eligible rows ---
    from eval.doc_eval_agent import make_doc_eval_row_key

    def _row_eval_id(r: dict) -> str:
        return r.get("doc_extraction_id") or r.get("agent_call_id") or ""

    def _is_eligible(r: dict) -> bool:
        src = r.get("extraction_source")
        if src == "transcript":
            # Transcripts: need a transcript_s3_key to fetch content from S3
            return bool(r.get("transcript_s3_key", "").strip())
        # All others: need a real library_item_url
        return r.get("library_item_url", "").strip().lower() not in ("", "n/a")

    eligible = [
        r for r in all_rows
        if _is_eligible(r)
        and _row_eval_id(r)
        and _row_eval_id(r) not in already_evald
        and (upper_bound is None or r.get("run_timestamp", "") <= upper_bound)
    ]

    # Deduplicate by (library_item_url or transcript_s3_key, eval_row_key) keeping most recent row per key
    dedup_by_key: dict[tuple, dict] = {}
    for row in eligible:
        url_key = row.get("library_item_url") or row.get("transcript_s3_key") or ""
        rk = (url_key, make_doc_eval_row_key(row))
        existing = dedup_by_key.get(rk)
        if existing is None or (row.get("run_timestamp", "") > existing.get("run_timestamp", "")):
            dedup_by_key[rk] = row

    selected = list(dedup_by_key.values())
    unique_doc_ids = len({_row_eval_id(r) for r in selected})
    log.info(
        "Selected %d doc extraction rows across %d doc extraction call(s) for evaluation (upper_bound=%s)",
        len(selected), unique_doc_ids, upper_bound or "none",
    )
    return selected


def _make_eval_run_id() -> str:
    return f"doc-eval-{int(time.time())}"


# Maximum agenda-item rows per QA agent call.  With reasoning_effort=low the
# first-pass agent runs out of budget on large meeting packets (13+ items) and
# the formatter fills in fake "No QA evaluation provided" / Incorrect scores for
# the unevaluated rows.  Batching keeps each call tractable.
_MAX_ROWS_PER_EVAL_CALL = 5


def _group_by_call(rows: list[dict]) -> list[list[dict]]:
    """Group rows so each unique document extraction gets its own QA call.

    New rows (with doc_extraction_id) group by that ID — one unique ID per
    extract_document_data() call, shared across agenda items of the same doc.
    Old rows (no doc_extraction_id) fall back to (agent_call_id, library_item_url).
    Groups larger than _MAX_ROWS_PER_EVAL_CALL are split into sequential batches.
    """
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        eid = row.get("doc_extraction_id") or ""
        if eid:
            key = (eid, "")
        else:
            cid = row.get("agent_call_id", "unknown")
            url = row.get("library_item_url") or ""
            key = (cid, url)
        groups.setdefault(key, []).append(row)

    result: list[list[dict]] = []
    for group in groups.values():
        if len(group) <= _MAX_ROWS_PER_EVAL_CALL:
            result.append(group)
        else:
            for i in range(0, len(group), _MAX_ROWS_PER_EVAL_CALL):
                result.append(group[i:i + _MAX_ROWS_PER_EVAL_CALL])
    return result


def run(
    limit: int = _DEFAULT_LIMIT,
    agent_call_ids: list[str] | None = None,
    library_item_url: str | None = None,
    dry_run: bool = False,
) -> list[dict]:
    from eval.doc_eval_agent import evaluate_doc_extraction_call, make_doc_eval_row_key
    from eval.doc_result_store import store_doc_eval_results

    eval_run_id = _make_eval_run_id()
    eval_timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    log.info("Starting doc eval run %s", eval_run_id)

    rows = _load_doc_rows(agent_call_ids, limit, library_item_url)
    if not rows:
        log.info("No eligible doc extraction rows found")
        return []

    alert_lookup = _load_alert_lookup()
    groups = _group_by_call(rows)
    log.info("Evaluating %d doc extraction row(s) across %d call(s)", len(rows), len(groups))

    if dry_run:
        for row in rows:
            print(json.dumps({
                "agent_call_id": row.get("agent_call_id"),
                "eval_row_key": make_doc_eval_row_key(row),
                "document_title": row.get("document_title"),
                "library_item_url": row.get("library_item_url"),
                "run_id": row.get("run_id"),
                "run_timestamp": row.get("run_timestamp"),
            }, indent=2))
        return rows

    eval_rows = []
    for i, group in enumerate(groups, 1):
        doc_eid = group[0].get("doc_extraction_id") or group[0].get("agent_call_id", "unknown")
        call_id = group[0].get("agent_call_id", "unknown")
        lib_url = group[0].get("library_item_url", "") or ""

        # Skip URLs we can't fetch text from (web pages, unsupported types).
        # PDF and docx are supported; empty lib_url = transcript row (uses transcript_s3_key).
        if lib_url:
            url_lower = lib_url.lower().split("?")[0].split(";")[0]
            is_supported = url_lower.endswith(".pdf") or any(
                url_lower.endswith(ext) for ext in (".docx", ".doc")
            )
            if not is_supported:
                log.info(
                    "[%d/%d] Skipping doc_extraction_id=%s — unsupported URL type (%s)",
                    i, len(groups), doc_eid[-8:], lib_url.split("/")[-1],
                )
                continue

        alert_row = alert_lookup.get((call_id, lib_url)) or alert_lookup.get((call_id, ""))
        log.info(
            "[%d/%d] Evaluating doc_extraction_id=%s rows=%d document=%s alert_found=%s",
            i, len(groups), doc_eid[-8:], len(group),
            group[0].get("document_title") or lib_url,
            alert_row is not None,
        )

        score_results = evaluate_doc_extraction_call(rows=group, alert_row=alert_row)

        # Merge per-row scores back into source rows
        for row, score_result in zip(group, score_results):
            eval_row_key = score_result.get("eval_row_key") or make_doc_eval_row_key(row)
            eval_rows.append({
                **row,
                "eval_run_id": eval_run_id,
                "eval_timestamp": eval_timestamp,
                "eval_scores": score_result.get("eval_scores", {}),
                "overall_summary": score_result.get("overall_summary"),
                "eval_row_key": eval_row_key,
            })

    store_doc_eval_results(eval_rows, eval_run_id)
    log.info("Doc eval run %s complete — %d rows evaluated", eval_run_id, len(eval_rows))
    return eval_rows


def _load_secrets() -> None:
    try:
        from bubble.ssm_loader import load_openai_env_from_ssm, load_db_env_from_ssm
        load_openai_env_from_ssm()
        load_db_env_from_ssm()
    except Exception as e:
        log.warning("SSM loader failed: %s", e)
    if not os.environ.get("OPENAI_API_KEY"):
        log.warning("OPENAI_API_KEY not set — eval agent calls will fail")


def main():
    _load_secrets()
    parser = argparse.ArgumentParser(description="Run document extraction QA evaluation pipeline")
    parser.add_argument("--limit", type=int, default=_DEFAULT_LIMIT)
    parser.add_argument("--agent-call-ids", type=str, default=None)
    parser.add_argument("--library-item-url", type=str, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    call_ids = None
    if args.agent_call_ids:
        call_ids = [x.strip() for x in args.agent_call_ids.split(",")]

    try:
        run(
            limit=args.limit,
            agent_call_ids=call_ids,
            library_item_url=args.library_item_url,
            dry_run=args.dry_run,
        )
    except Exception:
        import traceback
        log.error("Doc eval run failed:\n%s", traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
