"""
Reference resolution reporting: record resolution decisions and write debug report.
Used by enrich_refs resolvers (organization, type1, naic group, calendar linking).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# In-memory list of resolution records for the current run
_records: list[dict[str, Any]] = []

# Default report path
DEFAULT_REPORT_PATH = Path("debug") / "reference_resolution_report.json"


def ResolutionResult(
    kind: str,
    field: str,
    chosen_ids: list[str],
    candidates: list[str],
    status: str,
    evidence: dict[str, Any] | None = None,
    *,
    target: str = "Resource",
    index: int | None = None,
) -> dict[str, Any]:
    """
    Build a resolution result record (dict). Use record_resolution() to append.
    kind: "organization" | "type1" | "naic_group" | "calendar_linking"
    field: e.g. "Organization", "Type1", "NAIC Group (tree node)", "Related calendar items"
    chosen_ids: IDs that were chosen (list, may be empty).
    candidates: IDs or identifiers that were considered.
    status: "resolved" | "no_match" | "skipped" | "ai_override"
    evidence: optional dict with method, path, title, etc.
    """
    out: dict[str, Any] = {
        "kind": kind,
        "field": field,
        "chosen_ids": list(chosen_ids),
        "candidates": list(candidates),
        "status": status,
        "evidence": dict(evidence) if evidence else {},
    }
    if target:
        out["target"] = target
    if index is not None:
        out["index"] = index
    return out


def record_resolution(
    kind: str,
    field: str,
    chosen_ids: list[str],
    candidates: list[str],
    status: str,
    evidence: dict[str, Any] | None = None,
    *,
    target: str = "Resource",
    index: int | None = None,
) -> None:
    """Append a resolution decision to the in-memory list."""
    _records.append(
        ResolutionResult(
            kind=kind,
            field=field,
            chosen_ids=chosen_ids,
            candidates=candidates,
            status=status,
            evidence=evidence,
            target=target,
            index=index,
        )
    )


def get_records() -> list[dict[str, Any]]:
    """Return a copy of the current records (does not clear)."""
    return list(_records)


# Statuses that count as "resolved" for metric summary
_RESOLVED_STATUSES = frozenset({"resolved", "RESOLVED", "multi_resolved", "MULTI_RESOLVED", "ai_override"})


def get_resolution_summary(records: list[dict[str, Any]] | None = None) -> dict[str, dict[str, int]]:
    """
    Aggregate resolution records by field: resolved vs unresolved counts.
    records: default get_records(). Returns e.g. {"Organization": {"resolved": 5, "unresolved": 1}, ...}.
    """
    recs = records if records is not None else get_records()
    by_field: dict[str, dict[str, int]] = {}
    for r in recs:
        field = (r.get("field") or "unknown").strip() or "unknown"
        if field not in by_field:
            by_field[field] = {"resolved": 0, "unresolved": 0}
        status = (r.get("status") or "").strip()
        if status in _RESOLVED_STATUSES:
            by_field[field]["resolved"] += 1
        else:
            by_field[field]["unresolved"] += 1
    return by_field


def format_resolution_summary(by_field: dict[str, dict[str, int]]) -> str:
    """Human-readable per-field resolved/unresolved summary."""
    lines = ["Reference resolution (resolved / unresolved):"]
    for field in sorted(by_field.keys()):
        counts = by_field[field]
        r = counts.get("resolved", 0)
        u = counts.get("unresolved", 0)
        lines.append(f"  {field}: {r} resolved, {u} unresolved")
    return "\n".join(lines) if lines else "Reference resolution: no records"


def clear_records() -> None:
    """Clear the in-memory list (e.g. before a new run)."""
    _records.clear()


def write_reference_resolution_report(path: Path | str | None = None, records: list[dict[str, Any]] | None = None) -> None:
    """
    Write resolution records to debug/reference_resolution_report.json (or path).
    If records is None, uses the current in-memory list (get_records()).
    """
    out_path = Path(path) if path is not None else DEFAULT_REPORT_PATH
    data = records if records is not None else get_records()
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("Reference resolution report written to %s (%d records)", out_path, len(data))
    except Exception as e:
        log.debug("Failed to write reference resolution report: %s", e)
