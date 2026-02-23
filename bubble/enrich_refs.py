"""
Enrich Bubble Resource and Calendar Item payloads with resolved reference fields.
Uses targets/diff context (org_path, label, url), deterministic heuristics, and optional AI.
Reference fields are set to Bubble object IDs (Data API format). When uncertain, leaves fields empty.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from bubble import lookups
from bubble.client import BubbleAPIError

log = logging.getLogger(__name__)

# Tree names (override via env)
ORGANIZATION_TREE_NAME = os.environ.get("BUBBLE_ORGANIZATION_TREE", "Organization/Publisher")
NAIC_GROUP_TREE_NAME = os.environ.get("BUBBLE_NAIC_GROUP_TREE", "Organization/Publisher")
TYPE1_TREE_NAME = os.environ.get("BUBBLE_TYPE1_TREE", "Organization/Publisher")

# Type1 allowed node names (exact match in tree)
TYPE1_OPTIONS = ("News", "Agenda/Materials", "In the weeds")

# Minimum confidence to apply AI suggestion (0–1)
AI_CONFIDENCE_THRESHOLD = 0.7


# ---------------------------------------------------------------------------
# Pure functions (unit-testable, no I/O)
# ---------------------------------------------------------------------------


def infer_naic_group_path(org_path: list[str] | str | None) -> list[str]:
    """
    Derive a normalized path list from target org_path for NAIC group tree lookup.

    Accepts list (e.g. ["NAIC", "E", "Working Groups"]) or string with " › " separator.
    Returns list of non-empty segments. Does not add or assume "NAIC"; pass-through only.

    Example:
        infer_naic_group_path(["NAIC", "E", "Working Groups"]) -> ["NAIC", "E", "Working Groups"]
        infer_naic_group_path("NAIC › E › Working Groups") -> ["NAIC", "E", "Working Groups"]
    """
    if org_path is None:
        return []
    if isinstance(org_path, str):
        s = (org_path or "").strip()
        if not s:
            return []
        return [p.strip() for p in re.split(r"\s*›\s*", s) if p and p.strip()]
    return [str(p).strip() for p in org_path if p is not None and str(p).strip()]


def classify_resource_type_deterministic(
    title: str = "",
    url: str = "",
    notes: str = "",
) -> str | None:
    """
    Classify resource Type1 from title/url/notes using keywords only.
    Returns one of "News", "Agenda/Materials", "In the weeds", or None if uncertain.

    Heuristics:
    - Agenda/Materials: agenda, materials, minutes, call, webex in title/url/notes.
    - In the weeds: "in the weeds", deep-dive, technical, exposure draft.
    - News: news, update, announcement (and not agenda/materials).
    - Otherwise None.
    """
    text = " ".join([title, url, notes]).lower()
    if not text.strip():
        return None

    if any(
        kw in text
        for kw in ("agenda", "materials", "minutes", "call", "webex", "meeting link")
    ):
        return "Agenda/Materials"
    if any(kw in text for kw in ("in the weeds", "deep-dive", "technical", "exposure draft")):
        return "In the weeds"
    if any(kw in text for kw in ("news", "update", "announcement")):
        return "News"
    return None


def _parse_ai_classification_response(raw: str) -> dict[str, Any] | None:
    """
    Parse AI response to {type1_node_name, topic_node_path, confidence}.
    Returns None if invalid. Pure aside from parsing.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        # Allow wrapped in markdown code block
        if "```" in raw:
            raw = re.sub(r"^.*?```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```.*$", "", raw)
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    conf = data.get("confidence")
    if conf is not None:
        try:
            conf = float(conf)
        except (TypeError, ValueError):
            conf = 0.0
        if not (0 <= conf <= 1):
            conf = 0.0
    else:
        conf = 0.0
    type1 = data.get("type1_node_name")
    if type1 is not None and not isinstance(type1, str):
        type1 = None
    topic_path = data.get("topic_node_path")
    if topic_path is not None and not isinstance(topic_path, list):
        topic_path = None
    if topic_path is not None:
        topic_path = [str(x).strip() for x in topic_path if x is not None and str(x).strip()]
    return {
        "type1_node_name": type1,
        "topic_node_path": topic_path or [],
        "confidence": conf,
    }


