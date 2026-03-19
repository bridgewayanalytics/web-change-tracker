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
    # Strip BBCode and "The " prefix duplicates — prefer the canonical (shorter) name
    seen_lower: set[str] = set()
    candidate_names: list[str] = []
    for k in sorted(topic_candidates.keys()):
        if k != k.strip():
            continue
        low = k.lower()
        if low in seen_lower:
            continue
        # Skip BBCode-wrapped variants (the clean name is already in the list)
        cleaned = strip_bbcode(k).strip()
        if cleaned != k and cleaned.lower() in seen_lower:
            continue
        # Skip "The X" if "X" is already in the list
        no_the = re.sub(r"^The\s+", "", k, flags=re.IGNORECASE)
        if no_the != k and no_the.lower() in seen_lower:
            continue
        seen_lower.add(low)
        # Also register without "The" so later "The X" variants are skipped
        if no_the != k:
            seen_lower.add(no_the.lower())
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
        "- NEVER select placeholder topics like 'Calendar Events with no Topic' "
        "or 'No Topic' — these are not real topics.\n"
        "- The organization path tells you which regulatory body/committee this "
        "resource belongs to. Use it for context, but do NOT just match the "
        "organization name as a topic.\n\n"
        "## Decision framework\n\n"
        "1. MEETING AGENDAS: If the document is a meeting agenda for a committee, "
        "the topic is the committee's primary focus area, NOT a generic 'landscape' topic.\n"
        "   Example: 'International Insurance Relations Committee Agenda' → "
        "'International Association of Insurance Supervisors (IAIS)' "
        "(the committee focuses on IAIS matters), NOT 'The International Landscape'.\n"
        "   Example: 'Climate and Resiliency Task Force Agenda' → "
        "'NAIC Climate Initiatives' (the task force's focus).\n\n"
        "2. SUBJECT-MATTER vs ORGANIZATION topics:\n"
        "   * If the document is ABOUT a specific regulatory subject (private equity, "
        "solvency, mortgages, stress testing), pick the subject-matter topic.\n"
        "     Example: 'Supervision of PE Insurers in Bermuda' → 'Private Equity "
        "Owned Insurers' not 'The Bermuda Monetary Authority (BMA)'.\n"
        "     Example: 'BoE Financial Systemwide Stress Exercise' → 'U.K. Solvency' "
        "not 'U.K. Bank of England (BoE)' (stress testing = solvency regulation).\n"
        "     Example: 'Fit-For-55 Climate Scenario Analysis' by EIOPA → "
        "'European Insurance and Occupational Pensions Authority (EIOPA) Climate "
        "Initiatives' not a generic ESA/JC topic.\n"
        "   * If the document is a general publication BY an organization "
        "(annual report, market report, progress update, policy statement), "
        "pick the organization topic.\n"
        "     Example: 'FSB Progress Report on Climate Disclosures' → "
        "'Financial Stability Board (FSB)' not a climate topic.\n"
        "     Example: 'GIMAR' → 'International Association of Insurance "
        "Supervisors (IAIS)' not a market topic.\n\n"
        "3. SPECIFICITY: When multiple topics seem relevant, prefer the MORE "
        "SPECIFIC topic over a generic one.\n"
        "   Example: Prefer 'EIOPA Climate Initiatives' over 'Joint Committee (JC) "
        "of the European Supervisory Authorities (ESAs)' for EIOPA climate docs.\n\n"
        "4. REGULATORY vs INSTRUMENT topics: When a document describes how "
        "insurers HOLD or REPORT certain investments (regulatory/accounting "
        "perspective), prefer the regulatory/accounting topic over the "
        "instrument-type topic.\n"
        "   Example: 'U.S. Insurer Investments in CMBS' from Capital Markets "
        "Bureau → prefer the Schedule BA/accounting topic over 'CMBS & RMBS'.\n"
        "   Example: NAIC reports on insurer mortgage fund holdings → 'Residential "
        "Mortgage Funds Under Schedule BA' not 'CMBS & RMBS'.\n\n"
        "5. U.K. REGULATORS: Documents from the Bank of England (BoE) or "
        "Prudential Regulation Authority (PRA) about solvency, capital, stress "
        "testing, or insurance regulation → 'U.K. Solvency' (the regulatory "
        "substance), NOT 'U.K. Bank of England (BoE)' (the organization).\n\n"
        "6. Use the organization path to disambiguate when two topics are close.\n"
        "- confidence is your certainty from 0 to 1."
    )
    user_msg = (
        "## Resource\n"
        f"- Title: {title}\n"
        f"- URL: {url[:120]}\n"
    )
    if notes:
        user_msg += f"- Notes: {notes[:300]}\n"
    user_msg += (
        f"- Organization path: {org_str}\n"
        f"- Section label: {label}\n\n"
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
        ], reasoning_effort="medium")
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

    # Resolve: exact match, then case-insensitive, then BBCode-stripped, then "The" stripped
    node_id = topic_candidates.get(raw_name)
    if not node_id:
        node_id = topic_candidates.get(raw_name.lower())
    if not node_id:
        cleaned = strip_bbcode(raw_name)
        node_id = topic_candidates.get(cleaned) or topic_candidates.get(cleaned.lower())
    if not node_id:
        # Strip leading "The " and try again
        no_the = re.sub(r"^The\s+", "", raw_name, flags=re.IGNORECASE)
        if no_the != raw_name:
            node_id = (
                topic_candidates.get(no_the)
                or topic_candidates.get(no_the.lower())
            )
            if node_id:
                # Update the reported name to match the canonical form
                result["topic_name"] = no_the

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


# ---------------------------------------------------------------------------
# Agenda item matching helpers
# ---------------------------------------------------------------------------

# Ref number extraction from resource Name — e.g. "SAPWG#2024-04" or "#2024-16"
_RE_REF_IN_NAME = re.compile(
    r"(?:(?:SAPWG|VOSTF|LATF|BWG|LRBCWG|RBC[-\s]?IRE|CATF|RAWG|SSWG)#?\s*"
    r"(?:Ref\s*#?\s*)?(\d{4}[-\u2013]\d{1,3}))"
    r"|(?:(?:Ref|Reference|Item)\s*#?\s*(\d{4}[-\u2013]\d{1,3}))"
    r"|(?:#(\d{4}[-\u2013]\d{1,3}))",
    re.IGNORECASE,
)

