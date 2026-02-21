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

# In-module caches (keyed by cache key string)
_tree_cache: dict[str, dict] = {}
_tree_nodes_cache: dict[str, list[dict]] = {}


def _client() -> "BubbleClient":
    """Lazy singleton client with cache enabled for read-heavy lookups."""
    return get_client(use_cache=True)


def _tree_node_name(node: dict) -> str:
    """Display name of a tree node (Bubble may use 'Name' or 'name')."""
    return (node.get("Name") or node.get("name") or "").strip()


def _tree_node_id(node: dict) -> str | None:
    """Bubble unique id of a tree node."""
    return node.get("_id") or node.get("id")


def _tree_node_parent(node: dict) -> str | None:
    """Parent tree node id (reference)."""
    parent = node.get("Parent") or node.get("parent")
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
            print(n.get("Name"), n.get("_id"))
    """
    if tree_id in _tree_nodes_cache:
        return _tree_nodes_cache[tree_id]
    try:
        client = _client()
        nodes = list(
            client.list_all(
                TYPE_TREE_NODE,
                constraints=[{"key": "Tree", "constraint_type": "equals", "value": tree_id}],
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
    by_id = {_tree_node_id(n): n for n in nodes if _tree_node_id(n)}
    by_parent: dict[str, list[dict]] = {}
    roots: list[dict] = []
    for n in nodes:
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
        if q in _tree_node_name(n).lower():
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
    """Clear in-module caches for trees and tree nodes (e.g. between runs or after writes)."""
    global _tree_cache, _tree_nodes_cache
    _tree_cache.clear()
    _tree_nodes_cache.clear()
