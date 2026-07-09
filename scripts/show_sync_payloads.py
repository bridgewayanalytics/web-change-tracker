"""
Reconstruct exact Eidarix payloads that were (or would be) sent for synced alert rows.

Usage:
  python scripts/show_sync_payloads.py                          # show all rows with bubble_sync_status=synced
  python scripts/show_sync_payloads.py --agent-call-ids a,b     # specific rows
  python scripts/show_sync_payloads.py --last N                 # last N synced rows
"""

import argparse
import json
import os
import sys

sys.path.insert(0, ".")

from bubble.ssm_loader import load_openai_env_from_ssm, load_db_env_from_ssm
try:
    load_openai_env_from_ssm()
    load_db_env_from_ssm()
except Exception:
    pass

import boto3

BUCKET = os.environ.get("CHANGELOG_BUCKET", "web-change-tracker-prod-artifacts-815039343351")
ALERTS_KEY = "alerts/alerts_table.jsonl"
DOC_EXTRACTIONS_KEY = "alerts/document_extractions_table.jsonl"

EIDARIX_VERSION = os.environ.get("EIDARIX_VERSION", "test")
SPACE_IDS = {
    "test": "1768998437948x865417918648382000",
    "live": "1770642377799x775210694699370900",
}


def _s3():
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _load_jsonl(key):
    body = _s3().get_object(Bucket=BUCKET, Key=key)["Body"].read().decode("utf-8")
    rows = []
    for line in body.splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def _resolve_names(client, type_name, constraints, title_field="Title"):
    try:
        result = client.search(type_name, constraints=constraints, limit=200)
        name_to_id = {}
        for item in (result.get("results") or []):
            name = (item.get(title_field) or item.get("Name") or "").strip()
            if name:
                name_to_id[name] = item.get("_id") or ""
        # Handle pagination
        cursor = result.get("cursor")
        count = result.get("count", 0)
        remaining = count - len(result.get("results", []))
        while cursor and remaining > 0:
            result = client.search(type_name, constraints=constraints, limit=200, cursor=cursor)
            for item in (result.get("results") or []):
                name = (item.get(title_field) or item.get("Name") or "").strip()
                if name:
                    name_to_id[name] = item.get("_id") or ""
            cursor = result.get("cursor")
            remaining -= len(result.get("results", []))
        return name_to_id
    except Exception as e:
        print(f"  [warn] could not list {type_name}: {e}", file=sys.stderr)
        return {}