# Score thresholds for agenda item matching
AGENDA_ITEM_MATCH_THRESHOLD = float(os.environ.get("AGENDA_ITEM_MATCH_THRESHOLD", "1.5"))
AGENDA_ITEM_AI_CONFIDENCE_THRESHOLD = float(os.environ.get("AGENDA_ITEM_AI_CONFIDENCE_THRESHOLD", "0.7"))


def _extract_ref_numbers_from_name(name: str) -> list[str]:
    """Extract reference numbers from a resource Name string."""
    refs: list[str] = []
    for m in _RE_REF_IN_NAME.finditer(name or ""):
        ref = m.group(1) or m.group(2) or m.group(3)
        if ref:
            ref = ref.replace("\u2013", "-")
            if ref not in refs:
                refs.append(ref)
    return refs


def _normalize_ref(ref: str) -> str:
    """Normalize a ref number for comparison: strip group prefix, normalize separators."""
    ref = (ref or "").strip()
    ref = ref.replace("\u2013", "-")
    # Strip group prefix: "SAPWG#2024-04" -> "2024-04"
    m = re.search(r"(\d{4}[-]\d{1,3})", ref)
    return m.group(1) if m else ref.lower()


def _extract_all_normalized_refs(ref_field: str) -> list[str]:
    """Extract ALL YYYY-NN ref numbers from a (possibly multi-ref) Bubble field.

    Handles: "RBC-IRE-WG#2025-22", "Proposal 2025-22-IRE",
             "SAPWG#2019-21 and LRBCWG#2024-L8", "2025-22-IRE MOD"
    """
    ref_field = (ref_field or "").replace("\u2013", "-")
    return list(dict.fromkeys(m.group(0) for m in re.finditer(r"\d{4}-\d{1,3}", ref_field)))


# Stopwords for keyword extraction from resource names
_TITLE_STOPWORDS = frozenset({
    "a", "an", "the", "of", "on", "in", "to", "for", "and", "or", "by", "at",
    "its", "is", "be", "are", "was", "were", "with", "from", "as", "has", "had",
    "that", "this", "it", "not", "but", "all", "no", "do", "if", "so",
    # NAIC/meeting context noise words
    "meeting", "agenda", "committee", "task", "force", "working", "group",
    "november", "december", "january", "february", "march", "april", "may",
    "june", "july", "august", "september", "october",
    "2024", "2025", "2023", "2022", "2026",
    "naic", "national", "pm", "am", "et",
    "attachment", "updated",
})


def _extract_title_search_keywords(resource_name: str) -> list[str]:
    """Extract 2-3 distinctive keyword phrases from a resource name for title-based search.

    Strategy:
    1. Remove parenthetical codes (e.g., "(E)", "(EX)", "(A)")
    2. Remove common stopwords and context noise
    3. Return the 2-3 longest remaining words as individual search terms

    These are used as "text contains" queries against the BA title field.
    """
    name = (resource_name or "").strip()
    if not name:
        return []
    # Remove parenthetical codes like (E), (EX), (A), (G)
    name = re.sub(r"\([A-Z]{1,3}\)", "", name)
    # Remove ref patterns like #2024-16, SAPWG#2024-16
    name = re.sub(r"[A-Z]*#?\d{4}[-–]\d{1,3}", "", name)
    # Remove dates like "November 18, 2024" or "12/2024"
    name = re.sub(r"\b\d{1,2}/\d{2,4}\b", "", name)
    name = re.sub(r",?\s*\d{1,2}:\d{2}\s*(?:PM|AM|ET)\b", "", name, flags=re.IGNORECASE)
    # Split on non-alpha
    tokens = re.findall(r"[A-Za-z]+(?:[-][A-Za-z]+)*", name)
    # Filter stopwords and short tokens
    meaningful = [t for t in tokens if t.lower() not in _TITLE_STOPWORDS and len(t) >= 3]
    if not meaningful:
        return []
    # Return longest 3 tokens (most distinctive)
    meaningful.sort(key=lambda t: len(t), reverse=True)
    return meaningful[:3]


