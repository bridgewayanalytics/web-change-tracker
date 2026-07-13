"""
Bubble sync executor — executes the actions described in an alert's bubble_action field.

Sequence:
  1. Resolve org names → Bubble _id values (shared between event and library item)
  2. Library item: CREATE or find-and-UPDATE (using field_ids from preview)
  3. Calendar item: CREATE or find-and-UPDATE (using field_ids from preview),
     then link the library item via relevant_resources_list_custom_resource
     (the write field ID for the "Agenda" display-name field — list<libraryitem>)

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

# Eidarix workflow API — version-aware base URL for write operations
_EIDARIX_VERSION = os.environ.get("EIDARIX_VERSION", "test")
_EIDARIX_SPACE_IDS = {
    "test": "1768998437948x865417918648382000",
    "live": "1770642377799x775210694699370900",
}


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
        name = (org.get("Name") or "").strip()
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
            n = (org.get("Name") or "").strip()
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
        t = (topic.get("Title") or "").strip()
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


def _eidarix_wf_post(workflow: str, payload: dict) -> dict:
    """POST to one of Mori's Eidarix workflow endpoints. Returns the parsed JSON response."""
    import json
    import requests
    from bubble.bridgemind import BUBBLE_API_KEY
    version = _EIDARIX_VERSION
    url = f"https://eidarix.bridgewayanalytics.com/version-{version}/api/1.1/wf/{workflow}/"
    log.info("eidarix: POST %s payload=%s", workflow, json.dumps(payload, default=str))
    resp = requests.post(url, json=payload, headers={"Authorization": f"Bearer {BUBBLE_API_KEY}"}, timeout=30)
    log.info("eidarix: %s response status=%d body=%s", workflow, resp.status_code, resp.text[:500])
    resp.raise_for_status()
    return resp.json()


def _resolve_calendar_item_type_id(type_name: str) -> str | None:
    """
    Resolve a calendar item type display name (e.g. "Meeting") to its Bubble _id
    by querying the calendaritemtype endpoint for the active space.
    Returns None if not found.
    """
    from bubble.bridgemind import get_client, SPACE_CONSTRAINT
    client = get_client()
    try:
        result = client.search("calendaritemtype", constraints=SPACE_CONSTRAINT, limit=50)
        for item in (result.get("results") or []):
            if (item.get("display") or item.get("Title") or "").strip().lower() == type_name.strip().lower():
                return item.get("_id")
        log.warning("bubble_sync: calendar item type %r not found in Bubble", type_name)
    except Exception as e:
        log.warning("bubble_sync: could not resolve calendar item type %r: %s", type_name, e)
    return None


def _resolve_library_item_type_id(type_name: str) -> str | None:
    """
    Resolve a library item type display name (e.g. "Agenda & Materials") to its
    Bubble _id by querying the libraryitemtype endpoint for the active space.
    Returns None if not found.
    """
    from bubble.bridgemind import get_client, SPACE_CONSTRAINT
    client = get_client()
    try:
        result = client.search("libraryitemtype", constraints=SPACE_CONSTRAINT, limit=50)
        for item in (result.get("results") or []):
            if (item.get("Title") or "").strip().lower() == type_name.strip().lower():
                return item.get("_id")
        log.warning("bubble_sync: library item type %r not found in Bubble", type_name)
    except Exception as e:
        log.warning("bubble_sync: could not resolve library item type %r: %s", type_name, e)
    return None


def _parse_date_to_iso(raw: str) -> str:
    """Convert 'June 16, 2026' or ISO strings to 'YYYY-MM-DD'. Returns '' on failure."""
    if not raw:
        return ""
    raw = raw.strip()
    if len(raw) >= 10 and raw[4] == "-":
        return raw[:10]
    from datetime import datetime
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %Y", "%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ""


def _build_library_item_wf_payload(
    row: dict,
    agent_call_id: str,
    lp: dict,
    agenda_item_ids: list[str],
    topic_ids: list[str],
    org_ids: list[str],
) -> dict:
    """
    Build the payload for wf/create-library-item/ per docs/bubble_sync_payload_spec.json.
    Fields: title, chronicle_topics (IDs), alert_id, space_id, agenda_items (IDs),
            date (YYYY-MM-DD), date_display, summary, url or file, type (ID), organizations (IDs).
    """
    space_id = _EIDARIX_SPACE_IDS.get(_EIDARIX_VERSION, _EIDARIX_SPACE_IDS["test"])
    field_ids = lp.get("field_ids") or {}

    title = (field_ids.get("name_text") or lp.get("title") or "").strip()

    url = (field_ids.get("url_text") or lp.get("url") or "").strip()
    url = url if url and url.upper() != "N/A" else None

    # Prefer event date (most reliable); fall back to doc extraction date
    date = _parse_date_to_iso((row.get("event_start_date_time") or "")[:10])
    if not date:
        date = _parse_date_to_iso(field_ids.get("date_date") or "")

    summary = (field_ids.get("description_text") or "").strip() or None

    type_name = lp.get("type") or "Agenda & Materials"
    type_id = _resolve_library_item_type_id(type_name)

    payload: dict = {
        "title": title,
        "alert_id": agent_call_id,
        "space_id": space_id,
    }
    if topic_ids:
        payload["chronicle_topics"] = topic_ids
    if agenda_item_ids:
        payload["agenda_items"] = agenda_item_ids
    if date:
        payload["date"] = date
        payload["date_display"] = "Full date"
    if summary:
        payload["summary"] = summary
    if url:
        payload["url"] = url
    if type_id:
        payload["type"] = type_id
    if org_ids:
        payload["organizations"] = org_ids

    return payload


