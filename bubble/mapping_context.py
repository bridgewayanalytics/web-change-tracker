"""
Extract compact candidate lists from a Bubble snapshot for AI mapping context.
Used when snapshot is available to give the model real tree nodes and calendar items.
"""

from __future__ import annotations

import os
from typing import Any

# Tree names (match enrich_refs / lookups)
ORGANIZATION_TREE_NAME = os.environ.get("BUBBLE_ORGANIZATION_TREE", "Organization/Publisher")
NAIC_GROUP_TREE_NAME = os.environ.get("BUBBLE_NAIC_GROUP_TREE", "Organization/Publisher")
TYPE1_TREE_NAME = os.environ.get("BUBBLE_TYPE1_TREE", "Organization/Publisher")

# Cap list sizes for token bounds; prefer NAIC-related
CANDIDATE_LIST_LIMIT = 200
TYPE1_OPTIONS = ("News", "Agenda/Materials", "In the weeds", "Agenda & Materials")


def _tree_id_from_obj(obj: dict) -> str | None:
    """Tree id from tree or tree node (Tree field may be id string or object)."""
    tid = obj.get("_id") or obj.get("id")
    if isinstance(tid, str):
        return tid
    tree = obj.get("Tree") or obj.get("tree")
    if isinstance(tree, str):
        return tree
    if isinstance(tree, dict):
        return tree.get("_id") or tree.get("id")
    return None


def _node_name(n: dict) -> str:
    return (n.get("Name") or n.get("name") or "").strip()


def _node_id(n: dict) -> str | None:
    return n.get("_id") or n.get("id")


def _build_paths_for_tree(snapshot: dict, tree_id: str) -> list[tuple[dict, list[str]]]:
    """Return list of (node, path_segments) for all nodes in tree. Path from root to node."""
    nodes = [n for n in (snapshot.get("tree_nodes") or []) if _tree_id_from_obj(n) == tree_id]
    if not nodes:
        return []
    by_id: dict[str, dict] = {}
    for n in nodes:
        nid = _node_id(n)
        if nid:
            by_id[str(nid)] = n
    # Parent can be id string or object
    def parent_id(node: dict) -> str | None:
        p = node.get("Parent") or node.get("parent") or node.get("parent_node")
        if p is None:
            return None
        if isinstance(p, str):
            return p
        if isinstance(p, dict):
            return p.get("_id") or p.get("id")
        return None

    result: list[tuple[dict, list[str]]] = []
    for n in nodes:
        segs: list[str] = []
        current: dict | None = n
        while current:
            name = _node_name(current)
            segs.append(name or "?")
            pid = parent_id(current) if current else None
            current = by_id.get(str(pid)) if pid else None
        segs.reverse()
        result.append((n, segs))
    return result


def _path_str(segs: list[str]) -> str:
    return " › ".join(segs) if segs else ""


def extract_organization_tree_nodes(snapshot: dict) -> list[dict[str, Any]]:
    """
    Tree nodes under Organization/Publisher tree.
    Returns list of {id, name, path} (path = " › ".join from root to node).
    Capped at CANDIDATE_LIST_LIMIT; NAIC-related nodes preferred.
    """
    trees = snapshot.get("trees") or []
    tree = next((t for t in trees if (t.get("Name") or t.get("name") or "").strip() == ORGANIZATION_TREE_NAME), None)
    if not tree:
        return []
    tree_id = _tree_id_from_obj(tree)
    if not tree_id:
        return []
    node_paths = _build_paths_for_tree(snapshot, tree_id)
    out: list[dict[str, Any]] = []
    for n, segs in node_paths:
        nid = _node_id(n)
        if not nid:
            continue
        path = _path_str(segs)
        name = _node_name(n)
        out.append({"id": nid, "name": name, "path": path})
    # Prefer NAIC-related (path or name contains NAIC)
    def naic_score(item: dict) -> int:
        p = (item.get("path") or "").lower()
        nm = (item.get("name") or "").lower()
        if "naic" in p or "naic" in nm:
            return 1
        return 0
    out.sort(key=lambda x: (-naic_score(x), x.get("path") or ""))
    return out[:CANDIDATE_LIST_LIMIT]