def _get_agenda_item_candidates(
    naic_group_node_id: str,
    bubble_snapshot: dict | None = None,
    *,
    fallback_ref_numbers: list[str] | None = None,
    resource_name: str = "",
    resource_id: str = "",
) -> tuple[list[dict], str]:
    """
    Fetch candidate Agenda Items, with multi-tier retrieval for incomplete linkage.

    Tier 0 (bidirectional): Agenda items whose Resources field links to this resource.
    Tier 1 (group-scoped): Agenda items where Discussed at (list) matches NAIC group.
    Tier 2 (ref fallback): Search by ref number if group didn't cover them.
    Tier 3 (title fallback): Search by BA title keywords from resource name.

    Returns (candidates_list, retrieval_source) where retrieval_source is
    "resource_linked" | "group_scoped" | "ref_fallback" | "title_fallback" | "none".
    """
    # --- Tier 0: Bidirectional lookup (highest priority) ---
    # Agenda items that directly link to this resource via their Resources field.
    # These are the strongest signal — they were explicitly linked in Bubble.
    resource_linked: list[dict] = []
    if resource_id and not bubble_snapshot:
        resource_linked = lookups.search_agenda_items_by_resource(resource_id)
        if resource_linked:
            for item in resource_linked:
                item["__retrieval_source"] = "resource_linked"
            log.info(
                "agenda item bidirectional: %d item(s) directly link to resource %s",
                len(resource_linked), resource_id[:20],
            )

    # --- Tier 1: group-scoped retrieval ---
    group_candidates: list[dict] = []
    if naic_group_node_id:
        if bubble_snapshot:
            all_items = bubble_snapshot.get("agenda_items") or []
            for item in all_items:
                discussed = item.get("Discussed at list") or item.get("Discussed at")
                if discussed == naic_group_node_id:
                    group_candidates.append(item)
                elif isinstance(discussed, list) and naic_group_node_id in discussed:
                    group_candidates.append(item)
        else:
            group_candidates = lookups.search_agenda_items_by_naic_group(naic_group_node_id)

    # Check if group candidates have any ref overlap with the requested refs
    # (if so, the primary retrieval is sufficient — skip ref fallback)
    group_has_ref_overlap = False
    if group_candidates and fallback_ref_numbers:
        ref_norms_wanted = set(fallback_ref_numbers)
        for item in group_candidates:
            item_refs = set(_extract_all_normalized_refs(_agenda_item_ref(item)))
            if item_refs & ref_norms_wanted:
                group_has_ref_overlap = True
                break

    # --- Collect supplemental candidates from fallback strategies ---
    seen_ids: set[str] = {str(_obj_id(c)) for c in group_candidates if _obj_id(c)}
    supplemental: list[dict] = []
    retrieval_tag = "group_scoped"

    # Fallback 1: ref-based retrieval (only if group didn't cover the refs)
    if fallback_ref_numbers and not group_has_ref_overlap:
        ref_candidates: list[dict] = []
        if bubble_snapshot:
            all_items = bubble_snapshot.get("agenda_items") or []
            for item in all_items:
                iid = str(_obj_id(item) or "")
                if iid in seen_ids:
                    continue
                item_refs = set(_extract_all_normalized_refs(_agenda_item_ref(item)))
                if item_refs & set(fallback_ref_numbers):
                    ref_candidates.append(item)
                    seen_ids.add(iid)
        else:
            for ref_num in fallback_ref_numbers[:5]:  # bounded: max 5 ref queries
                results = lookups.search_agenda_items_by_ref(ref_num)
                for item in results:
                    iid = str(_obj_id(item) or "")
                    if iid not in seen_ids:
                        ref_candidates.append(item)
                        seen_ids.add(iid)

        if ref_candidates:
            log.info(
                "agenda item fallback: %d candidate(s) found via ref search "
                "(refs: %s), supplementing %d group-scoped candidate(s)",
                len(ref_candidates), ", ".join(fallback_ref_numbers[:5]),
                len(group_candidates),
            )
            for item in ref_candidates:
                item["__retrieval_source"] = "ref_fallback"
            supplemental.extend(ref_candidates)
            retrieval_tag = "ref_fallback"

    # Fallback 2: title-based retrieval
    # Always runs to find sparse items (no Discussed at, no numeric refs) that
    # share keyword overlap with the resource name.
    title_keywords = _extract_title_search_keywords(resource_name) if resource_name else []
    if title_keywords:
        title_candidates: list[dict] = []
        if bubble_snapshot:
            all_items = bubble_snapshot.get("agenda_items") or []
            for item in all_items:
                iid = str(_obj_id(item) or "")
                if iid in seen_ids:
                    continue
                ba_title = (_agenda_item_title(item) or "").lower()
                if any(kw.lower() in ba_title for kw in title_keywords):
                    title_candidates.append(item)
                    seen_ids.add(iid)
        else:
            for kw in title_keywords:
                results = lookups.search_agenda_items_by_title(kw)
                for item in results:
                    iid = str(_obj_id(item) or "")
                    if iid not in seen_ids:
                        title_candidates.append(item)
                        seen_ids.add(iid)

        if title_candidates:
            log.info(
                "agenda item fallback: %d candidate(s) found via title search "
                "(keywords: %s), supplementing %d existing candidate(s)",
                len(title_candidates), ", ".join(title_keywords),
                len(group_candidates) + len(supplemental),
            )
            for item in title_candidates:
                item["__retrieval_source"] = "title_fallback"
            supplemental.extend(title_candidates)
            if retrieval_tag == "group_scoped":
                retrieval_tag = "title_fallback"

    # Combine and return
    # Resource-linked items are prepended (highest priority) and deduplicated
    all_candidates: list[dict] = []
    final_seen: set[str] = set()

    # Tier 0 first (resource-linked)
    if resource_linked:
        for item in resource_linked:
            iid = str(_obj_id(item) or "")
            if iid and iid not in final_seen:
                all_candidates.append(item)
                final_seen.add(iid)

    # Then group-scoped
    for item in group_candidates:
        iid = str(_obj_id(item) or "")
        if iid and iid not in final_seen:
            item.setdefault("__retrieval_source", "group_scoped")
            all_candidates.append(item)
            final_seen.add(iid)

    # Then supplemental (ref + title fallbacks)
    for item in supplemental:
        iid = str(_obj_id(item) or "")
        if iid and iid not in final_seen:
            all_candidates.append(item)
            final_seen.add(iid)

    if resource_linked:
        return all_candidates, "resource_linked"
    if supplemental:
        return all_candidates, retrieval_tag
    if group_candidates:
        return all_candidates, "group_scoped"
    return [], "none"


# Known ref prefix → keywords that should appear in the NAIC group name.
# Used to detect cross-group ref matches in fallback retrieval.
_REF_PREFIX_GROUP_KEYWORDS: dict[str, list[str]] = {
    "sapwg": ["statutory", "accounting"],
    "rbc-ire-wg": ["risk-based", "capital", "investment", "risk"],
    "rbc-ire": ["risk-based", "capital", "investment", "risk"],
    "bwg": ["blanks"],
    "lrbcwg": ["life", "risk-based", "capital"],
    "vostf": ["valuation", "securities"],
    "latf": ["life", "actuarial"],
    "catf": ["capital", "adequacy"],
    "rawg": ["risk", "assessment"],
    "sswg": ["structured", "securities"],
}

# Regex to extract group prefix from a ref field: "RBC-IRE-WG#2025-22" -> "RBC-IRE-WG"
_RE_REF_PREFIX = re.compile(
    r"^([A-Z][A-Z0-9]*(?:-[A-Z][A-Z0-9]*)*(?:#|-WG#?))",
    re.IGNORECASE,
)


def _extract_ref_prefix(ref_field: str) -> str:
    """Extract group abbreviation prefix from a ref string.

    "RBC-IRE-WG#2025-22" -> "rbc-ire-wg"
    "SAPWG#2025-22"      -> "sapwg"
    "2025-22"            -> ""
    """
    ref_field = (ref_field or "").strip()
    m = _RE_REF_PREFIX.match(ref_field)
    if not m:
        return ""
    prefix = m.group(1).rstrip("#").lower()
    return prefix


