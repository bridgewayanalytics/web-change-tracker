"""
Backfill agent_call_id on existing alerts_table.jsonl and
document_extractions_table.jsonl rows in S3.

Groups rows by (run_id, target_id) — rows from the same page change share one
agent call — and assigns a UUID v4 to each group. Rows that already have an
agent_call_id are left unchanged.

Usage:
    python scripts/backfill_call_id.py [--dry-run]

Requires: AWS credentials with access to the artifacts S3 bucket.
"""

import argparse
import json
import logging
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

_DEFAULT_BUCKET = "web-change-tracker-prod-artifacts-815039343351"
BUCKET = os.environ.get("CHANGELOG_BUCKET") or os.environ.get("BUBBLE_ARTIFACT_BUCKET") or _DEFAULT_BUCKET


def s3_client():
    import boto3
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def download_jsonl(client, key: str) -> list[dict]:
    """Download a JSONL file from S3 and return as list of dicts."""
    try:
        resp = client.get_object(Bucket=BUCKET, Key=key)
        body = resp["Body"].read().decode("utf-8")
    except client.exceptions.NoSuchKey:
        log.warning("Key not found: s3://%s/%s", BUCKET, key)
        return []
    except Exception as e:
        log.error("Failed to download %s: %s", key, e)
        return []

    rows = []
    for i, line in enumerate(body.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as e:
            log.warning("Skipping malformed line %d in %s: %s", i, key, e)
    return rows


def backfill_rows(rows: list[dict]) -> tuple[list[dict], int]:
    """
    Assign agent_call_id to rows missing it, grouped by (run_id, target_id).

    Returns (updated_rows, count_of_rows_modified).
    """
    # Build group keys → UUID mapping
    group_ids: dict[tuple[str, str], str] = {}
    modified = 0

    for row in rows:
        existing = row.get("agent_call_id")
        if existing and existing.strip():
            # Already has a call ID — register it for the group so other rows
            # in the same group get the same ID
            gk = (row.get("run_id", ""), row.get("target_id", ""))
            if gk not in group_ids:
                group_ids[gk] = existing
            continue

        gk = (row.get("run_id", ""), row.get("target_id", ""))
        if gk not in group_ids:
            group_ids[gk] = str(uuid.uuid4())

        row["agent_call_id"] = group_ids[gk]
        modified += 1

    return rows, modified


def upload_jsonl(client, key: str, rows: list[dict]) -> None:
    """Upload a list of dicts as JSONL to S3."""
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
    client.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=body.encode("utf-8"),
        ContentType="application/x-ndjson",
    )


def process_file(client, key: str, dry_run: bool) -> None:
    log.info("Processing s3://%s/%s ...", BUCKET, key)
    rows = download_jsonl(client, key)
    if not rows:
        log.info("  No rows found — skipping.")
        return

    total = len(rows)
    already_have = sum(1 for r in rows if r.get("agent_call_id", "").strip())
    log.info("  %d total rows, %d already have agent_call_id", total, already_have)

    rows, modified = backfill_rows(rows)
    log.info("  %d rows modified", modified)

    if modified == 0:
        log.info("  Nothing to update.")
        return

    # Show a sample
    for r in rows[:3]:
        log.info("  Sample: run_id=%s target_id=%s agent_call_id=%s",
                 r.get("run_id", "?")[:12], r.get("target_id", "?")[:20],
                 r.get("agent_call_id", "?")[:8])

    if dry_run:
        log.info("  Dry run — not uploading.")
        return

    upload_jsonl(client, key, rows)
    log.info("  Uploaded %d rows to s3://%s/%s", len(rows), BUCKET, key)


def main():
    parser = argparse.ArgumentParser(description="Backfill agent_call_id on JSONL files in S3")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without uploading")
    args = parser.parse_args()

    client = s3_client()

    process_file(client, "alerts/alerts_table.jsonl", args.dry_run)
    process_file(client, "alerts/document_extractions_table.jsonl", args.dry_run)

    log.info("Done.")


if __name__ == "__main__":
    main()