def _create_agenda_items(row: dict, agent_call_id: str, topic_name_to_id: dict[str, str]) -> list[str]:
    """
    Create one Eidarix agendaitem per entry in agenda_item_title_chronicle_topics[].
    Skips N/A entries. Returns list of created Bubble IDs.
    """
    agenda_entries = row.get("agenda_item_title_chronicle_topics") or []
    official_titles = row.get("agenda_item_title_official") or []
    standardized_ids = row.get("agenda_item_standardized_id") or []

    space_id = _EIDARIX_SPACE_IDS.get(_EIDARIX_VERSION, _EIDARIX_SPACE_IDS["test"])
    created_ids: list[str] = []

    for i, entry in enumerate(agenda_entries):
        title = (entry.get("agenda_item_title") or "").strip()
        if not title or title.upper() == "N/A":
            continue

        topic_names = entry.get("chronicle_topics") or []
        topic_ids = [topic_name_to_id[t] for t in topic_names if t in topic_name_to_id]

        off_entry = official_titles[i] if i < len(official_titles) else {}
        std_entry = standardized_ids[i] if i < len(standardized_ids) else {}

        official_title = (off_entry.get("official_title") or "").strip()
        reference_id = (std_entry.get("standardized_id") or "").strip()
        if official_title.upper() == "N/A":
            official_title = ""
        if reference_id.upper() == "N/A":
            reference_id = ""

        payload = {
            "title": title,
            "official_title": official_title,
            "reference_id": reference_id,
            "chronicle_topics": topic_ids,
            "alert_id": agent_call_id,
            "space_id": space_id,
        }

        try:
            result = _eidarix_wf_post("create-agenda-item", payload)
            # Eidarix workflow response: {"bubble_id": "..."}
            item_id = result.get("bubble_id") or result.get("id") or result.get("_id")
            if item_id:
                created_ids.append(item_id)
                log.info("eidarix: created agenda item '%s' → id=%s", title, item_id)
            else:
                log.warning("eidarix: create-agenda-item returned no id for '%s': %s", title, result)
        except Exception as exc:
            log.error("eidarix: failed to create agenda item '%s': %s", title, exc)

    return created_ids


