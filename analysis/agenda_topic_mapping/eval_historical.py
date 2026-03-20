#!/usr/bin/env python3
"""
Historical evaluation harness for agenda item matching and topic suggestion.

Runs the production inference logic on a bounded sample of Bubble resources
with known agenda item and/or topic assignments, then compares predicted vs
actual.

Usage:
    python analysis/agenda_topic_mapping/eval_historical.py
    python analysis/agenda_topic_mapping/eval_historical.py --limit 50 --verbose

Requires BUBBLE_API_URL and BUBBLE_API_KEY in environment (or .env).

Outputs:
    analysis/agenda_topic_mapping/eval_results.json
    analysis/agenda_topic_mapping/eval_summary.md
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("eval_historical")

OUTPUT_DIR = Path(__file__).resolve().parent
RESULTS_FILE = OUTPUT_DIR / "eval_results.json"
SUMMARY_FILE = OUTPUT_DIR / "eval_summary.md"


# ---------------------------------------------------------------------------
# Bubble client and helpers
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


_node_name_cache: dict[str, str | None] = {}
_node_obj_cache: dict[str, dict | None] = {}


def _get_node_obj(client, node_id: str) -> dict | None:
    """Fetch a Tree node object by ID (cached)."""
    if not node_id:
        return None
    if node_id in _node_obj_cache:
        return _node_obj_cache[node_id]
    try:
        obj = client.get("Tree node", node_id)
        _node_obj_cache[node_id] = obj
        return obj
    except Exception:
        _node_obj_cache[node_id] = None
        return None


def resolve_node_name(client, node_id: str) -> str | None:
    if not node_id:
        return None
    if node_id in _node_name_cache:
        return _node_name_cache[node_id]
    obj = _get_node_obj(client, node_id)
    if obj:
        name = (obj.get("name") or obj.get("Name") or "").strip()
        _node_name_cache[node_id] = name or None
        return name or None
    _node_name_cache[node_id] = None
    return None


def resolve_node_path(client, node_id: str, max_depth: int = 10) -> list[str]:
    """Walk up the tree node parent chain to build the full path (root → leaf)."""
    segments: list[str] = []
    current_id = node_id
    seen: set[str] = set()
    while current_id and len(segments) < max_depth:
        if current_id in seen:
            break
        seen.add(current_id)
        obj = _get_node_obj(client, current_id)
        if not obj:
            break
        name = (obj.get("name") or obj.get("Name") or "").strip()
        if name:
            segments.append(name)
        parent = obj.get("parent") or obj.get("Parent") or obj.get("parent_node")
        if isinstance(parent, dict):
            current_id = parent.get("_id") or parent.get("id") or ""
        elif isinstance(parent, str):
            current_id = parent
        else:
            break
    segments.reverse()  # root → leaf order
    return segments


def resolve_org_path_for_resource(client, resource: dict) -> list[str]:
    """Derive NAIC group path from a resource's Organization field (List of Tree Node IDs).

    The Organization field contains IDs of tree nodes in the Organization tree.
    We resolve each to its full path and return the longest (most specific) one,
    which typically represents the NAIC working group / task force.
    """
    org_field = resource.get("Organization") or []
    if isinstance(org_field, str):
        org_field = [org_field]

    best_path: list[str] = []
    for item in org_field:
        node_id = item if isinstance(item, str) else (
            (item.get("_id") or item.get("id")) if isinstance(item, dict) else None
        )
        if not node_id:
            continue
        path = resolve_node_path(client, node_id)
        if len(path) > len(best_path):
            best_path = path

    return best_path


# ---------------------------------------------------------------------------
# Data pull
# ---------------------------------------------------------------------------


def pull_resources_with_topic(client, limit: int = 100) -> list[dict]:
    """Fetch resources with topic suggestion populated."""
    log.info("Fetching resources with topic suggestion (limit=%d)...", limit)
    from bubble.lookups import TYPE_RESOURCE
    constraints = [{"key": "topic suggestion", "constraint_type": "is_not_empty"}]
    results = take(client.list_all(TYPE_RESOURCE, constraints=constraints, page_size=100), limit)
    log.info("  -> %d resources with topic suggestion", len(results))
    return results


def pull_agenda_items_with_resources(client, limit: int = 500) -> list[dict]:
    """Fetch agenda items that have resources linked."""
    log.info("Fetching agenda items (limit=%d) to find ones with resources...", limit)
    from bubble.lookups import TYPE_AGENDA_ITEM
    all_items = take(client.list_all(TYPE_AGENDA_ITEM, page_size=100), limit)
    with_resources = [a for a in all_items if a.get("Resources")]
    log.info("  -> %d agenda items fetched, %d have resources", len(all_items), len(with_resources))
    return with_resources


def pull_calendar_items_with_agenda(client, limit: int = 200) -> list[dict]:
    """Fetch calendar items with attached agenda items."""
    log.info("Fetching calendar items (limit=%d) to find ones with agenda items...", limit)
    from bubble.lookups import TYPE_CALENDAR_ITEM
    results = take(client.list_all(TYPE_CALENDAR_ITEM, page_size=100), limit)
    with_agenda = [c for c in results if c.get("attached agenda items")]
    log.info("  -> %d calendar items fetched, %d have agenda items", len(results), len(with_agenda))
    return with_agenda


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_topic_ids_from_agenda_items(
    agenda_item_ids: list[str],
    known_agenda_items: dict[str, dict],
) -> list[str]:
    """Extract all unique topic IDs from a list of agenda items' Topics fields."""
    topic_ids: list[str] = []
    for aid in agenda_item_ids:
        aitem = known_agenda_items.get(aid)
        if not aitem:
            continue
        topics = aitem.get("Topics") or []
        if isinstance(topics, list):
            for t in topics:
                tid = t if isinstance(t, str) else (
                    (t.get("_id") or t.get("id")) if isinstance(t, dict) else None
                )
                if tid and tid not in topic_ids:
                    topic_ids.append(tid)
    return topic_ids


