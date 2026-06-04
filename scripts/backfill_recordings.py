"""
Backfill recording_s3_key on existing alerts_table.jsonl rows.

Reads alerts_table.jsonl from S3, runs find_recording() on rows that have
an event_title and event_start_date_time but no recording_s3_key, then
patches any matches back into the JSONL.

Usage:
    AWS_PROFILE=bridgeway python scripts/backfill_recordings.py [--dry-run] [--limit 50]

Options:
    --dry-run   Print matches without writing to S3
    --limit N   Only process the first N unmatched rows (default: all)
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

_DEFAULT_BUCKET = "web-change-tracker-prod-artifacts-815039343351"
BUCKET = os.environ.get("CHANGELOG_BUCKET") or os.environ.get("BUBBLE_ARTIFACT_BUCKET") or _DEFAULT_BUCKET
ALERTS_KEY = "alerts/alerts_table.jsonl"


def s3_client():
    import boto3
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def load_rows(client) -> list[dict]:
    body = client.get_object(Bucket=BUCKET, Key=ALERTS_KEY)["Body"].read().decode("utf-8")
    rows = []
    for line in body.splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def save_rows(client, rows: list[dict]) -> None:
    combined = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows).encode("utf-8")
    client.put_object(
        Bucket=BUCKET,
        Key=ALERTS_KEY,
        Body=combined,
        ContentType="application/x-ndjson",
    )


def _is_na(val: str) -> bool:
    return not val or val.strip().upper() in ("N/A", "N/A.", "-", "")


def main():
    from bubble.recording_matcher import find_recording

    parser = argparse.ArgumentParser(description="Backfill recording_s3_key on alerts")
    parser.add_argument("--dry-run", action="store_true", help="Print matches without writing")
    parser.add_argument("--limit", type=int, default=0, help="Max rows to attempt (0=all)")
    args = parser.parse_args()

    client = s3_client()
    log.info("Loading %s from s3://%s", ALERTS_KEY, BUCKET)
    rows = load_rows(client)
    log.info("Loaded %d rows", len(rows))

    candidates = [
        (i, r) for i, r in enumerate(rows)
        if not r.get("recording_s3_key")
        and not _is_na(str(r.get("event_title", "") or ""))
        and not _is_na(str(r.get("event_start_date_time", "") or ""))
    ]
    log.info("%d rows eligible (have event but no recording_s3_key)", len(candidates))

    if args.limit:
        candidates = candidates[: args.limit]

    matched = 0
    for idx, (row_i, row) in enumerate(candidates):
        event_title = str(row.get("event_title", "") or "")
        event_start = str(row.get("event_start_date_time", "") or "")
        key = find_recording(event_title, event_start)
        if key:
            log.info(
                "[%d/%d] MATCH: '%s' (%s) → %s",
                idx + 1, len(candidates),
                event_title[:60], event_start[:10], key,
            )
            if not args.dry_run:
                rows[row_i]["recording_s3_key"] = key
            matched += 1
        else:
            log.debug("[%d/%d] no match: '%s' (%s)", idx + 1, len(candidates), event_title[:60], event_start[:10])

    log.info("Matched %d / %d candidates", matched, len(candidates))

    if matched == 0:
        log.info("Nothing to write.")
        return

    if args.dry_run:
        log.info("Dry run — not writing to S3.")
        return

    log.info("Writing updated rows to s3://%s/%s", BUCKET, ALERTS_KEY)
    save_rows(client, rows)
    log.info("Done.")


if __name__ == "__main__":
    main()