def apply_ai_classification(
    resource: dict,
    context: dict,
    *,
    type1_nodes_by_name: dict[str, str],
    topic_tree_id: str | None,
    confidence_threshold: float = AI_CONFIDENCE_THRESHOLD,
    ai_response: dict[str, Any] | None,
    bubble_snapshot: dict | None = None,
) -> tuple[str | None, str | None]:
    """
    Apply AI classification only when confidence >= threshold and suggested nodes exist.
    type1_nodes_by_name: map node display name -> _id for Type1 tree.
    topic_tree_id: tree id for resolving topic_node_path (find_tree_node_by_path).
    ai_response: from request (type1_node_name, topic_node_path, confidence).
    bubble_snapshot: when provided, resolve topic from snapshot instead of lookups.

    Returns (type1_node_id_or_none, topic_node_id_or_none). Either can be None.
    """
    if not ai_response or ai_response.get("confidence", 0) < confidence_threshold:
        return (None, None)
    type1_id = None
    type1_name = ai_response.get("type1_node_name")
    if type1_name and type1_name in type1_nodes_by_name:
        type1_id = type1_nodes_by_name[type1_name]
    topic_id = None
    topic_path = ai_response.get("topic_node_path") or []
    if topic_path and topic_tree_id:
        if bubble_snapshot:
            node = _find_node_by_path_in_snapshot(bubble_snapshot, topic_tree_id, topic_path)
            if node:
                topic_id = node.get("_id") or node.get("id")
        else:
            node = lookups.find_tree_node_by_path(topic_tree_id, topic_path)
            if node:
                topic_id = node.get("_id") or node.get("id")
    return (type1_id, topic_id)


# ---------------------------------------------------------------------------
# Resolvers (use lookups; log, no secrets)
# ---------------------------------------------------------------------------


def _tree_id_from_obj(obj: dict) -> str | None:
    """Get tree id from a tree or tree node object (Tree field may be id string or object)."""
    tid = obj.get("_id") or obj.get("id")
    if isinstance(tid, str):
        return tid
    tree = obj.get("Tree") or obj.get("tree")
    if isinstance(tree, str):
        return tree
    if isinstance(tree, dict):
        return tree.get("_id") or tree.get("id")
    return None


def _resolve_organization_naic_node_from_snapshot(snapshot: dict, tree_name: str) -> str | None:
    """Resolve NAIC node from snapshot (trees + tree_nodes). Returns node _id or None."""
    trees = snapshot.get("trees") or []
    tree = next((t for t in trees if (t.get("Name") or t.get("name") or "").strip() == tree_name), None)
    if not tree:
        return None
    tree_id = _tree_id_from_obj(tree)
    if not tree_id:
        return None
    nodes = snapshot.get("tree_nodes") or []
    for n in nodes:
        if _tree_id_from_obj(n) != tree_id:
            continue
        name = (n.get("Name") or n.get("name") or "").strip()
        if name == "NAIC":
            return n.get("_id") or n.get("id")
    return None


def _find_node_by_path_in_snapshot(snapshot: dict, tree_id: str, path: list[str]) -> dict | None:
    """Find tree node at path in snapshot (same path-walk logic as lookups.find_tree_node_by_path)."""
    if not path:
        return None
    nodes = [n for n in (snapshot.get("tree_nodes") or []) if _tree_id_from_obj(n) == tree_id]
    by_parent: dict[str, list[dict]] = {}
    roots: list[dict] = []
    for n in nodes:
        pid = n.get("Parent") or n.get("parent") or n.get("parent_node")
        if isinstance(pid, dict):
            pid = pid.get("_id") or pid.get("id")
        if not pid:
            roots.append(n)
        else:
            by_parent.setdefault(str(pid), []).append(n)
    current: dict | None = None
    for segment in path:
        seg_lower = (segment or "").strip().lower()
        if not seg_lower:
            continue
        cands = roots if current is None else by_parent.get(current.get("_id") or current.get("id") or "", [])
        found = None
        for c in cands:
            if ((c.get("Name") or c.get("name") or "").strip().lower() == seg_lower):
                found = c
                break
        if found is None:
            return None
        current = found
    return current


def _resolve_naic_group_node_from_snapshot(snapshot: dict, tree_name: str, path: list[str]) -> str | None:
    """Resolve NAIC group node by path from snapshot. Returns node _id or None."""
    if not path:
        return None
    trees = snapshot.get("trees") or []
    tree = next((t for t in trees if (t.get("Name") or t.get("name") or "").strip() == tree_name), None)
    if not tree:
        return None
    tree_id = _tree_id_from_obj(tree)
    if not tree_id:
        return None
    node = _find_node_by_path_in_snapshot(snapshot, tree_id, path)
    if not node:
        return None
    return node.get("_id") or node.get("id")


