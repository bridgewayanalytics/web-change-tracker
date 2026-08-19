import logging

log = logging.getLogger()
log.setLevel(logging.INFO)


def lambda_handler(event, context):
    agent_call_id = event.get("agent_call_id")
    action = event.get("action", "all")
    if not agent_call_id:
        return {"ok": False, "error": "agent_call_id required"}
    try:
        from bubble.bubble_sync import sync_alert
        result = sync_alert(agent_call_id, action=action)
        return result
    except Exception as e:
        log.exception("bubble_sync lambda failed")
        return {"ok": False, "error": str(e)}
