#!/usr/bin/env python3
"""
Pull historical Bubble data for agenda/topic analysis.

Usage:
    # Requires BUBBLE_API_URL and BUBBLE_API_KEY in environment (or .env)
    python analysis/agenda_topic_mapping/pull_historical_data.py

Outputs:
    analysis/agenda_topic_mapping/historical_samples.json
    analysis/agenda_topic_mapping/chronicles_tree.json
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass  # .env must be loaded by shell or not needed

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("pull_historical_data")

OUTPUT_DIR = Path(__file__).resolve().parent
SAMPLES_FILE = OUTPUT_DIR / "historical_samples.json"
CHRONICLES_FILE = OUTPUT_DIR / "chronicles_tree.json"

# ---------------------------------------------------------------------------
# Bubble client setup
# ---------------------------------------------------------------------------

def get_client():
    from bubble.client import get_client as _get_client
    return _get_client(use_cache=True)


def take(gen, limit: int) -> list[dict]:
    out = []
    for i, item in enumerate(gen):
        if i >= limit:
            break
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

CHRONICLES_TREE_ID = "1710771208905x698956612053237800"  # from snapshot


def pull_chronicles_tree_nodes(client, limit=500) -> list[dict]:
    """Fetch all nodes in the Chronicles tree."""
    log.info("Fetching Chronicles tree nodes (limit=%d)...", limit)
    from bubble.lookups import TYPE_TREE_NODE, TREE_NODE_TREE_FIELD
    constraints = [{"key": TREE_NODE_TREE_FIELD, "constraint_type": "equals", "value": CHRONICLES_TREE_ID}]
    results = take(client.list_all(TYPE_TREE_NODE, constraints=constraints, page_size=100), limit)
    log.info("  -> %d Chronicles tree nodes fetched", len(results))
    return results


def pull_resources_with_topic(client, limit=100) -> list[dict]:
    """Fetch resources where topic suggestion is populated."""
    log.info("Fetching resources with topic suggestion (limit=%d)...", limit)
    from bubble.lookups import TYPE_RESOURCE
    constraints = [{"key": "topic suggestion", "constraint_type": "is_not_empty"}]
    results = take(client.list_all(TYPE_RESOURCE, constraints=constraints, page_size=100), limit)
    log.info("  -> %d resources with topic suggestion", len(results))
    return results


def pull_resources_pdf_with_topic(client, limit=50) -> list[dict]:
    """Fetch PDF resources with topic suggestion populated."""
    log.info("Fetching PDF resources with topic suggestion (limit=%d)...", limit)
    from bubble.lookups import TYPE_RESOURCE
    constraints = [
        {"key": "URL", "constraint_type": "text contains", "value": ".pdf"},
        {"key": "topic suggestion", "constraint_type": "is_not_empty"},
    ]
    results = take(client.list_all(TYPE_RESOURCE, constraints=constraints, page_size=100), limit)
    log.info("  -> %d PDF resources with topic suggestion", len(results))
    return results


def pull_resources_with_topic_and_calendar(resources_with_topic: list[dict]) -> list[dict]:
    """Filter already-fetched resources that also have Related calendar items."""
    results = [
        r for r in resources_with_topic
        if (r.get("Related calendar items") or r.get("related_meetings_list_custom_calendar_items") or [])
    ]
    log.info("Resources with topic + calendar items: %d (filtered from %d)", len(results), len(resources_with_topic))
    return results


def pull_calendar_items_with_agenda(client, limit=200) -> list[dict]:
    """Fetch calendar items and filter for those with attached agenda items populated."""
    log.info("Fetching calendar items (limit=%d) to find ones with agenda items...", limit)
    from bubble.lookups import TYPE_CALENDAR_ITEM
    # Fetch a broader set and filter locally (is_not_empty may not work for list fields)
    results = take(client.list_all(TYPE_CALENDAR_ITEM, page_size=100), limit)
    with_agenda = [c for c in results if c.get("attached agenda items")]
    log.info("  -> %d calendar items fetched, %d have agenda items", len(results), len(with_agenda))
    return with_agenda


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------

def resolve_tree_node_name(client, node_id: str, cache: dict) -> str | None:
    """Resolve a tree node ID to its display name, with caching."""
    if not node_id:
        return None
    if node_id in cache:
        return cache[node_id]
    try:
        from bubble.lookups import TYPE_TREE_NODE
        obj = client.get(TYPE_TREE_NODE, node_id)
        name = (obj.get("name") or obj.get("Name") or "").strip()
        cache[node_id] = name
        return name
    except Exception as e:
        log.warning("Could not resolve tree node %s: %s", node_id, e)
        cache[node_id] = None
        return None


def resolve_calendar_item(client, cal_id: str, cache: dict) -> dict | None:
    """Resolve a calendar item ID to {title, date}."""
    if not cal_id:
        return None
    if cal_id in cache:
        return cache[cal_id]
    try:
        from bubble.lookups import TYPE_CALENDAR_ITEM
        obj = client.get(TYPE_CALENDAR_ITEM, cal_id)
        result = {
            "title": (obj.get("title") or "").strip(),
            "date": obj.get("date"),
            "NAIC Group (tree node)": obj.get("NAIC Group (tree node)"),
        }
        cache[cal_id] = result
        return result
    except Exception as e:
        log.warning("Could not resolve calendar item %s: %s", cal_id, e)
        cache[cal_id] = None
        return None


def clean_resource_sample(resource: dict, client, node_cache: dict, cal_cache: dict) -> dict:
    """Extract a clean analysis record from a raw Bubble resource."""
    # Resolve topic suggestion ID to name
    topic_id = resource.get("topic suggestion")
    if isinstance(topic_id, dict):
        topic_id = topic_id.get("_id") or topic_id.get("id")
    topic_name = resolve_tree_node_name(client, topic_id, node_cache) if topic_id else None

    # Resolve calendar items
    cal_ids = resource.get("Related calendar items") or []
    if isinstance(cal_ids, str):
        cal_ids = [cal_ids]
    calendar_items = []
    for cid in cal_ids[:3]:  # cap at 3
        if isinstance(cid, dict):
            cid = cid.get("_id") or cid.get("id")
        resolved = resolve_calendar_item(client, cid, cal_cache)
        if resolved:
            calendar_items.append(resolved)

    # Resolve Type1
    type1_ids = resource.get("Type1") or []
    type1_names = []
    for tid in (type1_ids if isinstance(type1_ids, list) else [type1_ids]):
        if isinstance(tid, dict):
            tid = tid.get("_id") or tid.get("id")
        name = resolve_tree_node_name(client, tid, node_cache)
        if name:
            type1_names.append(name)

    return {
        "_id": resource.get("_id"),
        "Name": (resource.get("Name") or "").strip(),
        "URL": (resource.get("URL") or "").strip(),
        "notes": (resource.get("notes") or "").strip(),
        "parent": (resource.get("parent") or "").strip(),
        "date": resource.get("date"),
        "topic_suggestion_id": topic_id,
        "topic_suggestion_name": topic_name,
        "Type1_names": type1_names,
        "Related_calendar_items": calendar_items,
        "is_pdf": (resource.get("URL") or "").lower().endswith(".pdf"),
    }


def clean_calendar_sample(cal: dict) -> dict:
    """Extract a clean analysis record from a raw Bubble calendar item."""
    agenda_items = cal.get("attached agenda items") or []
    agenda = cal.get("Agenda") or []
    return {
        "_id": cal.get("_id"),
        "title": (cal.get("title") or "").strip(),
        "date": cal.get("date"),
        "NAIC Group (tree node)": cal.get("NAIC Group (tree node)"),
        "attached_agenda_items": agenda_items,
        "agenda_items_count": len(agenda_items) if isinstance(agenda_items, list) else 0,
        "Agenda": agenda,
        "Relevant Documents": cal.get("Relevant Documents") or [],
        "subtopic": (cal.get("subtopic") or "").strip(),
        "has_topic": cal.get("has topic"),
        "event_description": (cal.get("event description") or "").strip()[:200],
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    client = get_client()
    node_cache: dict[str, str | None] = {}
    cal_cache: dict[str, dict | None] = {}

    # 1. Chronicles tree
    chronicle_nodes = pull_chronicles_tree_nodes(client)
    chronicle_tree = []
    for n in chronicle_nodes:
        nid = n.get("_id") or n.get("id")
        name = (n.get("name") or n.get("Name") or "").strip()
        parent = n.get("parent") or n.get("Parent") or n.get("parent_node")
        if isinstance(parent, dict):
            parent = parent.get("_id") or parent.get("id")
        chronicle_tree.append({"_id": nid, "name": name, "parent": parent})
        if nid:
            node_cache[str(nid)] = name

    CHRONICLES_FILE.write_text(json.dumps(chronicle_tree, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Wrote %d chronicle nodes to %s", len(chronicle_tree), CHRONICLES_FILE)

    # 2. Resources with topic suggestion
    resources_with_topic = pull_resources_with_topic(client)
    cleaned_topic_resources = [clean_resource_sample(r, client, node_cache, cal_cache) for r in resources_with_topic]

    # 3. PDF resources with topic
    pdf_topic_resources = pull_resources_pdf_with_topic(client)
    cleaned_pdf_resources = [clean_resource_sample(r, client, node_cache, cal_cache) for r in pdf_topic_resources]

    # 4. Resources with topic + calendar (filter from already-fetched set)
    topic_cal_resources = pull_resources_with_topic_and_calendar(resources_with_topic)
    cleaned_topic_cal = [clean_resource_sample(r, client, node_cache, cal_cache) for r in topic_cal_resources]

    # 5. Calendar items with agenda items
    cal_with_agenda = pull_calendar_items_with_agenda(client)
    cleaned_calendars = [clean_calendar_sample(c) for c in cal_with_agenda]

    # Assemble
    samples = {
        "metadata": {
            "chronicles_tree_node_count": len(chronicle_tree),
            "resources_with_topic_count": len(cleaned_topic_resources),
            "pdf_resources_with_topic_count": len(cleaned_pdf_resources),
            "resources_with_topic_and_calendar_count": len(cleaned_topic_cal),
            "calendar_items_with_agenda_count": len(cleaned_calendars),
        },
        "resources_with_topic": cleaned_topic_resources,
        "pdf_resources_with_topic": cleaned_pdf_resources,
        "resources_with_topic_and_calendar": cleaned_topic_cal,
        "calendar_items_with_agenda": cleaned_calendars,
    }

    SAMPLES_FILE.write_text(json.dumps(samples, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Wrote historical samples to %s", SAMPLES_FILE)

    # Print summary
    print("\n" + "=" * 60)
    print("HISTORICAL DATA PULL SUMMARY")
    print("=" * 60)
    print(f"Chronicles tree nodes:              {len(chronicle_tree)}")
    print(f"Resources with topic suggestion:    {len(cleaned_topic_resources)}")
    print(f"  - of which are PDFs:              {sum(1 for r in cleaned_topic_resources if r['is_pdf'])}")
    print(f"PDF resources with topic:           {len(cleaned_pdf_resources)}")
    print(f"Resources with topic + calendar:    {len(cleaned_topic_cal)}")
    print(f"Calendar items with agenda items:   {len(cleaned_calendars)}")
    print()

    if cleaned_topic_resources:
        topic_names = [r["topic_suggestion_name"] for r in cleaned_topic_resources if r["topic_suggestion_name"]]
        unique_topics = sorted(set(topic_names))
        print(f"Unique topics assigned:             {len(unique_topics)}")
        for t in unique_topics[:20]:
            count = topic_names.count(t)
            print(f"  - {t} ({count}x)")
        if len(unique_topics) > 20:
            print(f"  ... and {len(unique_topics) - 20} more")
        print()

    if cleaned_calendars:
        print("Agenda items structure sample:")
        for c in cleaned_calendars[:3]:
            print(f"  Calendar: {c['title'][:60]}")
            print(f"    Date: {c['date']}")
            print(f"    Agenda items ({c['agenda_items_count']}):")
            items = c["attached_agenda_items"]
            for item in (items[:5] if isinstance(items, list) else []):
                if isinstance(item, dict):
                    print(f"      - {json.dumps(item)[:120]}")
                else:
                    print(f"      - {str(item)[:120]}")
            print()

    print("Files written:")
    print(f"  {SAMPLES_FILE}")
    print(f"  {CHRONICLES_FILE}")


if __name__ == "__main__":
    main()
