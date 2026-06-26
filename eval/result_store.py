"""
Store QA evaluation results to S3.

Uses eval_results_table.jsonl keyed by agent_call_id (upsert, not append).
Re-running QA on a row replaces its existing entry.
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


def _load_existing(client, bucket: str) -> dict[str, dict]:
    """Return existing eval results as {agent_call_id: row}."""
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
            cid = r.get("agent_call_id", "")
            if cid:
                existing[cid] = r
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
    Upsert eval_rows into eval_results_table.jsonl keyed by agent_call_id.
    Re-running QA on the same row replaces the prior entry.
    """
    if not eval_rows:
        return

    bucket = _get_bucket()
    client = _s3_client()

    try:
        existing = _load_existing(client, bucket)
        for row in eval_rows:
            cid = row.get("agent_call_id", "")
            if cid:
                existing[cid] = row
        _write(client, bucket, existing, eval_run_id)
        log.info("Upserted %d eval rows into %s", len(eval_rows), _RESULTS_KEY)
    except Exception as e:
        log.error("Failed to write eval_results_table.jsonl: %s", e)


def delete_eval_result(agent_call_id: str) -> None:
    """Remove the eval result for a given agent_call_id."""
    bucket = _get_bucket()
    client = _s3_client()
    try:
        existing = _load_existing(client, bucket)
        if agent_call_id not in existing:
            log.info("No eval result found for agent_call_id=%s — nothing to delete", agent_call_id)
            return
        del existing[agent_call_id]
        _write(client, bucket, existing, "delete")
        log.info("Deleted eval result for agent_call_id=%s", agent_call_id)
    except Exception as e:
        log.error("Failed to delete eval result for %s: %s", agent_call_id, e)
