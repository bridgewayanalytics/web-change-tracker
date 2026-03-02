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
from bubble.reference_resolution import record_resolution

log = logging.getLogger(__name__)

# Tree names (override via env)
ORGANIZATION_TREE_NAME = os.environ.get("BUBBLE_ORGANIZATION_TREE", "Organization")
NAIC_GROUP_TREE_NAME = os.environ.get("BUBBLE_NAIC_GROUP_TREE", "Organization")
TYPE1_TREE_NAME = os.environ.get("BUBBLE_TYPE1_TREE", "Resources Types")
TOPIC_TREE_NAME = os.environ.get("BUBBLE_TOPIC_TREE", "Chronicles")

# Type1 node names that exist in the "Resources Types" tree (from live API).
# Override via BUBBLE_TYPE1_OPTIONS (comma-separated).
_DEFAULT_TYPE1_OPTIONS = (
    "Resource",
    "Existing Requirements & Guidance",
    "Publication",
    "Proposed Guidance & Support Materials",
    "Agenda & Materials",
    "Newsreel",
    "In the Weeds",
    "Other",
    "Web Repository",
    "Podcasts & Webinars",
)
_raw_type1 = os.environ.get("BUBBLE_TYPE1_OPTIONS", "").strip()
TYPE1_OPTIONS: tuple[str, ...] = tuple(
    s.strip() for s in _raw_type1.split(",") if s.strip()
) if _raw_type1 else _DEFAULT_TYPE1_OPTIONS

# Fallback node name when no specific rule matches. Must exist in the tree.
TYPE1_FALLBACK = os.environ.get("BUBBLE_TYPE1_FALLBACK", "Other")

# Section type → Type1 node name. Deterministic mapping from the scrape section.
# Override via BUBBLE_SECTION_TYPE1_MAP (format: "docs=Publication,event_links=Agenda & Materials,...").
_DEFAULT_SECTION_TYPE1_MAP: dict[str, str] = {
    "docs": "Publication",
    "event_links": "Agenda & Materials",
    "events": "Agenda & Materials",
}
_raw_section_map = os.environ.get("BUBBLE_SECTION_TYPE1_MAP", "").strip()
SECTION_TYPE1_MAP: dict[str, str] = dict(
    (pair.split("=", 1)[0].strip(), pair.split("=", 1)[1].strip())
    for pair in _raw_section_map.split(",")
    if "=" in pair
) if _raw_section_map else _DEFAULT_SECTION_TYPE1_MAP

# Minimum confidence to apply AI suggestion (0–1)
AI_CONFIDENCE_THRESHOLD = 0.7

# Calendar linking when resource has __meeting_meta: date window ±N days (configurable)
CALENDAR_LINK_MEETING_META_WINDOW_DAYS = int(os.environ.get("CALENDAR_LINK_MEETING_META_WINDOW_DAYS", "2"))

# Calendar linking via NAIC group tree node: date window ±N days (configurable)
CALENDAR_NAIC_GROUP_WINDOW_DAYS = int(os.environ.get("CALENDAR_NAIC_GROUP_WINDOW_DAYS", "7"))
# When no date is available, cap the number of upcoming items returned per group
CALENDAR_NAIC_GROUP_NO_DATE_CAP = int(os.environ.get("CALENDAR_NAIC_GROUP_NO_DATE_CAP", "3"))


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
    section_type: str = "",
) -> str:
    """
    Classify resource Type1 deterministically.

    1. If section_type maps via SECTION_TYPE1_MAP, use that.
    2. Else use keyword heuristics on title/url/notes.
    3. Falls back to TYPE1_FALLBACK ("Other") so every resource gets a Type1.

    Returns a node name string (never None).
    """
    if section_type and section_type in SECTION_TYPE1_MAP:
        return SECTION_TYPE1_MAP[section_type]

    text = " ".join([title, url, notes]).lower()
    if not text.strip():
        return TYPE1_FALLBACK

    if any(kw in text for kw in (
        "agenda", "materials", "minutes", "call", "webex", "meeting link",
    )):
        return SECTION_TYPE1_MAP.get("event_links", "Agenda & Materials")
    if any(kw in text for kw in ("in the weeds", "deep-dive", "technical", "exposure draft")):
        return "In the Weeds"
    if any(kw in text for kw in ("news", "update", "announcement", "newsreel")):
        return "Newsreel"
    if any(kw in text for kw in ("podcast", "webinar")):
        return "Podcasts & Webinars"
    if any(kw in text for kw in ("proposed", "draft", "proposal")):
        return "Proposed Guidance & Support Materials"
    if any(kw in text for kw in ("guidance", "guideline", "requirement", "regulation")):
        return "Existing Requirements & Guidance"

    return TYPE1_FALLBACK


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


def _obj_id(obj: dict) -> str | None:
    """The object's own _id."""
    return obj.get("_id") or obj.get("id")


def _node_tree_id(node: dict) -> str | None:
    """The tree that a tree-node belongs to (parent_tree field)."""
    tree = node.get("parent_tree") or node.get("Tree") or node.get("tree")
    if isinstance(tree, str):
        return tree
    if isinstance(tree, dict):
        return tree.get("_id") or tree.get("id")
    return None


def _node_name(n: dict) -> str:
    """Display name of a tree node. Live API returns lowercase 'name'."""
    return (n.get("name") or n.get("Name") or "").strip()


