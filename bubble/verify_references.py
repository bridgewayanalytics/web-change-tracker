"""
Reference verification for Bubble payloads using a Bubble snapshot.

verify_all_references(payloads, snapshot, mode) will:
- Discover reference fields from a static config based on schema metadata.
- Validate that each referenced ID exists in the appropriate snapshot map
  (Tree nodes, Calendar Items, Resources).
- Validate that AI-selected IDs are a subset of the candidate sets exposed
  via mapping_context (organization_tree_nodes, naic_group_tree_nodes,
  resource_type_tree_nodes, recent_calendar_items).
- Emit debug/verify_report.json with per-field issues.
- In e2e_verify mode, fail (SystemExit) if any strict field has invalid/unresolved refs.

This function does not mutate payloads; callers may choose whether/how to drop
invalid references.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

from bubble.mapping_context import build_mapping_context

log = logging.getLogger(__name__)

DEBUG_VERIFY_REPORT = Path("debug") / "verify_report.json"


def _id_from_obj(obj: Any) -> str | None:
    if isinstance(obj, str):
        return obj.strip() or None
    if isinstance(obj, dict):
        v = obj.get("_id") or obj.get("id")
        return str(v).strip() if v else None
    return None


def _extract_ids(val: Any, single: bool = False) -> list[str]:
    """Extract a list of string IDs from a field value."""
    if val is None:
        return []
    if single:
        sid = _id_from_obj(val) or (val[0] if isinstance(val, list) and val else None)
        sid = _id_from_obj(sid)
        return [sid] if sid else []
    if isinstance(val, list):
        out: list[str] = []
        for v in val:
            sid = _id_from_obj(v)
            if sid:
                out.append(sid)
        return out
    sid = _id_from_obj(val)
    return [sid] if sid else []


def _build_snapshot_maps(snapshot: dict) -> dict[str, dict[str, dict]]:
    """Build maps of id -> object for tree nodes, calendar items, and resources."""
    tree_nodes: dict[str, dict] = {}
    for n in snapshot.get("tree_nodes") or []:
        sid = _id_from_obj(n)
        if sid:
            tree_nodes[sid] = n
    calendar_items: dict[str, dict] = {}
    for c in snapshot.get("calendar_items") or []:
        sid = _id_from_obj(c)
        if sid:
            calendar_items[sid] = c
    resources: dict[str, dict] = {}
    for r in snapshot.get("resources") or []:
        sid = _id_from_obj(r)
        if sid:
            resources[sid] = r
    return {
        "tree_node": tree_nodes,
        "calendar": calendar_items,
        "resource": resources,
    }


# Reference field configuration (derived from schema metadata)
# target: "resource" or "calendar"
# ref_type: which snapshot map to look in
# strict: if True, unresolved/invalid refs cause e2e_verify to fail
# candidate_key: mapping_context list to use for candidate IDs (AI bound)
REF_CONFIG = [
    # Resource: Organization (List of Tree Nodes)
    {
        "target": "resource",
        "field": "Organization",
        "ref_type": "tree_node",
        "strict": True,
        "candidate_key": "organization_tree_nodes",
        "single": False,
    },
    # Resource: Type1 (List of Tree Nodes)
    {
        "target": "resource",
        "field": "Type1",
        "ref_type": "tree_node",
        "strict": True,
        "candidate_key": "resource_type_tree_nodes",
        "single": False,
    },
    # Resource: topic suggestion (Tree Node, optional)
    {
        "target": "resource",
        "field": "topic suggestion",
        "ref_type": "tree_node",
        "strict": False,
        "candidate_key": "naic_group_tree_nodes",
        "single": True,
    },
    # Resource: Related calendar items (List of Calendar Items)
    {
        "target": "resource",
        "field": "Related calendar items",
        "ref_type": "calendar",
        "strict": True,
        "candidate_key": "recent_calendar_items",
        "single": False,
    },
    # Calendar: NAIC Group (tree node)
    {
        "target": "calendar",
        "field": "NAIC Group (tree node)",
        "ref_type": "tree_node",
        "strict": True,
        "candidate_key": "naic_group_tree_nodes",
        "single": True,
    },
    # Calendar: Agenda (List of Resources) - non-strict for now
    {
        "target": "calendar",
        "field": "Agenda",
        "ref_type": "resource",
        "strict": False,
        "candidate_key": None,
        "single": False,
    },
    # Calendar: Relevant Documents (List of Chronicle Links) - no snapshot type yet; skip
]


def verify_all_references(
    payloads: dict[str, list[dict]],
    snapshot: dict | None,
    *,
    mode: str = "normal",
) -> dict[str, Any]:
    """
    Verify references in Resource and Calendar Item payloads against a Bubble snapshot.

    payloads: {"resources": [...], "calendar_items": [...]}
    snapshot: dict from bubble.snapshot.build_bubble_snapshot
    mode: "normal" or "e2e_verify"

    Returns a report dict and writes debug/verify_report.json.
    """
    resources = payloads.get("resources") or []
    calendar_items = payloads.get("calendar_items") or []

    if not snapshot:
        report = {
            "error": "no_snapshot",
            "resources": [],
            "calendar_items": [],
        }
        try:
            DEBUG_VERIFY_REPORT.parent.mkdir(parents=True, exist_ok=True)
            DEBUG_VERIFY_REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
        except Exception as e:
            log.debug("Failed to write verify report: %s", e)
        if mode == "e2e_verify":
            log.error("Reference verification failed: no Bubble snapshot available")
            sys.exit(1)
        return report

    type_maps = _build_snapshot_maps(snapshot)
    mapping_ctx = build_mapping_context(snapshot)

    candidate_sets: dict[str, set[str]] = {}
    for key in ("organization_tree_nodes", "naic_group_tree_nodes", "resource_type_tree_nodes", "recent_calendar_items"):
        items = mapping_ctx.get(key) or []
        ids = {str(it.get("id")).strip() for it in items if isinstance(it, dict) and it.get("id")}
        candidate_sets[key] = ids

    res_issues: list[dict[str, Any]] = []
    cal_issues: list[dict[str, Any]] = []
    strict_fail = False

    def _check_field(
        target: str,
        idx: int,
        obj: dict,
        cfg: dict,
    ) -> None:
        nonlocal strict_fail
        field = cfg["field"]
        ref_type = cfg["ref_type"]
        strict = cfg["strict"]
        single = cfg.get("single", False)
        candidate_key = cfg.get("candidate_key")

        if field not in obj:
            if strict:
                # Missing strict field counts as unresolved
                issues = [{"id": None, "reason": "unresolved_missing_field"}]
                entry = {"index": idx, "field": field, "strict": strict, "issues": issues}
                if target == "resource":
                    res_issues.append(entry)
                else:
                    cal_issues.append(entry)
                strict_fail = True
            return

        ids = _extract_ids(obj.get(field), single=single)
        issues: list[dict[str, Any]] = []

        if strict and not ids:
            issues.append({"id": None, "reason": "unresolved_empty"})

        # Existence in snapshot type map
        type_map = type_maps.get(ref_type, {})
        for rid in ids:
            if rid not in type_map:
                issues.append({"id": rid, "reason": "missing_in_snapshot"})

        # Candidate subset check
        if candidate_key and candidate_key in candidate_sets:
            allowed = candidate_sets[candidate_key]
            for rid in ids:
                if rid not in allowed:
                    issues.append({"id": rid, "reason": "not_in_candidates"})

        if issues:
            entry = {"index": idx, "field": field, "strict": strict, "issues": issues}
            if target == "resource":
                res_issues.append(entry)
            else:
                cal_issues.append(entry)
            if strict:
                strict_fail = True

    # Check resources
    for i, r in enumerate(resources):
        for cfg in REF_CONFIG:
            if cfg["target"] != "resource":
                continue
            _check_field("resource", i, r, cfg)

    # Check calendar items
    for i, c in enumerate(calendar_items):
        for cfg in REF_CONFIG:
            if cfg["target"] != "calendar":
                continue
            _check_field("calendar", i, c, cfg)

    report = {
        "resources": res_issues,
        "calendar_items": cal_issues,
        "summary": {
            "resource_issue_count": sum(len(e["issues"]) for e in res_issues),
            "calendar_issue_count": sum(len(e["issues"]) for e in cal_issues),
            "strict_fail": bool(strict_fail),
        },
    }

    try:
        DEBUG_VERIFY_REPORT.parent.mkdir(parents=True, exist_ok=True)
        DEBUG_VERIFY_REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8"))
    except Exception as e:
        log.debug("Failed to write verify report: %s", e)

    if mode == "e2e_verify" and strict_fail:
        log.error("Reference verification failed: strict issues present (see debug/verify_report.json)")
        sys.exit(1)

    return report

