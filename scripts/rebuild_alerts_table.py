"""
Rebuild alerts_table.jsonl and alerts_table.xlsx from scratch using already-stored
agent_output.json and doc_extractions.json files in S3.

This does NOT re-run any LLM agents. It simply re-processes the raw stored outputs
through the current _build_table_rows logic, which reflects the latest field names
and schema from the web-tracking-agent config.

Safe to run any number of times — it fully replaces the existing table rather than
appending, so there are no duplicates.

Usage:
    python scripts/rebuild_alerts_table.py [--dry-run] [--limit N]

    --dry-run   Print rows without writing anything to S3.
    --limit N   Only process the N most recent agent outputs (for testing).

Requires: AWS credentials with read/write access to the artifacts bucket.
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

_DEFAULT_BUCKET = "web-change-tracker-prod-artifacts-815039343351"
BUCKET = os.environ.get("CHANGELOG_BUCKET") or os.environ.get("BUBBLE_ARTIFACT_BUCKET") or _DEFAULT_BUCKET


def s3():
    import boto3
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def fetch_json(client, key: str) -> dict | list | None:
    try:
        resp = client.get_object(Bucket=BUCKET, Key=key)
        return json.loads(resp["Body"].read().decode("utf-8"))
    except Exception:
        return None


def list_agent_outputs(client) -> list[dict]:
    """
    Find all pages/<target_id>/YYYY/MM/DD/<run_id>/agent_output.json objects in S3.
    Returns list of dicts with target_id, run_id, prefix, last_modified.
    """
    paginator = client.get_paginator("list_objects_v2")
    entries: list[dict] = []

    for page in paginator.paginate(Bucket=BUCKET, Prefix="pages/"):
        for obj in page.get("Contents") or []:
            key = obj["Key"]
            if key.endswith("/agent_output.json"):
                # pages/<target_id>/YYYY/MM/DD/<run_id>/agent_output.json
                parts = key.split("/")
                if len(parts) >= 6:
                    entries.append({
                        "target_id": parts[1],
                        "run_id": parts[5],
                        "prefix": "/".join(parts[:6]),
                        "agent_output_key": key,
                        "last_modified": obj["LastModified"],
                    })

    entries.sort(key=lambda x: x["last_modified"])
    return entries


def run_rebuild(dry_run: bool = False, limit: int | None = None):
    from storage.alert_s3 import _build_table_rows, _build_xlsx, _put, _get_bucket

    client = s3()
    alert_bucket = _get_bucket()

    log.info("Scanning S3 for agent_output.json files in s3://%s/pages/...", BUCKET)
    entries = list_agent_outputs(client)

    if not entries:
        log.info("No agent outputs found.")
        return

    log.info("Found %d agent output(s) total.", len(entries))

    if limit:
        entries = entries[-limit:]  # most recent N
        log.info("Processing most recent %d (--limit %d).", len(entries), limit)

    all_rows: list[dict] = []
    skipped = 0

    for entry in entries:
        prefix = entry["prefix"]
        target_id = entry["target_id"]
        run_id = entry["run_id"]

        agent_output = fetch_json(client, entry["agent_output_key"])
        if not agent_output or not isinstance(agent_output, dict):
            log.warning("  Skipping %s/%s — could not load agent_output.json", target_id, run_id)
            skipped += 1
            continue

        if agent_output.get("alert_type") == "No Meaningful Change":
            skipped += 1
            continue

        # Load doc_extractions.json (may not exist)
        doc_extractions = fetch_json(client, f"{prefix}/doc_extractions.json") or []

        # Load meta.json for timestamps and source URL
        meta = fetch_json(client, f"{prefix}/meta.json") or {}
        run_timestamp = meta.get("run_timestamp") or 0
        run_timestamp_iso = (
            datetime.fromtimestamp(run_timestamp, tz=timezone.utc).isoformat()
            if run_timestamp
            else entry["last_modified"].isoformat()
        )
        source_url = meta.get("url") or ""

        rows = _build_table_rows(
            agent_output, doc_extractions,
            run_id, run_timestamp_iso, target_id, source_url,
        )
        all_rows.extend(rows)
        log.info("  %s / %s  alert_type=%s  rows=%d",
                 target_id, run_id, agent_output.get("alert_type", "?"), len(rows))

    log.info("Built %d row(s) from %d entry/entries (%d skipped).",
             len(all_rows), len(entries), skipped)

    if not all_rows:
        log.info("Nothing to write.")
        return

    if dry_run:
        log.info("[DRY RUN] Would write %d row(s) — sample of first row:", len(all_rows))
        print(json.dumps(all_rows[0], indent=2, ensure_ascii=False))
        log.info("[DRY RUN] Columns that would be written: %s",
                 sorted({k for r in all_rows for k in r}))
        return

    # Build JSONL from all rows (sorted chronologically — entries were sorted by last_modified)
    jsonl_body = "\n".join(json.dumps(r, ensure_ascii=False) for r in all_rows).encode("utf-8")

    jsonl_key = "alerts/alerts_table.jsonl"
    log.info("Writing %d row(s) to s3://%s/%s (replaces existing)...", len(all_rows), alert_bucket, jsonl_key)
    _put(client, alert_bucket, jsonl_key, jsonl_body, "application/x-ndjson", "rebuild")

    # Regenerate Excel
    xlsx_key = "alerts/alerts_table.xlsx"
    log.info("Regenerating s3://%s/%s...", alert_bucket, xlsx_key)
    xlsx_bytes = _build_xlsx(all_rows)
    _put(client, alert_bucket, xlsx_key, xlsx_bytes,
         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "rebuild")

    log.info("Done. %d row(s) written. Columns: %s",
             len(all_rows), sorted({k for r in all_rows for k in r}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Rebuild alerts_table from stored agent outputs")
    parser.add_argument("--dry-run", action="store_true", help="Print without writing to S3")
    parser.add_argument("--limit", type=int, default=None, help="Only process N most recent outputs")
    args = parser.parse_args()
    run_rebuild(dry_run=args.dry_run, limit=args.limit)
