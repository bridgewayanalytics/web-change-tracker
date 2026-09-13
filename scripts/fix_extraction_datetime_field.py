"""
One-time fix: normalize data_extraction_date_time → data_extraction_datetime in
document_extractions_table.jsonl to match the DynamoDB column registry ID.

Problems this fixes:
  1. 639 rows only have 'data_extraction_date_time' (underscore before "time") —
     the DynamoDB column registry ID is 'data_extraction_datetime', so those rows
     show '—' in the dashboard's Data Extraction Date & Time column.
  2. 98 rows have the old 'data_extraction_datetime' key but with stale wall-clock
     extraction times (Eastern time) instead of run_timestamp — overwrite with run_timestamp.

Usage:
  python scripts/fix_extraction_datetime_field.py [--dry-run]
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("fix_extraction_datetime_field")

_BUCKET = (
    os.environ.get("CHANGELOG_BUCKET", "").strip()
    or os.environ.get("BUBBLE_ARTIFACT_BUCKET", "").strip()
    or "web-change-tracker-prod-artifacts-815039343351"
)
_KEY = "alerts/document_extractions_table.jsonl"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    import boto3
    client = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))

    body = client.get_object(Bucket=_BUCKET, Key=_KEY)["Body"].read().decode("utf-8")
    rows = []
    for line in body.splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    log.info("Loaded %d rows from s3://%s/%s", len(rows), _BUCKET, _KEY)

    renamed = 0
    overwritten = 0
    for row in rows:
        run_ts = str(row.get("run_timestamp") or "").strip()
        old_key_val = row.get("data_extraction_date_time")   # new underscore name (wrong key)
        registry_val = row.get("data_extraction_datetime")    # correct registry key

        # Set the correct registry key to run_timestamp
        if run_ts and row.get("data_extraction_datetime") != run_ts:
            row["data_extraction_datetime"] = run_ts
            if registry_val is not None:
                overwritten += 1
            else:
                renamed += 1

        # Remove the old underscore variant
        if "data_extraction_date_time" in row:
            del row["data_extraction_date_time"]

    log.info("Renamed (had only new-underscore key): %d rows", renamed)
    log.info("Overwritten (had stale old key): %d rows", overwritten)
    log.info("Total rows processed: %d", len(rows))

    if args.dry_run:
        log.info("[DRY RUN] No writes made.")
        return

    body_out = "\n".join(json.dumps(r, default=str) for r in rows)
    client.put_object(
        Bucket=_BUCKET,
        Key=_KEY,
        Body=body_out.encode("utf-8"),
        ContentType="application/x-ndjson",
        Metadata={"source": "fix-extraction-datetime-field"},
    )
    log.info("Wrote %d rows back to s3://%s/%s", len(rows), _BUCKET, _KEY)
    log.info("Done.")


if __name__ == "__main__":
    main()