# ---------------------------------------------------------------------------
# Evaluation logic
# ---------------------------------------------------------------------------


def evaluate_resource(
    resource: dict,
    client,
    *,
    known_agenda_items: dict[str, dict] | None = None,
    verbose: bool = False,
    use_ai: bool = False,
) -> dict[str, Any]:
    """
    Evaluate a single resource against known assignments.

    Returns a result dict with multi-assignment metrics:
        - Agenda items: recall, precision, false positives/negatives
        - Topics (single): exact match against topic suggestion field
        - Topics (multi): recall against all topics from matched agenda items
        - Topics (extended actual): all topics from actual agenda items' Topics fields
    """
    from bubble.enrich_refs import (
        TOPIC_TREE_NAME,
        NAIC_GROUP_TREE_NAME,
        _build_topic_candidates,
        _resolve_agenda_items_for_resource,
        _resolve_topic_enhanced,
        _resolve_naic_group_node,
        _parse_calendar_title_topics,
        infer_naic_group_path,
        _is_placeholder_topic,
    )
    from scrape.pdf_agenda_signals import extract_agenda_signals_from_bytes, signals_to_dict

    rid = resource.get("_id") or resource.get("id") or ""
    rname = (resource.get("Name") or resource.get("name") or "").strip()
    rurl = (resource.get("URL") or "").strip()

    result: dict[str, Any] = {
        "resource_id": rid,
        "resource_name": rname[:80],
        "resource_url": rurl[:120],
    }

    # --- Actual topic (from topic suggestion field — single) ---
    actual_topic_id = resource.get("topic suggestion")
    if isinstance(actual_topic_id, dict):
        actual_topic_id = actual_topic_id.get("_id") or actual_topic_id.get("id")
    actual_topic_name = resolve_node_name(client, actual_topic_id) if actual_topic_id else None
    result["actual_topic_id"] = actual_topic_id
    result["actual_topic_name"] = actual_topic_name

    # --- Actual agenda items (from known_agenda_items that reference this resource) ---
    actual_agenda_ids: list[str] = []
    if known_agenda_items:
        for aid, aitem in known_agenda_items.items():
            linked_resources = aitem.get("Resources") or []
            if isinstance(linked_resources, list):
                for lr in linked_resources:
                    lr_id = lr if isinstance(lr, str) else (lr.get("_id") or lr.get("id") if isinstance(lr, dict) else None)
                    if lr_id and lr_id == rid:
                        actual_agenda_ids.append(aid)
    result["actual_agenda_item_ids"] = actual_agenda_ids

    # --- Extended actual topics (all topics from actual agenda items) ---
    actual_extended_topic_ids: list[str] = []
    if known_agenda_items and actual_agenda_ids:
        actual_extended_topic_ids = _extract_topic_ids_from_agenda_items(
            actual_agenda_ids, known_agenda_items
        )
    result["actual_extended_topic_ids"] = actual_extended_topic_ids
    result["actual_extended_topic_names"] = [
        resolve_node_name(client, tid) or tid for tid in actual_extended_topic_ids
    ]

    # --- Determine NAIC group ---
    path = resolve_org_path_for_resource(client, resource)
    if not path:
        parent = (resource.get("parent") or "").strip()
        path = infer_naic_group_path(parent)
    naic_group_node_id = None
    if path:
        naic_group_node_id, _ = _resolve_naic_group_node(NAIC_GROUP_TREE_NAME, path)
    if not naic_group_node_id and path and len(path) > 1:
        naic_group_node_id, _ = _resolve_naic_group_node(NAIC_GROUP_TREE_NAME, [path[-1]])

    result["resolved_org_path"] = path
    result["naic_group_node_id"] = naic_group_node_id
    if verbose:
        log.info("  Org path: %s -> node_id: %s", " › ".join(path) if path else "(none)", naic_group_node_id or "(none)")

    # --- PDF agenda signals ---
    pdf_signals_dict = None
    if rurl.lower().endswith(".pdf"):
        try:
            import requests
            resp = requests.get(rurl, timeout=15)
            resp.raise_for_status()
            signals = extract_agenda_signals_from_bytes(resp.content)
            if signals:
                pdf_signals_dict = signals_to_dict(signals)
        except Exception as e:
            if verbose:
                log.warning("  PDF download/parse failed for %s: %s", rurl[:60], e)

    # Build resource dict for inference (strip _id to prevent bidirectional lookup)
    test_resource = {k: v for k, v in resource.items() if k not in ("_id", "id")}
    if pdf_signals_dict:
        test_resource["__pdf_agenda_signals"] = pdf_signals_dict
    result["pdf_signals"] = pdf_signals_dict

    # === Run agenda item matching ===
    agenda_result = _resolve_agenda_items_for_resource(
        test_resource,
        {"label": path[-1] if path else "", "org_path": path[:-1] if len(path) > 1 else ["NAIC"]},
        naic_group_node_id,
        bubble_snapshot=None,
        use_ai=use_ai,
    )
    predicted_agenda_ids = agenda_result.get("matched_ids") or []
    all_inherited_topic_ids = agenda_result.get("inherited_topic_ids") or []
    result["predicted_agenda_item_ids"] = predicted_agenda_ids
    result["agenda_match_method"] = agenda_result.get("method", "none")
    result["agenda_candidate_count"] = len(agenda_result.get("candidates") or [])

    # --- Agenda item metrics: recall, precision, false pos/neg ---
    actual_agenda_set = set(actual_agenda_ids)
    predicted_agenda_set = set(predicted_agenda_ids)
    overlap_agenda = actual_agenda_set & predicted_agenda_set
    result["agenda_overlap"] = sorted(overlap_agenda)
    result["agenda_false_positives"] = sorted(predicted_agenda_set - actual_agenda_set)
    result["agenda_false_negatives"] = sorted(actual_agenda_set - predicted_agenda_set)
    result["agenda_recall"] = (
        len(overlap_agenda) / len(actual_agenda_set)
        if actual_agenda_set else None
    )
    result["agenda_precision"] = (
        len(overlap_agenda) / len(predicted_agenda_set)
        if predicted_agenda_set else None
    )
    result["agenda_match"] = bool(overlap_agenda) if actual_agenda_set else None

    # === Run topic suggestion (single — existing behavior) ===
    topic_candidates = _build_topic_candidates(TOPIC_TREE_NAME)
    topic_result = _resolve_topic_enhanced(
        test_resource,
        {"label": path[-1] if path else "", "org_path": path[:-1] if len(path) > 1 else ["NAIC"]},
        topic_candidates,
        matched_agenda_items_result={
            "matched_ids": predicted_agenda_ids,
            "inherited_topic_ids": all_inherited_topic_ids,
            "method": agenda_result.get("method", "none"),
        } if predicted_agenda_ids else None,
        use_ai=use_ai,
    )
    predicted_topic_id = topic_result.get("topic_id")
    predicted_topic_name = topic_result.get("topic_name")
    if predicted_topic_id and not predicted_topic_name:
        predicted_topic_name = resolve_node_name(client, predicted_topic_id)

    result["predicted_topic_id"] = predicted_topic_id
    result["predicted_topic_name"] = predicted_topic_name
    result["topic_source"] = topic_result.get("source", "unresolved")

    # === Multi-topic: all inherited topics + AI suggestion ===
    predicted_all_topic_ids: list[str] = []
    for tid in all_inherited_topic_ids:
        if tid not in predicted_all_topic_ids:
            predicted_all_topic_ids.append(tid)
    # Add the AI/single-pick topic if not already included
    if predicted_topic_id and predicted_topic_id not in predicted_all_topic_ids:
        predicted_all_topic_ids.append(predicted_topic_id)
    result["predicted_all_topic_ids"] = predicted_all_topic_ids
    result["predicted_all_topic_names"] = [
        resolve_node_name(client, tid) or tid for tid in predicted_all_topic_ids
    ]

    # === Topic metrics ===

    # 1. Single-topic exact match (existing metric)
    id_match = (
        actual_topic_id is not None
        and predicted_topic_id is not None
        and actual_topic_id == predicted_topic_id
    )
    name_match = (
        actual_topic_name is not None
        and predicted_topic_name is not None
        and _normalize_topic_name(actual_topic_name) == _normalize_topic_name(predicted_topic_name)
    )
    both_placeholder = (
        actual_topic_name is not None
        and _is_placeholder_topic(actual_topic_name)
        and (predicted_topic_name is None or _is_placeholder_topic(predicted_topic_name))
    )
    result["topic_match"] = id_match or name_match or both_placeholder

    # 2. Multi-topic recall: is the actual topic suggestion in our full predicted set?
    if actual_topic_id:
        actual_norm = _normalize_topic_name(actual_topic_name) if actual_topic_name else None
        found_in_multi = actual_topic_id in predicted_all_topic_ids
        if not found_in_multi and actual_norm:
            for tid in predicted_all_topic_ids:
                tname = resolve_node_name(client, tid)
                if tname and _normalize_topic_name(tname) == actual_norm:
                    found_in_multi = True
                    break
        if not found_in_multi and both_placeholder:
            found_in_multi = True
        result["topic_in_multi_set"] = found_in_multi
    else:
        result["topic_in_multi_set"] = None

    # 3. Extended actual topics: recall of our predicted set vs all actual agenda item topics
    if actual_extended_topic_ids:
        extended_actual_set = set(actual_extended_topic_ids)
        predicted_topic_set = set(predicted_all_topic_ids)
        extended_overlap = extended_actual_set & predicted_topic_set
        # Also check by normalized name for fuzzy matches
        if len(extended_overlap) < len(extended_actual_set):
            pred_names_norm = {}
            for tid in predicted_all_topic_ids:
                tn = resolve_node_name(client, tid)
                if tn:
                    pred_names_norm[_normalize_topic_name(tn)] = tid
            for atid in extended_actual_set - extended_overlap:
                atn = resolve_node_name(client, atid)
                if atn and _normalize_topic_name(atn) in pred_names_norm:
                    extended_overlap.add(atid)
        result["extended_topic_overlap"] = sorted(extended_overlap)
        result["extended_topic_recall"] = (
            len(extended_overlap) / len(extended_actual_set)
        )
        result["extended_topic_false_negatives"] = sorted(extended_actual_set - extended_overlap)
        result["extended_topic_false_positives"] = sorted(predicted_topic_set - extended_actual_set)
        result["extended_topic_precision"] = (
            len(extended_overlap) / len(predicted_topic_set)
            if predicted_topic_set else None
        )
    else:
        result["extended_topic_overlap"] = []
        result["extended_topic_recall"] = None
        result["extended_topic_false_negatives"] = []
        result["extended_topic_false_positives"] = sorted(set(predicted_all_topic_ids))
        result["extended_topic_precision"] = None

    if verbose:
        match_icon = "Y" if result["topic_match"] else "N"
        multi_icon = "Y" if result.get("topic_in_multi_set") else "N"
        log.info(
            "  [%s/%s] %s -> topic: %s (actual: %s) via %s | multi-topics: %d",
            match_icon, multi_icon, rname[:50],
            predicted_topic_name or "(none)", actual_topic_name or "(none)",
            result["topic_source"], len(predicted_all_topic_ids),
        )

    return result