def _resolve_organization_naic_node_from_snapshot(
    snapshot: dict, tree_name: str,
) -> tuple[str | None, dict]:
    """Resolve NAIC node from snapshot using normalized name matching.

    Returns (node_id, evidence).
    """
    evidence: dict = {"source": "snapshot", "tree_name": tree_name}
    trees = snapshot.get("trees") or []
    tree = next((t for t in trees if (t.get("Name") or t.get("name") or "").strip() == tree_name), None)
    if not tree:
        evidence["failure"] = "tree_not_found"
        evidence["available_trees"] = [
            (t.get("Name") or t.get("name") or "").strip() for t in trees
        ][:15]
        return None, evidence
    tree_id = _obj_id(tree)
    if not tree_id:
        evidence["failure"] = "tree_has_no_id"
        return None, evidence
    evidence["tree_id"] = tree_id
    nodes = [n for n in (snapshot.get("tree_nodes") or []) if _node_tree_id(n) == tree_id]
    node_names = [_node_name(n) for n in nodes if _node_name(n)]
    evidence["node_count"] = len(nodes)
    evidence["node_names_sample"] = node_names[:20]

    target_norm = _normalize_for_matching("NAIC")
    matches: list[tuple[str, str]] = []
    for n in nodes:
        raw = _node_name(n)
        nid = _obj_id(n)
        if not raw or not nid:
            continue
        if _normalize_for_matching(raw) == target_norm:
            matches.append((raw, str(nid)))

    evidence["naic_candidates"] = matches
    if len(matches) == 1:
        raw_name, nid = matches[0]
        evidence["resolved_id"] = nid
        evidence["resolved_name"] = raw_name
        return nid, evidence
    if len(matches) > 1:
        evidence["failure"] = "ambiguous_naic_matches"
        return None, evidence
    evidence["failure"] = "naic_node_not_found"
    return None, evidence


def _find_node_by_path_in_snapshot(snapshot: dict, tree_id: str, path: list[str]) -> dict | None:
    """Find tree node at path in snapshot (same path-walk logic as lookups.find_tree_node_by_path)."""
    if not path:
        return None
    all_nodes = [n for n in (snapshot.get("tree_nodes") or []) if _node_tree_id(n) == tree_id]
    nodes = [n for n in all_nodes if _node_name(n)]
    by_parent: dict[str, list[dict]] = {}
    roots: list[dict] = []
    for n in nodes:
        pid = n.get("parent") or n.get("Parent") or n.get("parent_node")
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
        cands = roots if current is None else by_parent.get(_obj_id(current) or "", [])
        found = None
        for c in cands:
            if _node_name(c).lower() == seg_lower:
                found = c
                break
        if found is None:
            return None
        current = found
    return current


_PARENS_RE = re.compile(r"\s*\([^)]*\)\s*")
_PUNCT_RE = re.compile(r"[^\w\s]")


def _normalize_for_matching(name: str) -> str:
    """Normalize a node/label name for fuzzy matching.

    Lowercase, strip parenthesised codes like (E), replace hyphens with spaces
    (so "Risk-Based" and "Risk Based" both become "risk based"),
    remove remaining punctuation, collapse whitespace.
    """
    s = _PARENS_RE.sub(" ", name)
    s = s.replace("-", " ")
    s = _PUNCT_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def _build_naic_group_node_map(
    tree_name: str, bubble_snapshot: dict | None = None,
) -> dict[str, list[tuple[str, str]]]:
    """Load all nodes in the Organization tree and build a normalized-name → [(raw_name, node_id)] map."""
    if bubble_snapshot:
        trees = bubble_snapshot.get("trees") or []
        tree = next((t for t in trees if (t.get("Name") or t.get("name") or "").strip() == tree_name), None)
        if not tree:
            return {}
        tree_id = _obj_id(tree)
        if not tree_id:
            return {}
        nodes = [n for n in (bubble_snapshot.get("tree_nodes") or []) if _node_tree_id(n) == tree_id]
    else:
        tree = lookups.get_tree_by_name(tree_name)
        if not tree:
            return {}
        tree_id = tree.get("_id") or tree.get("id")
        nodes = lookups.get_tree_nodes_in_tree(tree_id)

    result: dict[str, list[tuple[str, str]]] = {}
    for n in nodes:
        raw = _node_name(n)
        nid = _obj_id(n)
        if not raw or not nid:
            continue
        key = _normalize_for_matching(raw)
        if key:
            result.setdefault(key, []).append((raw, str(nid)))
    return result


def _resolve_naic_group_node(
    tree_name: str, path: list[str], bubble_snapshot: dict | None = None,
) -> tuple[str | None, dict]:
    """Resolve NAIC group tree node by normalized label matching.

    Uses the last segment of *path* (the target label) and matches it against all
    Organization tree node names using normalized comparison (strip parenthesised codes,
    punctuation, case-insensitive).

    Returns (node_id, evidence).
    """
    evidence: dict = {"tree_name": tree_name, "path": path}
    if not path:
        evidence["failure"] = "empty_path"
        return None, evidence

    label = path[-1]
    evidence["original_label"] = label
    norm_label = _normalize_for_matching(label)
    evidence["normalized_label"] = norm_label
    if not norm_label:
        evidence["failure"] = "empty_normalized_label"
        return None, evidence

    node_map = _build_naic_group_node_map(tree_name, bubble_snapshot)
    if not node_map:
        evidence["failure"] = "no_nodes_loaded"
        return None, evidence
    evidence["total_nodes"] = sum(len(v) for v in node_map.values())

    # Exact normalized match
    exact = node_map.get(norm_label)
    if exact:
        raw_name, nid = exact[0]
        evidence["match_type"] = "exact_normalized"
        evidence["candidate_matches"] = [(raw, nid_) for raw, nid_ in exact]
        evidence["chosen_raw_name"] = raw_name
        evidence["chosen_node_id"] = nid
        log.info("NAIC group resolved: '%s' -> '%s' (id=%s) [exact]", label, raw_name, nid)
        return nid, evidence

    # Substring fallback: normalized label is contained in a node's normalized name (or vice versa)
    substring_matches: list[tuple[str, str, str]] = []
    for norm_key, entries in node_map.items():
        if norm_label in norm_key or norm_key in norm_label:
            for raw, nid in entries:
                substring_matches.append((raw, nid, norm_key))

    evidence["candidate_matches"] = [(raw, nid) for raw, nid, _ in substring_matches]

    if len(substring_matches) == 1:
        raw_name, nid, _ = substring_matches[0]
        evidence["match_type"] = "substring_unique"
        evidence["chosen_raw_name"] = raw_name
        evidence["chosen_node_id"] = nid
        log.info("NAIC group resolved: '%s' -> '%s' (id=%s) [substring]", label, raw_name, nid)
        return nid, evidence

    if len(substring_matches) > 1:
        evidence["match_type"] = "substring_ambiguous"
        evidence["failure"] = "ambiguous_matches"
        log.warning("NAIC group ambiguous for '%s': %d matches: %s",
                     label, len(substring_matches),
                     [(r, n) for r, n, _ in substring_matches[:5]])
        return None, evidence

    # Token-overlap fallback: score each node by Jaccard-like overlap of word tokens.
    # Handles labels that differ by a word or two (e.g. missing "and").
    TOKEN_OVERLAP_THRESHOLD = 0.75
    label_tokens = set(norm_label.split())
    if len(label_tokens) >= 3:
        scored: list[tuple[float, str, str, str]] = []
        for norm_key, entries in node_map.items():
            node_tokens = set(norm_key.split())
            if not node_tokens:
                continue
            intersection = label_tokens & node_tokens
            union = label_tokens | node_tokens
            score = len(intersection) / len(union) if union else 0.0
            if score >= TOKEN_OVERLAP_THRESHOLD:
                for raw, nid in entries:
                    scored.append((score, raw, nid, norm_key))
        scored.sort(key=lambda x: x[0], reverse=True)

        evidence["token_scored_top5"] = [
            {"raw": raw, "id": nid, "score": round(sc, 3)} for sc, raw, nid, _ in scored[:5]
        ]

        if len(scored) == 1 or (len(scored) > 1 and scored[0][0] - scored[1][0] >= 0.05):
            best_score, raw_name, nid, _ = scored[0]
            evidence["match_type"] = "token_overlap"
            evidence["token_overlap_score"] = round(best_score, 3)
            evidence["candidate_matches"] = [(raw, nid_) for _, raw, nid_, _ in scored]
            evidence["chosen_raw_name"] = raw_name
            evidence["chosen_node_id"] = nid
            log.info("NAIC group resolved: '%s' -> '%s' (id=%s) [token_overlap=%.3f]",
                     label, raw_name, nid, best_score)
            return nid, evidence

        if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.05:
            evidence["match_type"] = "token_overlap_ambiguous"
            evidence["failure"] = "ambiguous_token_matches"
            evidence["candidate_matches"] = [(raw, nid_) for _, raw, nid_, _ in scored]
            log.warning("NAIC group token-overlap ambiguous for '%s': top scores %s",
                        label, [(round(s, 3), r) for s, r, _, _ in scored[:3]])
            return None, evidence

    evidence["match_type"] = "none"
    evidence["failure"] = "no_match"
    log.warning("NAIC group not found for label '%s' (normalized='%s')", label, norm_label)
    return None, evidence