def _resolve_organization_naic_node(tree_name: str, bubble_snapshot: dict | None = None) -> str | None:
    """Resolve Tree Node 'NAIC' under organization tree. Returns node _id or None."""
    if bubble_snapshot:
        nid = _resolve_organization_naic_node_from_snapshot(bubble_snapshot, tree_name)
        if nid:
            log.info("Resolved Organization node: NAIC -> node_id=%s (snapshot)", nid)
        return nid
    tree = lookups.get_tree_by_name(tree_name)
    if not tree:
        log.warning("Organization tree not found: %s", tree_name)
        return None
    tree_id = tree.get("_id") or tree.get("id")
    node = lookups.find_tree_node_by_path(tree_id, ["NAIC"])
    if not node:
        log.warning("NAIC node not found under tree %s", tree_name)
        return None
    nid = node.get("_id") or node.get("id")
    log.info("Resolved Organization node: NAIC -> node_id=%s", nid)
    return nid


def _resolve_naic_group_node(tree_name: str, path: list[str], bubble_snapshot: dict | None = None) -> str | None:
    """Resolve NAIC group tree node by path. Returns node _id or None."""
    if not path:
        return None
    if bubble_snapshot:
        return _resolve_naic_group_node_from_snapshot(bubble_snapshot, tree_name, path)
    tree = lookups.get_tree_by_name(tree_name)
    if not tree:
        return None
    tree_id = tree.get("_id") or tree.get("id")
    node = lookups.find_tree_node_by_path(tree_id, path)
    if not node:
        return None
    return node.get("_id") or node.get("id")


def _resolve_type1_node_by_name(
    tree_name: str, node_name: str, bubble_snapshot: dict | None = None
) -> str | None:
    """Resolve a single Type1 tree node by exact display name. Returns _id or None."""
    if bubble_snapshot:
        trees = bubble_snapshot.get("trees") or []
        tree = next((t for t in trees if (t.get("Name") or t.get("name") or "").strip() == tree_name), None)
        if not tree:
            return None
        tree_id = _tree_id_from_obj(tree)
        if not tree_id:
            return None
        for n in bubble_snapshot.get("tree_nodes") or []:
            if _tree_id_from_obj(n) != tree_id:
                continue
            if (n.get("Name") or n.get("name") or "").strip() == node_name:
                return n.get("_id") or n.get("id")
        return None
    tree = lookups.get_tree_by_name(tree_name)
    if not tree:
        return None
    tree_id = tree.get("_id") or tree.get("id")
    nodes = lookups.get_tree_nodes_in_tree(tree_id)
    for n in nodes:
        name = (n.get("Name") or n.get("name") or "").strip()
        if name == node_name:
            return n.get("_id") or n.get("id")
    return None


def _build_type1_nodes_by_name(tree_name: str, bubble_snapshot: dict | None = None) -> dict[str, str]:
    """Build map node_name -> _id for Type1 options. Only includes names that exist."""
    out: dict[str, str] = {}
    for name in TYPE1_OPTIONS:
        nid = _resolve_type1_node_by_name(tree_name, name, bubble_snapshot)
        if nid:
            out[name] = nid
    return out


def _match_calendar_item_from_snapshot(
    snapshot: dict, title: str, notes: str, tolerance_days: int = 7
) -> str | None:
    """Match a calendar item from snapshot by title and optional date. Returns _id or None."""
    title = (title or "").strip().lower()
    if not title:
        return None
    date_iso = None
    for m in re.finditer(r"(\d{4})[-/](\d{2})[-/](\d{2})", (notes or "") + " " + (title or "")):
        date_iso = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        break
    if not date_iso:
        for m in re.finditer(
            r"(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2}),?\s+(\d{4})",
            ((notes or "") + " " + (title or "")).lower(),
            re.I,
        ):
            month_map = {
                "january": "01", "february": "02", "march": "03", "april": "04",
                "may": "05", "june": "06", "july": "07", "august": "08",
                "september": "09", "october": "10", "november": "11", "december": "12",
            }
            date_iso = f"{m.group(3)}-{month_map.get(m.group(1).lower(), '01')}-{m.group(2).zfill(2)}"
            break
    items = snapshot.get("calendar_items") or []
    for c in items:
        ct = (c.get("title") or "").strip().lower()
        if title not in ct and ct not in title:
            continue
        cd = c.get("date")
        if date_iso and cd:
            if isinstance(cd, str) and date_iso in cd:
                pass
            else:
                continue
        cid = c.get("_id") or c.get("id")
        if cid:
            return cid
    for c in items:
        ct = (c.get("title") or "").strip().lower()
        if title in ct or ct in title:
            return c.get("_id") or c.get("id")
    return None