def _ref_prefix_matches_group(ref_prefix: str, naic_group_name: str) -> bool:
    """Check if a ref prefix is compatible with the resolved NAIC group name."""
    if not ref_prefix or not naic_group_name:
        return True  # no prefix to check — assume compatible
    prefix_lower = ref_prefix.lower().rstrip("#")
    group_lower = naic_group_name.lower()

    # Direct substring check: "sapwg" in group name, or group abbreviation tokens
    if prefix_lower in group_lower:
        return True

    # Check known prefix → keyword mapping
    keywords = _REF_PREFIX_GROUP_KEYWORDS.get(prefix_lower, [])
    if keywords:
        return any(kw in group_lower for kw in keywords)

    # Heuristic: tokenize the prefix and check overlap with group name tokens
    prefix_tokens = {t for t in re.split(r"[-_\s]+", prefix_lower) if len(t) >= 2}
    group_tokens = {t for t in re.split(r"[-_\s()\[\]]+", group_lower) if len(t) >= 2}
    if prefix_tokens and prefix_tokens & group_tokens:
        return True

    return False


# Penalty applied to cross-group ref matches from fallback retrieval
_CROSS_GROUP_REF_PENALTY = 2.5


def _agenda_item_ref(item: dict) -> str:
    """Extract the canonical ref number from an Agenda Item."""
    ref = (item.get("BA Ref #") or item.get("Ref #") or "").strip()
    return ref


def _agenda_item_title(item: dict) -> str:
    """Extract the best title from an Agenda Item."""
    return (item.get("BA title") or item.get("NAIC Title") or "").strip()


def _tokenize_for_matching(text: str) -> set[str]:
    """Tokenize a string for overlap scoring. Lowercase, >=2 chars."""
    return {t.lower() for t in re.split(r"[^\w]+", text) if t and len(t) >= 2}


def _score_agenda_item_match(
    item: dict,
    resource_name: str,
    pdf_signals: dict | None,
    naic_group_name: str = "",
) -> tuple[float, dict[str, Any]]:
    """
    Deterministic ref-based scoring for agenda item matching (Tier 1).

    Only scores based on unambiguous ref number matches:
    - Ref # match from resource Name: +3.0
    - Ref # match from PDF text: +3.0
    - Cross-group penalty for ref_fallback items with wrong prefix: -2.5

    Title/content matching is handled by the LLM tier (Tier 2).

    Returns (score, evidence_dict).
    """
    evidence: dict[str, Any] = {
        "agenda_item_id": _obj_id(item),
        "ba_title": _agenda_item_title(item)[:80],
        "ref": _agenda_item_ref(item),
    }
    score = 0.0
    item_ref = _agenda_item_ref(item)
    # Extract ALL normalized refs from the Bubble field (handles multi-ref fields,
    # suffixed refs like "2025-22-IRE", group prefixes like "RBC-IRE-WG#2025-22")
    item_ref_norms = set(_extract_all_normalized_refs(item_ref)) if item_ref else set()

    # 1. Ref # match from resource Name
    name_refs = _extract_ref_numbers_from_name(resource_name)
    if item_ref_norms and name_refs:
        name_ref_norms = {_normalize_ref(r) for r in name_refs}
        matched_refs = item_ref_norms & name_ref_norms
        if matched_refs:
            score += 3.0
            evidence["ref_match_source"] = "resource_name"
            evidence["ref_matched"] = sorted(matched_refs)[0]

    # 2. Ref # match from PDF signals
    if item_ref_norms and pdf_signals:
        pdf_refs = pdf_signals.get("ref_numbers") or []
        pdf_ref_norms = {_normalize_ref(r) for r in pdf_refs}
        matched_refs = item_ref_norms & pdf_ref_norms
        if matched_refs and evidence.get("ref_match_source") != "resource_name":
            score += 3.0
            evidence["ref_match_source"] = "pdf_text"
            evidence["ref_matched"] = sorted(matched_refs)[0]

    # 3. Cross-group penalty: if item came from ref_fallback and its ref prefix
    # doesn't match the resolved NAIC group, apply a heavy penalty
    is_fallback = item.get("__retrieval_source") == "ref_fallback"
    if is_fallback and score > 0 and naic_group_name and item_ref:
        ref_prefix = _extract_ref_prefix(item_ref)
        if ref_prefix and not _ref_prefix_matches_group(ref_prefix, naic_group_name):
            score -= _CROSS_GROUP_REF_PENALTY
            evidence["cross_group_penalty"] = _CROSS_GROUP_REF_PENALTY
            evidence["ref_prefix"] = ref_prefix
            evidence["group_match"] = False
        else:
            evidence["group_match"] = True

    evidence["total_score"] = round(score, 2)
    return (score, evidence)


def _collect_inherited_topics(items: list[dict]) -> list[str]:
    """Extract unique topic IDs from a list of matched Agenda Items."""
    inherited: list[str] = []
    for item in items:
        topics = item.get("Topics") or []
        if isinstance(topics, list):
            for t in topics:
                tid = t if isinstance(t, str) else (t.get("_id") or t.get("id") if isinstance(t, dict) else None)
                if tid and tid not in inherited:
                    inherited.append(tid)
    return inherited