def _resolve_organization_naic_node(
    tree_name: str, bubble_snapshot: dict | None = None,
) -> tuple[str | None, dict]:
    """Resolve the 'NAIC' node under the Organization tree using normalized name matching.

    Loads all nodes and selects the one whose normalized name equals "naic".
    Returns (node_id, evidence) where evidence always explains the outcome.
    """
    if bubble_snapshot:
        nid, evidence = _resolve_organization_naic_node_from_snapshot(bubble_snapshot, tree_name)
        if nid:
            log.info("Resolved Organization node: NAIC -> node_id=%s (snapshot)", nid)
        else:
            log.warning("Organization resolution failed (snapshot): %s", evidence.get("failure"))
        return nid, evidence

    evidence: dict = {"source": "live", "tree_name": tree_name}
    tree = lookups.get_tree_by_name(tree_name)
    if not tree:
        log.warning("Organization tree not found: %s", tree_name)
        evidence["failure"] = "tree_not_found"
        return None, evidence

    tree_id = tree.get("_id") or tree.get("id")
    evidence["tree_id"] = tree_id

    all_nodes = lookups.get_tree_nodes_in_tree(tree_id)
    node_names = [_node_name(n) for n in all_nodes if _node_name(n)]
    evidence["node_count"] = len(all_nodes)
    evidence["node_names_sample"] = node_names[:20]

    target_norm = _normalize_for_matching("NAIC")
    matches: list[tuple[str, str]] = []
    for n in all_nodes:
        raw = _node_name(n)
        nid = n.get("_id") or n.get("id")
        if not raw or not nid:
            continue
        if _normalize_for_matching(raw) == target_norm:
            matches.append((raw, str(nid)))

    evidence["naic_candidates"] = matches
    if len(matches) == 1:
        raw_name, nid = matches[0]
        log.info("Resolved Organization node: NAIC -> '%s' node_id=%s", raw_name, nid)
        evidence["resolved_id"] = nid
        evidence["resolved_name"] = raw_name
        return nid, evidence
    if len(matches) > 1:
        log.warning("Ambiguous NAIC matches under tree '%s': %s", tree_name, matches)
        evidence["failure"] = "ambiguous_naic_matches"
        return None, evidence

    log.warning("NAIC node not found under tree '%s' (nodes: %s)", tree_name, node_names[:10])
    evidence["failure"] = "naic_node_not_found"
    return None, evidence




def _resolve_type1_node_by_name(
    tree_name: str, node_name: str, bubble_snapshot: dict | None = None
) -> str | None:
    """Resolve a single Type1 tree node by display name (case-insensitive). Returns _id or None."""
    target = (node_name or "").strip().lower()
    if not target:
        return None
    if bubble_snapshot:
        trees = bubble_snapshot.get("trees") or []
        tree = next((t for t in trees if (t.get("Name") or t.get("name") or "").strip() == tree_name), None)
        if not tree:
            return None
        tree_id = _obj_id(tree)
        if not tree_id:
            return None
        for n in bubble_snapshot.get("tree_nodes") or []:
            if _node_tree_id(n) != tree_id:
                continue
            if _node_name(n).lower() == target:
                return _obj_id(n)
        return None
    tree = lookups.get_tree_by_name(tree_name)
    if not tree:
        return None
    tree_id = tree.get("_id") or tree.get("id")
    nodes = lookups.get_tree_nodes_in_tree(tree_id)
    for n in nodes:
        if _node_name(n).lower() == target:
            return _obj_id(n)
    return None