def _match_calendar_item_for_resource(
    resource_title: str,
    resource_notes: str,
    resource_context: dict,
    calendar_payload: list[dict],
    calendar_context: list[dict],
    tolerance_days: int = 7,
    bubble_snapshot: dict | None = None,
) -> str | None:
    """
    Try to match an existing Bubble calendar item by title + date (from notes or context).
    Returns calendar item _id if found, else None. For new items not yet in Bubble,
    we do not create; caller may link in-payload by index after create.
    """
    if bubble_snapshot:
        cid = _match_calendar_item_from_snapshot(
            bubble_snapshot, resource_title, resource_notes, tolerance_days
        )
        if cid:
            log.info(
                "Calendar link: matched meeting id=%s (title match + date tolerance, snapshot)",
                cid,
            )
        return cid
    title = (resource_title or "").strip()
    notes = (resource_notes or "").strip()
    date_iso = None
    for m in re.finditer(r"(\d{4})[-/](\d{2})[-/](\d{2})", notes + " " + title):
        date_iso = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        break
    if not date_iso:
        for m in re.finditer(
            r"(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2}),?\s+(\d{4})",
            (notes + " " + title).lower(),
            re.I,
        ):
            month_map = {
                "january": "01", "february": "02", "march": "03", "april": "04",
                "may": "05", "june": "06", "july": "07", "august": "08",
                "september": "09", "october": "10", "november": "11", "december": "12",
            }
            date_iso = f"{m.group(3)}-{month_map.get(m.group(1).lower(), '01')}-{m.group(2).zfill(2)}"
            break
    existing = lookups.find_calendar_item_by_title_date(
        title, start_dt_iso=date_iso, tolerance_days=tolerance_days
    )
    if existing:
        cid = existing.get("_id") or existing.get("id")
        log.info(
            "Calendar link: matched meeting id=%s (title match + date tolerance)",
            cid,
        )
        return cid
    return None


# ---------------------------------------------------------------------------
# AI request (optional)
# ---------------------------------------------------------------------------