# ---------------------------------------------------------------------------
# Topic name normalization for comparison
# ---------------------------------------------------------------------------


def _normalize_topic_name(name: str) -> str:
    """Normalize a topic name for comparison: strip BBCode, 'The ' prefix, whitespace."""
    s = re.sub(r"\[/?b\]", "", name or "")
    s = re.sub(r"\[/?color[^\]]*\]", "", s)
    s = re.sub(r"^The\s+", "", s.strip(), flags=re.IGNORECASE)
    return s.strip()


# ---------------------------------------------------------------------------
# Summary generation
# ---------------------------------------------------------------------------


def compute_summary(results: list[dict]) -> dict[str, Any]:
    """Compute aggregate metrics from evaluation results."""
    total = len(results)
    if total == 0:
        return {"total": 0}

    # --- Topic metrics (single) ---
    has_actual_topic = [r for r in results if r.get("actual_topic_id")]
    topic_matches = [r for r in has_actual_topic if r.get("topic_match")]
    topic_predicted = [r for r in has_actual_topic if r.get("predicted_topic_id")]

    # --- Topic metrics (multi — is actual in predicted set?) ---
    topic_in_multi = [r for r in has_actual_topic if r.get("topic_in_multi_set")]

    # --- Topic metrics (extended — recall of all actual agenda item topics) ---
    has_extended = [r for r in results if r.get("actual_extended_topic_ids")]
    extended_recalls = [r["extended_topic_recall"] for r in has_extended if r.get("extended_topic_recall") is not None]
    extended_precisions = [r["extended_topic_precision"] for r in has_extended if r.get("extended_topic_precision") is not None]

    # --- Agenda item metrics ---
    has_actual_agenda = [r for r in results if r.get("actual_agenda_item_ids")]
    agenda_matches = [r for r in has_actual_agenda if r.get("agenda_match")]
    agenda_predicted = [r for r in results if r.get("predicted_agenda_item_ids")]
    agenda_recalls = [r["agenda_recall"] for r in has_actual_agenda if r.get("agenda_recall") is not None]
    agenda_precisions = [r["agenda_precision"] for r in results if r.get("agenda_precision") is not None]

    # Total false positives across all resources
    total_agenda_fp = sum(len(r.get("agenda_false_positives", [])) for r in results)
    total_agenda_fn = sum(len(r.get("agenda_false_negatives", [])) for r in has_actual_agenda)
    total_agenda_correct = sum(len(r.get("agenda_overlap", [])) for r in has_actual_agenda)
    total_actual_agenda = sum(len(r.get("actual_agenda_item_ids", [])) for r in has_actual_agenda)

    # Topic source breakdown
    source_counts: dict[str, int] = {}
    for r in results:
        src = r.get("topic_source", "unresolved")
        source_counts[src] = source_counts.get(src, 0) + 1

    return {
        "total": total,
        "topic_single": {
            "has_actual": len(has_actual_topic),
            "predicted": len(topic_predicted),
            "matches": len(topic_matches),
            "accuracy": round(len(topic_matches) / len(has_actual_topic), 3) if has_actual_topic else 0,
            "coverage": round(len(topic_predicted) / len(has_actual_topic), 3) if has_actual_topic else 0,
        },
        "topic_multi": {
            "has_actual": len(has_actual_topic),
            "in_multi_set": len(topic_in_multi),
            "recall": round(len(topic_in_multi) / len(has_actual_topic), 3) if has_actual_topic else 0,
        },
        "topic_extended": {
            "resources_with_extended": len(has_extended),
            "avg_recall": round(sum(extended_recalls) / len(extended_recalls), 3) if extended_recalls else 0,
            "avg_precision": round(sum(extended_precisions) / len(extended_precisions), 3) if extended_precisions else 0,
        },
        "agenda_items": {
            "has_actual": len(has_actual_agenda),
            "predicted_any": len(agenda_predicted),
            "resources_with_overlap": len(agenda_matches),
            "resource_match_rate": round(len(agenda_matches) / len(has_actual_agenda), 3) if has_actual_agenda else 0,
            "total_actual": total_actual_agenda,
            "total_correct": total_agenda_correct,
            "total_false_positives": total_agenda_fp,
            "total_false_negatives": total_agenda_fn,
            "item_recall": round(total_agenda_correct / total_actual_agenda, 3) if total_actual_agenda else 0,
            "item_precision": round(total_agenda_correct / (total_agenda_correct + total_agenda_fp), 3) if (total_agenda_correct + total_agenda_fp) else 0,
            "avg_recall": round(sum(agenda_recalls) / len(agenda_recalls), 3) if agenda_recalls else 0,
            "avg_precision": round(sum(agenda_precisions) / len(agenda_precisions), 3) if agenda_precisions else 0,
        },
        "topic_source_breakdown": source_counts,
    }