def _get_all_type1_node_names(tree_name: str, bubble_snapshot: dict | None = None) -> list[tuple[str, str]]:
    """Return all (name, _id) pairs for named nodes under the Type1 tree. Skips nodes with name=None."""
    if bubble_snapshot:
        trees = bubble_snapshot.get("trees") or []
        tree = next((t for t in trees if (t.get("Name") or t.get("name") or "").strip() == tree_name), None)
        if not tree:
            return []
        tree_id = _obj_id(tree)
        if not tree_id:
            return []
        out = []
        for n in bubble_snapshot.get("tree_nodes") or []:
            if _node_tree_id(n) != tree_id:
                continue
            name = _node_name(n)
            nid = _obj_id(n)
            if name and nid:
                out.append((name, str(nid)))
        return out
    tree = lookups.get_tree_by_name(tree_name)
    if not tree:
        return []
    tree_id = tree.get("_id") or tree.get("id")
    nodes = lookups.get_tree_nodes_in_tree(tree_id)
    return [
        (_node_name(n), str(_obj_id(n)))
        for n in nodes if _node_name(n) and _obj_id(n)
    ]


def _build_type1_nodes_by_name(tree_name: str, bubble_snapshot: dict | None = None) -> dict[str, str]:
    """
    Build name→id map from ALL nodes in the Resources Types tree. Case-insensitive keys.
    This is a complete lookup table; the deterministic classifier picks a name and we check
    whether it exists here.
    """
    all_nodes = _get_all_type1_node_names(tree_name, bubble_snapshot)
    if all_nodes:
        log.info("Type1 tree '%s' has %d node(s): %s", tree_name, len(all_nodes), [n for n, _ in all_nodes])
    else:
        log.warning("Type1 tree '%s' has no nodes (or tree not found). Type1 will not resolve.", tree_name)
        return {}
    out: dict[str, str] = {}
    for name, nid in all_nodes:
        out[name] = nid
        out[name.lower()] = nid
    log.info("Type1 name→id map: %s", {n: nid[:20] + "…" for n, nid in out.items() if n == n.lower()})
    return out


# ---------------------------------------------------------------------------
# Topic suggestion helpers (Chronicles tree)
# ---------------------------------------------------------------------------

# BBCode tags commonly found in Bubble tree node names
_BBCODE_RE = re.compile(r"\[/?(?:b|i|u|s|color|size|url|img|quote|code|list|\*)[^\]]*\]", re.IGNORECASE)

TOPIC_AI_CONFIDENCE_THRESHOLD = float(os.environ.get("TOPIC_AI_CONFIDENCE_THRESHOLD", "0.65"))


def strip_bbcode(text: str) -> str:
    """Remove BBCode markup tags, collapse whitespace, strip edges."""
    cleaned = _BBCODE_RE.sub("", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # Strip zero-width spaces and other invisible unicode
    cleaned = cleaned.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "").strip()
    return cleaned


def _get_all_tree_nodes(tree_name: str, bubble_snapshot: dict | None = None) -> list[dict]:
    """Fetch all tree-node dicts for a given tree name."""
    if bubble_snapshot:
        trees = bubble_snapshot.get("trees") or []
        tree = next((t for t in trees if (t.get("Name") or t.get("name") or "").strip() == tree_name), None)
        if not tree:
            return []
        tree_id = _obj_id(tree)
        if not tree_id:
            return []
        return [n for n in (bubble_snapshot.get("tree_nodes") or []) if _node_tree_id(n) == tree_id]
    tree = lookups.get_tree_by_name(tree_name)
    if not tree:
        return []
    tree_id = tree.get("_id") or tree.get("id")
    return lookups.get_tree_nodes_in_tree(tree_id)


def _build_topic_candidates(
    tree_name: str, bubble_snapshot: dict | None = None,
) -> dict[str, str]:
    """
    Build a map of  display_name → node_id  for every named node in the topic tree.

    Keys are BBCode-stripped, case-preserved names for AI matching.
    A separate lowercase key is also stored for case-insensitive fallback.
    The original Bubble name (with BBCode) is mapped through to its ID so resolution
    always works even when the raw name contains markup.
    """
    all_nodes = _get_all_tree_nodes(tree_name, bubble_snapshot)
    nodes = [n for n in all_nodes if _node_name(n)]
    if not nodes:
        log.warning("topic tree '%s': no named nodes found (%d total).", tree_name, len(all_nodes))
        return {}

    result: dict[str, str] = {}
    for n in nodes:
        nid = _obj_id(n)
        if not nid:
            continue
        raw_name = _node_name(n)
        clean = strip_bbcode(raw_name)
        if not clean:
            continue
        result[clean] = str(nid)
        result[clean.lower()] = str(nid)
        # Also keep the raw name so exact Bubble names resolve
        if raw_name != clean:
            result[raw_name] = str(nid)

    log.info("topic tree '%s': %d candidate(s) loaded", tree_name, len(nodes))
    if result:
        sample = sorted({k for k in result if k == k.lower()})[:8]
        log.debug("topic candidate sample (lowered): %s", sample)
    return result


