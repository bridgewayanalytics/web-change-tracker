"""
Select alert rows eligible for QA evaluation.

Eligible rows: ingest_status == "approved" — meaning a human reviewed the
content and published it (approved for newsreel ingest). At that point the
newsreel relevance decision has been made and chronicles are updated,
providing the ground truth context the eval agent needs.

Rows that were only classified (bubble_action set) but never reviewed/approved
are excluded — there is no human-validated ground truth to evaluate against.
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
    Load alert rows eligible for evaluation (ingest_status == "approved").

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

        if row.get("ingest_status") != "approved":
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

    # Deduplicate by agent_call_id to count distinct agent calls, then apply limit
    # at the agent-call level — but return ALL rows per selected call (siblings included).
    # Siblings share the same agent_call_id and are grouped together in run_eval.py
    # so the eval agent can score each row with full context of the run.
    seen: set[str] = set()
    selected_call_ids: list[str] = []
    for row in rows:
        cid = row.get("agent_call_id", "")
        if cid and cid not in seen:
            seen.add(cid)
            selected_call_ids.append(cid)
        elif not cid:
            selected_call_ids.append("")

    if limit is not None:
        selected_call_ids = selected_call_ids[:limit]

    selected_set = set(selected_call_ids)
    result = [r for r in rows if r.get("agent_call_id", "") in selected_set]

    log.info(
        "Selected %d agent calls (%d total rows including siblings) for evaluation",
        len(selected_call_ids), len(result),
    )
    return result
