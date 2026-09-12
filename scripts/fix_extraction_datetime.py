"""
One-time fix: set data_extraction_date_time = run_timestamp for all rows in
document_extractions_table.jsonl where the two values differ.

The backfill script (Sep 12 2026) called extract_document_data() without
original_datetime, so _stamp_extraction_datetime() used wall-clock time and
stamped every re-extracted row with the backfill run time instead of the
original pipeline run time. _build_doc_extraction_rows() now always overrides
this going forward; this script corrects the existing data.

Usage:
  python scripts/fix_extraction_datetime.py [--dry-run]
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("fix_extraction_datetime")

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

    fixed = 0
    for row in rows:
        run_ts = str(row.get("run_timestamp") or "").strip()
        ext_dt = str(row.get("data_extraction_date_time") or "").strip()
        if run_ts and ext_dt != run_ts:
            row["data_extraction_date_time"] = run_ts
            fixed += 1

    log.info("Rows to fix: %d / %d", fixed, len(rows))

    if args.dry_run:
        log.info("[DRY RUN] No writes made.")
        return

    body_out = "\n".join(json.dumps(r, default=str) for r in rows)
    client.put_object(
        Bucket=_BUCKET,
        Key=_KEY,
        Body=body_out.encode("utf-8"),
        ContentType="application/x-ndjson",
        Metadata={"source": "fix-extraction-datetime"},
    )
    log.info("Wrote %d rows back to s3://%s/%s", len(rows), _BUCKET, _KEY)
    log.info("Done. Fixed %d rows.", fixed)


if __name__ == "__main__":
    main()