def _resolve_agenda_items_for_resource(
    resource: dict,
    context: dict,
    naic_group_node_id: str | None,
    bubble_snapshot: dict | None,
    *,
    use_ai: bool = False,
    _chat_fn=None,
) -> dict[str, Any]:
    """
    Two-tier agenda item matching:

    Tier 1 (deterministic): Match by ref number. Unambiguous — if the PDF
    contains ref #2025-22 and a candidate has RBC-IRE-WG#2025-22, that's a
    match. Cross-group refs are penalized.

    Tier 2 (LLM): For remaining group-scoped candidates not matched by ref,
    ask the LLM to decide which (if any) relate to this resource. The LLM
    sees the full context: resource name, PDF numbered items, candidate titles.

    Returns dict:
        matched_ids: list[str]       - IDs of matched Agenda Items
        candidates: list[dict]       - all scored candidates with evidence
        retrieval_source: str
        method: str                  - "ref_match" | "ref_match+ai" | "ai" | "none"
        ai_used: bool
        inherited_topic_ids: list[str]
    """
    empty_result: dict[str, Any] = {
        "matched_ids": [],
        "candidates": [],
        "retrieval_source": "none",
        "method": "none",
        "ai_used": False,
        "inherited_topic_ids": [],
    }

    resource_id = str(resource.get("_id") or resource.get("id") or "").strip()
    resource_name = (resource.get("Name") or "").strip()

    if not naic_group_node_id and not resource_id:
        return empty_result

    pdf_signals = resource.get("__pdf_agenda_signals") if isinstance(resource.get("__pdf_agenda_signals"), dict) else None

    # Collect all known ref numbers for fallback retrieval
    all_ref_numbers: list[str] = []
    name_refs = _extract_ref_numbers_from_name(resource_name)
    all_ref_numbers.extend(_normalize_ref(r) for r in name_refs)
    if pdf_signals:
        for r in (pdf_signals.get("ref_numbers") or []):
            nr = _normalize_ref(r)
            if nr and nr not in all_ref_numbers:
                all_ref_numbers.append(nr)

    # Fetch candidate agenda items (bidirectional + group-scoped + fallbacks)
    raw_candidates, retrieval_source = _get_agenda_item_candidates(
        naic_group_node_id or "", bubble_snapshot,
        fallback_ref_numbers=all_ref_numbers or None,
        resource_name=resource_name,
        resource_id=resource_id,
    )
    if not raw_candidates:
        return empty_result

    # Resolve NAIC group name for cross-group detection
    naic_group_name = (context.get("label") or "").strip()
    if not naic_group_name and pdf_signals:
        naic_group_name = (pdf_signals.get("group_name_hint") or "").strip()

    # --- Tier 1: Deterministic ref matching ---
    scored: list[tuple[dict, float, dict]] = []
    for item in raw_candidates:
        score, evidence = _score_agenda_item_match(
            item, resource_name, pdf_signals, naic_group_name=naic_group_name,
        )
        evidence["retrieval_source"] = item.get("__retrieval_source", "group_scoped")
        scored.append((item, score, evidence))

    scored.sort(key=lambda x: x[1], reverse=True)

    # Build candidates detail for debug
    candidates_detail = [
        {
            "id": _obj_id(item) or "",
            "ba_title": _agenda_item_title(item)[:80],
            "ref": _agenda_item_ref(item),
            "score": round(score, 2),
            "retrieval_source": evidence.get("retrieval_source", "group_scoped"),
            "evidence": evidence,
        }
        for item, score, evidence in scored
    ]

    result = dict(empty_result)
    result["candidates"] = candidates_detail
    result["retrieval_source"] = retrieval_source

    # --- Tier 0: Resource-linked items are automatic matches ---
    # These items explicitly link to this resource in Bubble — highest confidence.
    resource_linked_matched: list[dict] = []
    resource_linked_ids: set[str] = set()
    for item in raw_candidates:
        if item.get("__retrieval_source") == "resource_linked":
            iid = str(_obj_id(item) or "")
            if iid:
                resource_linked_matched.append(item)
                resource_linked_ids.add(iid)

    # --- Tier 1: Deterministic ref matching ---
    ref_matched: list[dict] = []
    ref_matched_ids: set[str] = set()
    for item, score, ev in scored:
        iid = str(_obj_id(item) or "")
        if score >= AGENDA_ITEM_MATCH_THRESHOLD and iid not in resource_linked_ids:
            if iid:
                ref_matched.append(item)
                ref_matched_ids.add(iid)

    already_matched_ids = resource_linked_ids | ref_matched_ids

    # --- Multi-ref dampening ---
    # If ref matching produced 10+ results, this is likely an omnibus document.
    # Don't trust ref matches for topic inheritance — too noisy.
    is_omnibus = len(ref_matched) >= 10
    if is_omnibus:
        log.info(
            "agenda item multi-ref dampening: %d ref matches detected for '%s' "
            "— treating as omnibus document, skipping ref-based topic inheritance",
            len(ref_matched), resource_name[:60],
        )

    # --- Tier 2: LLM for remaining candidates (group-scoped + title fallback) ---
    ai_matched: list[dict] = []
    ai_evidence: dict = {}
    _AI_ELIGIBLE_SOURCES = {"group_scoped", "title_fallback"}
    remaining_for_ai = [
        item for item in raw_candidates
        if str(_obj_id(item) or "") not in already_matched_ids
        and item.get("__retrieval_source", "group_scoped") in _AI_ELIGIBLE_SOURCES
    ]

    if use_ai and remaining_for_ai:
        ai_result = _resolve_agenda_items_ai(
            resource, context, remaining_for_ai, pdf_signals, _chat_fn
        )
        ai_evidence = ai_result.get("evidence", {})
        if ai_result.get("matched_ids"):
            ai_matched_ids_by_id = {str(_obj_id(item)): item for item in remaining_for_ai if _obj_id(item)}
            for mid in ai_result["matched_ids"]:
                item = ai_matched_ids_by_id.get(mid)
                if item:
                    ai_matched.append(item)

    # Combine results — resource-linked first (highest priority)
    # For omnibus docs, only use resource-linked + AI matches for topic inheritance
    # (ref matches are too noisy for topic but still valid as agenda matches)
    all_matched = resource_linked_matched + ref_matched + ai_matched

    if is_omnibus:
        # For topic inheritance, only use resource-linked + AI matches (not noisy ref matches)
        topic_source_items = resource_linked_matched + ai_matched
    else:
        topic_source_items = all_matched

    if all_matched:
        result["matched_ids"] = [str(_obj_id(item)) for item in all_matched if _obj_id(item)]
        result["inherited_topic_ids"] = _collect_inherited_topics(topic_source_items) if topic_source_items else []
        result["ai_used"] = bool(ai_matched)
        if ai_evidence:
            result["ai_evidence"] = ai_evidence

        if resource_linked_matched:
            result["method"] = "resource_linked"
        elif ref_matched and ai_matched:
            result["method"] = "ref_match+ai"
        elif ref_matched:
            result["method"] = "ref_match"
        else:
            result["method"] = "ai"

    return result


