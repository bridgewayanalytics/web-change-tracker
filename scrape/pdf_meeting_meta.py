"""
Deterministic extraction of meeting metadata from NAIC-style PDF meeting packets.
Uses plain text from the PDF (pypdf first, pdfminer.six fallback). No AI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO


@dataclass(frozen=True)
class MeetingMeta:
    """Normalized meeting metadata extracted from a PDF."""

    group_name: str
    date_iso: str  # YYYY-MM-DD
    start_time_local: str | None  # HH:MM
    end_time_local: str | None  # HH:MM
    timezone: str | None  # e.g. "ET", "CT"


# ---------------------------------------------------------------------------
# Text extraction (pypdf first, then pdfminer.six)
# ---------------------------------------------------------------------------


def _extract_text_pypdf(pdf_bytes: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(pdf_bytes))
    parts = []
    for page in reader.pages:
        t = page.extract_text()
        if t:
            parts.append(t)
    return "\n".join(parts) if parts else ""


def _extract_text_pdfminer(pdf_bytes: bytes) -> str:
    from pdfminer.high_level import extract_text

    return extract_text(BytesIO(pdf_bytes)) or ""


def _extract_plain_text(pdf_bytes: bytes) -> str:
    """Extract plain text from PDF; prefer pypdf, fallback to pdfminer.six if empty."""
    text = _extract_text_pypdf(pdf_bytes)
    if not (text or "").strip():
        text = _extract_text_pdfminer(pdf_bytes)
    return text or ""


# ---------------------------------------------------------------------------
# Regex patterns for NAIC meeting packet headers
# ---------------------------------------------------------------------------

# Group/committee line: e.g. "REINSURANCE (E) TASK FORCE" or "Reinsurance (E) Task Force"
# Allow optional leading/trailing and parenthetical codes (E), (C), etc.
_GROUP_PATTERN = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9\s\-&\',/()]+(?:\([A-Z]\))?\s*(?:TASK\s+FORCE|WORKING\s+GROUP|COMMITTEE|GROUP|SUBGROUP|TEAM)?)\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# Date line: "Monday, March 2, 2026" or "March 2, 2026"
_MONTH_NAMES = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]
_DATE_PATTERN = re.compile(
    r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*,\s*"
    r"?(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"(\d{1,2})\s*,\s*(\d{4})",
    re.IGNORECASE,
)
_DATE_PATTERN_NO_WEEKDAY = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"(\d{1,2})\s*,\s*(\d{4})",
    re.IGNORECASE,
)

# Time line: HH:MM – HH:MM p.m. ET
_TIME_LINE_PATTERN = re.compile(
    r"(\d{1,2})\s*:\s*(\d{2})\s*(?:a\.m\.|am|p\.m\.|pm)?\s*"
    r"[–\-]\s*"
    r"(\d{1,2})\s*:\s*(\d{2})\s*(a\.m\.|am|p\.m\.|pm)\s*(ET|CT|MT|PT|EST|CST|MST|PST)?",
    re.IGNORECASE,
)
# Single time: 2:00 p.m. ET
_TIME_SINGLE_PATTERN = re.compile(
    r"(\d{1,2})\s*:\s*(\d{2})\s*(a\.m\.|am|p\.m\.|pm)\s*(ET|CT|MT|PT|EST|CST|MST|PST)?",
    re.IGNORECASE,
)


def _parse_date(match: re.Match) -> str:
    """Convert match (month name, day, year) to YYYY-MM-DD."""
    month_name, day, year = match.group(1).lower(), match.group(2), match.group(3)
    try:
        month_num = _MONTH_NAMES.index(month_name) + 1
    except ValueError:
        return ""
    d, y = int(day.strip()), int(year.strip())
    return f"{y:04d}-{month_num:02d}-{d:02d}"


def _parse_date_no_weekday(match: re.Match) -> str:
    month_name, day, year = match.group(1).lower(), match.group(2), match.group(3)
    try:
        month_num = _MONTH_NAMES.index(month_name) + 1
    except ValueError:
        return ""
    d, y = int(day.strip()), int(year.strip())
    return f"{y:04d}-{month_num:02d}-{d:02d}"


def _normalize_time(h: int, m: int, ampm: str | None) -> str:
    """Return HH:MM in 24h-like form (we keep 12h semantics; output HH:MM)."""
    ampm = (ampm or "").lower()
    if "p" in ampm and h != 12:
        h += 12
    elif "a" in ampm and h == 12:
        h = 0
    return f"{h:02d}:{m:02d}"


def _normalize_timezone(tz: str | None) -> str | None:
    """Return short form ET, CT, MT, PT or None."""
    if not (tz or "").strip():
        return None
    tz = tz.strip().upper()
    if tz in ("ET", "EST", "EDT"):
        return "ET"
    if tz in ("CT", "CST", "CDT"):
        return "CT"
    if tz in ("MT", "MST", "MDT"):
        return "MT"
    if tz in ("PT", "PST", "PDT"):
        return "PT"
    return tz if len(tz) <= 3 else None


def _find_group_name(text: str) -> str | None:
    """Extract committee/group name from first ~50 lines; prefer line that looks like a header."""
    lines = text.splitlines()
    for line in lines[:50]:
        line = line.strip()
        if len(line) < 5:
            continue
        # Skip lines that are clearly not group names
        if re.match(r"^\d", line) or "page" in line.lower() or line.lower().startswith("http"):
            continue
        # Prefer lines containing TASK FORCE, WORKING GROUP, COMMITTEE, etc.
        if re.search(r"TASK\s+FORCE|WORKING\s+GROUP|COMMITTEE|SUBGROUP", line, re.I):
            return line.strip()
        # Or all-caps / title case line that could be a group (e.g. "REINSURANCE (E) TASK FORCE")
        m = _GROUP_PATTERN.match(line)
        if m:
            return m.group(1).strip()
    # Fallback: first non-empty, non-number line that's not a date
    for line in lines[:30]:
        line = line.strip()
        if len(line) < 4:
            continue
        if _DATE_PATTERN.search(line) or _DATE_PATTERN_NO_WEEKDAY.search(line):
            continue
        if re.match(r"^\d+[/\-]", line):
            continue
        return line
    return None


def _find_date_iso(text: str) -> str | None:
    """Extract first clear meeting date as YYYY-MM-DD."""
    m = _DATE_PATTERN.search(text)
    if m:
        return _parse_date(m)
    m = _DATE_PATTERN_NO_WEEKDAY.search(text)
    if m:
        return _parse_date_no_weekday(m)
    return None


def _find_times(text: str) -> tuple[str | None, str | None, str | None]:
    """Extract start_time (HH:MM), end_time (HH:MM), timezone from first ~30 lines."""
    lines = text.splitlines()
    chunk = "\n".join(lines[:30])
    # Prefer full range: 2:00 – 3:00 p.m. ET
    m = _TIME_LINE_PATTERN.search(chunk)
    if m:
        h1, m1, h2, m2, ampm, tz = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), m.group(5), m.group(6)
        start = _normalize_time(h1, m1, ampm)
        end = _normalize_time(h2, m2, ampm)
        return (start, end, _normalize_timezone(tz))
    # Single time
    m = _TIME_SINGLE_PATTERN.search(chunk)
    if m:
        h, mi, ampm, tz = int(m.group(1)), int(m.group(2)), m.group(3), m.group(4)
        return (_normalize_time(h, mi, ampm), None, _normalize_timezone(tz))
    return (None, None, None)


_YEAR_MIN = 2018
_SENTENCE_END_RE = re.compile(r"[.!?]\s+[A-Z]")


def validate_meeting_meta(meta: MeetingMeta) -> dict:
    """Validate extracted MeetingMeta fields. Returns {"valid": bool, "reasons": [...]}."""
    import datetime

    reasons: list[str] = []
    year_max = datetime.date.today().year + 2

    if meta.date_iso:
        try:
            year = int(meta.date_iso.split("-")[0])
            if year < _YEAR_MIN:
                reasons.append(f"date_iso year {year} < {_YEAR_MIN}")
            elif year > year_max:
                reasons.append(f"date_iso year {year} > {year_max}")
        except (ValueError, IndexError):
            reasons.append(f"date_iso unparseable: {meta.date_iso!r}")

    if meta.group_name:
        if len(meta.group_name) > 80:
            reasons.append(f"group_name too long ({len(meta.group_name)} chars)")
        if _SENTENCE_END_RE.search(meta.group_name):
            reasons.append("group_name looks like prose (multiple sentences)")

    return {"valid": len(reasons) == 0, "reasons": reasons}


def extract_meeting_metadata_from_pdf(url: str, pdf_bytes: bytes) -> MeetingMeta | None:
    """
    Extract meeting metadata from PDF bytes (deterministic, no AI).

    Uses plain text from the PDF (pypdf first, pdfminer.six if empty).
    Returns None if confidence is low (e.g., no date found).

    Normalized fields:
      - group_name: str
      - date_iso: YYYY-MM-DD
      - start_time_local / end_time_local: HH:MM or None
      - timezone: "ET"|"CT"|... or None
    """
    if not pdf_bytes:
        return None
    text = _extract_plain_text(pdf_bytes)
    if not text.strip():
        return None

    date_iso = _find_date_iso(text)
    if not date_iso:
        return None

    group_name = _find_group_name(text)
    if not group_name:
        group_name = ""  # date is required; group can be empty but we still return

    start_time_local, end_time_local, timezone = _find_times(text)

    return MeetingMeta(
        group_name=group_name,
        date_iso=date_iso,
        start_time_local=start_time_local,
        end_time_local=end_time_local,
        timezone=timezone,
    )
