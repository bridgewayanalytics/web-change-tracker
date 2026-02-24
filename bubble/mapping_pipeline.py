"""
4-stage mapping pipeline: deterministic mapping → candidate assembly →
AI selection (bounded to candidate IDs) → verification gate.
Uses bubble/lookups, enrich_refs, mapping_context, ai_enrichment, schemas.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

from bubble import enrich_refs
from bubble import mapping_context

log = logging.getLogger(__name__)

DEBUG_VERIFY_REPORT = Path("debug") / "verify_report.json"

# Reference fields that must contain only IDs from the candidate set
REFERENCE_FIELDS_RESOURCE = ["Organization", "Type1", "topic suggestion", "Related calendar items"]
REFERENCE_FIELDS_CALENDAR = ["NAIC Group (tree node)"]


def _normalize_id(val: Any) -> str | None:
    """Return string id or None."""
    if val is None:
        return None
    if isinstance(val, str) and val.strip():
        return val.strip()
    if isinstance(val, dict):
        return val.get("_id") or val.get("id")
    return None


def _ids_from_list(val: Any) -> list[str]:
    """Extract list of string ids from a field value."""
    if val is None:
        return []
    if isinstance(val, str) and val.strip():
        return [val.strip()]
    if isinstance(val, list):
        out = []
        for x in val:
            sid = _normalize_id(x)
            if sid:
                out.append(sid)
        return out
    return []


def _single_id(val: Any) -> str | None:
    """Extract single id from a field value."""
    if val is None:
        return None
    if isinstance(val, str) and val.strip():
        return val.strip()
    if isinstance(val, list) and len(val) > 0:
        return _normalize_id(val[0])
    return _normalize_id(val)


# ---------------------------------------------------------------------------
# Stage 1: Deterministic mapping
# ---------------------------------------------------------------------------


def deterministic_mapping(
    resources: list[dict],
    calendar_items: list[dict],
    resource_context: list[dict],
    calendar_context: list[dict],
    *,
    bubble_snapshot: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    Apply only deterministic reference resolution (no AI).
    Uses enrich_refs with use_ai=False.
    """
    return enrich_refs.enrich_refs(
        resources,
        calendar_items,
        resource_context,
        calendar_context,
        use_ai=False,
        bubble_snapshot=bubble_snapshot,
    )


# ---------------------------------------------------------------------------
# Stage 2: Candidate assembly
# ---------------------------------------------------------------------------


def assemble_candidates(bubble_snapshot: dict | None) -> dict[str, Any]:
    """
    Build mapping context and allowed_reference_ids from snapshot.
    Returns {"mapping_context": {...}, "allowed_ids": frozenset[str]}.
    If snapshot is None or empty, allowed_ids is empty (verification will reject all refs).
    """
    if not bubble_snapshot:
        return {"mapping_context": {}, "allowed_ids": frozenset()}
    ctx = mapping_context.build_mapping_context(bubble_snapshot)
    allowed: set[str] = set()
    for key in (
        "organization_tree_nodes",
        "naic_group_tree_nodes",
        "resource_type_tree_nodes",
        "recent_calendar_items",
    ):
        for item in ctx.get(key) or []:
            iid = item.get("id") if isinstance(item, dict) else None
            if iid:
                allowed.add(str(iid).strip())
    return {"mapping_context": ctx, "allowed_ids": frozenset(allowed)}


# ---------------------------------------------------------------------------
# Stage 3: AI selection (bounded by verification in stage 4)
# ---------------------------------------------------------------------------