def _resolve_agenda_items_ai(
    resource: dict,
    context: dict,
    candidates: list[dict],
    pdf_signals: dict | None,
    _chat_fn=None,
) -> dict[str, Any]:
    """
    Ask AI to rank Agenda Item candidates for a resource.

    Returns dict with:
        matched_ids: list[str]
        evidence: dict
    """
    result: dict[str, Any] = {"matched_ids": [], "evidence": {}}

    if _chat_fn is None:
        try:
            from bubble.openai_client import chat_json
            _chat_fn = chat_json
        except ImportError:
            log.warning("agenda item AI: openai_client not available")
            return result

    title = (resource.get("Name") or "").strip()
    url = (resource.get("URL") or "").strip()
    notes = (resource.get("notes") or "").strip()
    parent = (resource.get("parent") or "").strip()

    # Build candidate descriptions
    candidate_lines: list[str] = []
    id_map: dict[str, str] = {}
    for i, item in enumerate(candidates[:20]):
        aid = _obj_id(item) or ""
        ref = _agenda_item_ref(item)
        ba_title = _agenda_item_title(item)
        desc = (item.get("Description") or "")[:200]
        label = f"[{i+1}]"
        id_map[label] = str(aid)
        id_map[str(i + 1)] = str(aid)
        candidate_lines.append(f"{label} Ref: {ref} | Title: {ba_title} | Desc: {desc}")

    pdf_context = ""
    if pdf_signals:
        refs = pdf_signals.get("ref_numbers") or []
        items = (pdf_signals.get("numbered_items") or [])[:15]
        struct = pdf_signals.get("structure_type") or ""
        if refs:
            pdf_context += f"\nPDF ref numbers found: {', '.join(refs)}"
        if items:
            pdf_context += f"\nPDF numbered agenda items extracted from document:"
            for i, itm in enumerate(items, 1):
                pdf_context += f"\n  {i}. {itm}"
        if struct:
            pdf_context += f"\nPDF structure type: {struct}"

    group_name = (context.get("label") or "").strip()
    org_path = context.get("org_path") or []
    org_str = " › ".join(str(s) for s in org_path) if org_path else ""
    system_msg = (
        "You are matching a regulatory/insurance document to agenda items.\n"
        "The document belongs to the organization/committee shown below.\n"
        "You will see the document's title, URL, organization path, and any "
        "extracted PDF content (numbered agenda items).\n\n"
        "Some agenda items may have already been matched by reference number. "
        "You are evaluating the REMAINING candidates that could not be matched "
        "by ref number alone.\n\n"
        "Match based on:\n"
        "- STRONG title similarity between the resource and the agenda item's "
        "BA title (keywords or acronyms in common)\n"
        "- Clear topical alignment (the document directly discusses the agenda "
        "item's specific subject matter)\n"
        "- Organization context (prefer items from the same group/committee)\n\n"
        "BE STRICT: Only match when there is CLEAR, DIRECT topical or title "
        "alignment. Do NOT match based on vague thematic similarity.\n"
        "- A document about 'Prudent Person Principle' matches 'BMA Request "
        "for Comment on Prudent Person Principle' (direct topic match).\n"
        "- A 'Federal Advisory Committee on Insurance Agenda' matches an "
        "agenda item about 'FACI' or 'FIO' (same committee), NOT an unrelated "
        "IAIS item even if international topics appear on the agenda.\n"
        "- 'SAPWG Adoptions' should match agenda items about SAPWG statutory "
        "accounting, NOT unrelated items that happen to be in the same group.\n\n"
        "Respond with ONLY a JSON object:\n"
        '{"matches": [<number>], "confidence": <0.0-1.0>}\n'
        "where matches is a list of candidate numbers (e.g. [1] or [1, 3]),\n"
        'or {"matches": [], "confidence": 0} if none fit.'
    )
    user_msg = (
        f"## Organization\n"
        f"- Group: {group_name}\n"
        f"- Path: {org_str}\n\n"
        f"## Resource (document)\n"
        f"- Title: {title}\n"
        f"- URL: {url[:120]}\n"
    )
    if notes:
        user_msg += f"- Notes: {notes[:300]}\n"
    user_msg += f"{pdf_context}\n\n"
    user_msg += f"## Remaining Agenda Item Candidates\n"
    for line in candidate_lines:
        user_msg += f"{line}\n"

    try:
        data = _chat_fn([
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ], reasoning_effort="medium")
    except Exception:
        log.warning("agenda item AI: call failed for resource %s", title[:60], exc_info=True)
        return result

    if not isinstance(data, dict):
        return result

    matches = data.get("matches") or []
    confidence = 0.0
    try:
        confidence = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0

    result["evidence"] = {
        "ai_matches": matches,
        "ai_confidence": confidence,
        "candidates_sent": len(candidates[:20]),
    }

    if confidence < AGENDA_ITEM_AI_CONFIDENCE_THRESHOLD:
        return result

    matched_ids: list[str] = []
    for match_ref in matches:
        ref_str = str(match_ref)
        aid = id_map.get(ref_str) or id_map.get(f"[{ref_str}]")
        if aid and aid not in matched_ids:
            matched_ids.append(aid)
    result["matched_ids"] = matched_ids
    return result


# ---------------------------------------------------------------------------
# Enhanced topic suggestion helpers
# ---------------------------------------------------------------------------


def _parse_calendar_title_topics(title: str) -> list[str]:
    """
    Parse topic names from a calendar item title.

    Calendar titles follow: "NAIC {GROUP} | Topic1; Topic2; Topic3"
    Returns list of topic name strings (the semicolon-separated part after |).
    """
    if not title or "|" not in title:
        return []
    # Take everything after the first "|"
    _, _, topic_part = title.partition("|")
    topic_part = topic_part.strip()
    if not topic_part:
        return []
    topics = [t.strip() for t in topic_part.split(";") if t.strip()]
    return topics


