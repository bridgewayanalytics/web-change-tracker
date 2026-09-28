"""
Bulk pipeline re-run + QA eval for specific agent_call_ids.

For each unique (run_id, target_id) group derived from the given agent_call_ids:
  1. Re-runs spike.py in rerun mode (re-runs page_change_agent with current DynamoDB config)
  2. Auto-accepts the rerun result into alerts_table.jsonl (mirrors /api/rerun/accept logic)
Then runs QA eval on the original agent_call_ids (which are preserved on the updated rows).

Usage:
  python -m eval.run_bulk_reeval --agent-call-ids a,b,c [--dry-run]
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
_DEFAULT_RERUN_TIMEOUT = 900  # 15 minutes per target rerun


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


def _run_spike_rerun(run_id: str, target_id: str) -> None:
    """
    Invoke spike.py in rerun mode (RERUN_RUN_ID + RERUN_TARGET_ID env vars).
    Writes result to alerts/reruns/{run_id}/{target_id}/result.json.
    """
    env = os.environ.copy()
    env["RERUN_RUN_ID"] = run_id
    env["RERUN_TARGET_ID"] = target_id
    env["RERUN_MODE"] = "alerts"

    cmd = [sys.executable, "spike.py"]
    log.info("rerun: run_id=%s target_id=%s — starting spike.py", run_id, target_id)
    result = subprocess.run(cmd, env=env, timeout=_DEFAULT_RERUN_TIMEOUT)
    if result.returncode != 0:
        raise RuntimeError(
            f"spike.py rerun exited {result.returncode} for run_id={run_id} target_id={target_id}"
        )
    log.info("rerun: run_id=%s target_id=%s — done", run_id, target_id)


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


def run(agent_call_ids: list[str], dry_run: bool = False) -> None:
    from bubble.ssm_loader import load_openai_env_from_ssm, load_db_env_from_ssm
    try:
        load_openai_env_from_ssm()
        load_db_env_from_ssm()
    except Exception as e:
        log.warning("SSM loader failed: %s", e)

    s3 = _get_s3()
    bucket = _bucket()
    id_set = set(agent_call_ids)

    log.info("bulk_reeval: loading alerts_table.jsonl...")
    all_rows = _load_jsonl(s3, bucket, ALERTS_KEY)
    if not all_rows:
        log.error("bulk_reeval: alerts_table.jsonl is empty or missing")
        return

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
        log.error("bulk_reeval: no matching rows with run_id+target_id found for %s", agent_call_ids)
        return

    log.info("bulk_reeval: %d agent_call_ids → %d unique (run_id, target_id) groups",
             len(agent_call_ids), len(groups))

    if dry_run:
        for (run_id, target_id), cids in groups.items():
            log.info("  [dry-run] would rerun run_id=%s target_id=%s (%d call_ids)", run_id, target_id, len(cids))
        return

    # Phase 1: rerun each group sequentially (each writes its own staging file)
    failed_groups: list[tuple[str, str]] = []
    for run_id, target_id in groups:
        try:
            _run_spike_rerun(run_id, target_id)
        except Exception as e:
            log.error("bulk_reeval: rerun failed for run_id=%s target_id=%s: %s", run_id, target_id, e)
            failed_groups.append((run_id, target_id))

    succeeded_groups = [(r, t) for (r, t) in groups if (r, t) not in failed_groups]
    if not succeeded_groups:
        log.error("bulk_reeval: all reruns failed — aborting")
        sys.exit(1)

    # Phase 2: accept all successful rerun results into alerts_table.jsonl
    log.info("bulk_reeval: accepting %d rerun results...", len(succeeded_groups))
    current_rows = list(all_rows)
    for run_id, target_id in succeeded_groups:
        current_rows = _accept_rerun_result(s3, bucket, run_id, target_id, current_rows)

    _save_jsonl(s3, bucket, ALERTS_KEY, current_rows)
    log.info("bulk_reeval: alerts_table.jsonl updated (%d rows)", len(current_rows))

    # Phase 3: QA eval on the original agent_call_ids (preserved on updated rows)
    log.info("bulk_reeval: running QA eval for %d agent_call_ids...", len(agent_call_ids))
    from eval.run_eval import run as run_eval
    run_eval(agent_call_ids=agent_call_ids)

    log.info("bulk_reeval: complete")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(description="Bulk pipeline re-run + QA eval")
    parser.add_argument("--agent-call-ids", required=True, help="Comma-separated agent_call_ids")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    ids = [x.strip() for x in args.agent_call_ids.split(",") if x.strip()]
    if not ids:
        log.error("No agent_call_ids provided")
        sys.exit(1)

    log.info("bulk_reeval: agent_call_ids=%s dry_run=%s", ids, args.dry_run)
    run(agent_call_ids=ids, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