def ai_selection_stage(
    resources: list[dict],
    calendar_items: list[dict],
    resource_context: list[dict],
    calendar_context: list[dict],
    *,
    use_ai: bool = True,
    bubble_snapshot: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    Run AI-based reference selection. Output is not yet verified;
    stage 4 (verification_gate) will reject any ID not in candidates.
    When use_ai=False, returns inputs unchanged.
    """
    if not use_ai:
        return (list(resources), list(calendar_items))
    return enrich_refs.enrich_refs(
        resources,
        calendar_items,
        resource_context,
        calendar_context,
        use_ai=True,
        bubble_snapshot=bubble_snapshot,
    )


# ---------------------------------------------------------------------------
# Stage 4: Verification gate
# ---------------------------------------------------------------------------


def verification_gate(
    resources: list[dict],
    calendar_items: list[dict],
    allowed_ids: frozenset[str],
    *,
    fallback_resources: list[dict] | None = None,
    fallback_calendar: list[dict] | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    Ensure reference fields only contain IDs from allowed_ids.
    Invalid IDs are replaced with the fallback value for that field (from
    deterministic stage), or empty list / None if no fallback.
    """
    fallback_resources = fallback_resources or []
    fallback_calendar = fallback_calendar or []

    def filter_ids(ids_list: list[str]) -> list[str]:
        return [i for i in ids_list if i in allowed_ids]

    out_resources: list[dict] = []
    for i, r in enumerate(resources):
        out = dict(r)
        fb = fallback_resources[i] if i < len(fallback_resources) else {}
        for field in REFERENCE_FIELDS_RESOURCE:
            if field not in out:
                continue
            val = out[field]
            if field == "topic suggestion":
                sid = _single_id(val)
                if sid and sid not in allowed_ids:
                    fb_sid = _single_id(fb.get(field)) if fb else None
                    out[field] = fb_sid if fb_sid and fb_sid in allowed_ids else None
                elif sid is None and val is not None and (isinstance(val, str) and val.strip() or isinstance(val, list) and val):
                    out[field] = None
            else:
                ids = _ids_from_list(val)
                filtered = filter_ids(ids)
                if ids and not filtered:
                    fallback_val = fb.get(field) if fb else []
                    out[field] = filter_ids(_ids_from_list(fallback_val))
                else:
                    out[field] = filtered
        out_resources.append(out)

    out_calendar: list[dict] = []
    for i, c in enumerate(calendar_items):
        out = dict(c)
        fb = fallback_calendar[i] if i < len(fallback_calendar) else {}
        for field in REFERENCE_FIELDS_CALENDAR:
            if field not in out:
                continue
            sid = _single_id(out[field])
            if sid and sid not in allowed_ids:
                fb_sid = _single_id(fb.get(field)) if fb else None
                out[field] = fb_sid if fb_sid and fb_sid in allowed_ids else None
            elif sid and sid in allowed_ids:
                out[field] = sid
        out_calendar.append(out)

    return (out_resources, out_calendar)


# ---------------------------------------------------------------------------
# verify_all_references: run gate + report, optional strict exit
# ---------------------------------------------------------------------------

_SINGLE_ID_FIELDS_RESOURCE = frozenset({"topic suggestion"})
_SINGLE_ID_FIELDS_CALENDAR = frozenset({"NAIC Group (tree node)"})


def _empty_ref_fallback_resources(resources: list[dict]) -> list[dict]:
    """Copy resources with all reference fields set to [] or None."""
    out: list[dict] = []
    for r in resources:
        fb = dict(r)
        for field in REFERENCE_FIELDS_RESOURCE:
            if field in fb:
                fb[field] = None if field in _SINGLE_ID_FIELDS_RESOURCE else []
        out.append(fb)
    return out


def _empty_ref_fallback_calendar(calendar_items: list[dict]) -> list[dict]:
    """Copy calendar items with all reference fields set to [] or None."""
    out: list[dict] = []
    for c in calendar_items:
        fb = dict(c)
        for field in REFERENCE_FIELDS_CALENDAR:
            if field in fb:
                fb[field] = None if field in _SINGLE_ID_FIELDS_CALENDAR else []
        out.append(fb)
    return out


def _dropped_ids_report(
    before_resources: list[dict],
    after_resources: list[dict],
    before_calendar: list[dict],
    after_calendar: list[dict],
) -> dict[str, Any]:
    """Build report of dropped IDs per index/field."""
    resources_report: list[dict] = []
    for i in range(max(len(before_resources), len(after_resources))):
        br = before_resources[i] if i < len(before_resources) else {}
        ar = after_resources[i] if i < len(after_resources) else {}
        for field in REFERENCE_FIELDS_RESOURCE:
            if field not in br:
                continue
            before_ids = [_single_id(br[field])] if field in _SINGLE_ID_FIELDS_RESOURCE else _ids_from_list(br[field])
            after_ids = [_single_id(ar.get(field))] if field in _SINGLE_ID_FIELDS_RESOURCE else _ids_from_list(ar.get(field))
            before_set = {x for x in before_ids if x}
            after_set = {x for x in after_ids if x}
            dropped = sorted(before_set - after_set)
            if dropped:
                resources_report.append({"index": i, "field": field, "dropped_ids": dropped})
    calendar_report: list[dict] = []
    for i in range(max(len(before_calendar), len(after_calendar))):
        bc = before_calendar[i] if i < len(before_calendar) else {}
        ac = after_calendar[i] if i < len(after_calendar) else {}
        for field in REFERENCE_FIELDS_CALENDAR:
            if field not in bc:
                continue
            before_id = _single_id(bc[field])
            after_id = _single_id(ac.get(field))
            if before_id and before_id != after_id:
                calendar_report.append({"index": i, "field": field, "dropped_ids": [before_id]})
    return {"resources": resources_report, "calendar_items": calendar_report}


def verify_all_references(
    resources: list[dict],
    calendar_items: list[dict],
    bubble_snapshot: dict | None,
    *,
    mode: str = "normal",
    artifact_output_dir: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    Validate all reference fields against allowed_ids (from snapshot). Invalid IDs are dropped
    and replaced with fallback (empty). Writes verify_report.json to artifact_output_dir or debug/.
    mode: "normal" -> drop invalid, log warnings; "e2e_verify" -> drop invalid, exit non-zero if any.
    When bubble_snapshot is None: in e2e_verify exit non-zero; else skip and return unchanged.
    """
    report_path = (Path(artifact_output_dir) / "verify_report.json") if artifact_output_dir else DEBUG_VERIFY_REPORT
    if bubble_snapshot is None:
        report = {"skipped": True, "reason": "no snapshot", "invalid_dropped": {"resources": [], "calendar_items": []}}
        try:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        except Exception as e:
            log.debug("Failed to write verify report: %s", e)
        if mode == "e2e_verify":
            log.error("Cannot verify references without Bubble snapshot (use --e2e-bubble with --e2e-bubble-verify)")
            sys.exit(1)
        return (list(resources), list(calendar_items))

    candidates = assemble_candidates(bubble_snapshot)
    allowed_ids = candidates["allowed_ids"]
    fallback_resources = _empty_ref_fallback_resources(resources)
    fallback_calendar = _empty_ref_fallback_calendar(calendar_items)

    verified_resources, verified_calendar = verification_gate(
        [dict(r) for r in resources],
        [dict(c) for c in calendar_items],
        allowed_ids,
        fallback_resources=fallback_resources,
        fallback_calendar=fallback_calendar,
    )
    invalid_dropped = _dropped_ids_report(
        resources, verified_resources, calendar_items, verified_calendar
    )
    report = {
        "allowed_ids_count": len(allowed_ids),
        "invalid_dropped": invalid_dropped,
    }
    if invalid_dropped["resources"] or invalid_dropped["calendar_items"]:
        for item in invalid_dropped["resources"]:
            for did in item["dropped_ids"]:
                log.warning(
                    "Reference verification: dropped invalid ID %r from Resource[%d].%s",
                    did, item["index"], item["field"],
                )
        for item in invalid_dropped["calendar_items"]:
            for did in item["dropped_ids"]:
                log.warning(
                    "Reference verification: dropped invalid ID %r from CalendarItem[%d].%s",
                    did, item["index"], item["field"],
                )
        if mode == "e2e_verify":
            log.error("Reference verification failed: invalid IDs found (see %s)", report_path)
            try:
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            except Exception as e:
                log.debug("Failed to write verify report: %s", e)
            sys.exit(1)
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    except Exception as e:
        log.debug("Failed to write verify report: %s", e)
    return (verified_resources, verified_calendar)


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------


def run_pipeline(
    resources: list[dict],
    calendar_items: list[dict],
    resource_context: list[dict],
    calendar_context: list[dict],
    *,
    bubble_snapshot: dict | None = None,
    use_ai: bool = True,
) -> tuple[list[dict], list[dict]]:
    """
    Run the 4-stage pipeline:
    1. Deterministic mapping
    2. Candidate assembly (from snapshot)
    3. AI selection (bounded; invalid IDs stripped in stage 4)
    4. Verification gate (only candidate IDs allowed; fallback to deterministic)
    """
    det_resources, det_calendar = deterministic_mapping(
        resources,
        calendar_items,
        resource_context,
        calendar_context,
        bubble_snapshot=bubble_snapshot,
    )
    candidates = assemble_candidates(bubble_snapshot)
    allowed_ids = candidates["allowed_ids"]

    ai_resources, ai_calendar = ai_selection_stage(
        det_resources,
        det_calendar,
        resource_context,
        calendar_context,
        use_ai=use_ai,
        bubble_snapshot=bubble_snapshot,
    )

    return verification_gate(
        ai_resources,
        ai_calendar,
        allowed_ids,
        fallback_resources=det_resources,
        fallback_calendar=det_calendar,
    )