def build_payloads(row, doc_row=None):
    """
    Reconstruct the exact Eidarix payloads for a given alert row.
    Returns dict with keys: agenda_item_payloads, library_item_payload, issues.
    """
    from bubble.bridgemind import get_client, TYPE_ORGANIZATION, TYPE_CHRONICLE_TOPIC, SPACE_CONSTRAINT
    from bubble.bubble_sync_classifier import enrich_with_doc_extraction
    from bubble.bubble_sync import _parse_date_to_iso, _resolve_library_item_type_id

    plan = dict(row.get("bubble_action") or {})
    if not plan:
        return {"error": "no bubble_action on row"}

    if doc_row:
        enrich_with_doc_extraction(plan, doc_row)

    ep = plan.get("event_preview") or {}
    lp = plan.get("library_item_preview") or {}
    space_id = SPACE_IDS.get(EIDARIX_VERSION, SPACE_IDS["test"])

    client = get_client()
    issues = []

    # Resolve orgs
    org_names = ep.get("group") or lp.get("group") or []
    org_name_to_id = _resolve_names(client, TYPE_ORGANIZATION, SPACE_CONSTRAINT, title_field="Name")
    org_ids = []
    for name in org_names:
        oid = org_name_to_id.get(name)
        if oid:
            org_ids.append(oid)
        else:
            issues.append(f"ORG NOT FOUND: {name!r}")

    # Resolve chronicle topics
    _topic_key = "topics___dt_list_custom_newsreel_update"
    ep_topic_names = [t for t in ((ep.get("field_ids") or {}).get(_topic_key) or []) if isinstance(t, str)]
    lp_topic_names = [t for t in ((lp.get("field_ids") or {}).get(_topic_key) or []) if isinstance(t, str)]
    all_topic_names = list({*ep_topic_names, *lp_topic_names})
    topic_name_to_id = _resolve_names(client, TYPE_CHRONICLE_TOPIC, SPACE_CONSTRAINT, title_field="Title")
    topic_ids = []
    for name in all_topic_names:
        tid = topic_name_to_id.get(name)
        if tid:
            topic_ids.append(tid)
        else:
            issues.append(f"CHRONICLE TOPIC NOT FOUND: {name!r}")

    # Build agenda item payloads
    agenda_item_payloads = []
    agenda_entries = row.get("agenda_item_title_chronicle_topics") or []
    official_titles = row.get("agenda_item_title_official") or []
    standardized_ids = row.get("agenda_item_standardized_id") or []

    all_agenda_topic_names = list({
        t for entry in agenda_entries for t in (entry.get("chronicle_topics") or [])
        if isinstance(t, str)
    })
    agenda_topic_name_to_id = {}
    for name in all_agenda_topic_names:
        tid = topic_name_to_id.get(name)
        if tid:
            agenda_topic_name_to_id[name] = tid
        else:
            issues.append(f"AGENDA CHRONICLE TOPIC NOT FOUND: {name!r}")

    for i, entry in enumerate(agenda_entries):
        title = (entry.get("agenda_item_title") or "").strip()
        if not title or title.upper() == "N/A":
            continue
        topic_names = entry.get("chronicle_topics") or []
        resolved_topic_ids = [agenda_topic_name_to_id[t] for t in topic_names if t in agenda_topic_name_to_id]
        off_entry = official_titles[i] if i < len(official_titles) else {}
        std_entry = standardized_ids[i] if i < len(standardized_ids) else {}
        official_title = (off_entry.get("official_title") or "").strip()
        reference_id = (std_entry.get("standardized_id") or "").strip()
        if official_title.upper() == "N/A":
            official_title = ""
        if reference_id.upper() == "N/A":
            reference_id = ""
        agenda_item_payloads.append({
            "title": title,
            "official_title": official_title,
            "reference_id": reference_id,
            "chronicle_topics": resolved_topic_ids,
            "alert_id": row.get("agent_call_id", ""),
            "space_id": space_id,
        })

    # Build library item payload
    field_ids = lp.get("field_ids") or {}
    title = (field_ids.get("name_text") or lp.get("title") or "").strip()
    url = (field_ids.get("url_text") or lp.get("url") or "").strip()
    url = url if url and url.upper() != "N/A" else None
    date = _parse_date_to_iso((row.get("event_start_date_time") or "")[:10])
    if not date:
        date = _parse_date_to_iso(field_ids.get("date_date") or "")
    summary = (field_ids.get("description_text") or "").strip() or None
    type_name = lp.get("type") or "Agenda & Materials"
    type_id = _resolve_library_item_type_id(type_name)
    if not type_id:
        issues.append(f"LIBRARY ITEM TYPE NOT FOUND: {type_name!r}")

    # Library item chronicle topics = union of agenda topics + doc extraction topics
    agenda_topic_names_for_lib = list({
        t for entry in agenda_entries
        for t in (entry.get("chronicle_topics") or [])
        if isinstance(t, str) and t.upper() not in ("N/A", "")
    })
    lib_topic_names = list({*agenda_topic_names_for_lib, *lp_topic_names})
    lib_topic_ids = [topic_name_to_id[t] for t in lib_topic_names if t in topic_name_to_id]

    lib_payload: dict = {
        "title": title,
        "alert_id": row.get("agent_call_id", ""),
        "space_id": space_id,
    }
    if lib_topic_ids:
        lib_payload["chronicle_topics"] = lib_topic_ids
    lib_payload["agenda_items"] = ["<id_from_step1_per_item>"]  # placeholder — real run uses actual IDs
    if date:
        lib_payload["date"] = date
        lib_payload["date_display"] = "Full date"
    if summary:
        lib_payload["summary"] = summary
    if url:
        lib_payload["url"] = url
    if type_id:
        lib_payload["type"] = type_id
    if org_ids:
        lib_payload["organizations"] = org_ids

    return {
        "agent_call_id": row.get("agent_call_id"),
        "alert_type": row.get("alert_type"),
        "alert_title": row.get("alert_title"),
        "bubble_sync_status": row.get("bubble_sync_status"),
        "eidarix_version": EIDARIX_VERSION,
        "agenda_item_payloads": agenda_item_payloads,
        "library_item_payload": lib_payload,
        "issues": issues,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-call-ids", type=str, default=None)
    parser.add_argument("--last", type=int, default=None)
    args = parser.parse_args()

    print(f"Loading alerts from s3://{BUCKET}/{ALERTS_KEY} ...", file=sys.stderr)
    all_rows = _load_jsonl(ALERTS_KEY)
    print(f"  {len(all_rows)} rows total", file=sys.stderr)

    print(f"Loading doc extractions ...", file=sys.stderr)
    try:
        doc_rows = _load_jsonl(DOC_EXTRACTIONS_KEY)
        doc_by_call_id = {r.get("agent_call_id"): r for r in doc_rows if r.get("agent_call_id")}
    except Exception as e:
        print(f"  [warn] could not load doc extractions: {e}", file=sys.stderr)
        doc_by_call_id = {}

    if args.agent_call_ids:
        ids = {x.strip() for x in args.agent_call_ids.split(",")}
        rows = [r for r in all_rows if r.get("agent_call_id") in ids]
    elif args.last:
        synced = [r for r in all_rows if r.get("bubble_sync_status") == "synced"]
        rows = synced[-args.last:]
    else:
        rows = [r for r in all_rows if r.get("bubble_sync_status") == "synced"]

    print(f"  {len(rows)} matching rows", file=sys.stderr)

    for row in rows:
        call_id = row.get("agent_call_id", "")
        doc_row = doc_by_call_id.get(call_id)
        result = build_payloads(row, doc_row)
        print(json.dumps(result, indent=2, default=str))
        print()


if __name__ == "__main__":
    main()