def _resolve_topic_suggestion_ai(
    resource: dict,
    context: dict,
    topic_candidates: dict[str, str],
    _chat_fn=None,
) -> dict:
    """
    Ask AI to select exactly one topic name from the Chronicles tree candidates.

    Returns a result dict with keys:
        topic_name, node_id, confidence, candidates_sent, status
    AI must not output IDs; we enforce allowlist membership.
    """
    empty = {"topic_name": None, "node_id": None, "confidence": 0.0,
             "candidates_sent": [], "status": "unresolved"}
    if not topic_candidates:
        return empty

    if _chat_fn is None:
        from bubble.openai_client import chat_json as _chat_fn

    # Build deduplicated candidate list (clean names, case-preserved)
    seen_lower: set[str] = set()
    candidate_names: list[str] = []
    for k in sorted(topic_candidates.keys()):
        if k.lower() in seen_lower or k != k.strip():
            continue
        if k.lower() == k and k in topic_candidates:
            pass
        if k.lower() not in seen_lower:
            seen_lower.add(k.lower())
            candidate_names.append(k)

    title = (resource.get("Name") or "").strip()
    url = (resource.get("URL") or "").strip()
    notes = (resource.get("notes") or "").strip()
    parent = (resource.get("parent") or "").strip()
    label = (context.get("label") or "").strip()
    org_path = context.get("org_path") or []
    org_str = " › ".join(str(s) for s in org_path) if org_path else ""

    system_msg = (
        "You are a topic classifier for insurance/regulatory resources.\n"
        "Given a resource, select the single most appropriate topic name from the "
        "candidate list, or null if none fits.\n\n"
        "Respond with ONLY a JSON object:\n"
        '{"topic_name": "<exact name from list>", "confidence": <0.0-1.0>}\n'
        'or {"topic_name": null, "confidence": 0}\n\n'
        "Rules:\n"
        "- topic_name MUST be an exact string from the candidate list, or null.\n"
        "- Do NOT output Bubble IDs.\n"
        "- Do NOT invent topic names.\n"
        "- confidence is your certainty from 0 to 1."
    )
    user_msg = (
        "## Resource\n"
        f"- Title: {title}\n"
        f"- URL: {url[:120]}\n"
        f"- Notes: {notes[:300]}\n"
        f"- Parent: {parent}\n"
        f"- Section label: {label}\n"
        f"- Organization path: {org_str}\n\n"
        "## Candidate topic names (select exactly one or null)\n"
    )
    for name in candidate_names:
        user_msg += f"- {name}\n"

    result = dict(empty)
    result["candidates_sent"] = candidate_names[:10]

    try:
        data = _chat_fn([
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ], reasoning_effort="low")
    except Exception:
        log.warning("topic suggestion: AI call failed for resource %s", title[:60], exc_info=True)
        return result

    if not isinstance(data, dict):
        return result

    raw_name = data.get("topic_name")
    confidence = 0.0
    try:
        confidence = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    result["confidence"] = confidence

    if raw_name is None or not isinstance(raw_name, str) or not raw_name.strip():
        result["status"] = "ai_null"
        return result

    raw_name = raw_name.strip()
    result["topic_name"] = raw_name

    if confidence < TOPIC_AI_CONFIDENCE_THRESHOLD:
        log.debug("topic suggestion: confidence %.2f < threshold %.2f for '%s'",
                   confidence, TOPIC_AI_CONFIDENCE_THRESHOLD, raw_name)
        result["status"] = "low_confidence"
        return result

    # Resolve: exact match, then case-insensitive, then BBCode-stripped
    node_id = topic_candidates.get(raw_name)
    if not node_id:
        node_id = topic_candidates.get(raw_name.lower())
    if not node_id:
        cleaned = strip_bbcode(raw_name)
        node_id = topic_candidates.get(cleaned) or topic_candidates.get(cleaned.lower())

    if node_id:
        result["node_id"] = node_id
        result["status"] = "resolved"
    else:
        log.warning(
            "topic suggestion: AI returned '%s' (conf=%.2f) not in candidates (%d). Dropping.",
            raw_name, confidence, len(candidate_names),
        )
        result["status"] = "not_in_candidates"

    return result


def _match_calendar_item_from_snapshot(
    snapshot: dict, title: str, notes: str, tolerance_days: int = 7
) -> list[tuple[str, float]]:
    """
    Return candidate calendar items from snapshot as (id, score), sorted by score desc.
    Score is based on title match and presence of a matching date token.
    """
    title = (title or "").strip().lower()
    if not title:
        return []
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
    candidates: list[tuple[str, float]] = []
    for c in items:
        ct = (c.get("title") or "").strip().lower()
        if not ct:
            continue
        # Basic title similarity: substring either way
        if title not in ct and ct not in title:
            continue
        score = 1.0
        cd = c.get("date")
        if date_iso and isinstance(cd, str) and date_iso in cd:
            score += 1.0
        cid = c.get("_id") or c.get("id")
        if cid:
            candidates.append((str(cid), score))
    # If no candidates with date filter, allow title-only matches (already added above)
    candidates.sort(key=lambda t: t[1], reverse=True)
    return candidates


def _tokens_from_group_name(group_name: str) -> list[str]:
    """Extract meaningful tokens from group name for title matching."""
    if not (group_name or "").strip():
        return []
    raw = re.split(r"[^\w]+", (group_name or "").strip())
    return [t for t in raw if t and (len(t) >= 2 or (len(t) == 1 and t.isupper()))]


def _calendar_items_in_date_window(
    date_iso: str,
    window_days: int,
    bubble_snapshot: dict | None = None,
) -> list[dict]:
    """Return calendar items whose date is within ±window_days of date_iso."""
    if not date_iso or not str(date_iso).strip():
        return []
    try:
        from datetime import datetime, timedelta, timezone
        dt = datetime.fromisoformat(str(date_iso).strip().replace("Z", "+00:00")[:10])
    except ValueError:
        return []
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    low = (dt - timedelta(days=window_days)).strftime("%Y-%m-%d")
    high = (dt + timedelta(days=window_days)).strftime("%Y-%m-%d")
    if bubble_snapshot:
        items = bubble_snapshot.get("calendar_items") or []
        return [c for c in items if isinstance(c.get("date"), str) and low <= (c.get("date") or "")[:10] <= high]
    return lookups.search_calendar_items_by_date_window(date_iso, window_days=window_days)


def _score_calendar_candidate(
    item: dict,
    group_tokens: list[str],
    context_tokens: list[str],
    date_iso: str,
) -> tuple[float, dict[str, Any]]:
    """Score a calendar item for meeting_meta-based linking. Returns (score, evidence)."""
    title = (item.get("title") or "").strip().lower()
    item_date = (item.get("date") or "")
    item_date_str = (item_date[:10] if isinstance(item_date, str) and len(item_date) >= 10 else str(item_date))
    evidence: dict[str, Any] = {"title": item.get("title"), "date": item_date_str}
    score = 0.0
    if date_iso and item_date_str and date_iso[:10] == item_date_str:
        score += 1.0
        evidence["date_in_window"] = True
    else:
        evidence["date_in_window"] = bool(date_iso and item_date_str)
    matched = [t for t in group_tokens if t and t.lower() in title]
    if matched:
        score += 0.5 * len(matched)
        evidence["group_tokens_matched"] = matched
    for t in context_tokens:
        if t and t.lower() in title:
            score += 0.3
            evidence.setdefault("context_tokens_matched", []).append(t)
    return (score, evidence)


