"""
Read-only Bubble lookup helpers for trees, tree nodes, calendar items, and resources.
Uses in-module caching to avoid repeatedly pulling large trees in a single run.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from bubble.client import BubbleAPIError, get_client

if TYPE_CHECKING:
    from bubble.client import BubbleClient

log = logging.getLogger(__name__)

# Bubble Data API type names (override via env: BUBBLE_TYPE_TREE, etc.)
TYPE_TREE = os.environ.get("BUBBLE_TYPE_TREE", "Tree")
TYPE_TREE_NODE = os.environ.get("BUBBLE_TYPE_TREE_NODE", "Tree node")
TYPE_CALENDAR_ITEM = os.environ.get("BUBBLE_TYPE_CALENDAR_ITEM", "Calendar item")
TYPE_RESOURCE = os.environ.get("BUBBLE_TYPE_RESOURCE", "Resource")
TYPE_AGENDA_ITEM = os.environ.get("BUBBLE_TYPE_AGENDA_ITEM", "Agenda item")
# Field on Tree node that references the Tree (Bubble may use "Tree" or "parent_tree")
TREE_NODE_TREE_FIELD = os.environ.get("BUBBLE_TREE_NODE_TREE_FIELD", "parent_tree")

# In-module caches (keyed by cache key string)
_tree_cache: dict[str, dict] = {}
_tree_nodes_cache: dict[str, list[dict]] = {}
_agenda_items_cache: dict[str, list[dict]] = {}


def _client() -> "BubbleClient":
    """Lazy singleton client with cache enabled for read-heavy lookups."""
    return get_client(use_cache=True)


def _tree_node_name(node: dict) -> str:
    """Display name of a tree node. Live API returns lowercase 'name'."""
    return (node.get("name") or node.get("Name") or "").strip()


def _tree_node_id(node: dict) -> str | None:
    """Bubble unique id of a tree node."""
    return node.get("_id") or node.get("id")


def _tree_node_parent(node: dict) -> str | None:
    """Parent tree node id (reference). Live API may use 'parent' or 'Parent'."""
    parent = node.get("parent") or node.get("Parent") or node.get("parent_node")
    if isinstance(parent, str):
        return parent
    if isinstance(parent, dict):
        return parent.get("_id") or parent.get("id")
    return None


def get_all_trees() -> list[dict]:
    """
    Return all Tree things (no constraints). Use for listing available trees.

    Example:
        for tree in get_all_trees():
            print(tree.get("Name"), tree.get("_id"))
    """
    try:
        client = _client()
        return list(client.list_all(TYPE_TREE, page_size=100))
    except BubbleAPIError:
        raise
    except Exception as e:
        log.warning("get_all_trees() failed: %s", e)
        return []


def get_tree_by_name(name: str) -> dict | None:
    """
    Return the Tree thing whose name matches (exact), or None if not found.

    Example:
        tree = get_tree_by_name("NAIC Organization")
        if tree:
            tree_id = tree.get("_id") or tree.get("id")
    """
    cache_key = f"tree_name:{name}"
    if cache_key in _tree_cache:
        return _tree_cache[cache_key]
    try:
        client = _client()
        out = client.search(
            TYPE_TREE,
            constraints=[{"key": "Name", "constraint_type": "equals", "value": name}],
            limit=1,
        )
        results = out.get("results", [])
        tree = results[0] if results else None
        if tree is not None:
            _tree_cache[cache_key] = tree
        return tree
    except BubbleAPIError:
        raise
    except Exception as e:
        log.warning("get_tree_by_name(%r) failed: %s", name, e)
        return None


def get_tree_nodes_in_tree(tree_id: str) -> list[dict]:
    """
    Return all Tree node things that belong to the given tree (read from cache when possible).

    Example:
        nodes = get_tree_nodes_in_tree("123456x789")
        for n in nodes:
            print(n.get("name"), n.get("_id"))
    """
    if tree_id in _tree_nodes_cache:
        return _tree_nodes_cache[tree_id]
    try:
        client = _client()
        nodes = list(
            client.list_all(
                TYPE_TREE_NODE,
                constraints=[{"key": TREE_NODE_TREE_FIELD, "constraint_type": "equals", "value": tree_id}],
                page_size=100,
            )
        )
        _tree_nodes_cache[tree_id] = nodes
        return nodes
    except BubbleAPIError:
        raise
    except Exception as e:
        log.warning("get_tree_nodes_in_tree(%r) failed: %s", tree_id, e)
        return []


def find_tree_node_by_path(tree_id: str, path: list[str]) -> dict | None:
    """
    Find the Tree node at the given path (ordered list of node names from root to leaf).

    Path example: ["NAIC", "Financial Condition (E) Committee", "Capital Adequacy (E) Task Force"]
    Returns the node for the last segment, or None if any segment is missing.

    Example:
        node = find_tree_node_by_path(tree_id, ["NAIC", "E", "Working Groups"])
    """
    if not path:
        return None
    path = [p.strip() for p in path if p and str(p).strip()]
    if not path:
        return None

    nodes = get_tree_nodes_in_tree(tree_id)
    named_nodes = [n for n in nodes if _tree_node_name(n)]
    by_id = {_tree_node_id(n): n for n in named_nodes if _tree_node_id(n)}
    by_parent: dict[str, list[dict]] = {}
    roots: list[dict] = []
    for n in named_nodes:
        pid = _tree_node_parent(n)
        if not pid:
            roots.append(n)
        else:
            by_parent.setdefault(pid, []).append(n)

    def find_child(parent_node: dict | None, segment: str) -> dict | None:
        if parent_node is None:
            cands = roots
        else:
            cands = by_parent.get(_tree_node_id(parent_node) or "", [])
        seg_lower = segment.lower()
        for c in cands:
            if _tree_node_name(c).lower() == seg_lower:
                return c
        return None

    current: dict | None = None
    for segment in path:
        current = find_child(current, segment)
        if current is None:
            return None
    return current


def find_tree_nodes_fuzzy(tree_id: str, query: str, limit: int = 10) -> list[dict]:
    """
    Find Tree nodes in the tree whose name contains the query (case-insensitive), up to limit.

    Example:
        nodes = find_tree_nodes_fuzzy(tree_id, "Capital Adequacy", limit=5)
    """
    if not query or not query.strip():
        return []
    q = query.strip().lower()
    nodes = get_tree_nodes_in_tree(tree_id)
    out: list[dict] = []
    for n in nodes:
        name = _tree_node_name(n)
        if not name:
            continue
        if q in name.lower():
            out.append(n)
            if len(out) >= limit:
                break
    return out


def find_calendar_item_by_title_date(
    title: str,
    start_dt_iso: str | None = None,
    tolerance_days: int = 7,
) -> dict | None:
    """
    Find a Calendar item that matches the title and optionally a start date within tolerance.

    title: substring match (case-insensitive) on the item's title.
    start_dt_iso: optional ISO date or datetime string (e.g. "2025-01-15" or "2025-01-15T14:00:00Z").
    tolerance_days: number of days before/after start_dt to consider a match (default 7).

    Returns the first matching item or None.

    Example:
        item = find_calendar_item_by_title_date("Capital Adequacy Task Force", "2025-02-01", tolerance_days=3)
    """
    try:
        client = _client()
        constraints: list[dict] = [
            {"key": "title", "constraint_type": "text contains", "value": title},
        ]
        if start_dt_iso:
            try:
                dt = datetime.fromisoformat(start_dt_iso.replace("Z", "+00:00"))
            except ValueError:
                dt = datetime.now(timezone.utc)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            low = (dt - timedelta(days=tolerance_days)).strftime("%Y-%m-%d")
            high = (dt + timedelta(days=tolerance_days)).strftime("%Y-%m-%d")
            constraints.append({"key": "date", "constraint_type": "greater than", "value": low})
            constraints.append({"key": "date", "constraint_type": "less than", "value": high})
        out = client.search(TYPE_CALENDAR_ITEM, constraints=constraints, limit=1)
        results = out.get("results", [])
        return results[0] if results else None
    except BubbleAPIError:
        raise
    except Exception as e:
        log.warning("find_calendar_item_by_title_date failed: %s", e)
        return None


def search_calendar_items_by_date_window(
    date_iso: str,
    window_days: int = 2,
    limit: int = 50,
) -> list[dict]:
    """
    Return Calendar items whose date falls in [date_iso - window_days, date_iso + window_days].
    Used for __meeting_meta-based calendar linking; scoring by title/group_name is done by caller.

    date_iso: center date (YYYY-MM-DD).
    window_days: half-window in days (default 2 → ±2 days).
    limit: max results (default 50).

    Returns list of full calendar item dicts (each has _id, title, date, etc.).
    """
    if not date_iso or not str(date_iso).strip():
        return []
    try:
        dt = datetime.fromisoformat(str(date_iso).strip().replace("Z", "+00:00")[:10])
    except ValueError:
        return []
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    low = (dt - timedelta(days=window_days)).strftime("%Y-%m-%d")
    high = (dt + timedelta(days=window_days)).strftime("%Y-%m-%d")
    try:
        client = _client()
        constraints: list[dict] = [
            {"key": "date", "constraint_type": "greater than", "value": low},
            {"key": "date", "constraint_type": "less than", "value": high},
        ]
        out = client.search(TYPE_CALENDAR_ITEM, constraints=constraints, limit=limit)
        return out.get("results", []) or []
    except BubbleAPIError:
        raise
    except Exception as e:
        log.warning("search_calendar_items_by_date_window failed: %s", e)
        return []


def search_calendar_items_by_naic_group(
    naic_group_node_id: str,
    date_iso: str | None = None,
    window_days: int = 7,
    limit: int = 50,
) -> tuple[list[dict], dict]:
    """
    Return Calendar items whose "NAIC Group (tree node)" equals the given node ID.

    If date_iso is provided, also constrains date to ±window_days.
    If date_iso is None, returns upcoming items (date > today) capped at limit.

    Returns (results, meta) where meta includes the constraints JSON sent to Bubble
    and any error information for auditing.
    """
    meta: dict = {"naic_group_node_id": naic_group_node_id, "date_iso": date_iso,
                  "window_days": window_days, "limit": limit}
    if not naic_group_node_id or not str(naic_group_node_id).strip():
        meta["error"] = "empty_node_id"
        return [], meta
    constraints: list[dict] = [
        {"key": "NAIC Group (tree node)", "constraint_type": "equals", "value": naic_group_node_id},
    ]
    date_mode = "no_date_upcoming"
    if date_iso and str(date_iso).strip():
        try:
            dt = datetime.fromisoformat(str(date_iso).strip().replace("Z", "+00:00")[:10])
        except ValueError:
            dt = None
        if dt:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            low = (dt - timedelta(days=window_days)).strftime("%Y-%m-%d")
            high = (dt + timedelta(days=window_days)).strftime("%Y-%m-%d")
            constraints.append({"key": "date", "constraint_type": "greater than", "value": low})
            constraints.append({"key": "date", "constraint_type": "less than", "value": high})
            date_mode = "date_window"
            meta["date_low"] = low
            meta["date_high"] = high
        else:
            date_mode = "date_parse_failed_fallback_upcoming"
            today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
            constraints.append({"key": "date", "constraint_type": "greater than", "value": today})
    else:
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        constraints.append({"key": "date", "constraint_type": "greater than", "value": today})
    meta["date_mode"] = date_mode
    meta["constraints"] = constraints
    try:
        client = _client()
        out = client.search(TYPE_CALENDAR_ITEM, constraints=constraints, limit=limit)
        results = out.get("results", []) or []
        meta["result_count"] = len(results)
        return results, meta
    except BubbleAPIError:
        raise
    except Exception as e:
        import traceback
        log.warning("search_calendar_items_by_naic_group failed: %s", e)
        meta["error"] = str(e)
        meta["traceback"] = traceback.format_exc()
        return [], meta


def search_agenda_items_by_naic_group(
    naic_group_node_id: str,
    limit: int = 100,
) -> list[dict]:
    """
    Return Agenda Items whose 'Discussed at list' or 'Discussed at' includes
    the given NAIC group node ID.

    Tries 'Discussed at list' first (newer list field), falls back to
    'Discussed at' (single reference). Results are cached per node ID.

    Returns list of full Agenda Item dicts (each has _id, BA title, Ref #, Topics, etc.).
    """
    cache_key = f"agenda_items:{naic_group_node_id}"
    if cache_key in _agenda_items_cache:
        return _agenda_items_cache[cache_key]
    if not naic_group_node_id or not str(naic_group_node_id).strip():
        return []
    try:
        client = _client()
        # Try "Discussed at list" first (list field — use "contains" for list membership)
        results: list[dict] = []
        try:
            constraints: list[dict] = [
                {"key": "Discussed at list", "constraint_type": "contains", "value": naic_group_node_id},
            ]
            out = client.search(TYPE_AGENDA_ITEM, constraints=constraints, limit=limit)
            results = out.get("results", []) or []
        except BubbleAPIError as e:
            log.debug("Discussed at list query failed (%s), trying Discussed at", e)
        if not results:
            # Fallback: "Discussed at" (single reference field)
            constraints2: list[dict] = [
                {"key": "Discussed at", "constraint_type": "equals", "value": naic_group_node_id},
            ]
            out2 = client.search(TYPE_AGENDA_ITEM, constraints=constraints2, limit=limit)
            results = out2.get("results", []) or []
        _agenda_items_cache[cache_key] = results
        return results
    except Exception as e:
        log.warning("search_agenda_items_by_naic_group(%r) failed: %s", naic_group_node_id, e)
        return []


def search_agenda_items_by_ref(
    ref_number: str,
    limit: int = 20,
) -> list[dict]:
    """
    Search Agenda Items whose 'BA Ref #' field contains a ref number.

    Uses 'text contains' constraint to handle prefixed/suffixed refs
    (e.g. searching '2025-22' matches 'RBC-IRE-WG#2025-22').
    Results are cached per ref number.
    """
    cache_key = f"agenda_items_ref:{ref_number}"
    if cache_key in _agenda_items_cache:
        return _agenda_items_cache[cache_key]
    if not ref_number or not str(ref_number).strip():
        return []
    try:
        client = _client()
        constraints: list[dict] = [
            {"key": "BA Ref #", "constraint_type": "text contains", "value": ref_number},
        ]
        out = client.search(TYPE_AGENDA_ITEM, constraints=constraints, limit=limit)
        results = out.get("results", []) or []
        _agenda_items_cache[cache_key] = results
        return results
    except BubbleAPIError as e:
        log.warning("search_agenda_items_by_ref(%r) API error: %s", ref_number, e)
        return []
    except Exception as e:
        log.warning("search_agenda_items_by_ref(%r) failed: %s", ref_number, e)
        return []


def search_agenda_items_by_title(
    keyword: str,
    limit: int = 20,
) -> list[dict]:
    """
    Search Agenda Items whose 'BA title' field contains the given keyword.

    Uses 'text contains' constraint for substring matching.
    Results are cached per keyword.
    """
    cache_key = f"agenda_items_title:{keyword}"
    if cache_key in _agenda_items_cache:
        return _agenda_items_cache[cache_key]
    if not keyword or not str(keyword).strip():
        return []
    try:
        client = _client()
        constraints: list[dict] = [
            {"key": "BA title", "constraint_type": "text contains", "value": keyword},
        ]
        out = client.search(TYPE_AGENDA_ITEM, constraints=constraints, limit=limit)
        results = out.get("results", []) or []
        _agenda_items_cache[cache_key] = results
        return results
    except BubbleAPIError as e:
        log.warning("search_agenda_items_by_title(%r) API error: %s", keyword, e)
        return []
    except Exception as e:
        log.warning("search_agenda_items_by_title(%r) failed: %s", keyword, e)
        return []


def search_agenda_items_by_resource(
    resource_id: str,
    limit: int = 10,
) -> list[dict]:
    """
    Search Agenda Items whose 'Resources' field contains the given resource ID.

    This is the bidirectional lookup: instead of asking "which agenda items belong
    to this group?", we ask "which agenda items link to this resource?".
    Solves retrieval gaps where Discussed at list is empty.
    """
    cache_key = f"agenda_items_resource:{resource_id}"
    if cache_key in _agenda_items_cache:
        return _agenda_items_cache[cache_key]
    if not resource_id or not str(resource_id).strip():
        return []
    try:
        client = _client()
        constraints: list[dict] = [
            {"key": "Resources", "constraint_type": "contains", "value": resource_id},
        ]
        out = client.search(TYPE_AGENDA_ITEM, constraints=constraints, limit=limit)
        results = out.get("results", []) or []
        _agenda_items_cache[cache_key] = results
        return results
    except BubbleAPIError as e:
        log.warning("search_agenda_items_by_resource(%r) API error: %s", resource_id, e)
        return []
    except Exception as e:
        log.warning("search_agenda_items_by_resource(%r) failed: %s", resource_id, e)
        return []


def get_agenda_item(item_id: str) -> dict | None:
    """Fetch a single Agenda Item by ID."""
    if not item_id or not str(item_id).strip():
        return None
    try:
        client = _client()
        return client.get(TYPE_AGENDA_ITEM, item_id)
    except BubbleAPIError:
        return None
    except Exception as e:
        log.warning("get_agenda_item(%r) failed: %s", item_id, e)
        return None


def find_resources_by_url(url: str) -> list[dict]:
    """
    Find all Resource things whose URL equals the given url (for dedupe / matching).

    Example:
        existing = find_resources_by_url("https://example.com/doc.pdf")
        if existing:
            # already imported
            pass
    """
    if not url or not url.strip():
        return []
    url = url.strip()
    try:
        client = _client()
        out = client.search(
            TYPE_RESOURCE,
            constraints=[{"key": "URL", "constraint_type": "equals", "value": url}],
            limit=100,
        )
        return out.get("results", [])
    except BubbleAPIError:
        raise
    except Exception as e:
        log.warning("find_resources_by_url failed: %s", e)
        return []


def clear_lookups_cache() -> None:
    """Clear in-module caches for trees, tree nodes, and agenda items."""
    global _tree_cache, _tree_nodes_cache, _agenda_items_cache
    _tree_cache.clear()
    _tree_nodes_cache.clear()
    _agenda_items_cache.clear()
