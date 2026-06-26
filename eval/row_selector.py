"""
Select alert rows eligible for QA evaluation.

Eligible rows: have bubble_action set (classified as applicable for Bubble),
meaning they represent a real event, library item, or resource — not a
carousel change or irrelevant alert.

Can be filtered further by date range or limited to a sample size.
"""

import json
import logging
import os

log = logging.getLogger(__name__)

_ALERTS_KEY = "alerts/alerts_table.jsonl"
_DEFAULT_BUCKET = "web-change-tracker-prod-artifacts-815039343351"


def _get_bucket() -> str:
    return (
        os.environ.get("CHANGELOG_BUCKET", "").strip()
        or os.environ.get("BUBBLE_ARTIFACT_BUCKET", "").strip()
        or _DEFAULT_BUCKET
    )


def _s3_client():
    import boto3
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def load_eligible_rows(
    *,
    limit: int | None = None,
    since_run_timestamp: int | None = None,
    agent_call_ids: list[str] | None = None,
) -> list[dict]:
    """
    Load alert rows eligible for evaluation.

    Args:
        limit: max rows to return (most recent first)
        since_run_timestamp: only include rows at or after this Unix timestamp
        agent_call_ids: if provided, only return rows with these agent_call_ids

    Returns list of alert row dicts, most recent first.
    """
    bucket = _get_bucket()
    client = _s3_client()

    try:
        resp = client.get_object(Bucket=bucket, Key=_ALERTS_KEY)
        lines = resp["Body"].read().decode("utf-8").strip().split("\n")
    except Exception as e:
        log.error("Failed to load alerts_table.jsonl: %s", e)
        return []

    rows = []
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue

        if not row.get("bubble_action"):
            continue

        if since_run_timestamp is not None:
            ts = row.get("run_timestamp")
            if ts is None or int(ts) < since_run_timestamp:
                continue

        if agent_call_ids is not None:
            if row.get("agent_call_id") not in agent_call_ids:
                continue

        rows.append(row)

    # Most recent first
    rows.sort(key=lambda r: r.get("run_timestamp", 0), reverse=True)

    if limit is not None:
        rows = rows[:limit]

    log.info("Selected %d eligible rows for evaluation", len(rows))
    return rows