def extract_naic_group_tree_nodes(snapshot: dict) -> list[dict[str, Any]]:
    """
    Tree nodes under NAIC (e.g. NAIC › Financial Condition (E) Committee › ...).
    Same tree as organization; filter to nodes whose path starts with "NAIC".
    Returns list of {id, name, path}. Capped at CANDIDATE_LIST_LIMIT.
    """
    trees = snapshot.get("trees") or []
    tree = next((t for t in trees if (t.get("Name") or t.get("name") or "").strip() == NAIC_GROUP_TREE_NAME), None)
    if not tree:
        return []
    tree_id = _tree_id_from_obj(tree)
    if not tree_id:
        return []
    node_paths = _build_paths_for_tree(snapshot, tree_id)
    out: list[dict[str, Any]] = []
    for n, segs in node_paths:
        path = _path_str(segs)
        if not path.strip().lower().startswith("naic"):
            continue
        nid = _node_id(n)
        if not nid:
            continue
        name = _node_name(n)
        out.append({"id": nid, "name": name, "path": path})
    out.sort(key=lambda x: (x.get("path") or "", x.get("name") or ""))
    return out[:CANDIDATE_LIST_LIMIT]


def extract_resource_type_tree_nodes(snapshot: dict) -> list[dict[str, Any]]:
    """
    Resource Type / Type1 tree nodes (News, Agenda & Materials, In the weeds, etc.).
    Returns list of {id, name, path}. Prefer names in TYPE1_OPTIONS. Capped at CANDIDATE_LIST_LIMIT.
    """
    trees = snapshot.get("trees") or []
    tree = next((t for t in trees if (t.get("Name") or t.get("name") or "").strip() == TYPE1_TREE_NAME), None)
    if not tree:
        return []
    tree_id = _tree_id_from_obj(tree)
    if not tree_id:
        return []
    node_paths = _build_paths_for_tree(snapshot, tree_id)
    # Prefer nodes whose name matches Type1 options
    type1_lower = {s.lower() for s in TYPE1_OPTIONS}
    out: list[dict[str, Any]] = []
    for n, segs in node_paths:
        nid = _node_id(n)
        if not nid:
            continue
        name = _node_name(n)
        path = _path_str(segs)
        out.append({"id": nid, "name": name, "path": path})
    def type1_score(item: dict) -> int:
        nm = (item.get("name") or "").lower()
        if any(t in nm or nm in t for t in type1_lower):
            return 1
        return 0
    out.sort(key=lambda x: (-type1_score(x), x.get("path") or ""))
    return out[:CANDIDATE_LIST_LIMIT]


def extract_recent_calendar_items(snapshot: dict) -> list[dict[str, Any]]:
    """
    Existing calendar items: id, title, date, naic_group (name or id if present).
    Returns list of {id, title, date, naic_group}. Capped at CANDIDATE_LIST_LIMIT; prefer NAIC-related.
    """
    items = snapshot.get("calendar_items") or []
    out: list[dict[str, Any]] = []
    for c in items:
        cid = c.get("_id") or c.get("id")
        if not cid:
            continue
        title = (c.get("title") or "").strip()
        date_val = c.get("date")
        naic_group = c.get("NAIC Group (tree node)")
        if isinstance(naic_group, dict):
            naic_group = naic_group.get("Name") or naic_group.get("name") or naic_group.get("_id") or naic_group.get("id")
        out.append({
            "id": cid,
            "title": title,
            "date": date_val,
            "naic_group": naic_group,
        })
    def naic_score(item: dict) -> int:
        ng = str(item.get("naic_group") or "").lower()
        ti = (item.get("title") or "").lower()
        if "naic" in ng or "naic" in ti:
            return 1
        return 0
    out.sort(key=lambda x: (-naic_score(x), (x.get("date") or ""), x.get("title") or ""))
    return out[:CANDIDATE_LIST_LIMIT]


def build_mapping_context(snapshot: dict) -> dict[str, Any]:
    """
    Build full mapping context for AI: organization nodes, NAIC group nodes, resource type nodes, calendar items.
    All lists truncated to CANDIDATE_LIST_LIMIT with NAIC/relevance preferred.
    """
    return {
        "organization_tree_nodes": extract_organization_tree_nodes(snapshot),
        "naic_group_tree_nodes": extract_naic_group_tree_nodes(snapshot),
        "resource_type_tree_nodes": extract_resource_type_tree_nodes(snapshot),
        "recent_calendar_items": extract_recent_calendar_items(snapshot),
    }
