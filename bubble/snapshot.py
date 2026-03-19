"""
Build a read-only Bubble snapshot for mapping: trees, tree nodes, calendar items, resources.
Writes debug/bubble_snapshot.json when built. Uses same type names as lookups.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bubble.client import BubbleClient

from bubble import lookups

log = logging.getLogger(__name__)

# Type names (same as lookups; no extra env here so snapshot stays in sync with lookups)
_TYPE_TREE = lookups.TYPE_TREE
_TYPE_TREE_NODE = lookups.TYPE_TREE_NODE
_TYPE_CALENDAR_ITEM = lookups.TYPE_CALENDAR_ITEM
_TYPE_RESOURCE = lookups.TYPE_RESOURCE
_TYPE_AGENDA_ITEM = lookups.TYPE_AGENDA_ITEM

# Default limit per type when paginating
DEFAULT_SNAPSHOT_LIMIT = 200

# Path for snapshot output (relative to cwd)
DEBUG_SNAPSHOT_PATH = Path("debug") / "bubble_snapshot.json"


def _take(gen, limit: int) -> list[dict]:
    """Consume up to `limit` items from a generator of dicts."""
    out: list[dict] = []
    for i, item in enumerate(gen):
        if i >= limit:
            break
        out.append(item)
    return out


def _pick_id_name_main(obj: dict) -> dict:
    """Include _id, id, Name, name, and any main_* keys."""
    out: dict[str, Any] = {}
    for k in ("_id", "id"):
        if k in obj:
            out[k] = obj[k]
    for k in ("Name", "name"):
        if k in obj:
            out[k] = obj[k]
    for k, v in obj.items():
        if k.startswith("main_"):
            out[k] = v
    return out


def _pick_tree_node(obj: dict) -> dict:
    """Fields needed for mapping: id, name, parent, tree, topic/chronicle if present."""
    out: dict[str, Any] = {}
    for k in ("_id", "id"):
        if k in obj:
            out[k] = obj[k]
    for k in ("Name", "name"):
        if k in obj:
            out[k] = obj[k]
    for k in ("Parent", "parent", "parent_node"):
        if k in obj:
            out[k] = obj[k]
    for k in ("Tree", "tree", "parent_tree"):
        if k in obj:
            out[k] = obj[k]
    for k in ("topic", "chronicle", "topic (chronicle)"):
        if k in obj:
            out[k] = obj[k]
    return out


def _pick_calendar_item(obj: dict) -> dict:
    """id, title, date, NAIC Group (tree node) if present."""
    out: dict[str, Any] = {}
    for k in ("_id", "id"):
        if k in obj:
            out[k] = obj[k]
    for k in ("title", "date", "NAIC Group (tree node)"):
        if k in obj:
            out[k] = obj[k]
    return out


def _pick_resource(obj: dict) -> dict:
    """id, URL, Name, parent, Type, Type1, topic suggestion if present (light)."""
    out: dict[str, Any] = {}
    for k in ("_id", "id"):
        if k in obj:
            out[k] = obj[k]
    for k in ("URL", "Name", "parent", "Type", "Type1", "topic suggestion"):
        if k in obj:
            out[k] = obj[k]
    return out


def _pick_agenda_item(obj: dict) -> dict:
    """Agenda Item fields needed for matching: id, titles, ref numbers, topics, resources, group."""
    out: dict[str, Any] = {}
    for k in ("_id", "id"):
        if k in obj:
            out[k] = obj[k]
    for k in (
        "BA title", "NAIC Title", "Ref #", "BA Ref #",
        "Topics", "Resources",
        "Discussed at", "Discussed at list",
        "Category", "Status", "Description",
        "SSAP Ref.", "SSAP Ref. - texts",
    ):
        if k in obj:
            out[k] = obj[k]
    return out


def build_bubble_snapshot(
    client: "BubbleClient",
    limit: int = DEFAULT_SNAPSHOT_LIMIT,
) -> dict[str, Any]:
    """
    Build a snapshot of Bubble data for mapping: trees, tree_nodes, calendar_items, resources.
    Uses pagination (list_all) and caps each type at `limit` items.
    Returns dict with keys: trees, tree_nodes, calendar_items, resources (each a list of trimmed objects).
    """
    snapshot: dict[str, Any] = {
        "trees": [],
        "tree_nodes": [],
        "calendar_items": [],
        "resources": [],
        "agenda_items": [],
    }

    # Trees: no constraints, up to limit
    try:
        gen = client.list_all(_TYPE_TREE, page_size=min(100, limit))
        raw_trees = _take(gen, limit)
        snapshot["trees"] = [_pick_id_name_main(t) for t in raw_trees]
    except Exception as e:
        log.warning("Snapshot trees fetch failed: %s", e)

    # Tree nodes: paginated, up to limit
    try:
        gen = client.list_all(_TYPE_TREE_NODE, page_size=min(100, limit))
        raw_nodes = _take(gen, limit)
        snapshot["tree_nodes"] = [_pick_tree_node(n) for n in raw_nodes]
    except Exception as e:
        log.warning("Snapshot tree_nodes fetch failed: %s", e)

    # Calendar items: paginated, up to limit
    try:
        gen = client.list_all(_TYPE_CALENDAR_ITEM, page_size=min(100, limit))
        raw_cal = _take(gen, limit)
        snapshot["calendar_items"] = [_pick_calendar_item(c) for c in raw_cal]
    except Exception as e:
        log.warning("Snapshot calendar_items fetch failed: %s", e)

    # Resources: paginated, up to limit
    try:
        gen = client.list_all(_TYPE_RESOURCE, page_size=min(100, limit))
        raw_res = _take(gen, limit)
        snapshot["resources"] = [_pick_resource(r) for r in raw_res]
    except Exception as e:
        log.warning("Snapshot resources fetch failed: %s", e)

    # Agenda Items: paginated, up to limit
    try:
        gen = client.list_all(_TYPE_AGENDA_ITEM, page_size=min(100, limit))
        raw_agenda = _take(gen, limit)
        snapshot["agenda_items"] = [_pick_agenda_item(a) for a in raw_agenda]
    except Exception as e:
        log.warning("Snapshot agenda_items fetch failed: %s", e)

    num_nodes = len(snapshot["tree_nodes"])
    num_cal = len(snapshot["calendar_items"])
    num_res = len(snapshot["resources"])
    num_agenda = len(snapshot["agenda_items"])
    log.info(
        "Loaded Bubble snapshot: %d tree nodes, %d calendar items, %d resources, %d agenda items",
        num_nodes,
        num_cal,
        num_res,
        num_agenda,
    )

    # Write to debug/bubble_snapshot.json
    try:
        DEBUG_SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        DEBUG_SNAPSHOT_PATH.write_text(
            json.dumps(snapshot, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        log.info("Wrote Bubble snapshot to %s", DEBUG_SNAPSHOT_PATH)
    except Exception as e:
        log.warning("Failed to write Bubble snapshot to %s: %s", DEBUG_SNAPSHOT_PATH, e)

    return snapshot
