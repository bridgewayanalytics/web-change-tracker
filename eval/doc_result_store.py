"""
Store document extraction QA results to S3.

Mirrors result_store.py but writes to doc_eval_results_table.jsonl.
Upsert keyed by agent_call_id (or agent_call_id|library_item_url for multi-doc rows).
"""

import json
import logging
import os

log = logging.getLogger(__name__)

_DEFAULT_BUCKET = "web-change-tracker-prod-artifacts-815039343351"
_RESULTS_KEY = "alerts/doc_eval_results_table.jsonl"


def _get_bucket() -> str:
    return (
        os.environ.get("CHANGELOG_BUCKET", "").strip()
        or os.environ.get("BUBBLE_ARTIFACT_BUCKET", "").strip()
        or _DEFAULT_BUCKET
    )


def _s3_client():
    import boto3
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _row_key(row: dict) -> str:
    return row.get("eval_row_key") or row.get("agent_call_id") or ""


def _load_existing(client, bucket: str) -> dict[str, dict]:
    try:
        body = client.get_object(Bucket=bucket, Key=_RESULTS_KEY)["Body"].read().decode("utf-8")
    except Exception:
        return {}
    existing: dict[str, dict] = {}
    for line in body.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            key = _row_key(r)
            if key:
                existing[key] = r
        except json.JSONDecodeError:
            pass
    return existing


def _write(client, bucket: str, rows: dict[str, dict], eval_run_id: str) -> None:
    if not rows:
        return
    body = "\n".join(json.dumps(r, default=str) for r in rows.values())
    client.put_object(
        Bucket=bucket,
        Key=_RESULTS_KEY,
        Body=body.encode("utf-8"),
        ContentType="application/x-ndjson",
        Metadata={"eval_run_id": eval_run_id},
    )


def store_doc_eval_results(eval_rows: list[dict], eval_run_id: str) -> None:
    if not eval_rows:
        return
    bucket = _get_bucket()
    client = _s3_client()
    try:
        existing = _load_existing(client, bucket)
        for row in eval_rows:
            key = _row_key(row)
            if key:
                existing[key] = row
        _write(client, bucket, existing, eval_run_id)
        log.info("Upserted %d doc eval rows into %s", len(eval_rows), _RESULTS_KEY)
    except Exception as e:
        log.error("Failed to write doc_eval_results_table.jsonl: %s", e)