def sync_alert(agent_call_id: str, action: str = "all") -> dict:
    """
    Execute Bubble sync for an alert identified by agent_call_id.

    action: "all" (default) | "event" | "library_item"
      - "all": sync both library item and calendar item
      - "event": sync calendar item only; links to existing bubble_library_item_id if present on row
      - "library_item": sync library item only

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

    run_lib = action in ("all", "library_item")
    run_event = action in ("all", "event")

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
    agenda_item_ids: list[str] = []

    # When syncing event-only, carry forward any library item ID already on the row
    # so we can link it to the calendar item.
    if action == "event":
        existing_lib_id = str(row.get("bubble_library_item_id") or "").strip() or None
        if existing_lib_id:
            bubble_library_item_id = existing_lib_id
            log.info("bubble_sync: action=event — linking existing bubble_library_item_id=%s", bubble_library_item_id)

    try:
        # ── Agenda items (Eidarix) ────────────────────────────────────────────
        # Created first — agenda items → library item → event (Mori's required order)
        if plan.get("agenda_items") and action in ("all",):
            # Build topic name→ID map from the row's agenda item entries
            agenda_entries = row.get("agenda_item_title_chronicle_topics") or []
            all_topic_names = list({
                t
                for entry in agenda_entries
                for t in (entry.get("chronicle_topics") or [])
                if isinstance(t, str)
            })
            topic_name_to_id: dict[str, str] = {}
            if all_topic_names:
                from bubble.bridgemind import TYPE_CHRONICLE_TOPIC, SPACE_CONSTRAINT
                for topic in client.list_all(TYPE_CHRONICLE_TOPIC, constraints=SPACE_CONSTRAINT):
                    name = (topic.get("Title") or "").strip()
                    if name in all_topic_names:
                        topic_name_to_id[name] = topic.get("_id") or ""
            agenda_item_ids = _create_agenda_items(row, agent_call_id, topic_name_to_id)
            log.info("bubble_sync: created %d agenda item(s) via Eidarix", len(agenda_item_ids))

        # ── Library item ─────────────────────────────────────────────────────
        if run_lib:
            if lib_action == "create":
                # Collect all chronicle topic names: agenda item topics + doc extraction topics
                agenda_entries = row.get("agenda_item_title_chronicle_topics") or []
                agenda_topic_names = list({
                    t
                    for entry in agenda_entries
                    for t in (entry.get("chronicle_topics") or [])
                    if isinstance(t, str) and t.upper() not in ("N/A", "")
                })
                lib_topic_ids = _resolve_chronicle_topic_ids(
                    list({*agenda_topic_names, *lp_topic_names}), client
                ) if (agenda_topic_names or lp_topic_names) else topic_ids

                payload = _build_library_item_wf_payload(
                    row=row,
                    agent_call_id=agent_call_id,
                    lp=lp,
                    agenda_item_ids=agenda_item_ids,
                    topic_ids=lib_topic_ids,
                    org_ids=org_ids,
                )
                log.info("bubble_sync: CREATE libraryitem via Eidarix wf — payload keys=%s", list(payload.keys()))
                result = _eidarix_wf_post("create-library-item", payload)
                bubble_library_item_id = result.get("bubble_id") or result.get("id") or result.get("_id")
                if bubble_library_item_id:
                    log.info("bubble_sync: created libraryitem _id=%s", bubble_library_item_id)
                else:
                    log.warning("bubble_sync: create-library-item returned no id: %s", result)

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
        if run_event:
            if event_action == "create":
                space_id = _EIDARIX_SPACE_IDS.get(_EIDARIX_VERSION, _EIDARIX_SPACE_IDS["test"])

                # Resolve calendar item type (default: "Meeting" for most alert types)
                cal_type_name = "Meeting"
                cal_type_id = _resolve_calendar_item_type_id(cal_type_name)
                if not cal_type_id:
                    log.warning("bubble_sync: could not resolve calendaritemtype=%r — event type will be omitted", cal_type_name)

                start_dt = ep.get("start_datetime") or ""
                end_dt = ep.get("end_datetime") or ""
                location_url = ep.get("url") or None
                if location_url and location_url.upper() == "N/A":
                    location_url = None
                call_in = ep.get("call_in") or None
                if call_in and call_in.upper() == "N/A":
                    call_in = None

                # Chronicle topics for the event come from the resolved topic_ids pool
                event_topic_ids = topic_ids

                event_payload: dict = {
                    "alert_id": agent_call_id,
                    "space_id": space_id,
                }
                if event_topic_ids:
                    event_payload["chronicle_topics"] = event_topic_ids
                if agenda_item_ids:
                    event_payload["agenda_items"] = agenda_item_ids
                if bubble_library_item_id:
                    event_payload["agenda"] = [bubble_library_item_id]
                if start_dt:
                    event_payload["start_datetime"] = start_dt
                if end_dt:
                    event_payload["end_datetime"] = end_dt
                if location_url:
                    event_payload["location_url"] = location_url
                if call_in:
                    event_payload["phone_and_access_code"] = call_in
                if cal_type_id:
                    event_payload["type"] = cal_type_id
                if org_ids:
                    event_payload["organizations"] = org_ids

                log.info("bubble_sync: CREATE calendaritem via Eidarix wf/create-event — payload keys=%s", list(event_payload.keys()))
                event_result = _eidarix_wf_post("create-event", event_payload)
                bubble_event_id = event_result.get("bubble_id") or event_result.get("id") or event_result.get("_id")
                if bubble_event_id:
                    log.info("bubble_sync: created calendaritem _id=%s", bubble_event_id)
                else:
                    log.warning("bubble_sync: create-event returned no id: %s", event_result)

            elif event_action == "update":
                # wf/update-event not yet provided by Mori — log and skip
                log.info("bubble_sync: UPDATE calendaritem — wf/update-event endpoint not yet available, skipping")

    except Exception as exc:
        log.error("bubble_sync: error for agent_call_id=%s action=%s: %s", agent_call_id, action, exc, exc_info=True)
        patch_jsonl_row(
            _ALERTS_TABLE_KEY,
            {"agent_call_id": agent_call_id},
            {"bubble_sync_status": "error", "bubble_sync_error": str(exc)},
            bucket=bucket,
        )
        return {"ok": False, "error": str(exc), "plan": plan}

    # Patch status and IDs.
    # Only set bubble_sync_status="synced" when action=="all" to preserve
    # the existing full-sync semantics. Individual actions just stamp their IDs.
    patch_fields: dict = {}
    if action == "all":
        patch_fields["bubble_sync_status"] = "synced"
    if bubble_event_id:
        patch_fields["bubble_event_id"] = bubble_event_id
    if bubble_library_item_id and run_lib:
        patch_fields["bubble_library_item_id"] = bubble_library_item_id
    if agenda_item_ids:
        patch_fields["eidarix_agenda_item_ids"] = agenda_item_ids

    if patch_fields:
        patched = patch_jsonl_row(
            _ALERTS_TABLE_KEY,
            {"agent_call_id": agent_call_id},
            patch_fields,
            bucket=bucket,
        )
        log.info(
            "bubble_sync: patched %d row(s) for agent_call_id=%s action=%s event_id=%s lib_id=%s",
            patched, agent_call_id, action, bubble_event_id, bubble_library_item_id,
        )

    return {
        "ok": True,
        "plan": plan,
        "bubble_event_id": bubble_event_id,
        "bubble_library_item_id": bubble_library_item_id,
        "eidarix_agenda_item_ids": agenda_item_ids,
    }
