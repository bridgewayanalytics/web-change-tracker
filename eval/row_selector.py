"""
Select alert rows eligible for QA evaluation.

Eligibility: every row in alerts_table.jsonl is a candidate. The selector
returns the most-recent N agent calls (plus their sibling rows) subject to
the `limit` and `since_run_timestamp` arguments. There is no publication
gate — rows that have never been published to Bubble still qualify, because
QA is scoring the extraction agent's output, not Bubble editorial state.

Transcript rows (alert_type == "New Meeting Transcript Available") are
excluded — they share HTML with their parent alert and have no independent
content.

Rows specified by --agent-call-ids bypass filtering entirely.
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

    When agent_call_ids is provided, those rows are returned directly.

    Otherwise: filter out transcript rows and optionally rows older than
    since_run_timestamp, sort most-recent-first by alert_date_time,
    dedupe by agent_call_id, apply limit at the call level, and return
    all sibling rows for the selected calls.

    Args:
        limit: max agent calls to return (most recent first)
        since_run_timestamp: only include rows at or after this Unix timestamp
        agent_call_ids: if provided, return only these rows

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

    all_rows: list[dict] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            all_rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    # ── agent_call_ids bypass ────────────────────────────────────────────────
    if agent_call_ids is not None:
        result = [r for r in all_rows if r.get("agent_call_id") in agent_call_ids]
        result.sort(key=lambda r: r.get("run_timestamp", 0), reverse=True)
        log.info("Selected %d row(s) by agent_call_id", len(result))
        return result

    # ── Recency-based eligibility (no publication gate) ──────────────────────
    eligible = [
        r for r in all_rows
        if r.get("alert_type") != "New Meeting Transcript Available"
        and (
            since_run_timestamp is None
            or (r.get("run_timestamp") is not None and int(r.get("run_timestamp", 0)) >= since_run_timestamp)
        )
    ]
    log.info("row_selector: %d candidate rows after transcript/since filtering", len(eligible))

    # Most recent first — run_timestamp is sometimes int unix-seconds, sometimes
    # an ISO string. Normalize to int seconds. Rows with alert_date_time == "N/A"
    # must not float to the top (ASCII "N" beats digits in string sort).
    def _ts_seconds(r: dict) -> int:
        v = r.get("run_timestamp")
        if v is None:
            return 0
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, str):
            try:
                return int(v)
            except ValueError:
                pass
            try:
                from datetime import datetime
                s = v[:-1] + "+00:00" if v.endswith("Z") else v
                return int(datetime.fromisoformat(s).timestamp())
            except Exception:
                return 0
        return 0

    eligible.sort(key=_ts_seconds, reverse=True)

    # Deduplicate by agent_call_id, apply limit at call level,
    # then return all sibling rows for each selected call
    seen: set[str] = set()
    selected_call_ids: list[str] = []
    for row in eligible:
        cid = row.get("agent_call_id", "")
        if cid and cid not in seen:
            seen.add(cid)
            selected_call_ids.append(cid)

    if limit is not None:
        selected_call_ids = selected_call_ids[:limit]

    selected_set = set(selected_call_ids)
    result = [r for r in eligible if r.get("agent_call_id", "") in selected_set]

    log.info(
        "Selected %d agent call(s) (%d total rows including siblings) for evaluation",
        len(selected_call_ids), len(result),
    )
    return result
