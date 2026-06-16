"""
Bubble sync executor — executes the actions described in an alert's bubble_action field.

Sequence:
  1. Resolve org names → Bubble _id values (shared between event and library item)
  2. Library item: CREATE or find-and-UPDATE (using field_ids from preview)
  3. Calendar item: CREATE or find-and-UPDATE (using field_ids from preview),
     then link the library item via relevant_resources_list_custom_resource

Finding existing records:
  - calendaritem: match by space + org ID + date range (from match_search in preview)
  - libraryitem:  match by space + url (exact), fallback to title (exact)

bubble_sync_status lifecycle:
  absent / null   — not yet synced (or not applicable)
  "syncing"       — ECS task started; waiting for completion
  "synced"        — successful Bubble API call
  "error"         — executor raised an exception (bubble_sync_error has details)

Called by:
  - /api/bubble/sync (NAICDashboard- route) — fires ECS RunTask with BUBBLE_SYNC_AGENT_CALL_ID
  - spike.py BUBBLE_SYNC_AGENT_CALL_ID env var — for direct ECS execution
"""

import logging
import os
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

_ALERTS_TABLE_KEY = "alerts/alerts_table.jsonl"
_DOC_EXTRACTIONS_KEY = "alerts/document_extractions_table.jsonl"


def _get_bucket() -> str:
    return os.environ.get("CHANGELOG_BUCKET") or os.environ.get("BUBBLE_ARTIFACT_BUCKET", "")


def _s3_client():
    import boto3
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _find_row(agent_call_id: str, bucket: str) -> dict | None:
    import json
    try:
        body = _s3_client().get_object(Bucket=bucket, Key=_ALERTS_TABLE_KEY)["Body"].read().decode("utf-8")
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


def _find_doc_extraction(agent_call_id: str, bucket: str) -> dict | None:
    """
    Look up the document_extractions_table.jsonl row matching agent_call_id.
    Returns the extraction dict if found, None otherwise.
    """
    import json
    try:
        body = _s3_client().get_object(Bucket=bucket, Key=_DOC_EXTRACTIONS_KEY)["Body"].read().decode("utf-8")
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
        log.debug("bubble_sync: could not read doc extractions: %s", exc)
    return None


def _get_bubble_client():
    from bubble.bridgemind import get_client
    return get_client()


def _resolve_org_ids(org_names: list[str], client) -> list[str]:
    """Resolve org display names to Bubble _id values via the organization type."""
    from bubble.bridgemind import TYPE_ORGANIZATION, SPACE_CONSTRAINT
    if not org_names:
        return []
    name_to_id: dict[str, str] = {}
    for org in client.list_all(TYPE_ORGANIZATION, constraints=SPACE_CONSTRAINT):
        name = (org.get("name_text") or "").strip()
        oid = org.get("_id") or ""
        if name and oid:
            name_to_id[name] = oid
    ids = []
    for name in org_names:
        oid = name_to_id.get(name)
        if oid:
            ids.append(oid)
        else:
            log.warning("bubble_sync: org name not found in Bubble: %r", name)
    return ids


def _find_calendar_item(match_search: dict, client) -> str | None:
    """
    Find an existing calendaritem by org + date.
    match_search: {"org": "Life Actuarial (A) Task Force", "date": "2026-05-14"}
    Returns Bubble _id if found.
    """
    from bubble.bridgemind import TYPE_CALENDAR_ITEM, TYPE_ORGANIZATION, SPACE_CONSTRAINT

    org_name = match_search.get("org", "")
    date_str = match_search.get("date", "")

    if not date_str:
        log.warning("bubble_sync: match_search missing date, cannot locate calendaritem")
        return None

    constraints = list(SPACE_CONSTRAINT)

    try:
        day_end_dt = datetime.fromisoformat(date_str) + timedelta(days=1)
        constraints.append({"key": "date", "constraint_type": "greater than", "value": f"{date_str}T00:00:00.000Z"})
        constraints.append({"key": "date", "constraint_type": "less than", "value": day_end_dt.strftime("%Y-%m-%dT00:00:00.000Z")})
    except Exception as e:
        log.warning("bubble_sync: could not parse date %r: %s", date_str, e)

    if org_name:
        name_to_id: dict[str, str] = {}
        for org in client.list_all(TYPE_ORGANIZATION, constraints=SPACE_CONSTRAINT):
            n = (org.get("name_text") or "").strip()
            if n:
                name_to_id[n] = org.get("_id") or ""
        org_id = name_to_id.get(org_name)
        if org_id:
            constraints.append({"key": "orgs", "constraint_type": "contains", "value": org_id})

    result = client.search(TYPE_CALENDAR_ITEM, constraints=constraints, limit=10)
    items = result.get("results", [])
    if not items:
        log.info("bubble_sync: no calendaritem found for match_search=%s", match_search)
        return None
    if len(items) > 1:
        log.warning("bubble_sync: %d calendaritems found for match_search=%s, using first", len(items), match_search)
    return items[0].get("_id")


