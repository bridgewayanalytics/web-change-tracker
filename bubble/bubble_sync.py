"""
Bubble sync executor — executes the actions described in an alert's bubble_action field.

Currently STUBBED: logs what would be synced and patches bubble_sync_status: "synced".
Real Bubble API calls are wired in once schemas are confirmed.

bubble_sync_status lifecycle:
  absent / null   — not yet synced (or not applicable)
  "synced"        — stub: set immediately on confirm; real: after successful Bubble API call
  "error"         — executor raised an exception (bubble_sync_error has details)

Called by:
  - /api/bubble/sync (NAICDashboard- route) — patches JSONL directly for MVP stub
  - Future: spike.py BUBBLE_SYNC_AGENT_CALL_ID env var for ECS-based execution
"""

import logging
import os

log = logging.getLogger(__name__)

_ALERTS_TABLE_KEY = "alerts/alerts_table.jsonl"


def _get_bucket() -> str:
    return os.environ.get("CHANGELOG_BUCKET") or os.environ.get("BUBBLE_ARTIFACT_BUCKET", "")


def _s3_client():
    import boto3
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _find_row(agent_call_id: str, bucket: str) -> dict | None:
    import json
    try:
        client = _s3_client()
        body = client.get_object(Bucket=bucket, Key=_ALERTS_TABLE_KEY)["Body"].read().decode("utf-8")
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                if row.get("agent_call_id") == agent_call_id:
                    return row
            except Exception:
                pass
    except Exception as exc:
        log.warning("bubble_sync: could not read alerts table: %s", exc)
    return None


def sync_alert(agent_call_id: str) -> dict:
    """
    Execute Bubble sync for an alert identified by agent_call_id.

    STUB: logs the planned actions and marks the row as synced.
    Replace the TODO block with real Bubble API calls once schemas are confirmed.

    Returns: { ok: bool, plan: dict | None, error: str | None }
    """
    bucket = _get_bucket()
    if not bucket:
        return {"ok": False, "error": "CHANGELOG_BUCKET not set"}

    row = _find_row(agent_call_id, bucket)
    if not row:
        return {"ok": False, "error": f"no row for agent_call_id={agent_call_id}"}

    plan = row.get("bubble_action")
    if not plan:
        return {"ok": False, "error": "no bubble_action on row — run backfill_bubble_action.py"}

    # ── STUB ────────────────────────────────────────────────────────────────
    # TODO: replace with real Bubble API calls when schema is confirmed.
    #
    # For each action in the plan:
    #   if plan["event"] == "create":  bubble_api.create_event(plan["event_preview"])
    #   if plan["event"] == "update":  bubble_api.update_event(plan["event_preview"])
    #   if plan["library_item"] == "create": bubble_api.create_library_item(plan["library_item_preview"])
    #   if plan["library_item"] == "update": bubble_api.update_library_item(plan["library_item_preview"])
    #   if plan["agenda_items"]: bubble_api.create_agenda_items(plan["agenda_item_previews"])
    #
    # Store returned Bubble record IDs back on the alert row:
    #   patch_fields["bubble_event_id"] = result["event_id"]
    #   patch_fields["bubble_library_item_id"] = result["library_item_id"]
    # ── END STUB ────────────────────────────────────────────────────────────

    log.info(
        "BUBBLE SYNC STUB — agent_call_id=%s event=%s library_item=%s agenda_items=%s",
        agent_call_id,
        plan.get("event"),
        plan.get("library_item"),
        plan.get("agenda_items"),
    )

    from storage.alert_s3 import patch_jsonl_row
    patched = patch_jsonl_row(
        _ALERTS_TABLE_KEY,
        {"agent_call_id": agent_call_id},
        {"bubble_sync_status": "synced"},
        bucket=bucket,
    )
    log.info("bubble_sync: patched %d row(s) for agent_call_id=%s", patched, agent_call_id)

    return {"ok": True, "plan": plan}