def request_ai_classification(
    resource: dict,
    context: dict,
    *,
    openai_api_key: str | None = None,
) -> dict[str, Any] | None:
    """
    Call OpenAI to get type1_node_name, topic_node_path, confidence.
    Returns parsed dict or None on failure. No secrets in logs.
    """
    api_key = (openai_api_key or os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        log.debug("OPENAI_API_KEY not set, skipping AI classification")
        return None
    try:
        import openai
        client = openai.OpenAI(api_key=api_key)
        title = (resource.get("Name") or resource.get("title") or "").strip()
        url = (resource.get("URL") or "").strip()
        notes = (resource.get("notes") or "").strip()
        org_path = context.get("org_path") or []
        label = (context.get("label") or "").strip()
        prompt = f"""Classify this resource for Bubble.
Resource: title="{title}", url="{url[:80]}...", notes="{notes[:200]}..."
Context: org_path={org_path}, label="{label}"

Respond with exactly one JSON object (no markdown):
- "type1_node_name": one of "News", "Agenda/Materials", "In the weeds" or null if unsure
- "topic_node_path": list of path segments for a topic tree node (e.g. ["NAIC", "E", "Topic"]) or []
- "confidence": number 0.0 to 1.0

Only output valid JSON."""
        resp = client.chat.completions.create(
            model=os.environ.get("OPENAI_CLASSIFICATION_MODEL", "gpt-4o-mini"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        content = (resp.choices[0].message.content or "").strip()
        return _parse_ai_classification_response(content)
    except Exception as e:
        log.warning("AI classification request failed: %s", type(e).__name__)
        return None


# ---------------------------------------------------------------------------
# Main enrichment
# ---------------------------------------------------------------------------


def enrich_refs(
    resources_payload: list[dict],
    calendar_payload: list[dict],
    resource_context: list[dict],
    calendar_context: list[dict],
    *,
    organization_tree_name: str = ORGANIZATION_TREE_NAME,
    naic_group_tree_name: str = NAIC_GROUP_TREE_NAME,
    type1_tree_name: str = TYPE1_TREE_NAME,
    use_ai: bool = False,
    calendar_link_tolerance_days: int = 7,
    bubble_snapshot: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    Return (updated_resources, updated_calendar) with reference fields set to Bubble IDs.
    context lists must align 1:1 with payloads (same length and order).
    When uncertain, reference fields are left empty ([] or None).
    bubble_snapshot: when provided, use snapshot for resolution instead of live lookups (E2E).
    """
    updated_resources = [dict(r) for r in resources_payload]
    updated_calendar = [dict(c) for c in calendar_payload]

    # Resolve Organization: NAIC node (single node in list)
    org_naic_id = _resolve_organization_naic_node(organization_tree_name, bubble_snapshot)
    if org_naic_id:
        for r in updated_resources:
            r["Organization"] = [org_naic_id]

    # Resolve NAIC group tree node per calendar from context (Calendar has "NAIC Group (tree node)")
    for i, ctx in enumerate(calendar_context):
        if i >= len(updated_calendar):
            break
        path = infer_naic_group_path(ctx.get("org_path"))
        label = (ctx.get("label") or "").strip()
        if label:
            path = path + [label]
        if not path:
            continue
        nid = _resolve_naic_group_node(naic_group_tree_name, path, bubble_snapshot)
        if nid:
            updated_calendar[i]["NAIC Group (tree node)"] = nid

    # Type1: deterministic first, then optional AI override
    type1_by_name = _build_type1_nodes_by_name(type1_tree_name, bubble_snapshot)
    naic_group_tree_id = None
    if bubble_snapshot:
        trees = bubble_snapshot.get("trees") or []
        tree = next((t for t in trees if (t.get("Name") or t.get("name") or "").strip() == naic_group_tree_name), None)
        if tree:
            naic_group_tree_id = _tree_id_from_obj(tree)
    else:
        tree = lookups.get_tree_by_name(naic_group_tree_name)
        if tree:
            naic_group_tree_id = tree.get("_id") or tree.get("id")

    for i, r in enumerate(updated_resources):
        ctx = resource_context[i] if i < len(resource_context) else {}
        title = (r.get("Name") or "").strip()
        url = (r.get("URL") or "").strip()
        notes = (r.get("notes") or "").strip()
        type1_name = classify_resource_type_deterministic(title, url, notes)
        type1_id = None
        if type1_name and type1_name in type1_by_name:
            type1_id = type1_by_name[type1_name]
            r["Type1"] = [type1_id] if type1_id else []
            log.info("Type1: %s (deterministic) -> node_id=%s", type1_name, type1_id)
        else:
            r["Type1"] = []

        if use_ai:
            ai_resp = request_ai_classification(r, ctx)
            if ai_resp:
                ai_type1_id, topic_id = apply_ai_classification(
                    r, ctx,
                    type1_nodes_by_name=type1_by_name,
                    topic_tree_id=naic_group_tree_id,
                    ai_response=ai_resp,
                    bubble_snapshot=bubble_snapshot,
                )
                if ai_type1_id:
                    r["Type1"] = [ai_type1_id]
                    log.info(
                        "Type1: %s (AI, confidence %.2f) -> node_id=%s",
                        ai_resp.get("type1_node_name"),
                        ai_resp.get("confidence", 0),
                        ai_type1_id,
                    )
                if topic_id:
                    r["topic suggestion"] = topic_id
                    log.info(
                        "topic suggestion (AI, confidence %.2f) -> node_id=%s",
                        ai_resp.get("confidence", 0),
                        topic_id,
                    )

    # Related calendar items: match existing Bubble calendar by title + date
    for i, r in enumerate(updated_resources):
        ctx = resource_context[i] if i < len(resource_context) else {}
        title = (r.get("Name") or "").strip()
        notes = (r.get("notes") or "").strip()
        cid = _match_calendar_item_for_resource(
            title,
            notes,
            ctx,
            calendar_payload,
            calendar_context,
            tolerance_days=calendar_link_tolerance_days,
            bubble_snapshot=bubble_snapshot,
        )
        if cid:
            r["Related calendar items"] = r.get("Related calendar items") or []
            if cid not in r["Related calendar items"]:
                r["Related calendar items"] = r["Related calendar items"] + [cid]

    return (updated_resources, updated_calendar)