def _find_library_item(match_search: dict, client) -> str | None:
    """
    Find an existing libraryitem by URL (preferred) or title.
    match_search: {"url": "...", "title": "..."}
    Returns Bubble _id if found.
    """
    from bubble.bridgemind import TYPE_LIBRARY_ITEM, SPACE_CONSTRAINT

    url = match_search.get("url", "")
    title = match_search.get("title", "")

    if url:
        result = client.search(
            TYPE_LIBRARY_ITEM,
            constraints=list(SPACE_CONSTRAINT) + [{"key": "url_text", "constraint_type": "equals", "value": url}],
            limit=5,
        )
        items = result.get("results", [])
        if items:
            return items[0].get("_id")

    if title:
        result = client.search(
            TYPE_LIBRARY_ITEM,
            constraints=list(SPACE_CONSTRAINT) + [{"key": "name_text", "constraint_type": "equals", "value": title}],
            limit=5,
        )
        items = result.get("results", [])
        if items:
            return items[0].get("_id")

    log.info("bubble_sync: no libraryitem found for match_search=%s", match_search)
    return None


def _resolve_chronicle_topic_ids(topic_names: list[str], client) -> list[str]:
    """Resolve chronicle topic title strings to Bubble _id values."""
    from bubble.bridgemind import TYPE_CHRONICLE_TOPIC, SPACE_CONSTRAINT
    if not topic_names:
        return []
    name_to_id: dict[str, str] = {}
    for topic in client.list_all(TYPE_CHRONICLE_TOPIC, constraints=SPACE_CONSTRAINT):
        t = (topic.get("title_text") or "").strip()
        tid = topic.get("_id") or ""
        if t and tid:
            name_to_id[t] = tid
    ids = []
    for name in topic_names:
        tid = name_to_id.get(name)
        if tid:
            ids.append(tid)
        else:
            log.warning("bubble_sync: chronicle topic not found in Bubble: %r", name)
    return ids


def _inject_org_ids(field_ids: dict, org_ids: list[str]) -> dict:
    """Replace org name lists with resolved org ID lists in a field_ids dict."""
    out = dict(field_ids)
    for key in ("orgs__list_custom_organization", "organizations_list_custom_organization"):
        if key in out and isinstance(out[key], list):
            out[key] = org_ids
    return out


def _inject_topic_ids(field_ids: dict, topic_ids: list[str]) -> dict:
    """Replace chronicle topic name lists with resolved topic ID lists in a field_ids dict."""
    out = dict(field_ids)
    key = "topics___dt_list_custom_newsreel_update"
    if key in out and isinstance(out[key], list):
        out[key] = topic_ids
    return out


def _clean(field_ids: dict) -> dict:
    """Drop keys with falsy values (None, "", []) but keep explicit False booleans."""
    return {k: v for k, v in field_ids.items() if v is not None and v != "" and v != []}