def _resolve_calendar_by_naic_group(
    resource_context: dict,
    naic_group_tree_name: str,
    date_iso: str | None,
    window_days: int,
    no_date_cap: int,
    bubble_snapshot: dict | None = None,
) -> tuple[list[str], list[dict], str, dict]:
    """Resolve Related calendar items via the resource's NAIC group tree node.

    Derives the group path from context (org_path + label), resolves to a NAIC group
    tree node ID, then queries Calendar Items by that node ID.

    Returns (selected_ids, candidates_detail, status, evidence).
    """
    evidence: dict[str, Any] = {"method": "naic_group"}
    org_path = resource_context.get("org_path") or []
    label = (resource_context.get("label") or "").strip()
    path = infer_naic_group_path(org_path)
    if label:
        path = path + [label]
    evidence["org_path"] = org_path
    evidence["label"] = label
    evidence["derived_path"] = path

    if not path:
        evidence["failure"] = "empty_path"
        return [], [], "UNRESOLVED", evidence

    group_node_id, resolve_ev = _resolve_naic_group_node(naic_group_tree_name, path, bubble_snapshot)
    evidence["group_node_id"] = group_node_id
    evidence["group_resolve_detail"] = resolve_ev
    if not group_node_id:
        evidence["failure"] = resolve_ev.get("failure", "group_node_not_found")
        return [], [], "UNRESOLVED", evidence

    # Query calendar items by NAIC group node
    has_date = bool(date_iso and str(date_iso).strip())
    evidence["date_used"] = date_iso if has_date else None
    evidence["has_date"] = has_date

    if bubble_snapshot:
        all_items = bubble_snapshot.get("calendar_items") or []
        group_items = []
        for c in all_items:
            naic_ref = c.get("NAIC Group (tree node)")
            if naic_ref == group_node_id:
                group_items.append(c)
            elif isinstance(naic_ref, list) and group_node_id in naic_ref:
                group_items.append(c)
        snapshot_date_mode = "no_date_upcoming"
        if has_date:
            from datetime import datetime as _dt, timedelta as _td, timezone as _tz
            try:
                center = _dt.fromisoformat(str(date_iso).strip()[:10])
                if center.tzinfo is None:
                    center = center.replace(tzinfo=_tz.utc)
                low = (center - _td(days=window_days)).strftime("%Y-%m-%d")
                high = (center + _td(days=window_days)).strftime("%Y-%m-%d")
                group_items = [
                    c for c in group_items
                    if isinstance(c.get("date"), str) and low <= c["date"][:10] <= high
                ]
                snapshot_date_mode = "date_window"
                evidence["date_low"] = low
                evidence["date_high"] = high
            except (ValueError, TypeError):
                snapshot_date_mode = "date_parse_failed_fallback_upcoming"
        if snapshot_date_mode != "date_window":
            from datetime import datetime as _dt, timezone as _tz
            today = _dt.now(tz=_tz.utc).strftime("%Y-%m-%d")
            group_items = [
                c for c in group_items
                if isinstance(c.get("date"), str) and c["date"][:10] >= today
            ]
            group_items = group_items[:no_date_cap]
            evidence["fallback_no_date"] = True
        evidence["date_mode"] = snapshot_date_mode
        items = group_items
    else:
        items, lookup_meta = lookups.search_calendar_items_by_naic_group(
            group_node_id,
            date_iso=date_iso if has_date else None,
            window_days=window_days,
            limit=50 if has_date else no_date_cap,
        )
        evidence["bubble_lookup_meta"] = lookup_meta
        if not has_date and items:
            evidence["fallback_no_date"] = True

    evidence["candidate_count"] = len(items)
    candidates_detail: list[dict] = []
    selected_ids: list[str] = []
    for c in items:
        cid = c.get("_id") or c.get("id")
        if not cid:
            continue
        cid = str(cid)
        candidates_detail.append({
            "id": cid,
            "title": c.get("title"),
            "date": c.get("date"),
        })
        if cid not in selected_ids:
            selected_ids.append(cid)

    evidence["chosen_ids"] = selected_ids
    if not selected_ids:
        status = "UNRESOLVED"
    elif len(selected_ids) > 1:
        status = "MULTI_RESOLVED"
    else:
        status = "RESOLVED"
    if not has_date and selected_ids:
        evidence["note"] = f"no date; capped to {no_date_cap} upcoming items"
    return selected_ids, candidates_detail, status, evidence


