#!/usr/bin/env python3
"""
Single-document inference inspector.

Given a local PDF file (or URL) and optional NAIC group context, run the
production inference logic and print a clear, human-readable report showing
what the system would assign.

Usage:
    python scripts/infer_pdf.py /path/to/file.pdf --naic-group "Statutory Accounting Principles Working Group"
    python scripts/infer_pdf.py https://content.naic.org/some/doc.pdf --naic-group "SAPWG"
    python scripts/infer_pdf.py /path/to/file.pdf  # auto-detect group from PDF

Outputs:
    - Human-readable report to stdout
    - Machine-readable artifact: debug/single_pdf_inference.json

Requires BUBBLE_API_URL and BUBBLE_API_KEY in environment (or .env).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("infer_pdf")

ARTIFACT_PATH = PROJECT_ROOT / "debug" / "single_pdf_inference.json"


# ---------------------------------------------------------------------------
# Bubble helpers (read-only)
# ---------------------------------------------------------------------------


def _get_client():
    from bubble.client import get_client
    return get_client(use_cache=True)


_node_name_cache: dict[str, str | None] = {}


def resolve_node_name(client, node_id: str) -> str | None:
    """Resolve a tree node ID to its display name."""
    if not node_id:
        return None
    if node_id in _node_name_cache:
        return _node_name_cache[node_id]
    try:
        obj = client.get("Tree node", node_id)
        name = (obj.get("name") or obj.get("Name") or "").strip()
        _node_name_cache[node_id] = name or None
        return name or None
    except Exception:
        _node_name_cache[node_id] = None
        return None


def resolve_calendar_item(client, cal_id: str) -> dict | None:
    """Resolve a calendar item ID to {title, date, id}."""
    if not cal_id:
        return None
    try:
        obj = client.get("Calendar item", cal_id)
        return {
            "id": cal_id,
            "title": (obj.get("title") or "").strip(),
            "date": obj.get("date"),
        }
    except Exception:
        return None


def resolve_naic_group(client, group_name: str) -> tuple[str | None, str | None]:
    """
    Resolve a NAIC group name to (node_id, resolved_name) using the same
    logic as the production pipeline.
    """
    from bubble.enrich_refs import (
        NAIC_GROUP_TREE_NAME,
        infer_naic_group_path,
        _resolve_naic_group_node,
    )
    # Try the full name as a single-segment path
    path = infer_naic_group_path(group_name)
    if not path:
        path = [group_name]

    # Try with and without NAIC prefix
    nid, evidence = _resolve_naic_group_node(NAIC_GROUP_TREE_NAME, path)
    if nid:
        name = evidence.get("chosen_raw_name") or resolve_node_name(client, nid) or group_name
        return nid, name

    # Try just the last segment (common pattern)
    if len(path) > 1:
        nid, evidence = _resolve_naic_group_node(NAIC_GROUP_TREE_NAME, [path[-1]])
        if nid:
            name = evidence.get("chosen_raw_name") or resolve_node_name(client, nid) or group_name
            return nid, name

    # Try with NAIC prefix
    nid, evidence = _resolve_naic_group_node(NAIC_GROUP_TREE_NAME, ["NAIC"] + path)
    if nid:
        name = evidence.get("chosen_raw_name") or resolve_node_name(client, nid) or group_name
        return nid, name

    return None, None


# ---------------------------------------------------------------------------
# PDF loading
# ---------------------------------------------------------------------------


def load_pdf_bytes(source: str) -> tuple[bytes | None, str]:
    """
    Load PDF bytes from a file path or URL.
    Returns (bytes, source_description).
    """
    if source.startswith("http://") or source.startswith("https://"):
        try:
            import requests
            resp = requests.get(source, timeout=30)
            resp.raise_for_status()
            return resp.content, source
        except Exception as e:
            print(f"ERROR: Failed to download {source}: {e}", file=sys.stderr)
            return None, source
    else:
        p = Path(source).expanduser().resolve()
        if not p.exists():
            print(f"ERROR: File not found: {p}", file=sys.stderr)
            return None, str(p)
        return p.read_bytes(), str(p)


# ---------------------------------------------------------------------------
# Core inference
# ---------------------------------------------------------------------------


def run_inference(
    pdf_bytes: bytes,
    source_desc: str,
    naic_group_name: str | None = None,
    resource_name: str | None = None,
    meeting_date: str | None = None,
    use_ai: bool = False,
) -> dict[str, Any]:
    """
    Run the full production inference pipeline on a single PDF.
    Returns a structured result dict with all signals and decisions.
    """
    from scrape.pdf_agenda_signals import extract_agenda_signals_from_bytes, signals_to_dict
    from scrape.pdf_meeting_meta import extract_meeting_metadata_from_pdf, validate_meeting_meta
    from bubble.enrich_refs import (
        TOPIC_TREE_NAME,
        NAIC_GROUP_TREE_NAME,
        CALENDAR_NAIC_GROUP_WINDOW_DAYS,
        CALENDAR_NAIC_GROUP_NO_DATE_CAP,
        _build_topic_candidates,
        _resolve_agenda_items_for_resource,
        _resolve_topic_enhanced,
        _resolve_calendar_by_naic_group,
        _parse_calendar_title_topics,
        infer_naic_group_path,
    )
    from bubble import lookups

    client = _get_client()
    result: dict[str, Any] = {
        "source": source_desc,
        "resource_name": resource_name or Path(source_desc).stem,
    }

    # 1. Extract meeting metadata (date, group, time)
    meeting_meta = extract_meeting_metadata_from_pdf(source_desc, pdf_bytes)
    if meeting_meta:
        validation = validate_meeting_meta(meeting_meta)
        result["meeting_meta"] = {
            "group_name": meeting_meta.group_name,
            "date_iso": meeting_meta.date_iso,
            "start_time": meeting_meta.start_time_local,
            "end_time": meeting_meta.end_time_local,
            "timezone": meeting_meta.timezone,
            "valid": validation["valid"],
        }
    else:
        result["meeting_meta"] = None

    # 2. Extract PDF agenda signals
    agenda_signals = extract_agenda_signals_from_bytes(pdf_bytes)
    if agenda_signals:
        result["pdf_signals"] = signals_to_dict(agenda_signals)
    else:
        result["pdf_signals"] = None

    # 3. Resolve NAIC group
    group_node_id = None
    group_resolved_name = None
    group_source = None

    if naic_group_name:
        group_node_id, group_resolved_name = resolve_naic_group(client, naic_group_name)
        group_source = "cli_argument"
    if not group_node_id and meeting_meta and meeting_meta.group_name:
        group_node_id, group_resolved_name = resolve_naic_group(client, meeting_meta.group_name)
        group_source = "pdf_meeting_meta"
    if not group_node_id and agenda_signals and agenda_signals.group_name_hint:
        group_node_id, group_resolved_name = resolve_naic_group(client, agenda_signals.group_name_hint)
        group_source = "pdf_agenda_signal"

    result["naic_group"] = {
        "node_id": group_node_id,
        "name": group_resolved_name,
        "source": group_source,
    }

    # 4. Search for related calendar items
    date_iso = meeting_date
    if not date_iso and meeting_meta and meeting_meta.date_iso:
        date_iso = meeting_meta.date_iso

    calendar_items: list[dict] = []
    if group_node_id:
        resource_context = {
            "org_path": ["NAIC"],
            "label": group_resolved_name or naic_group_name or "",
        }
        cal_ids, cal_detail, cal_status, cal_evidence = _resolve_calendar_by_naic_group(
            resource_context,
            NAIC_GROUP_TREE_NAME,
            date_iso,
            window_days=CALENDAR_NAIC_GROUP_WINDOW_DAYS,
            no_date_cap=CALENDAR_NAIC_GROUP_NO_DATE_CAP,
        )
        for cid in cal_ids:
            resolved = resolve_calendar_item(client, cid)
            if resolved:
                calendar_items.append(resolved)
            else:
                calendar_items.append({"id": cid, "title": "(unresolved)", "date": None})

    result["calendar_items"] = calendar_items

    # 5. Agenda item matching
    r_name = resource_name or Path(source_desc).name
    resource = {
        "Name": r_name,
        "URL": source_desc if source_desc.startswith("http") else "",
        "notes": "",
        "parent": "",
    }
    if agenda_signals:
        resource["__pdf_agenda_signals"] = signals_to_dict(agenda_signals)

    agenda_result = _resolve_agenda_items_for_resource(
        resource,
        {"label": group_resolved_name or "", "org_path": ["NAIC"]},
        group_node_id,
        bubble_snapshot=None,
        use_ai=use_ai,
    )

    # Resolve agenda item details
    agenda_items_resolved: list[dict] = []
    for aid in agenda_result.get("matched_ids") or []:
        item = lookups.get_agenda_item(aid)
        if item:
            agenda_items_resolved.append({
                "id": aid,
                "ba_title": (item.get("BA title") or "").strip(),
                "naic_title": (item.get("NAIC Title") or "").strip(),
                "ref": (item.get("BA Ref #") or item.get("Ref #") or "").strip(),
                "topics": item.get("Topics") or [],
                "category": (item.get("Category") or "").strip() if isinstance(item.get("Category"), str) else "",
            })
        else:
            agenda_items_resolved.append({"id": aid, "ba_title": "(unresolved)", "ref": ""})

    result["agenda_match"] = {
        "method": agenda_result["method"],
        "retrieval_source": agenda_result.get("retrieval_source", "unknown"),
        "ai_used": agenda_result["ai_used"],
        "matched_items": agenda_items_resolved,
        "candidates": agenda_result["candidates"][:15],
        "inherited_topic_ids": agenda_result["inherited_topic_ids"],
    }

    # 6. Resolve inherited topic names
    inherited_topics: list[dict] = []
    for tid in agenda_result.get("inherited_topic_ids") or []:
        name = resolve_node_name(client, tid)
        inherited_topics.append({"id": tid, "name": name or "(unresolved)"})
    result["inherited_topics"] = inherited_topics

    # 7. Calendar title topic parsing (supplemental)
    supplemental_topics: list[dict] = []
    topic_candidates = _build_topic_candidates(TOPIC_TREE_NAME)
    for cal in calendar_items:
        cal_title = cal.get("title") or ""
        parsed = _parse_calendar_title_topics(cal_title)
        if parsed:
            from bubble.enrich_refs import _fuzzy_match_topic_to_candidates
            matched = _fuzzy_match_topic_to_candidates(parsed, topic_candidates)
            for name, nid in matched:
                if nid not in [t["id"] for t in inherited_topics + supplemental_topics]:
                    supplemental_topics.append({"id": nid, "name": name, "source": "calendar_title"})
    result["supplemental_topics"] = supplemental_topics

    # 8. Enhanced topic suggestion
    calendar_payload = [
        {"_id": c["id"], "title": c.get("title", ""), "date": c.get("date")}
        for c in calendar_items
    ]
    topic_result = _resolve_topic_enhanced(
        resource,
        {"label": group_resolved_name or "", "org_path": ["NAIC"]},
        topic_candidates,
        matched_agenda_items_result={
            "matched_ids": agenda_result["matched_ids"],
            "inherited_topic_ids": agenda_result["inherited_topic_ids"],
            "method": agenda_result["method"],
        } if agenda_result["matched_ids"] else None,
        calendar_payload=calendar_payload,
        linked_calendar_ids=[c["id"] for c in calendar_items],
        use_ai=use_ai,
    )

    final_topic_id = topic_result.get("topic_id")
    final_topic_name = topic_result.get("topic_name")
    if final_topic_id and not final_topic_name:
        final_topic_name = resolve_node_name(client, final_topic_id)

    result["final_topic"] = {
        "id": final_topic_id,
        "name": final_topic_name,
        "source": topic_result.get("source", "unresolved"),
    }

    return result


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------


def _name_id(name: str | None, nid: str | None) -> str:
    """Format 'Name (id)' or just 'Name' or '(unresolved)'."""
    if name and nid:
        return f"{name} ({nid[:20]}...)"
    if name:
        return name
    if nid:
        return f"(id: {nid[:20]}...)"
    return "(none)"


def print_report(result: dict) -> None:
    """Print a clean, human-readable inference report."""
    W = 72
    print("=" * W)
    print("  SINGLE-DOCUMENT INFERENCE REPORT")
    print("=" * W)
    print()

    # Source
    print(f"File: {result['source']}")
    print(f"Resource name: {result.get('resource_name', '?')}")
    print()

    # Meeting metadata
    meta = result.get("meeting_meta")
    if meta:
        print("--- Meeting Metadata (from PDF) ---")
        print(f"  Group name:  {meta.get('group_name') or '(none)'}")
        print(f"  Date:        {meta.get('date_iso') or '(none)'}")
        if meta.get("start_time"):
            time_str = meta["start_time"]
            if meta.get("end_time"):
                time_str += f" - {meta['end_time']}"
            if meta.get("timezone"):
                time_str += f" {meta['timezone']}"
            print(f"  Time:        {time_str}")
        print(f"  Valid:       {'Yes' if meta.get('valid') else 'No'}")
        print()

    # PDF agenda signals
    signals = result.get("pdf_signals")
    if signals:
        print("--- PDF Agenda Signals ---")
        print(f"  Structure type:    {signals.get('structure_type', 'none')}")
        print(f"  Agenda header:     {'Yes' if signals.get('has_agenda_header') else 'No'}")
        print(f"  Group name hint:   {signals.get('group_name_hint') or '(none)'}")
        refs = signals.get("ref_numbers") or []
        if refs:
            print(f"  Reference numbers: {', '.join(refs)}")
        else:
            print("  Reference numbers: (none)")
        items = signals.get("numbered_items") or []
        if items:
            print(f"  Numbered items:    {len(items)} detected")
            for item in items[:8]:
                print(f"    - {item[:80]}")
            if len(items) > 8:
                print(f"    ... and {len(items) - 8} more")
        print()
    else:
        print("--- PDF Agenda Signals ---")
        print("  (no signals extracted)")
        print()

    # NAIC group
    grp = result.get("naic_group") or {}
    print("--- NAIC Group ---")
    print(f"  Detected: {_name_id(grp.get('name'), grp.get('node_id'))}")
    print(f"  Source:   {grp.get('source') or '(not resolved)'}")
    print()

    # Calendar items
    cal_items = result.get("calendar_items") or []
    print("--- Related Calendar Items ---")
    if cal_items:
        for c in cal_items:
            date_str = f" [{c.get('date', '?')[:10]}]" if c.get("date") else ""
            print(f"  - {c.get('title', '?')}{date_str} ({c.get('id', '?')[:20]}...)")
    else:
        print("  (none found)")
    print()

    # Agenda item candidates
    candidates = (result.get("agenda_match") or {}).get("candidates") or []
    retrieval_source = (result.get("agenda_match") or {}).get("retrieval_source", "unknown")
    print(f"--- Agenda Item Candidates (retrieval: {retrieval_source}) ---")
    if candidates:
        for c in candidates[:10]:
            score = c.get("score", 0)
            ref = c.get("ref", "")
            title = c.get("ba_title", "")[:60]
            ref_str = f" [Ref: {ref}]" if ref else ""
            src = c.get("retrieval_source", "")
            src_tag = f" ({src})" if src and retrieval_source == "ref_fallback" else ""
            print(f"  score={score:5.1f}  {title}{ref_str}{src_tag}")
    else:
        print("  (no candidates)")
    print()

    # Matched agenda items
    matched = (result.get("agenda_match") or {}).get("matched_items") or []
    method = (result.get("agenda_match") or {}).get("method", "none")
    ai_used = (result.get("agenda_match") or {}).get("ai_used", False)
    print("--- Matched Agenda Items ---")
    print(f"  Method: {method}" + (" (AI ranking used)" if ai_used else ""))
    if matched:
        for m in matched:
            ref = f" [Ref: {m.get('ref', '')}]" if m.get("ref") else ""
            title = m.get("ba_title") or m.get("naic_title") or "(untitled)"
            print(f"  - {title}{ref}")
            if m.get("naic_title") and m.get("ba_title") and m["naic_title"] != m["ba_title"]:
                print(f"    NAIC Title: {m['naic_title']}")
            if m.get("category"):
                print(f"    Category: {m['category']}")
    else:
        print("  (no matches)")
    print()

    # Topics
    inherited = result.get("inherited_topics") or []
    supplemental = result.get("supplemental_topics") or []
    final = result.get("final_topic") or {}

    print("--- Chronicle Topics ---")
    print()
    print("  Inherited topics (from matched agenda items):")
    if inherited:
        for t in inherited:
            print(f"    - {t.get('name', '?')} ({t.get('id', '?')[:20]}...)")
    else:
        print("    (none)")
    print()
    print("  Supplemental topic candidates (from calendar title):")
    if supplemental:
        for t in supplemental:
            print(f"    - {t.get('name', '?')} (source: {t.get('source', '?')})")
    else:
        print("    (none)")
    print()
    print("  Final topic suggestion:")
    if final.get("name"):
        print(f"    {final['name']} (source: {final.get('source', '?')})")
    else:
        print(f"    (unresolved, source: {final.get('source', 'none')})")
    print()

    # Summary block
    print("=" * W)
    print("  SUMMARY")
    print("=" * W)
    print(f"  File:                      {result['source']}")
    print(f"  Detected NAIC Group:       {grp.get('name') or '(none)'}")
    cal_summary = "; ".join(c.get("title", "?") for c in cal_items) if cal_items else "(none)"
    print(f"  Related Calendar Items:    {cal_summary}")
    agenda_summary = "; ".join(
        (m.get("ba_title") or "?") + (f" [{m.get('ref')}]" if m.get("ref") else "")
        for m in matched
    ) if matched else "(none)"
    print(f"  Matched Agenda Items:      {agenda_summary}")
    inherited_summary = "; ".join(t.get("name", "?") for t in inherited) if inherited else "(none)"
    print(f"  Inherited Topics:          {inherited_summary}")
    supp_summary = "; ".join(t.get("name", "?") for t in supplemental) if supplemental else "(none)"
    print(f"  Supplemental Candidates:   {supp_summary}")
    final_name = final.get("name") or "(none)"
    final_src = final.get("source") or "unresolved"
    print(f"  Final Topic Suggestions:   {final_name} (via {final_src})")
    print("=" * W)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Single-document inference inspector. Runs production logic on a PDF and shows results.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python scripts/infer_pdf.py meeting_agenda.pdf --naic-group "SAPWG"
  python scripts/infer_pdf.py https://content.naic.org/doc.pdf --naic-group "Capital Adequacy Task Force"
  python scripts/infer_pdf.py materials.pdf  # auto-detect group from PDF content
""",
    )
    parser.add_argument(
        "pdf_source",
        help="Path to a local PDF file, or a URL to download",
    )
    parser.add_argument(
        "--naic-group",
        default=None,
        help="NAIC group name (e.g. 'Statutory Accounting Principles Working Group'). "
             "If omitted, the system will try to detect the group from the PDF.",
    )
    parser.add_argument(
        "--resource-name",
        default=None,
        help="Override the resource name (defaults to filename)",
    )
    parser.add_argument(
        "--meeting-date",
        default=None,
        help="Meeting date (ISO format: YYYY-MM-DD). Helps narrow calendar item search.",
    )
    parser.add_argument(
        "--output",
        default=str(ARTIFACT_PATH),
        help=f"Path for JSON artifact (default: {ARTIFACT_PATH})",
    )
    parser.add_argument(
        "--use-ai",
        action="store_true",
        help="Enable AI fallback for agenda item matching and topic suggestion",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.INFO)

    # Load PDF
    pdf_bytes, source_desc = load_pdf_bytes(args.pdf_source)
    if pdf_bytes is None:
        return 1

    print(f"Loaded PDF: {len(pdf_bytes):,} bytes from {source_desc}")
    print()

    # Run inference
    try:
        result = run_inference(
            pdf_bytes,
            source_desc,
            naic_group_name=args.naic_group,
            resource_name=args.resource_name,
            meeting_date=args.meeting_date,
            use_ai=args.use_ai,
        )
    except Exception as e:
        print(f"ERROR: Inference failed: {e}", file=sys.stderr)
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1

    # Print human-readable report
    print_report(result)

    # Write machine-readable artifact
    out_path = Path(args.output)
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(result, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        print(f"\nArtifact written: {out_path}")
    except Exception as e:
        print(f"WARNING: Failed to write artifact: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
