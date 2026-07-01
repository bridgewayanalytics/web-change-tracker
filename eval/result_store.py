"""
Store QA evaluation results to S3.

Uses eval_results_table.jsonl keyed by eval_row_key (upsert, not append).
Re-running QA on a row replaces its prior entry.

eval_row_key:
  - Single-row agent calls: agent_call_id (backward compatible)
  - Multi-row agent calls (siblings): agent_call_id + "|" + library_item_url
    Each sibling row gets its own entry, distinguished by library_item_url.
"""

import json
import logging
import os

log = logging.getLogger(__name__)

_DEFAULT_BUCKET = "web-change-tracker-prod-artifacts-815039343351"
_RESULTS_KEY = "alerts/eval_results_table.jsonl"


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
    """
    Stable unique key per eval row.
    Single-row agent calls: agent_call_id (backward compatible with prior stored results).
    Multi-row sibling groups: agent_call_id + "|" + library_item_url.
    The eval_row_key field is stamped on each row before storage.
    """
    return row.get("eval_row_key") or row.get("agent_call_id") or ""


def _load_existing(client, bucket: str) -> dict[str, dict]:
    """Return existing eval results as {eval_row_key: row}."""
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


def store_eval_results(eval_rows: list[dict], eval_run_id: str) -> None:
    """
    Upsert eval_rows into eval_results_table.jsonl keyed by eval_row_key.
    Re-running QA on the same row replaces the prior entry.
    """
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
        log.info("Upserted %d eval rows into %s", len(eval_rows), _RESULTS_KEY)
    except Exception as e:
        log.error("Failed to write eval_results_table.jsonl: %s", e)


def delete_eval_result(agent_call_id: str) -> None:
    """
    Remove eval result(s) for a given agent_call_id.
    Deletes all entries whose eval_row_key starts with agent_call_id
    (covers both single-row and sibling-group entries).
    """
    bucket = _get_bucket()
    client = _s3_client()
    try:
        existing = _load_existing(client, bucket)
        keys_to_delete = [k for k in existing if k == agent_call_id or k.startswith(agent_call_id + "|")]
        if not keys_to_delete:
            log.info("No eval result found for agent_call_id=%s — nothing to delete", agent_call_id)
            return
        for k in keys_to_delete:
            del existing[k]
        _write(client, bucket, existing, "delete")
        log.info("Deleted %d eval result(s) for agent_call_id=%s", len(keys_to_delete), agent_call_id)
    except Exception as e:
        log.error("Failed to delete eval result for %s: %s", agent_call_id, e)
