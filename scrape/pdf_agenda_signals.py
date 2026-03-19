"""
Extract agenda-related signals from NAIC PDF meeting materials.
Reference numbers, numbered agenda items, group name hints, structure classification.
Reuses text extraction from pdf_meeting_meta (pypdf -> pdfminer.six fallback).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PdfAgendaSignals:
    """Agenda-related signals extracted from a PDF."""

    ref_numbers: tuple[str, ...]  # e.g. ("2024-16", "2024-22")
    numbered_items: tuple[str, ...]  # extracted agenda item text lines
    group_name_hint: str | None  # NAIC group detected in header
    has_agenda_header: bool  # "AGENDA" header detected
    structure_type: str  # formal_agenda | numbered_list | meeting_minutes | outline | informal | none


# ---------------------------------------------------------------------------
# Regex patterns (derived from analysis/pdf_agenda_detection)
# ---------------------------------------------------------------------------

# Ref number patterns — e.g. "SAPWG#2024-04", "Ref #2024-16", "#2024-22"
_KNOWN_GROUPS = (
    r"SAPWG|VOSTF|LATF|BWG|LRBCWG|RBC[-\s]?IRE|CATF|RAWG|SSWG"
)
_RE_REF_NUMBER = re.compile(
    rf"(?:(?:{_KNOWN_GROUPS})#?\s*(?:Ref\s*#?\s*)?(\d{{4}}[-\u2013]\d{{1,3}}))"
    r"|(?:(?:Ref|Reference|Item)\s*#?\s*(\d{4}[-\u2013]\d{1,3}))"
    r"|(?:#(\d{4}[-\u2013]\d{1,3}))",
    re.IGNORECASE,
)

# Numbered agenda items — "1. Some item text" or "1) Some item text"
_RE_NUMBERED_ITEM = re.compile(r"^\s*(\d{1,3})\s*[.)]\s+(.+)", re.MULTILINE)

# Agenda header
_RE_AGENDA_HEADER = re.compile(
    r"(?:^|\n)\s*(?:AGENDA|Agenda|Meeting\s+Agenda|MEETING\s+AGENDA)\s*(?:\n|$)",
    re.IGNORECASE,
)

# Roll call / opening — indicator of meeting minutes or formal agenda
_RE_ROLL_CALL = re.compile(
    r"roll\s+call|call\s+to\s+order|opening\s+remarks", re.IGNORECASE
)

# Group name keywords
_RE_GROUP_KEYWORDS = re.compile(
    r"TASK\s+FORCE|WORKING\s+GROUP|COMMITTEE|SUBGROUP|SUB-?GROUP",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_agenda_signals(pdf_text: str) -> PdfAgendaSignals:
    """
    Extract agenda signals from pre-extracted PDF text.

    Returns PdfAgendaSignals with ref numbers, numbered items, group hint,
    agenda header flag, and structure classification.
    """
    if not pdf_text or not pdf_text.strip():
        return PdfAgendaSignals((), (), None, False, "none")

    ref_numbers = _extract_ref_numbers(pdf_text)
    numbered_items = _extract_numbered_items(pdf_text)
    group_name_hint = _find_group_hint(pdf_text)
    has_agenda_header = bool(_RE_AGENDA_HEADER.search(pdf_text[:2000]))
    structure_type = _classify_structure(pdf_text, has_agenda_header, numbered_items)

    return PdfAgendaSignals(
        ref_numbers=tuple(ref_numbers),
        numbered_items=tuple(numbered_items[:50]),
        group_name_hint=group_name_hint,
        has_agenda_header=has_agenda_header,
        structure_type=structure_type,
    )


def extract_agenda_signals_from_bytes(pdf_bytes: bytes) -> PdfAgendaSignals | None:
    """
    Extract agenda signals from raw PDF bytes.
    Returns None if text extraction fails or PDF is empty.
    """
    if not pdf_bytes:
        return None
    try:
        from scrape.pdf_meeting_meta import _extract_plain_text
    except ImportError:
        return None
    text = _extract_plain_text(pdf_bytes)
    if not text.strip():
        return None
    return extract_agenda_signals(text)


def signals_to_dict(signals: PdfAgendaSignals) -> dict[str, Any]:
    """Convert PdfAgendaSignals to a JSON-serializable dict for debug keys."""
    return {
        "ref_numbers": list(signals.ref_numbers),
        "numbered_items": list(signals.numbered_items[:20]),  # truncate for debug
        "group_name_hint": signals.group_name_hint,
        "has_agenda_header": signals.has_agenda_header,
        "structure_type": signals.structure_type,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_ref_numbers(text: str) -> list[str]:
    """Extract unique reference numbers from PDF text."""
    seen: set[str] = set()
    result: list[str] = []
    for m in _RE_REF_NUMBER.finditer(text):
        ref = m.group(1) or m.group(2) or m.group(3)
        if ref:
            ref = ref.replace("\u2013", "-")  # normalize en-dash
            if ref not in seen:
                seen.add(ref)
                result.append(ref)
    return result


def _extract_numbered_items(text: str) -> list[str]:
    """Extract numbered agenda item text lines."""
    items: list[str] = []
    for m in _RE_NUMBERED_ITEM.finditer(text):
        item_text = m.group(2).strip()
        if item_text and len(item_text) > 3:
            items.append(item_text)
    return items


def _find_group_hint(text: str) -> str | None:
    """Find NAIC group name in first 50 lines of text."""
    lines = text.splitlines()[:50]
    for line in lines:
        line = line.strip()
        if len(line) < 5:
            continue
        # Skip lines that are clearly not group names
        if re.match(r"^\d", line) or "page" in line.lower():
            continue
        if line.lower().startswith("http"):
            continue
        if _RE_GROUP_KEYWORDS.search(line):
            return line.strip()[:120]
    return None


def _classify_structure(
    text: str, has_agenda_header: bool, numbered_items: list[str]
) -> str:
    """Classify the document structure type."""
    has_roll_call = bool(_RE_ROLL_CALL.search(text[:3000]))
    has_numbered = len(numbered_items) >= 2
    first_chunk = text[:3000]

    if has_agenda_header and has_numbered:
        return "formal_agenda"
    if has_roll_call and not has_numbered:
        if re.search(r"\b(?:was|were|discussed|reported|presented)\b", first_chunk, re.I):
            return "meeting_minutes"
    if has_numbered:
        return "numbered_list"
    if re.search(r"^\s*[IVX]+\.\s+", text, re.MULTILINE):
        return "outline"
    if re.search(r"\b(?:discuss|consider|review|report)\b", first_chunk, re.I):
        return "informal"
    return "none"