def _match_calendar_item_for_resource(
    resource_title: str,
    resource_notes: str,
    resource_context: dict,
    calendar_payload: list[dict],
    calendar_context: list[dict],
    tolerance_days: int = 7,
    bubble_snapshot: dict | None = None,
    resource: dict | None = None,
    meeting_meta_window_days: int = CALENDAR_LINK_MEETING_META_WINDOW_DAYS,
) -> tuple[list[str], list[dict], str]:
    """
    Match a resource to existing Bubble calendar item(s).
    Returns (selected_ids, candidates_detail, status).
    candidates_detail: list of {id, title, date, score, evidence}.
    status: RESOLVED | MULTI_RESOLVED | AMBIGUOUS | UNRESOLVED.
    When resource has __meeting_meta.date_iso, queries calendar in ±meeting_meta_window_days and scores by group_name + context.
    """
    SCORE_THRESHOLD = 1.0
    AMBIGUOUS_DELTA = 0.25

    meeting_meta = (resource or {}).get("__meeting_meta") if isinstance(resource, dict) else None
    # Skip calendar linking from meeting_meta that failed validation
    if isinstance(meeting_meta, dict) and not meeting_meta.get("valid", True):
        meeting_meta = None
    date_iso_meta = (meeting_meta.get("date_iso") or "").strip() if isinstance(meeting_meta, dict) else ""

    if date_iso_meta and meeting_meta:
        group_name = (meeting_meta.get("group_name") or "").strip()
        group_tokens = _tokens_from_group_name(group_name)
        ctx = resource_context or {}
        label = (ctx.get("label") or "").strip()
        org_path = ctx.get("org_path") or []
        context_tokens = [t for t in ([label] + [str(p).strip() for p in org_path if p]) if t and len(t) >= 2]
        items = _calendar_items_in_date_window(date_iso_meta, meeting_meta_window_days, bubble_snapshot)
        candidates_detail: list[dict] = []
        for c in items:
            cid = c.get("_id") or c.get("id")
            if not cid:
                continue
            score, evidence = _score_calendar_candidate(c, group_tokens, context_tokens, date_iso_meta)
            candidates_detail.append({"id": str(cid), "title": c.get("title"), "date": c.get("date"), "score": round(score, 2), "evidence": evidence})
        candidates_detail.sort(key=lambda x: x["score"], reverse=True)
        selected_ids: list[str] = []
        status = "UNRESOLVED"
        if not candidates_detail:
            return (selected_ids, candidates_detail, status)
        above = [c for c in candidates_detail if c["score"] >= SCORE_THRESHOLD]
        if not above:
            return (selected_ids, candidates_detail, status)
        top_score = above[0]["score"]
        second_score = above[1]["score"] if len(above) > 1 else None
        if second_score is not None and (top_score - second_score) < AMBIGUOUS_DELTA:
            status = "AMBIGUOUS"
            return (selected_ids, candidates_detail, status)
        seen_titles: set[str] = set()
        for c in above:
            tit = (c.get("title") or "").strip().lower()
            if tit and tit not in seen_titles:
                seen_titles.add(tit)
                selected_ids.append(c["id"])
        status = "MULTI_RESOLVED" if len(selected_ids) > 1 else "RESOLVED"
        return (selected_ids, candidates_detail, status)

    # Legacy path: no __meeting_meta
    if bubble_snapshot:
        cid = _match_calendar_item_from_snapshot(
            bubble_snapshot, resource_title, resource_notes, tolerance_days
        )
        candidates = cid
        if not candidates:
            return ([], [], "UNRESOLVED")
        top_id, top_score = candidates[0]
        second_score = candidates[1][1] if len(candidates) > 1 else None
        detail = [{"id": c[0], "score": c[1], "title": None, "date": None, "evidence": {}} for c in candidates[:5]]
        if top_score < SCORE_THRESHOLD:
            return ([], detail, "UNRESOLVED")
        if second_score is not None and (top_score - second_score) < AMBIGUOUS_DELTA:
            return ([], detail, "AMBIGUOUS")
        if top_id:
            log.info("Calendar link: matched meeting id=%s (title match + date tolerance, snapshot)", top_id)
        return ([top_id] if top_id else [], detail, "RESOLVED" if top_id else "UNRESOLVED")
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
        if cid:
            log.info("Calendar link: matched meeting id=%s (title match + date tolerance)", cid)
            detail = [{"id": str(cid), "title": existing.get("title"), "date": existing.get("date"), "score": 1.0, "evidence": {}}]
            return ([str(cid)], detail, "RESOLVED")
    return ([], [], "UNRESOLVED")


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
    topic_tree_name: str = TOPIC_TREE_NAME,
    use_ai: bool = False,
    calendar_link_tolerance_days: int = 7,
    calendar_link_meeting_meta_window_days: int = CALENDAR_LINK_MEETING_META_WINDOW_DAYS,
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
    org_naic_id, org_evidence = _resolve_organization_naic_node(organization_tree_name, bubble_snapshot)
    for ri, r in enumerate(updated_resources):
        if org_naic_id:
            r["Organization"] = [org_naic_id]
            record_resolution(
                "organization",
                "Organization",
                chosen_ids=[org_naic_id],
                candidates=[org_naic_id],
                status="resolved",
                evidence=org_evidence,
                target="Resource",
                index=ri,
            )
        else:
            # Keep whatever string/value was already in Organization (don't overwrite)
            record_resolution(
                "organization",
                "Organization",
                chosen_ids=[],
                candidates=[],
                status="no_match",
                evidence=org_evidence,
                target="Resource",
                index=ri,
            )

    # Resolve NAIC group tree node per calendar from context (Calendar has "NAIC Group (tree node)")
    for i, ctx in enumerate(calendar_context):
        if i >= len(updated_calendar):
            break
        path = infer_naic_group_path(ctx.get("org_path"))
        label = (ctx.get("label") or "").strip()
        if label:
            path = path + [label]
        if not path:
            record_resolution(
                "naic_group",
                "NAIC Group (tree node)",
                chosen_ids=[],
                candidates=[],
                status="skipped",
                evidence={"reason": "empty_path"},
                target="CalendarItem",
                index=i,
            )
            continue
        nid, grp_evidence = _resolve_naic_group_node(naic_group_tree_name, path, bubble_snapshot)
        if nid:
            updated_calendar[i]["NAIC Group (tree node)"] = nid
            record_resolution(
                "naic_group",
                "NAIC Group (tree node)",
                chosen_ids=[nid],
                candidates=[nid],
                status="resolved",
                evidence=grp_evidence,
                target="CalendarItem",
                index=i,
            )
        else:
            record_resolution(
                "naic_group",
                "NAIC Group (tree node)",
                chosen_ids=[],
                candidates=[],
                status="no_match",
                evidence=grp_evidence,
                target="CalendarItem",
                index=i,
            )

    # --- Type1 resolution (deterministic from section_type / keywords) ---
    type1_by_name = _build_type1_nodes_by_name(type1_tree_name, bubble_snapshot)
    type1_available_names = sorted({n for n in type1_by_name if n == n.lower()})
    fallback_id = type1_by_name.get(TYPE1_FALLBACK) or type1_by_name.get(TYPE1_FALLBACK.lower())
    type1_resolved_count = 0
    type1_unresolved_count = 0
    for i, r in enumerate(updated_resources):
        ctx = resource_context[i] if i < len(resource_context) else {}
        title = (r.get("Name") or "").strip()
        url = (r.get("URL") or "").strip()
        notes = (r.get("notes") or "").strip()
        section_type = ctx.get("section_type", "")
        section_label = ctx.get("section_label", "")
        type1_name = classify_resource_type_deterministic(title, url, notes, section_type=section_type)
        type1_id = type1_by_name.get(type1_name) or type1_by_name.get(type1_name.lower())
        # If the classifier returned a name that doesn't exist in the tree, fall back
        if not type1_id and fallback_id:
            log.debug(
                "Type1: '%s' not in node map, falling back to '%s'",
                type1_name, TYPE1_FALLBACK,
            )
            type1_name = TYPE1_FALLBACK
            type1_id = fallback_id
        if type1_id:
            r["Type1"] = [type1_id]
            type1_resolved_count += 1
            log.info("Type1: section=%s -> '%s' -> node_id=%s", section_type, type1_name, type1_id)
            record_resolution(
                "type1", "Type1",
                chosen_ids=[type1_id],
                candidates=type1_available_names,
                status="resolved",
                evidence={
                    "method": "deterministic",
                    "section_type": section_type,
                    "section_label": section_label,
                    "type1_name": type1_name,
                },
                target="Resource", index=i,
            )
        else:
            r["Type1"] = []
            type1_unresolved_count += 1
            log.warning(
                "Type1: no match and no fallback for section=%s type1_name=%s; available=%s",
                section_type, type1_name, type1_available_names,
            )
            record_resolution(
                "type1", "Type1",
                chosen_ids=[],
                candidates=type1_available_names,
                status="no_match",
                evidence={
                    "method": "deterministic",
                    "section_type": section_type,
                    "section_label": section_label,
                    "type1_name": type1_name,
                    "available_nodes": type1_available_names,
                },
                target="Resource", index=i,
            )
    log.info("Type1 summary: resolved=%d  unresolved=%d", type1_resolved_count, type1_unresolved_count)

    # --- topic suggestion resolution (AI from Chronicles tree) ---
    topic_candidates = _build_topic_candidates(topic_tree_name, bubble_snapshot)
    topic_resolved_count = 0
    topic_unresolved_count = 0
    if use_ai and topic_candidates:
        for i, r in enumerate(updated_resources):
            ctx = resource_context[i] if i < len(resource_context) else {}
            ai_result = _resolve_topic_suggestion_ai(r, ctx, topic_candidates)
            topic_name = ai_result.get("topic_name")
            topic_id = ai_result.get("node_id")
            confidence = ai_result.get("confidence", 0.0)
            status = ai_result.get("status", "unresolved")

            evidence = {
                "method": "ai",
                "topic_name": topic_name,
                "confidence": confidence,
                "status": status,
                "candidates_sent": ai_result.get("candidates_sent", []),
            }
            if topic_id:
                r["topic suggestion"] = topic_id
                topic_resolved_count += 1
                log.info("topic suggestion: '%s' (conf=%.2f) -> node_id=%s",
                         topic_name, confidence, topic_id)
                record_resolution(
                    "topic_suggestion", "topic suggestion",
                    chosen_ids=[topic_id],
                    candidates=ai_result.get("candidates_sent", []),
                    status="resolved",
                    evidence=evidence,
                    target="Resource", index=i,
                )
            else:
                topic_unresolved_count += 1
                record_resolution(
                    "topic_suggestion", "topic suggestion",
                    chosen_ids=[],
                    candidates=ai_result.get("candidates_sent", []),
                    status=status,
                    evidence=evidence,
                    target="Resource", index=i,
                )
    elif not topic_candidates:
        log.warning("topic suggestion: no candidates loaded from tree '%s'; skipping.", topic_tree_name)
        topic_unresolved_count = len(updated_resources)
    else:
        log.info("topic suggestion: AI disabled; skipping topic resolution.")
        topic_unresolved_count = len(updated_resources)
    log.info("topic suggestion summary: resolved=%d  unresolved=%d", topic_resolved_count, topic_unresolved_count)

    # Related calendar items: primary strategy uses NAIC group tree node from context,
    # fallback to old title/date matching if group resolution fails.
    for i, r in enumerate(updated_resources):
        ctx = resource_context[i] if i < len(resource_context) else {}
        title = (r.get("Name") or "").strip()

        # Derive date: prefer __meeting_meta.date_iso, then resource date
        meeting_meta = r.get("__meeting_meta") if isinstance(r.get("__meeting_meta"), dict) else None
        if isinstance(meeting_meta, dict) and not meeting_meta.get("valid", True):
            meeting_meta = None
        date_iso = None
        if meeting_meta:
            date_iso = (meeting_meta.get("date_iso") or "").strip() or None
        if not date_iso:
            raw_date = r.get("date")
            if isinstance(raw_date, str) and raw_date.strip():
                date_iso = raw_date.strip()[:10]

        # Primary: NAIC group based resolution
        selected_ids, candidates_detail, status, cal_evidence = _resolve_calendar_by_naic_group(
            ctx,
            naic_group_tree_name,
            date_iso,
            window_days=CALENDAR_NAIC_GROUP_WINDOW_DAYS,
            no_date_cap=CALENDAR_NAIC_GROUP_NO_DATE_CAP,
            bubble_snapshot=bubble_snapshot,
        )
        cal_evidence["resource_title"] = title[:80]

        # Fallback to old title/date matching if NAIC group approach found nothing
        if not selected_ids:
            notes = (r.get("notes") or "").strip()
            fb_ids, fb_detail, fb_status = _match_calendar_item_for_resource(
                title, notes, ctx, calendar_payload, calendar_context,
                tolerance_days=calendar_link_tolerance_days,
                bubble_snapshot=bubble_snapshot,
                resource=r,
                meeting_meta_window_days=calendar_link_meeting_meta_window_days,
            )
            if fb_ids:
                selected_ids = fb_ids
                candidates_detail = fb_detail
                status = fb_status
                cal_evidence["fallback_method"] = "title_date_match"
                cal_evidence["fallback_candidates"] = fb_detail
                cal_evidence["fallback_status"] = fb_status

        existing_refs = r.get("Related calendar items") or []
        if not isinstance(existing_refs, list):
            existing_refs = []
        for cid in selected_ids:
            if cid not in existing_refs:
                existing_refs = existing_refs + [cid]
        r["Related calendar items"] = existing_refs

        rec_status = "unresolved"
        if status == "RESOLVED":
            rec_status = "resolved"
        elif status == "MULTI_RESOLVED":
            rec_status = "multi_resolved"
        elif status == "AMBIGUOUS":
            rec_status = "ambiguous"
        record_resolution(
            "calendar_linking",
            "Related calendar items",
            chosen_ids=list(selected_ids),
            candidates=[c.get("id") for c in candidates_detail if c.get("id")],
            status=rec_status,
            evidence=cal_evidence,
            target="Resource",
            index=i,
        )

    return (updated_resources, updated_calendar)