def write_summary_md(summary: dict, results: list[dict], path: Path) -> None:
    """Write eval_summary.md with human-readable metrics."""
    lines: list[str] = []
    lines.append("# Historical Evaluation Summary")
    lines.append("")
    lines.append(f"**Total resources evaluated:** {summary['total']}")
    lines.append("")

    # --- Topic suggestion (single pick) ---
    topic = summary.get("topic_single", {})
    lines.append("## Topic Suggestion (Single Pick)")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Resources with actual topic | {topic.get('has_actual', 0)} |")
    lines.append(f"| Resources where system predicted a topic | {topic.get('predicted', 0)} |")
    lines.append(f"| Exact match | {topic.get('matches', 0)} |")
    lines.append(f"| Accuracy (exact match / has_actual) | {topic.get('accuracy', 0):.1%} |")
    lines.append(f"| Coverage (predicted / has_actual) | {topic.get('coverage', 0):.1%} |")
    lines.append("")

    # --- Topic suggestion (multi — actual in predicted set?) ---
    topic_multi = summary.get("topic_multi", {})
    lines.append("## Topic Suggestion (Multi-Topic Set)")
    lines.append("")
    lines.append("*Does the actual topic appear anywhere in the full set of inherited + AI-suggested topics?*")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Resources with actual topic | {topic_multi.get('has_actual', 0)} |")
    lines.append(f"| Actual topic found in multi-topic set | {topic_multi.get('in_multi_set', 0)} |")
    lines.append(f"| Recall | {topic_multi.get('recall', 0):.1%} |")
    lines.append("")

    # --- Topic extended (recall against all actual agenda item topics) ---
    topic_ext = summary.get("topic_extended", {})
    if topic_ext.get("resources_with_extended"):
        lines.append("## Topic Coverage (Extended — All Agenda Item Topics)")
        lines.append("")
        lines.append("*For resources with actual agenda items: what % of the agenda items' topics did we predict?*")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        lines.append(f"| Resources with extended topic ground truth | {topic_ext.get('resources_with_extended', 0)} |")
        lines.append(f"| Average recall (actual topics covered) | {topic_ext.get('avg_recall', 0):.1%} |")
        lines.append(f"| Average precision (predicted topics that are correct) | {topic_ext.get('avg_precision', 0):.1%} |")
        lines.append("")

    # --- Topic source breakdown ---
    sources = summary.get("topic_source_breakdown", {})
    if sources:
        lines.append("### Topic source breakdown")
        lines.append("")
        lines.append("| Source | Count |")
        lines.append("|--------|-------|")
        for src, count in sorted(sources.items(), key=lambda x: -x[1]):
            lines.append(f"| {src} | {count} |")
        lines.append("")

    # --- Agenda item matching ---
    agenda = summary.get("agenda_items", {})
    lines.append("## Agenda Item Matching")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Resources with actual agenda item links | {agenda.get('has_actual', 0)} |")
    lines.append(f"| Resources where system predicted agenda items | {agenda.get('predicted_any', 0)} |")
    lines.append(f"| Resources with at least one correct match | {agenda.get('resources_with_overlap', 0)} |")
    lines.append(f"| Resource-level match rate | {agenda.get('resource_match_rate', 0):.1%} |")
    lines.append("")
    lines.append("### Item-Level Metrics")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Total actual agenda item links | {agenda.get('total_actual', 0)} |")
    lines.append(f"| Correctly predicted | {agenda.get('total_correct', 0)} |")
    lines.append(f"| False positives (noise/over-assignment) | {agenda.get('total_false_positives', 0)} |")
    lines.append(f"| False negatives (missed) | {agenda.get('total_false_negatives', 0)} |")
    lines.append(f"| Item recall (correct / total actual) | {agenda.get('item_recall', 0):.1%} |")
    lines.append(f"| Item precision (correct / (correct + FP)) | {agenda.get('item_precision', 0):.1%} |")
    lines.append(f"| Average per-resource recall | {agenda.get('avg_recall', 0):.1%} |")
    lines.append(f"| Average per-resource precision | {agenda.get('avg_precision', 0):.1%} |")
    lines.append("")

    # --- Sample results ---
    lines.append("## Sample Results")
    lines.append("")

    # Correct topic matches
    correct = [r for r in results if r.get("topic_match")]
    if correct:
        lines.append("### Correct topic predictions (single pick)")
        lines.append("")
        for r in correct[:10]:
            multi_count = len(r.get("predicted_all_topic_ids", []))
            lines.append(f"- **{r['resource_name']}**")
            lines.append(f"  - Actual: {r.get('actual_topic_name', '?')}")
            lines.append(f"  - Predicted: {r.get('predicted_topic_name', '?')} (via {r.get('topic_source', '?')})")
            if multi_count > 1:
                lines.append(f"  - All predicted topics ({multi_count}): {', '.join(r.get('predicted_all_topic_names', []))}")
        lines.append("")

    # Wrong single pick but found in multi-topic set
    wrong_but_in_multi = [
        r for r in results
        if r.get("actual_topic_id")
        and not r.get("topic_match")
        and r.get("topic_in_multi_set")
    ]
    if wrong_but_in_multi:
        lines.append("### Wrong single pick but actual IS in multi-topic set")
        lines.append("")
        for r in wrong_but_in_multi:
            lines.append(f"- **{r['resource_name']}**")
            lines.append(f"  - Actual: {r.get('actual_topic_name', '?')}")
            lines.append(f"  - Single pick: {r.get('predicted_topic_name', '?')} (via {r.get('topic_source', '?')})")
            lines.append(f"  - All predicted topics: {', '.join(r.get('predicted_all_topic_names', []))}")
        lines.append("")

    # Incorrect topic predictions (not in multi set either)
    wrong = [
        r for r in results
        if r.get("actual_topic_id")
        and r.get("predicted_topic_id")
        and not r.get("topic_match")
        and not r.get("topic_in_multi_set")
    ]
    if wrong:
        lines.append("### Incorrect topic predictions (not in multi-topic set)")
        lines.append("")
        for r in wrong[:10]:
            lines.append(f"- **{r['resource_name']}**")
            lines.append(f"  - Actual: {r.get('actual_topic_name', '?')}")
            lines.append(f"  - Predicted: {r.get('predicted_topic_name', '?')} (via {r.get('topic_source', '?')})")
            lines.append(f"  - All predicted topics: {', '.join(r.get('predicted_all_topic_names', []))}")
        lines.append("")

    # Unresolved
    unresolved = [r for r in results if r.get("actual_topic_id") and not r.get("predicted_topic_id") and not r.get("predicted_all_topic_ids")]
    if unresolved:
        lines.append(f"### Unresolved ({len(unresolved)} resources with actual topic but no prediction)")
        lines.append("")
        for r in unresolved[:10]:
            lines.append(f"- **{r['resource_name']}** (actual: {r.get('actual_topic_name', '?')})")
        lines.append("")

    # Agenda item details for resources with actual items
    has_agenda = [r for r in results if r.get("actual_agenda_item_ids")]
    if has_agenda:
        lines.append("## Agenda Item Details")
        lines.append("")
        lines.append("| Resource | Actual | Predicted | Overlap | FP | FN | Recall |")
        lines.append("|----------|--------|-----------|---------|----|----|--------|")
        for r in has_agenda:
            rn = r["resource_name"][:50]
            na = len(r.get("actual_agenda_item_ids", []))
            np_ = len(r.get("predicted_agenda_item_ids", []))
            no = len(r.get("agenda_overlap", []))
            nfp = len(r.get("agenda_false_positives", []))
            nfn = len(r.get("agenda_false_negatives", []))
            recall = r.get("agenda_recall")
            recall_str = f"{recall:.0%}" if recall is not None else "N/A"
            lines.append(f"| {rn} | {na} | {np_} | {no} | {nfp} | {nfn} | {recall_str} |")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("*Generated by `analysis/agenda_topic_mapping/eval_historical.py`*")

    path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Wrote evaluation summary: %s", path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate agenda item matching and topic suggestion against historical Bubble data.",
    )
    parser.add_argument(
        "--limit", type=int, default=100,
        help="Max resources to evaluate (default: 100)",
    )
    parser.add_argument(
        "--skip-pdf", action="store_true",
        help="Skip PDF downloads (faster, uses only metadata signals)",
    )
    parser.add_argument(
        "--use-ai", action="store_true",
        help="Enable LLM tier for agenda item matching and topic suggestion",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print per-resource results",
    )
    args = parser.parse_args()

    client = get_client()

    # Pull historical data
    resources = pull_resources_with_topic(client, limit=args.limit)
    agenda_items = pull_agenda_items_with_resources(client, limit=500)

    # Build agenda item lookup by ID
    known_agenda_items: dict[str, dict] = {}
    for a in agenda_items:
        aid = a.get("_id") or a.get("id")
        if aid:
            known_agenda_items[str(aid)] = a

    log.info("Evaluating %d resources against %d known agenda items...", len(resources), len(known_agenda_items))

    # Evaluate each resource
    results: list[dict] = []
    for i, resource in enumerate(resources):
        rname = (resource.get("Name") or "").strip()[:50]
        log.info("[%d/%d] Evaluating: %s", i + 1, len(resources), rname)
        try:
            result = evaluate_resource(
                resource, client,
                known_agenda_items=known_agenda_items,
                verbose=args.verbose,
                use_ai=args.use_ai,
            )
            results.append(result)
        except Exception as e:
            log.warning("  FAILED: %s: %s", rname, e)
            results.append({
                "resource_id": resource.get("_id") or "",
                "resource_name": rname,
                "error": str(e),
            })

    # Compute summary
    summary = compute_summary(results)

    topic_s = summary.get("topic_single", {})
    topic_m = summary.get("topic_multi", {})
    agenda = summary.get("agenda_items", {})
    log.info("Evaluation complete:")
    log.info("  Topic accuracy (single pick): %s (%d/%d)",
             f"{topic_s.get('accuracy', 0):.1%}",
             topic_s.get("matches", 0),
             topic_s.get("has_actual", 0))
    log.info("  Topic recall (multi-topic set): %s (%d/%d)",
             f"{topic_m.get('recall', 0):.1%}",
             topic_m.get("in_multi_set", 0),
             topic_m.get("has_actual", 0))
    log.info("  Agenda resource match rate: %s (%d/%d)",
             f"{agenda.get('resource_match_rate', 0):.1%}",
             agenda.get("resources_with_overlap", 0),
             agenda.get("has_actual", 0))
    log.info("  Agenda item recall: %s (%d/%d)",
             f"{agenda.get('item_recall', 0):.1%}",
             agenda.get("total_correct", 0),
             agenda.get("total_actual", 0))
    log.info("  Agenda item precision: %s",
             f"{agenda.get('item_precision', 0):.1%}")

    # Write results
    output = {
        "summary": summary,
        "results": results,
    }
    RESULTS_FILE.write_text(
        json.dumps(output, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    log.info("Wrote evaluation results: %s", RESULTS_FILE)

    # Write summary markdown
    write_summary_md(summary, results, SUMMARY_FILE)

    return 0


if __name__ == "__main__":
    sys.exit(main())
