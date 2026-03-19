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


def pull_agenda_items_with_resources(client, limit: int = 200) -> list[dict]:
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

    Returns a result dict with:
        resource_id, resource_name, resource_url,
        actual_topic_id, actual_topic_name,
        predicted_topic_id, predicted_topic_name, topic_source,
        topic_match: bool,
        actual_agenda_item_ids, predicted_agenda_item_ids,
        agenda_match: bool (any overlap),
        agenda_match_details, signals
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

    # Actual topic
    actual_topic_id = resource.get("topic suggestion")
    if isinstance(actual_topic_id, dict):
        actual_topic_id = actual_topic_id.get("_id") or actual_topic_id.get("id")
    actual_topic_name = resolve_node_name(client, actual_topic_id) if actual_topic_id else None
    result["actual_topic_id"] = actual_topic_id
    result["actual_topic_name"] = actual_topic_name

    # Actual agenda items (from known_agenda_items that reference this resource)
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

    # Determine NAIC group from resource Organization field (List of Tree Node IDs)
    path = resolve_org_path_for_resource(client, resource)
    if not path:
        # Fallback: try parent field in case it's a readable path (legacy)
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

    # PDF agenda signals (only for PDF resources)
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

    # Build resource dict for inference
    test_resource = dict(resource)
    if pdf_signals_dict:
        test_resource["__pdf_agenda_signals"] = pdf_signals_dict
    result["pdf_signals"] = pdf_signals_dict

    # Run agenda item matching
    agenda_result = _resolve_agenda_items_for_resource(
        test_resource,
        {"label": path[-1] if path else "", "org_path": path[:-1] if len(path) > 1 else ["NAIC"]},
        naic_group_node_id,
        bubble_snapshot=None,
        use_ai=use_ai,
    )
    predicted_agenda_ids = agenda_result.get("matched_ids") or []
    result["predicted_agenda_item_ids"] = predicted_agenda_ids
    result["agenda_match_method"] = agenda_result.get("method", "none")
    result["agenda_candidate_count"] = len(agenda_result.get("candidates") or [])

    # Agenda item overlap
    actual_set = set(actual_agenda_ids)
    predicted_set = set(predicted_agenda_ids)
    result["agenda_match"] = bool(actual_set & predicted_set) if actual_set else None
    result["agenda_overlap"] = sorted(actual_set & predicted_set)
    result["agenda_false_positives"] = sorted(predicted_set - actual_set)
    result["agenda_false_negatives"] = sorted(actual_set - predicted_set)

    # Run topic suggestion
    topic_candidates = _build_topic_candidates(TOPIC_TREE_NAME)
    topic_result = _resolve_topic_enhanced(
        test_resource,
        {"label": path[-1] if path else "", "org_path": path[:-1] if len(path) > 1 else ["NAIC"]},
        topic_candidates,
        matched_agenda_items_result={
            "matched_ids": predicted_agenda_ids,
            "inherited_topic_ids": agenda_result.get("inherited_topic_ids") or [],
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

    # Topic match — check ID match first, then normalized name match
    from bubble.enrich_refs import _is_placeholder_topic
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
    # Both placeholder → treat as match (both correctly identify "no real topic")
    both_placeholder = (
        actual_topic_name is not None
        and _is_placeholder_topic(actual_topic_name)
        and (predicted_topic_name is None or _is_placeholder_topic(predicted_topic_name))
    )
    result["topic_match"] = id_match or name_match or both_placeholder

    if verbose:
        match_icon = "Y" if result["topic_match"] else "N"
        log.info(
            "  [%s] %s -> topic: %s (actual: %s) via %s",
            match_icon, rname[:50],
            predicted_topic_name or "(none)", actual_topic_name or "(none)",
            result["topic_source"],
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

    # Topic metrics
    has_actual_topic = [r for r in results if r.get("actual_topic_id")]
    topic_matches = [r for r in has_actual_topic if r.get("topic_match")]
    topic_predicted = [r for r in has_actual_topic if r.get("predicted_topic_id")]

    # Agenda item metrics
    has_actual_agenda = [r for r in results if r.get("actual_agenda_item_ids")]
    agenda_matches = [r for r in has_actual_agenda if r.get("agenda_match")]
    agenda_predicted = [r for r in results if r.get("predicted_agenda_item_ids")]

    # Topic source breakdown
    source_counts: dict[str, int] = {}
    for r in results:
        src = r.get("topic_source", "unresolved")
        source_counts[src] = source_counts.get(src, 0) + 1

    return {
        "total": total,
        "topic": {
            "has_actual": len(has_actual_topic),
            "predicted": len(topic_predicted),
            "matches": len(topic_matches),
            "accuracy": round(len(topic_matches) / len(has_actual_topic), 3) if has_actual_topic else 0,
            "coverage": round(len(topic_predicted) / len(has_actual_topic), 3) if has_actual_topic else 0,
        },
        "agenda_items": {
            "has_actual": len(has_actual_agenda),
            "predicted_any": len(agenda_predicted),
            "matches": len(agenda_matches),
            "accuracy": round(len(agenda_matches) / len(has_actual_agenda), 3) if has_actual_agenda else 0,
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

    # Topic suggestion
    topic = summary.get("topic", {})
    lines.append("## Topic Suggestion")
    lines.append("")
    lines.append(f"| Metric | Value |")
    lines.append(f"|--------|-------|")
    lines.append(f"| Resources with actual topic | {topic.get('has_actual', 0)} |")
    lines.append(f"| Resources where system predicted a topic | {topic.get('predicted', 0)} |")
    lines.append(f"| Correct predictions | {topic.get('matches', 0)} |")
    lines.append(f"| Accuracy (correct / has_actual) | {topic.get('accuracy', 0):.1%} |")
    lines.append(f"| Coverage (predicted / has_actual) | {topic.get('coverage', 0):.1%} |")
    lines.append("")

    # Topic source breakdown
    sources = summary.get("topic_source_breakdown", {})
    if sources:
        lines.append("### Topic source breakdown")
        lines.append("")
        lines.append("| Source | Count |")
        lines.append("|--------|-------|")
        for src, count in sorted(sources.items(), key=lambda x: -x[1]):
            lines.append(f"| {src} | {count} |")
        lines.append("")

    # Agenda item matching
    agenda = summary.get("agenda_items", {})
    lines.append("## Agenda Item Matching")
    lines.append("")
    lines.append(f"| Metric | Value |")
    lines.append(f"|--------|-------|")
    lines.append(f"| Resources with actual agenda item link | {agenda.get('has_actual', 0)} |")
    lines.append(f"| Resources where system predicted agenda items | {agenda.get('predicted_any', 0)} |")
    lines.append(f"| Correct matches (overlap with actual) | {agenda.get('matches', 0)} |")
    lines.append(f"| Match rate | {agenda.get('accuracy', 0):.1%} |")
    lines.append("")

    # Sample results
    lines.append("## Sample Results")
    lines.append("")

    # Correct topic matches
    correct = [r for r in results if r.get("topic_match")]
    if correct:
        lines.append("### Correct topic predictions")
        lines.append("")
        for r in correct[:10]:
            lines.append(f"- **{r['resource_name']}**")
            lines.append(f"  - Actual: {r.get('actual_topic_name', '?')}")
            lines.append(f"  - Predicted: {r.get('predicted_topic_name', '?')} (via {r.get('topic_source', '?')})")
        lines.append("")

    # Incorrect topic predictions
    wrong = [r for r in results if r.get("actual_topic_id") and r.get("predicted_topic_id") and not r.get("topic_match")]
    if wrong:
        lines.append("### Incorrect topic predictions")
        lines.append("")
        for r in wrong[:10]:
            lines.append(f"- **{r['resource_name']}**")
            lines.append(f"  - Actual: {r.get('actual_topic_name', '?')}")
            lines.append(f"  - Predicted: {r.get('predicted_topic_name', '?')} (via {r.get('topic_source', '?')})")
        lines.append("")

    # Unresolved
    unresolved = [r for r in results if r.get("actual_topic_id") and not r.get("predicted_topic_id")]
    if unresolved:
        lines.append(f"### Unresolved ({len(unresolved)} resources with actual topic but no prediction)")
        lines.append("")
        for r in unresolved[:10]:
            lines.append(f"- **{r['resource_name']}** (actual: {r.get('actual_topic_name', '?')})")
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
    agenda_items = pull_agenda_items_with_resources(client, limit=200)

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
    log.info("Evaluation complete:")
    log.info("  Topic accuracy: %s (%d/%d)",
             f"{summary['topic']['accuracy']:.1%}" if summary.get("topic") else "N/A",
             summary.get("topic", {}).get("matches", 0),
             summary.get("topic", {}).get("has_actual", 0))
    log.info("  Agenda match rate: %s (%d/%d)",
             f"{summary['agenda_items']['accuracy']:.1%}" if summary.get("agenda_items") else "N/A",
             summary.get("agenda_items", {}).get("matches", 0),
             summary.get("agenda_items", {}).get("has_actual", 0))

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