def sync_alert(agent_call_id: str) -> dict:
    """
    Execute Bubble sync for an alert identified by agent_call_id.

    Returns: { ok: bool, plan: dict | None, bubble_event_id, bubble_library_item_id, error }
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

    client = _get_bubble_client()

    from bubble.bridgemind import TYPE_CALENDAR_ITEM, TYPE_LIBRARY_ITEM
    from storage.alert_s3 import patch_jsonl_row

    event_action = plan.get("event")
    lib_action = plan.get("library_item")
    ep = plan.get("event_preview") or {}
    lp = plan.get("library_item_preview") or {}

    # Enrich previews with doc extraction metadata if not already present.
    # Enrichment adds lib item fields (CREATE only) and chronicle topics on both previews.
    # Covers rows where spike.py didn't run the enrichment (old rows, backfilled rows).
    doc_row = _find_doc_extraction(agent_call_id, bucket)
    if doc_row:
        from bubble.bubble_sync_classifier import enrich_with_doc_extraction
        enrich_with_doc_extraction(plan, doc_row)
        ep = plan.get("event_preview") or {}
        lp = plan.get("library_item_preview") or {}

    # Org name → ID (shared; org names appear in both event and lib previews)
    org_names: list[str] = ep.get("group") or lp.get("group") or []
    org_ids = _resolve_org_ids(org_names, client) if org_names else []

    # Chronicle topic name → ID resolution (topics come from doc extraction enrichment)
    _topic_key = "topics___dt_list_custom_newsreel_update"
    ep_topic_names = [t for t in ((ep.get("field_ids") or {}).get(_topic_key) or []) if isinstance(t, str)]
    lp_topic_names = [t for t in ((lp.get("field_ids") or {}).get(_topic_key) or []) if isinstance(t, str)]
    all_topic_names = list({*ep_topic_names, *lp_topic_names})
    topic_ids = _resolve_chronicle_topic_ids(all_topic_names, client) if all_topic_names else []

    bubble_library_item_id: str | None = None
    bubble_event_id: str | None = None

    try:
        # ── Library item ─────────────────────────────────────────────────────
        if lib_action == "create":
            field_ids = _clean(_inject_org_ids(_inject_topic_ids(dict(lp.get("field_ids") or {}), topic_ids), org_ids))
            log.info("bubble_sync: CREATE libraryitem fields=%s", list(field_ids.keys()))
            bubble_library_item_id = client.create(TYPE_LIBRARY_ITEM, field_ids)
            log.info("bubble_sync: created libraryitem _id=%s", bubble_library_item_id)

        elif lib_action == "update":
            existing_lib_id = _find_library_item(lp.get("match_search") or {}, client)
            if existing_lib_id:
                field_ids = _clean(_inject_topic_ids(dict(lp.get("field_ids") or {}), topic_ids))
                if field_ids:
                    log.info("bubble_sync: UPDATE libraryitem _id=%s fields=%s", existing_lib_id, list(field_ids.keys()))
                    client.patch(TYPE_LIBRARY_ITEM, existing_lib_id, field_ids, scope="sync")
                bubble_library_item_id = existing_lib_id
            else:
                log.warning("bubble_sync: UPDATE libraryitem — no existing record for match_search=%s", lp.get("match_search"))

        # ── Calendar item ─────────────────────────────────────────────────────
        if event_action == "create":
            field_ids = _clean(_inject_org_ids(_inject_topic_ids(dict(ep.get("field_ids") or {}), topic_ids), org_ids))
            if bubble_library_item_id:
                field_ids["relevant_resources_list_custom_resource"] = [bubble_library_item_id]
            log.info("bubble_sync: CREATE calendaritem fields=%s", list(field_ids.keys()))
            bubble_event_id = client.create(TYPE_CALENDAR_ITEM, field_ids)
            log.info("bubble_sync: created calendaritem _id=%s", bubble_event_id)

        elif event_action == "update":
            existing_event_id = _find_calendar_item(ep.get("match_search") or {}, client)
            if existing_event_id:
                field_ids = _clean(_inject_org_ids(_inject_topic_ids(dict(ep.get("field_ids") or {}), topic_ids), org_ids))
                if bubble_library_item_id:
                    field_ids["relevant_resources_list_custom_resource"] = [bubble_library_item_id]
                if field_ids:
                    log.info("bubble_sync: UPDATE calendaritem _id=%s fields=%s", existing_event_id, list(field_ids.keys()))
                    client.patch(TYPE_CALENDAR_ITEM, existing_event_id, field_ids, scope="sync")
                bubble_event_id = existing_event_id
            else:
                log.warning("bubble_sync: UPDATE calendaritem — no existing record for match_search=%s", ep.get("match_search"))

    except Exception as exc:
        log.error("bubble_sync: error for agent_call_id=%s: %s", agent_call_id, exc, exc_info=True)
        patch_jsonl_row(
            _ALERTS_TABLE_KEY,
            {"agent_call_id": agent_call_id},
            {"bubble_sync_status": "error", "bubble_sync_error": str(exc)},
            bucket=bucket,
        )
        return {"ok": False, "error": str(exc), "plan": plan}

    # Patch status and IDs
    patch_fields: dict = {"bubble_sync_status": "synced"}
    if bubble_event_id:
        patch_fields["bubble_event_id"] = bubble_event_id
    if bubble_library_item_id:
        patch_fields["bubble_library_item_id"] = bubble_library_item_id

    patched = patch_jsonl_row(
        _ALERTS_TABLE_KEY,
        {"agent_call_id": agent_call_id},
        patch_fields,
        bucket=bucket,
    )
    log.info(
        "bubble_sync: patched %d row(s) for agent_call_id=%s event_id=%s lib_id=%s",
        patched, agent_call_id, bubble_event_id, bubble_library_item_id,
    )

    return {
        "ok": True,
        "plan": plan,
        "bubble_event_id": bubble_event_id,
        "bubble_library_item_id": bubble_library_item_id,
    }
