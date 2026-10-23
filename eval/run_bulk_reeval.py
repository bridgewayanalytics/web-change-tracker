"""
Bulk pipeline re-run + QA eval for specific agent_call_ids.

Modes:
  alerts (default): re-runs page_change_agent, accepts into alerts_table.jsonl, runs WTA QA eval
  docs:             re-runs document_agent only, accepts into document_extractions_table.jsonl, runs doc QA eval
  both:             re-runs page_change_agent + document_agent, accepts both, runs both QA evals

For each unique (run_id, target_id) group derived from the given agent_call_ids:
  1. Re-runs spike.py in the chosen RERUN_MODE
  2. Auto-accepts the rerun result (mirrors dashboard accept logic)
Then runs QA eval on the original agent_call_ids (which are preserved on the updated rows).

Usage:
  python -m eval.run_bulk_reeval --agent-call-ids a,b,c [--mode docs] [--dry-run]
  python -m eval.run_bulk_reeval --start-date 2026-08-15 --end-date 2026-10-22 --mode docs
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)

ALERTS_KEY = "alerts/alerts_table.jsonl"
DOC_EXT_KEY = "alerts/document_extractions_table.jsonl"
_DEFAULT_RERUN_TIMEOUT = 1800  # 30 minutes per target rerun (docs mode can be slow)


def _get_s3():
    import boto3
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _bucket() -> str:
    b = (
        os.environ.get("CHANGELOG_BUCKET", "").strip()
        or os.environ.get("BUBBLE_ARTIFACT_BUCKET", "").strip()
    )
    if not b:
        raise RuntimeError("No S3 bucket configured (CHANGELOG_BUCKET / BUBBLE_ARTIFACT_BUCKET)")
    return b


def _load_jsonl(s3, bucket: str, key: str) -> list[dict]:
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
    except s3.exceptions.NoSuchKey:
        return []
    rows = []
    for line in body.splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def _save_jsonl(s3, bucket: str, key: str, rows: list[dict]) -> None:
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"
    s3.put_object(Bucket=bucket, Key=key, Body=body.encode("utf-8"),
                  ContentType="application/x-ndjson")


def _run_spike_rerun(run_id: str, target_id: str, mode: str = "alerts") -> None:
    """
    Invoke spike.py in rerun mode (RERUN_RUN_ID + RERUN_TARGET_ID env vars).
    Writes result to alerts/reruns/{run_id}/{target_id}/result.json.

    mode: "alerts" (page_change only), "docs" (document_agent only), or "both".
    """
    env = os.environ.copy()
    env["RERUN_RUN_ID"] = run_id
    env["RERUN_TARGET_ID"] = target_id
    env["RERUN_MODE"] = mode

    cmd = [sys.executable, "spike.py"]
    log.info("rerun[%s]: run_id=%s target_id=%s — starting spike.py", mode, run_id, target_id)
    result = subprocess.run(cmd, env=env, timeout=_DEFAULT_RERUN_TIMEOUT)
    if result.returncode != 0:
        raise RuntimeError(
            f"spike.py rerun exited {result.returncode} for run_id={run_id} target_id={target_id}"
        )
    log.info("rerun[%s]: run_id=%s target_id=%s — done", mode, run_id, target_id)


def _accept_rerun_result(s3, bucket: str, run_id: str, target_id: str, rows: list[dict]) -> list[dict]:
    """
    Read rerun result from staging and patch rows in-place.
    Mirrors /api/rerun/accept logic exactly.
    Returns updated rows list.
    """
    result_key = f"alerts/reruns/{run_id}/{target_id}/result.json"
    try:
        raw = s3.get_object(Bucket=bucket, Key=result_key)["Body"].read().decode("utf-8")
        result = json.loads(raw)
    except Exception as e:
        log.error("accept: could not read %s: %s — skipping accept", result_key, e)
        return rows

    rerun_rows: list[dict] = (
        result.get("rerun_rows")
        or ([result["rerun"]] if result.get("rerun") else [])
    )
    if not rerun_rows:
        log.warning("accept: no rerun_rows in %s — skipping", result_key)
        return rows

    originals: list[dict] = (
        result.get("original_rows")
        or ([result["original"]] if result.get("original") else [])
    )
    first_orig = originals[0] if originals else {}

    always_preserve = ["run_timestamp", "agent_call_id", "alert_date_time", "run_id"]
    conditional_preserve = ["target_id", "source_url"]
    sync_preserve = [
        "bubble_action", "bubble_sync_status", "bubble_sync_error",
        "bubble_event_id", "bubble_library_item_id", "eidarix_agenda_item_ids",
        "transcript_s3_key", "manual_transcript_s3_key", "transcript_chunks_s3_key",
        "recording_s3_key", "ingest_status",
    ]

    orig_by_url: dict[str, dict] = {}
    for orig in originals:
        url = str(orig.get("library_item_url") or "")
        if url and url != "N/A":
            orig_by_url[url] = orig

    rerun_ts = result.get("rerun_timestamp") or datetime.now(timezone.utc).isoformat()
    for row in rerun_rows:
        for f in always_preserve:
            v = first_orig.get(f)
            if v is not None:
                row[f] = v
        for f in conditional_preserve:
            if not row.get(f):
                v = first_orig.get(f) or (target_id if f == "target_id" else None)
                if v is not None:
                    row[f] = v
        row_url = str(row.get("library_item_url") or "")
        matched = (orig_by_url.get(row_url) if row_url and row_url != "N/A" else None) or first_orig
        for f in sync_preserve:
            if row.get(f) is None:
                v = matched.get(f)
                if v is not None:
                    row[f] = v
        row["last_rerun_at"] = rerun_ts

    original_call_ids = {
        str(r.get("agent_call_id") or "") for r in originals if r.get("agent_call_id")
    }

    def is_original(r: dict) -> bool:
        if r.get("run_id") != run_id or r.get("target_id") != target_id:
            return False
        if not original_call_ids:
            return True
        return str(r.get("agent_call_id") or "") in original_call_ids

    patched: list[dict] = []
    inserted = False
    for r in rows:
        if is_original(r):
            if not inserted:
                patched.extend(rerun_rows)
                inserted = True
        else:
            patched.append(r)
    if not inserted:
        patched.extend(rerun_rows)

    log.info("accept: replaced %d original rows with %d rerun rows for run_id=%s target_id=%s",
             len(originals), len(rerun_rows), run_id, target_id)
    return patched


def _rid_to_ts(run_id: str) -> int:
    """Extract unix timestamp from run-NNNNNNNNNN run_id. Returns 0 on parse failure."""
    try:
        return int(str(run_id).replace("run-", "").strip())
    except Exception:
        return 0


def _enumerate_call_ids_by_date(all_rows: list[dict], start_ts: int, end_ts: int, mode: str) -> list[str]:
    """
    Find agent_call_ids with alerts in [start_ts, end_ts] (both inclusive).
    For mode='docs' / 'both': only agent_call_ids with a real library_item_url (eligible for doc extraction).
    For mode='alerts': any agent_call_id with a non-synthetic alert_type.
    """
    _SYNTHETIC_TYPES = {"New Meeting Transcript Available"}
    _NA_URLS = {"", "N/A", "N/A.", "-", "NONE"}
    call_ids: list[str] = []
    seen: set[str] = set()
    for row in all_rows:
        cid = str(row.get("agent_call_id") or "").strip()
        if not cid or cid in seen:
            continue
        rid = str(row.get("run_id") or "")
        ts = _rid_to_ts(rid)
        if ts < start_ts or ts > end_ts:
            continue
        if row.get("alert_type") in _SYNTHETIC_TYPES:
            continue
        if mode in ("docs", "both"):
            url = str(row.get("library_item_url") or "").strip().upper()
            if url in _NA_URLS:
                continue
        call_ids.append(cid)
        seen.add(cid)
    return call_ids


def _accept_doc_rerun_result(
    s3, bucket: str, run_id: str, target_id: str, all_doc_rows: list[dict]
) -> list[dict]:
    """
    Read the docs-mode rerun result and apply doc_rerun_rows to the full
    document_extractions_table.jsonl: remove all doc_original_rows and append doc_rerun_rows.
    Returns the updated all_doc_rows list.
    """
    result_key = f"alerts/reruns/{run_id}/{target_id}/result.json"
    try:
        raw = s3.get_object(Bucket=bucket, Key=result_key)["Body"].read().decode("utf-8")
        result = json.loads(raw)
    except Exception as e:
        log.error("accept_doc: could not read %s: %s", result_key, e)
        return all_doc_rows

    doc_rerun_rows = result.get("doc_rerun_rows") or []
    doc_original_rows = result.get("doc_original_rows") or []

    if not doc_rerun_rows and not doc_original_rows:
        log.warning("accept_doc: no doc rows in %s — skipping", result_key)
        return all_doc_rows

    # Identify rows to remove by (agent_call_id, library_item_url)
    remove_keys: set[tuple[str, str]] = set()
    for r in doc_original_rows:
        remove_keys.add((str(r.get("agent_call_id") or ""), str(r.get("library_item_url") or "")))

    kept = [
        r for r in all_doc_rows
        if (str(r.get("agent_call_id") or ""), str(r.get("library_item_url") or "")) not in remove_keys
    ]
    kept.extend(doc_rerun_rows)

    log.info(
        "accept_doc: replaced %d original rows with %d rerun rows for run_id=%s target_id=%s",
        len(doc_original_rows), len(doc_rerun_rows), run_id, target_id,
    )
    return kept


def run(
    agent_call_ids: list[str] | None = None,
    dry_run: bool = False,
    mode: str = "alerts",
    start_date: str | None = None,
    end_date: str | None = None,
) -> None:
    assert mode in ("alerts", "docs", "both"), f"invalid mode: {mode}"

    from bubble.ssm_loader import load_openai_env_from_ssm, load_db_env_from_ssm
    try:
        load_openai_env_from_ssm()
        load_db_env_from_ssm()
    except Exception as e:
        log.warning("SSM loader failed: %s", e)

    s3 = _get_s3()
    bucket = _bucket()

    log.info("bulk_reeval: loading alerts_table.jsonl...")
    all_rows = _load_jsonl(s3, bucket, ALERTS_KEY)
    if not all_rows:
        log.error("bulk_reeval: alerts_table.jsonl is empty or missing")
        return

    # Resolve agent_call_ids: either from --agent-call-ids or from --start-date/--end-date
    if start_date and end_date:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        start_ts = int(start_dt.timestamp())
        end_ts = int(end_dt.timestamp()) + 86400  # include the end-date day
        resolved_ids = _enumerate_call_ids_by_date(all_rows, start_ts, end_ts, mode)
        log.info(
            "bulk_reeval: date range %s → %s (ts %d → %d) resolved %d agent_call_ids",
            start_date, end_date, start_ts, end_ts, len(resolved_ids),
        )
        agent_call_ids = list(resolved_ids)

    if not agent_call_ids:
        log.error("bulk_reeval: no agent_call_ids resolved — provide --agent-call-ids or --start-date/--end-date")
        return

    id_set = set(agent_call_ids)

    # Build (run_id, target_id) groups from the selected agent_call_ids
    groups: dict[tuple[str, str], list[str]] = {}
    for row in all_rows:
        cid = row.get("agent_call_id")
        if cid not in id_set:
            continue
        run_id = str(row.get("run_id") or "").strip()
        tid = str(row.get("target_id") or "").strip()
        if not run_id or not tid:
            log.warning("bulk_reeval: missing run_id/target_id for agent_call_id=%s — skipping", cid)
            continue
        key = (run_id, tid)
        if key not in groups:
            groups[key] = []
        if cid not in groups[key]:
            groups[key].append(cid)

    if not groups:
        log.error("bulk_reeval: no matching rows with run_id+target_id found for %s", agent_call_ids[:5])
        return

    log.info("bulk_reeval[%s]: %d agent_call_ids → %d unique (run_id, target_id) groups",
             mode, len(agent_call_ids), len(groups))

    if dry_run:
        for i, ((run_id, target_id), cids) in enumerate(groups.items(), 1):
            log.info("  [dry-run %d/%d] would rerun run_id=%s target_id=%s (%d call_ids)",
                     i, len(groups), run_id, target_id, len(cids))
        log.info("  [dry-run] total agent_call_ids that would get QA: %d", len(agent_call_ids))
        return

    # Phase 1: rerun each group sequentially (each writes its own staging file)
    failed_groups: list[tuple[str, str]] = []
    total = len(groups)
    for i, (run_id, target_id) in enumerate(groups, 1):
        log.info("bulk_reeval[%s]: [%d/%d] rerun start", mode, i, total)
        try:
            _run_spike_rerun(run_id, target_id, mode=mode)
        except Exception as e:
            log.error("bulk_reeval: rerun failed for run_id=%s target_id=%s: %s", run_id, target_id, e)
            failed_groups.append((run_id, target_id))

    succeeded_groups = [(r, t) for (r, t) in groups if (r, t) not in failed_groups]
    if not succeeded_groups:
        log.error("bulk_reeval: all reruns failed — aborting")
        sys.exit(1)

    log.info("bulk_reeval[%s]: %d/%d reruns succeeded", mode, len(succeeded_groups), total)

    # Phase 2: accept rerun results
    if mode in ("alerts", "both"):
        log.info("bulk_reeval: accepting %d alert rerun results into alerts_table.jsonl...", len(succeeded_groups))
        current_rows = list(all_rows)
        for run_id, target_id in succeeded_groups:
            current_rows = _accept_rerun_result(s3, bucket, run_id, target_id, current_rows)
        _save_jsonl(s3, bucket, ALERTS_KEY, current_rows)
        log.info("bulk_reeval: alerts_table.jsonl updated (%d rows)", len(current_rows))

    if mode in ("docs", "both"):
        log.info("bulk_reeval: accepting %d doc rerun results into document_extractions_table.jsonl...", len(succeeded_groups))
        current_doc_rows = _load_jsonl(s3, bucket, DOC_EXT_KEY)
        for run_id, target_id in succeeded_groups:
            current_doc_rows = _accept_doc_rerun_result(s3, bucket, run_id, target_id, current_doc_rows)
        _save_jsonl(s3, bucket, DOC_EXT_KEY, current_doc_rows)
        log.info("bulk_reeval: document_extractions_table.jsonl updated (%d rows)", len(current_doc_rows))

    # Phase 3: QA eval on the original agent_call_ids (preserved on updated rows)
    if mode in ("alerts", "both"):
        log.info("bulk_reeval: running WTA QA eval for %d agent_call_ids...", len(agent_call_ids))
        from eval.run_eval import run as run_eval
        run_eval(agent_call_ids=agent_call_ids)

    if mode in ("docs", "both"):
        log.info("bulk_reeval: running doc QA eval for %d agent_call_ids...", len(agent_call_ids))
        from eval.run_doc_eval import run as run_doc_eval
        run_doc_eval(agent_call_ids=agent_call_ids)

    log.info("bulk_reeval: complete")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(description="Bulk pipeline re-run + QA eval")
    parser.add_argument("--agent-call-ids", help="Comma-separated agent_call_ids (OR provide --start-date/--end-date)")
    parser.add_argument("--start-date", help="YYYY-MM-DD lower bound on run_id date (inclusive)")
    parser.add_argument("--end-date", help="YYYY-MM-DD upper bound on run_id date (inclusive)")
    parser.add_argument("--mode", default="alerts", choices=["alerts", "docs", "both"],
                        help="Which rerun mode: alerts (page_change), docs (document extraction), or both")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    ids: list[str] | None = None
    if args.agent_call_ids:
        ids = [x.strip() for x in args.agent_call_ids.split(",") if x.strip()]
    if not ids and not (args.start_date and args.end_date):
        log.error("Must provide --agent-call-ids OR both --start-date and --end-date")
        sys.exit(1)

    log.info("bulk_reeval: mode=%s start=%s end=%s dry_run=%s agent_call_ids=%s",
             args.mode, args.start_date, args.end_date, args.dry_run,
             f"{len(ids)} ids" if ids else "(none)")
    run(
        agent_call_ids=ids,
        dry_run=args.dry_run,
        mode=args.mode,
        start_date=args.start_date,
        end_date=args.end_date,
    )


if __name__ == "__main__":
    main()
