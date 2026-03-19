"""
Extract compact candidate lists from a Bubble snapshot for AI mapping context.
Used when snapshot is available to give the model real tree nodes and calendar items.
"""

from __future__ import annotations

from typing import Any

from bubble.enrich_refs import (
    ORGANIZATION_TREE_NAME,
    NAIC_GROUP_TREE_NAME,
    TYPE1_TREE_NAME,
    TOPIC_TREE_NAME,
    TYPE1_OPTIONS,
)

CANDIDATE_LIST_LIMIT = 200


def _node_tree_id(obj: dict) -> str | None:
    """The tree that a tree-node belongs to (parent_tree field)."""
    tree = obj.get("parent_tree") or obj.get("Tree") or obj.get("tree")
    if isinstance(tree, str):
        return tree
    if isinstance(tree, dict):
        return tree.get("_id") or tree.get("id")
    return None


def _obj_id(obj: dict) -> str | None:
    """The object's own _id."""
    return obj.get("_id") or obj.get("id")


def _node_name(n: dict) -> str:
    """Display name (live API uses lowercase 'name')."""
    return (n.get("name") or n.get("Name") or "").strip()


def _build_paths_for_tree(snapshot: dict, tree_id: str) -> list[tuple[dict, list[str]]]:
    """Return list of (node, path_segments) for named nodes in tree. Path from root to node."""
    all_nodes = [n for n in (snapshot.get("tree_nodes") or []) if _node_tree_id(n) == tree_id]
    nodes = [n for n in all_nodes if _node_name(n)]
    if not nodes:
        return []
    by_id: dict[str, dict] = {}
    for n in nodes:
        nid = _obj_id(n)
        if nid:
            by_id[str(nid)] = n

    def parent_id(node: dict) -> str | None:
        p = node.get("parent") or node.get("Parent") or node.get("parent_node")
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
    Tree nodes under Organization tree.
    Returns list of {id, name, path} (path = " › ".join from root to node).
    Capped at CANDIDATE_LIST_LIMIT; NAIC-related nodes preferred.
    """
    trees = snapshot.get("trees") or []
    tree = next((t for t in trees if (t.get("Name") or t.get("name") or "").strip() == ORGANIZATION_TREE_NAME), None)
    if not tree:
        return []
    tree_id = _obj_id(tree)
    if not tree_id:
        return []
    node_paths = _build_paths_for_tree(snapshot, tree_id)
    out: list[dict[str, Any]] = []
    for n, segs in node_paths:
        nid = _obj_id(n)
        if not nid:
            continue
        path = _path_str(segs)
        name = _node_name(n)
        out.append({"id": nid, "name": name, "path": path})
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
    tree_id = _obj_id(tree)
    if not tree_id:
        return []
    node_paths = _build_paths_for_tree(snapshot, tree_id)
    out: list[dict[str, Any]] = []
    for n, segs in node_paths:
        path = _path_str(segs)
        if not path.strip().lower().startswith("naic"):
            continue
        nid = _obj_id(n)
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
    tree_id = _obj_id(tree)
    if not tree_id:
        return []
    node_paths = _build_paths_for_tree(snapshot, tree_id)
    type1_lower = {s.lower() for s in TYPE1_OPTIONS}
    out: list[dict[str, Any]] = []
    for n, segs in node_paths:
        nid = _obj_id(n)
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


def extract_topic_tree_nodes(snapshot: dict) -> list[dict[str, Any]]:
    """
    Topic / Chronicles tree nodes.
    Returns list of {id, name, path}. Capped at CANDIDATE_LIST_LIMIT.
    """
    trees = snapshot.get("trees") or []
    tree = next((t for t in trees if (t.get("Name") or t.get("name") or "").strip() == TOPIC_TREE_NAME), None)
    if not tree:
        return []
    tree_id = _obj_id(tree)
    if not tree_id:
        return []
    node_paths = _build_paths_for_tree(snapshot, tree_id)
    out: list[dict[str, Any]] = []
    for n, segs in node_paths:
        nid = _obj_id(n)
        if not nid:
            continue
        name = _node_name(n)
        path = _path_str(segs)
        out.append({"id": nid, "name": name, "path": path})
    out.sort(key=lambda x: x.get("path") or "")
    return out[:CANDIDATE_LIST_LIMIT]


def extract_agenda_items(snapshot: dict) -> list[dict[str, Any]]:
    """
    Agenda Items from snapshot: id, ba_title, naic_title, ref_num, topics, discussed_at.
    Returns list of {id, ba_title, naic_title, ref_num, topics, discussed_at}.
    Capped at CANDIDATE_LIST_LIMIT.
    """
    items = snapshot.get("agenda_items") or []
    out: list[dict[str, Any]] = []
    for a in items:
        aid = a.get("_id") or a.get("id")
        if not aid:
            continue
        ba_title = (a.get("BA title") or "").strip()
        naic_title = (a.get("NAIC Title") or "").strip()
        ref_num = (a.get("BA Ref #") or a.get("Ref #") or "").strip()
        topics = a.get("Topics") or []
        discussed_at = a.get("Discussed at list") or a.get("Discussed at")
        out.append({
            "id": aid,
            "ba_title": ba_title,
            "naic_title": naic_title,
            "ref_num": ref_num,
            "topics": topics,
            "discussed_at": discussed_at,
        })
    return out[:CANDIDATE_LIST_LIMIT]


def build_mapping_context(snapshot: dict) -> dict[str, Any]:
    """
    Build full mapping context for AI: organization nodes, NAIC group nodes,
    resource type nodes, topic nodes, calendar items, agenda items.
    All lists truncated to CANDIDATE_LIST_LIMIT with NAIC/relevance preferred.
    """
    return {
        "organization_tree_nodes": extract_organization_tree_nodes(snapshot),
        "naic_group_tree_nodes": extract_naic_group_tree_nodes(snapshot),
        "resource_type_tree_nodes": extract_resource_type_tree_nodes(snapshot),
        "topic_tree_nodes": extract_topic_tree_nodes(snapshot),
        "recent_calendar_items": extract_recent_calendar_items(snapshot),
        "agenda_items": extract_agenda_items(snapshot),
    }