def _fuzzy_match_topic_to_candidates(
    parsed_topics: list[str],
    topic_candidates: dict[str, str],
) -> list[tuple[str, str]]:
    """
    Match parsed topic strings against Chronicles tree candidate names.

    Returns list of (topic_name, node_id) for matches found.
    Uses case-insensitive substring matching and token overlap.
    """
    if not parsed_topics or not topic_candidates:
        return []

    # Build lowercase → (original_name, node_id) lookup
    candidates_lower: dict[str, tuple[str, str]] = {}
    for name, nid in topic_candidates.items():
        nl = name.lower()
        if nl not in candidates_lower:
            candidates_lower[nl] = (name, nid)

    matches: list[tuple[str, str]] = []
    seen_ids: set[str] = set()

    for parsed in parsed_topics:
        parsed_lower = parsed.lower().strip()
        if not parsed_lower:
            continue

        # Exact match (case-insensitive)
        if parsed_lower in candidates_lower:
            name, nid = candidates_lower[parsed_lower]
            if nid not in seen_ids:
                matches.append((name, nid))
                seen_ids.add(nid)
            continue

        # Substring match: parsed topic contained in candidate name or vice versa
        best_match: tuple[str, str] | None = None
        best_overlap = 0
        parsed_tokens = _tokenize_for_matching(parsed)
        for cand_lower, (name, nid) in candidates_lower.items():
            if nid in seen_ids:
                continue
            # Substring check
            if parsed_lower in cand_lower or cand_lower in parsed_lower:
                if nid not in seen_ids:
                    matches.append((name, nid))
                    seen_ids.add(nid)
                    best_match = None  # already matched
                    break
            # Token overlap
            cand_tokens = _tokenize_for_matching(name)
            overlap = len(parsed_tokens & cand_tokens)
            if overlap > best_overlap and overlap >= 2:
                best_overlap = overlap
                best_match = (name, nid)

        if best_match and best_match[1] not in seen_ids:
            matches.append(best_match)
            seen_ids.add(best_match[1])

    return matches


# Placeholder / meta topics that should not count as real content topics
_PLACEHOLDER_TOPIC_PATTERNS = re.compile(
    r"^calendar\s+events?\s+with\s+no\s+topic$"
    r"|^no\s+topic$"
    r"|^unassigned$"
    r"|^placeholder$"
    r"|^other$"
    r"|agenda\s+not\s+yet\s+posted"
    r"|not\s+covered\s+in\s+.*chronicles",
    re.IGNORECASE,
)


def _is_placeholder_topic(name: str) -> bool:
    """Return True if the topic name is a placeholder/meta entry, not real content."""
    return bool(_PLACEHOLDER_TOPIC_PATTERNS.search((name or "").strip()))


