"""
Build Bubble Resource and Calendar Item payloads from change events.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from bubble.schemas import CALENDAR_ITEM_SCHEMA_FIELDS, FULL_RESOURCE_SCHEMA_FIELDS

log = logging.getLogger(__name__)

_AGENDA_LINK_KEYWORDS = ("agenda", "materials", "minutes", "call", "webex")


def strip_debug_keys(obj: dict) -> dict:
    """
    Return a shallow copy of obj with any key starting with __ removed.
    Use before producing Bubble payload or writing last_bubble_*.json so debug-only
    fields (e.g. __meeting_meta, __key, __source) are not sent or persisted.
    """
    return {k: v for k, v in obj.items() if not k.startswith("__")}


def _fetch_url_bytes(url: str, timeout: int = 15) -> bytes | None:
    """Fetch URL and return raw bytes, or None on failure."""
    if not (url or "").strip():
        return None
    try:
        import requests
        r = requests.get(url.strip(), timeout=timeout)
        r.raise_for_status()
        return r.content
    except Exception as e:
        log.debug("Fetch PDF bytes failed for %s: %s", url[:80], type(e).__name__)
        return None


def apply_pdf_meeting_metadata(
    resources: list[dict],
    *,
    pdf_meeting_meta_enabled: bool,
    artifact_output_dir: str | None,
) -> None:
    """
    For PDF resources (VS Content Type == "PDF" or URL ends with .pdf), download bytes,
    call extract_meeting_metadata_from_pdf; if metadata returned: set date if null,
    set __meeting_meta (stripped by strip_debug_keys before Bubble payload).
    Writes artifact_output_dir/pdf_meeting_meta.json for auditing. Mutates resources.
    """
    if not pdf_meeting_meta_enabled or not resources:
        return
    try:
        from scrape.pdf_meeting_meta import extract_meeting_metadata_from_pdf, validate_meeting_meta
    except ImportError:
        log.warning("PDF meeting meta: scrape.pdf_meeting_meta not available, skipping")
        return

    artifact_entries: list[dict] = []
    meeting_meta_valid = 0
    meeting_meta_invalid = 0
    for r in resources:
        url = (r.get("URL") or "").strip()
        vs_type = (r.get("VS Content Type") or "").strip() if isinstance(r.get("VS Content Type"), str) else ""
        is_pdf = (vs_type and vs_type.upper() == "PDF") or (url.lower().endswith(".pdf") if url else False)
        if not is_pdf or not url:
            continue
        pdf_bytes = _fetch_url_bytes(url)
        if not pdf_bytes:
            continue
        meta = extract_meeting_metadata_from_pdf(url, pdf_bytes)
        if not meta:
            continue
        validation = validate_meeting_meta(meta)
        meta_dict: dict = {
            "group_name": meta.group_name,
            "date_iso": meta.date_iso,
            "start_time_local": meta.start_time_local,
            "end_time_local": meta.end_time_local,
            "timezone": meta.timezone,
            "valid": validation["valid"],
        }
        if not validation["valid"]:
            meeting_meta_invalid += 1
            meta_dict["rejection_reasons"] = validation["reasons"]
            if any("date_iso" in reason for reason in validation["reasons"]):
                meta_dict["date_iso"] = None
            if any("group_name" in reason for reason in validation["reasons"]):
                meta_dict["group_name"] = None
            r["__meeting_meta"] = meta_dict
            log.info("PDF meeting meta REJECTED: %s -> reasons=%s", url[:60], validation["reasons"])
            artifact_entries.append({
                "url": url,
                "Name": r.get("Name"),
                "meta": meta_dict,
                "date_set": False,
                "valid": False,
                "rejection_reasons": validation["reasons"],
            })
            continue

        meeting_meta_valid += 1
        r["__meeting_meta"] = meta_dict
        date_set = False
        if r.get("date") is None or (isinstance(r.get("date"), str) and not (r.get("date") or "").strip()):
            r["date"] = meta.date_iso
            date_set = True
        artifact_entries.append({
            "url": url,
            "Name": r.get("Name"),
            "meta": meta_dict,
            "date_set": date_set,
            "valid": True,
        })
        log.info("PDF meeting meta: %s -> date_iso=%s, group=%s", url[:60], meta.date_iso, (meta.group_name or "")[:40])

    log.info("PDF meeting meta summary: valid=%d  invalid=%d", meeting_meta_valid, meeting_meta_invalid)

    if artifact_entries and artifact_output_dir:
        out_path = Path(artifact_output_dir) / "pdf_meeting_meta.json"
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(artifact_entries, indent=2, ensure_ascii=False), encoding="utf-8")
            log.info("Wrote PDF meeting meta artifact: %s (%d entries)", out_path, len(artifact_entries))
        except Exception as e:
            log.warning("Failed to write pdf_meeting_meta.json: %s", e)


def apply_pdf_agenda_signals(
    resources: list[dict],
    *,
    artifact_output_dir: str | None = None,
) -> None:
    """
    For PDF resources, download bytes and extract agenda signals (ref numbers,
    numbered items, group hint, structure type). Stores results as
    ``__pdf_agenda_signals`` debug key on each resource (stripped before Bubble
    payload by ``strip_debug_keys``). Mutates resources in place.
    """
    if not resources:
        return
    try:
        from scrape.pdf_agenda_signals import extract_agenda_signals_from_bytes, signals_to_dict
    except ImportError:
        log.warning("PDF agenda signals: scrape.pdf_agenda_signals not available, skipping")
        return

    artifact_entries: list[dict] = []
    extracted_count = 0
    for r in resources:
        url = (r.get("URL") or "").strip()
        vs_type = (r.get("VS Content Type") or "").strip() if isinstance(r.get("VS Content Type"), str) else ""
        is_pdf = (vs_type and vs_type.upper() == "PDF") or (url.lower().endswith(".pdf") if url else False)
        if not is_pdf or not url:
            continue
        pdf_bytes = _fetch_url_bytes(url, timeout=15)
        if not pdf_bytes:
            continue
        signals = extract_agenda_signals_from_bytes(pdf_bytes)
        if signals is None:
            continue
        signals_dict = signals_to_dict(signals)
        r["__pdf_agenda_signals"] = signals_dict
        extracted_count += 1
        artifact_entries.append({
            "url": url,
            "Name": r.get("Name"),
            "signals": signals_dict,
        })
        log.debug(
            "PDF agenda signals: %s -> %d refs, %d items, structure=%s",
            url[:60], len(signals.ref_numbers), len(signals.numbered_items), signals.structure_type,
        )

    log.info("PDF agenda signals: extracted from %d/%d resources", extracted_count, len(resources))

    if artifact_entries and artifact_output_dir:
        out_path = Path(artifact_output_dir) / "pdf_agenda_signals.json"
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(artifact_entries, indent=2, ensure_ascii=False), encoding="utf-8")
            log.info("Wrote PDF agenda signals artifact: %s (%d entries)", out_path, len(artifact_entries))
        except Exception as e:
            log.warning("Failed to write pdf_agenda_signals.json: %s", e)


def validate_payload(schema_fields: list[str], obj: dict) -> dict:
    """
    Ensure NO extra keys and all schema keys are present.
    Missing keys are set to None (or [] for list fields).
    """
    result: dict = {}
    for k in schema_fields:
        if k in obj:
            result[k] = obj[k]
        elif k in (
            "Related calendar items",
            "attached agenda items",
            "Relevant Documents",
            "Agenda",
            "alerts",
            "Organization",
            "Type1",
        ):
            result[k] = obj.get(k) if obj.get(k) is not None else []
        else:
            result[k] = None
    return result


def _item_should_hide(item: dict, rtype: str) -> bool:
    """True if item should be excluded (denied URLs)."""
    from spike import _item_should_hide_from_report
    return _item_should_hide_from_report(item, rtype)


def _event_with_deduped_by_type(e: dict) -> dict:
    """Return event with change.by_type deduped (same as report)."""
    from spike import _event_with_deduped_by_type as _dedupe
    return _dedupe(e)


def _build_one_resource(item: dict, rtype: str, label: str, org_path: list) -> dict:
    """Build a single Bubble Resource object (docs, event_links, events only)."""
    org_label = org_path[0] if org_path else "NAIC"
    parent = " › ".join(org_path + [label]) if org_path else label
    name = (item.get("title") or item.get("label", "") or "").strip() or "Untitled"
    url = (item.get("url") or "").strip()
    section_label = {"docs": "Docs", "event_links": "Meeting Links", "events": "Meeting Links"}.get(rtype, "")
    notes = f"New {section_label.lower()[:-1]} in {label} ({section_label})"

    return {
        "archive": False,
        "Available To Vector Store": False,
        "Chunk Overlap": 200,
        "Chunk Size": 1000,
        "date": None,
        "Date display": None,
        "Name": name,
        "notes": notes,
        "Organization": org_label,
        "parent": parent,
        "Related calendar items": [],
        "URL": url,
    }


def _link_suggests_agenda_materials_webex(item: dict) -> bool:
    """True if link label or URL suggests agenda, materials, webex, or call."""
    text = " ".join([
        (item.get("title") or ""),
        (item.get("label") or ""),
        (item.get("url") or ""),
    ]).lower()
    return any(kw in text for kw in _AGENDA_LINK_KEYWORDS)


def _date_hints_from_text(text: str) -> set[str]:
    """Extract date-like substrings for matching (e.g. january 15, 2025-01-15, 01/15)."""
    if not text:
        return set()
    text = text.lower()
    hints: set[str] = set()
    # Month DD, YYYY or Month D, YYYY
    for m in re.finditer(r"(january|february|march|april|may|june|july|august|september|october|november|december)\s*(\d{1,2}),?\s*(\d{4})?", text):
        parts = [m.group(1), m.group(2)]
        if m.group(3):
            parts.append(m.group(3))
        hints.add(" ".join(parts))
    # YYYY-MM-DD
    for m in re.finditer(r"(\d{4})[-_](\d{2})[-_](\d{2})", text):
        hints.add(f"{m.group(1)}-{m.group(2)}-{m.group(3)}")
    # MM/DD/YYYY or MM-DD-YYYY
    for m in re.finditer(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})", text):
        hints.add(f"{m.group(3)}-{m.group(1).zfill(2)}-{m.group(2).zfill(2)}")
    return hints


def _meeting_date_hints(meeting: dict) -> set[str]:
    """Extract date hints from meeting date_text for matching."""
    date_text = (meeting.get("date_text") or "").strip()
    return _date_hints_from_text(date_text)


def _associate_links_to_meetings(
    meetings: list[dict],
    meeting_links: list[dict],
) -> dict[int, list[dict]]:
    """
    Associate agenda-like links to meetings. Returns {meeting_index: [links]}.
    Prefer links containing meeting date; fallback to first upcoming meeting.
    """
    result: dict[int, list[dict]] = {i: [] for i in range(len(meetings))}
    meeting_hints = [_meeting_date_hints(m) for m in meetings]

    for link in meeting_links:
        link_hints = _date_hints_from_text(
            " ".join([(link.get("title") or ""), (link.get("url") or "")])
        )
        best_idx = 0
        if link_hints and meeting_hints:
            for i, mh in enumerate(meeting_hints):
                if mh and link_hints & mh:
                    best_idx = i
                    break
        result[best_idx].append(link)

    return result


def _build_one_calendar_item(
    meeting: dict,
    label: str,
    org_path: list,
    agenda_links: list[dict] | None = None,
) -> dict:
    """Build a single Bubble Calendar Item object from a meeting."""
    title = (meeting.get("title") or "").strip() or f"{label} Meeting"
    date_text = (meeting.get("date_text") or "").strip()
    time_text = (meeting.get("time_text") or "").strip()
    expected_duration = (meeting.get("expected_duration") or "").strip()
    notes_val = (meeting.get("notes") or "").strip()

    # date: start_datetime if available, else date_text + time_text, else null
    date_val = None
    if date_text or time_text:
        date_val = f"{date_text} {time_text}".strip() if (date_text and time_text) else (date_text or time_text)

    # End time: end_datetime if available else null (we don't have it)
    end_time = None

    # length: from expected_duration if available
    length_val = expected_duration or None

    # Agenda: list of resource-like dicts {url, title} from associated meeting links
    agenda_list: list[dict] = []
    if agenda_links:
        for item in agenda_links:
            url = (item.get("url") or "").strip()
            if url:
                agenda_list.append({
                    "url": url,
                    "title": (item.get("title") or item.get("label") or "").strip() or "Agenda/Materials",
                })

    obj: dict = {
        "Agenda": agenda_list,
        "attached agenda items": [],
        "color": None,
        "date": date_val,
        "End time": end_time,
        "event description": notes_val,
        "full day": "no",
        "has topic": None,
        "length": length_val,
        "location": None,
        "NAIC Date/Meeting Type": None,
        "NAIC Group (legacy)": None,
        "NAIC Group (tree node)": " › ".join(org_path + [label]) if org_path else label,
        "no agenda type": None,
        "Outlook Event UID": None,
        "Outlook last sync": None,
        "outlook_icaluid": None,
        "phone_number_and_ac…": None,
        "Relevant Documents": [],
        "subtopic": None,
        "Timezone Code": "America/New_York",
        "title": title,
    }
    return obj


def build_resource_payload(diff: list[dict], targets: list[dict] | None = None) -> list[dict]:
    """
    Build Resource payload from added docs, event_links, and events.
    Meetings create Calendar Items, not Resources.
    """
    payload: list[dict] = []
    for e in diff:
        if "error" in e:
            continue
        label = e.get("label", "unknown")
        org_path = list(e.get("org_path") or [])

        deduped = _event_with_deduped_by_type(e)
        by_type = deduped.get("change", {}).get("by_type", {})

        for rtype in ("docs", "event_links", "events"):
            added = by_type.get(rtype, {}).get("added", [])
            for item in added:
                if _item_should_hide(item, rtype):
                    continue
                obj = _build_one_resource(item, rtype, label, org_path)
                payload.append(validate_payload(FULL_RESOURCE_SCHEMA_FIELDS, obj))

    return payload


def build_calendar_item_payload(diff: list[dict], targets: list[dict] | None = None) -> list[dict]:
    """
    Build Calendar Item payload from added meetings.
    Associates agenda/materials/webex meeting links (event_links, events) to meetings when possible.
    """
    payload: list[dict] = []
    total_attached = 0

    for e in diff:
        if "error" in e:
            continue
        label = e.get("label", "unknown")
        org_path = list(e.get("org_path") or [])

        deduped = _event_with_deduped_by_type(e)
        by_type = deduped.get("change", {}).get("by_type", {})
        meetings_added = by_type.get("meetings", {}).get("added", [])
        meeting_links: list[dict] = []
        for rtype in ("event_links", "events"):
            for item in by_type.get(rtype, {}).get("added", []):
                if _item_should_hide(item, rtype):
                    continue
                if _link_suggests_agenda_materials_webex(item):
                    meeting_links.append(item)

        visible_meetings = [m for m in meetings_added if not _item_should_hide(m, "meetings")]
        if not visible_meetings:
            continue

        association = _associate_links_to_meetings(visible_meetings, meeting_links)

        for i, meeting in enumerate(visible_meetings):
            agenda_for_meeting = association.get(i, [])
            total_attached += len(agenda_for_meeting)
            obj = _build_one_calendar_item(meeting, label, org_path, agenda_links=agenda_for_meeting)
            payload.append(validate_payload(CALENDAR_ITEM_SCHEMA_FIELDS, obj))

    if total_attached > 0:
        log.info("Attached %d meeting link(s) to Calendar Item(s)", total_attached)

    return payload


def build_resource_context(diff: list[dict]) -> list[dict]:
    """Return per-item context (org_id, org_path, label, url) in same order as build_resource_payload."""
    ctx: list[dict] = []
    for e in diff:
        if "error" in e:
            continue
        label = e.get("label", "unknown")
        org_path = list(e.get("org_path") or [])
        org_id = e.get("org_id")
        url = (e.get("url") or "").strip()
        deduped = _event_with_deduped_by_type(e)
        by_type = deduped.get("change", {}).get("by_type", {})
        for rtype in ("docs", "event_links", "events"):
            added = by_type.get(rtype, {}).get("added", [])
            for item in added:
                if _item_should_hide(item, rtype):
                    continue
                section_label = {"docs": "Docs", "event_links": "Meeting Links", "events": "Meeting Links"}.get(rtype, "")
                ctx.append({
                    "org_id": org_id,
                    "org_path": org_path,
                    "label": label,
                    "url": url,
                    "section_type": rtype,
                    "section_label": section_label,
                })
    return ctx


def build_calendar_item_context(diff: list[dict]) -> list[dict]:
    """Return per-item context (org_id, org_path, label, url) in same order as build_calendar_item_payload."""
    ctx: list[dict] = []
    for e in diff:
        if "error" in e:
            continue
        label = e.get("label", "unknown")
        org_path = list(e.get("org_path") or [])
        org_id = e.get("org_id")
        url = (e.get("url") or "").strip()
        deduped = _event_with_deduped_by_type(e)
        by_type = deduped.get("change", {}).get("by_type", {})
        meetings_added = by_type.get("meetings", {}).get("added", [])
        visible_meetings = [m for m in meetings_added if not _item_should_hide(m, "meetings")]
        for _ in visible_meetings:
            ctx.append({
                "org_id": org_id,
                "org_path": org_path,
                "label": label,
                "url": url,
            })
    return ctx
