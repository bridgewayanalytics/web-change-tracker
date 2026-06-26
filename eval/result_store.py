"""
Store QA evaluation results to S3.

Writes two files:
  alerts/eval_results_table.jsonl  — append-only, one line per evaluated row
  alerts/eval_results_latest.json  — full list from the most recent eval run
"""

import json
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_DEFAULT_BUCKET = "web-change-tracker-prod-artifacts-815039343351"
_RESULTS_KEY = "alerts/eval_results_table.jsonl"
_LATEST_KEY = "alerts/eval_results_latest.json"


def _get_bucket() -> str:
    return (
        os.environ.get("CHANGELOG_BUCKET", "").strip()
        or os.environ.get("BUBBLE_ARTIFACT_BUCKET", "").strip()
        or _DEFAULT_BUCKET
    )


def _s3_client():
    import boto3
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def store_eval_results(eval_rows: list[dict], eval_run_id: str) -> None:
    """
    Append eval_rows to eval_results_table.jsonl and write eval_results_latest.json.

    Each eval_row should contain the original alert fields plus:
      eval_run_id, eval_timestamp, eval_scores (per-field dict), overall_summary
    """
    if not eval_rows:
        return

    bucket = _get_bucket()
    client = _s3_client()

    # Append to JSONL
    try:
        try:
            existing = client.get_object(Bucket=bucket, Key=_RESULTS_KEY)["Body"].read().decode("utf-8")
        except client.exceptions.NoSuchKey:
            existing = ""

        new_lines = "\n".join(json.dumps(r, default=str) for r in eval_rows)
        combined = (existing.rstrip("\n") + "\n" + new_lines).lstrip("\n")

        client.put_object(
            Bucket=bucket,
            Key=_RESULTS_KEY,
            Body=combined.encode("utf-8"),
            ContentType="application/x-ndjson",
            Metadata={"eval_run_id": eval_run_id},
        )
        log.info("Appended %d eval rows to %s", len(eval_rows), _RESULTS_KEY)
    except Exception as e:
        log.error("Failed to write eval_results_table.jsonl: %s", e)

    # Write latest snapshot
    try:
        client.put_object(
            Bucket=bucket,
            Key=_LATEST_KEY,
            Body=json.dumps(eval_rows, indent=2, default=str).encode("utf-8"),
            ContentType="application/json",
            Metadata={"eval_run_id": eval_run_id},
        )
        log.info("Wrote eval_results_latest.json (%d rows)", len(eval_rows))
    except Exception as e:
        log.error("Failed to write eval_results_latest.json: %s", e)