def _resolve_topic_enhanced(
    resource: dict,
    context: dict,
    topic_candidates: dict[str, str],
    *,
    matched_agenda_items_result: dict | None = None,
    calendar_payload: list[dict] | None = None,
    calendar_context: list[dict] | None = None,
    linked_calendar_ids: list[str] | None = None,
    use_ai: bool = False,
    _chat_fn=None,
) -> dict[str, Any]:
    """
    Enhanced topic suggestion that uses multiple signal sources:

    1. Inherit from matched agenda items (highest priority)
    2. Parse calendar item title topics (secondary)
    3. AI classification (existing fallback)

    Returns dict:
        topic_id: str | None
        topic_name: str | None
        source: str  - "agenda_item_inheritance" | "calendar_title_parse" | "ai_classification" | "unresolved"
        inherited_topic_ids: list[str]
        calendar_title_topics: list[str]
        ai_result: dict | None
    """
    result: dict[str, Any] = {
        "topic_id": None,
        "topic_name": None,
        "source": "unresolved",
        "inherited_topic_ids": [],
        "calendar_title_topics": [],
        "ai_result": None,
    }

    # Path A: Inherit from matched agenda items
    if matched_agenda_items_result:
        inherited_ids = matched_agenda_items_result.get("inherited_topic_ids") or []
        # Filter out placeholder topics
        real_inherited_ids: list[str] = []
        for tid in inherited_ids:
            # Look up the name to check if it's a placeholder
            tname = None
            for name, nid in topic_candidates.items():
                if nid == tid and name == name.strip():
                    tname = name
                    break
            if tname and _is_placeholder_topic(tname):
                continue
            real_inherited_ids.append(tid)

        if real_inherited_ids:
            result["inherited_topic_ids"] = real_inherited_ids
            if len(real_inherited_ids) == 1:
                chosen_id = real_inherited_ids[0]
            elif use_ai and len(real_inherited_ids) > 1:
                # Multiple inherited topics — use AI to disambiguate
                ai_result = _resolve_topic_suggestion_ai(resource, context, topic_candidates, _chat_fn)
                if ai_result.get("node_id") and ai_result["node_id"] in real_inherited_ids:
                    chosen_id = ai_result["node_id"]
                else:
                    chosen_id = real_inherited_ids[0]
            else:
                chosen_id = real_inherited_ids[0]

            # Find the topic name from candidates
            for name, nid in topic_candidates.items():
                if nid == chosen_id and name == name.strip():
                    result["topic_id"] = chosen_id
                    result["topic_name"] = name
                    result["source"] = "agenda_item_inheritance"
                    return result
            # ID found but name not in candidates — still use the ID
            result["topic_id"] = chosen_id
            result["source"] = "agenda_item_inheritance"
            return result

    # Path B: Parse calendar item title topics
    if linked_calendar_ids and calendar_payload:
        for cal in calendar_payload:
            cal_id = cal.get("_id") or cal.get("id")
            if cal_id and str(cal_id) in [str(x) for x in linked_calendar_ids]:
                cal_title = (cal.get("title") or "").strip()
                parsed_topics = _parse_calendar_title_topics(cal_title)
                if parsed_topics:
                    result["calendar_title_topics"] = parsed_topics
                    matched = _fuzzy_match_topic_to_candidates(parsed_topics, topic_candidates)
                    # Filter out placeholder/meta topics that aren't real content topics
                    real_matches = [
                        (n, i) for n, i in matched
                        if not _is_placeholder_topic(n)
                    ]
                    if len(real_matches) == 1:
                        name, nid = real_matches[0]
                        result["topic_id"] = nid
                        result["topic_name"] = name
                        result["source"] = "calendar_title_parse"
                        return result
                    elif len(real_matches) > 1:
                        # Multiple real matches — ambiguous, don't auto-assign
                        result["calendar_title_matched"] = [(n, i) for n, i in real_matches]
                    elif not real_matches and matched:
                        # Only placeholder topics matched — still ambiguous
                        result["calendar_title_matched"] = [(n, i) for n, i in matched]
                break  # only check the first linked calendar item

    # Path C: AI classification (existing logic)
    if use_ai and topic_candidates:
        ai_result = _resolve_topic_suggestion_ai(resource, context, topic_candidates, _chat_fn)
        result["ai_result"] = ai_result
        if ai_result.get("node_id"):
            ai_topic_name = ai_result.get("topic_name") or ""
            # Reject placeholder topics from AI
            if _is_placeholder_topic(ai_topic_name):
                log.info("topic AI returned placeholder '%s' — rejecting", ai_topic_name)
            else:
                result["topic_id"] = ai_result["node_id"]
                result["topic_name"] = ai_topic_name
                result["source"] = "ai_classification"
                return result

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

    # --- Related calendar items: primary strategy uses NAIC group tree node ---
    # (moved before topic suggestion so calendar IDs are available for topic parsing)
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

    # --- Agenda Item matching (deterministic + AI fallback) ---
    agenda_match_resolved = 0
    agenda_match_unresolved = 0
    # Cache of NAIC group node ID per resource (reuse from calendar linking)
    for i, r in enumerate(updated_resources):
        ctx = resource_context[i] if i < len(resource_context) else {}

        # Resolve NAIC group for this resource (same logic as calendar linking)
        org_path = ctx.get("org_path") or []
        label = (ctx.get("label") or "").strip()
        path = infer_naic_group_path(org_path)
        if label:
            path = path + [label]
        naic_group_node_id = None
        if path:
            naic_group_node_id, _ = _resolve_naic_group_node(naic_group_tree_name, path, bubble_snapshot)

        agenda_result = _resolve_agenda_items_for_resource(
            r, ctx, naic_group_node_id, bubble_snapshot,
            use_ai=use_ai,
        )

        # Store debug artifact on resource
        r["__agenda_match"] = {
            "matched_ids": agenda_result["matched_ids"],
            "method": agenda_result["method"],
            "ai_used": agenda_result["ai_used"],
            "inherited_topic_ids": agenda_result["inherited_topic_ids"],
            "candidates": agenda_result["candidates"][:10],
        }

        if agenda_result["matched_ids"]:
            agenda_match_resolved += 1
            record_resolution(
                "agenda_item_matching",
                "agenda_items",
                chosen_ids=agenda_result["matched_ids"],
                candidates=[c["id"] for c in agenda_result["candidates"] if c.get("id")],
                status="resolved",
                evidence={
                    "method": agenda_result["method"],
                    "ai_used": agenda_result["ai_used"],
                    "inherited_topic_ids": agenda_result["inherited_topic_ids"],
                },
                target="Resource", index=i,
            )
        else:
            agenda_match_unresolved += 1
            record_resolution(
                "agenda_item_matching",
                "agenda_items",
                chosen_ids=[],
                candidates=[c["id"] for c in agenda_result["candidates"] if c.get("id")],
                status="no_match",
                evidence={
                    "method": agenda_result["method"],
                    "naic_group_node_id": naic_group_node_id,
                },
                target="Resource", index=i,
            )
    log.info("Agenda item matching summary: resolved=%d  unresolved=%d",
             agenda_match_resolved, agenda_match_unresolved)

    # --- Enhanced topic suggestion (agenda item inheritance > calendar title > AI) ---
    topic_candidates = _build_topic_candidates(topic_tree_name, bubble_snapshot)
    topic_resolved_count = 0
    topic_unresolved_count = 0

    if topic_candidates:
        # Build a combined calendar payload + snapshot calendar items for title parsing
        all_calendar = list(updated_calendar)
        if bubble_snapshot:
            for sc in (bubble_snapshot.get("calendar_items") or []):
                sc_id = sc.get("_id") or sc.get("id")
                if sc_id and sc_id not in {(c.get("_id") or c.get("id")) for c in all_calendar}:
                    all_calendar.append(sc)

        for i, r in enumerate(updated_resources):
            ctx = resource_context[i] if i < len(resource_context) else {}
            agenda_match = r.get("__agenda_match") if isinstance(r.get("__agenda_match"), dict) else None
            linked_cal_ids = r.get("Related calendar items") or []

            topic_result = _resolve_topic_enhanced(
                r, ctx, topic_candidates,
                matched_agenda_items_result=agenda_match,
                calendar_payload=all_calendar,
                calendar_context=calendar_context,
                linked_calendar_ids=linked_cal_ids,
                use_ai=use_ai,
            )

            topic_id = topic_result.get("topic_id")
            topic_name = topic_result.get("topic_name")
            source = topic_result.get("source", "unresolved")

            # Store debug artifact
            r["__topic_suggestion"] = {
                "source": source,
                "topic_id": topic_id,
                "topic_name": topic_name,
                "inherited_topic_ids": topic_result.get("inherited_topic_ids", []),
                "calendar_title_topics": topic_result.get("calendar_title_topics", []),
                "ai_result": {
                    "topic_name": (topic_result.get("ai_result") or {}).get("topic_name"),
                    "confidence": (topic_result.get("ai_result") or {}).get("confidence"),
                    "status": (topic_result.get("ai_result") or {}).get("status"),
                } if topic_result.get("ai_result") else None,
            }

            evidence = {
                "method": source,
                "topic_name": topic_name,
                "inherited_topic_ids": topic_result.get("inherited_topic_ids", []),
                "calendar_title_topics": topic_result.get("calendar_title_topics", []),
            }

            if topic_id:
                r["topic suggestion"] = topic_id
                topic_resolved_count += 1
                log.info("topic suggestion: '%s' via %s -> node_id=%s",
                         topic_name or "?", source, topic_id)
                record_resolution(
                    "topic_suggestion", "topic suggestion",
                    chosen_ids=[topic_id],
                    candidates=list(topic_candidates.keys())[:10],
                    status="resolved",
                    evidence=evidence,
                    target="Resource", index=i,
                )
            else:
                topic_unresolved_count += 1
                record_resolution(
                    "topic_suggestion", "topic suggestion",
                    chosen_ids=[],
                    candidates=list(topic_candidates.keys())[:10],
                    status=source if source != "unresolved" else "no_match",
                    evidence=evidence,
                    target="Resource", index=i,
                )
    else:
        log.warning("topic suggestion: no candidates loaded from tree '%s'; skipping.", topic_tree_name)
        topic_unresolved_count = len(updated_resources)
    log.info("topic suggestion summary: resolved=%d  unresolved=%d", topic_resolved_count, topic_unresolved_count)

    return (updated_resources, updated_calendar)
