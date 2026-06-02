"""
Match meeting alerts to S3 mp3 recordings in recordings-bucket-1.

Naming convention: {PREFIX}_{YYYY-MM-DD}.mp3 (e.g. NAIC_LATF_2026-05-21.mp3)
Matching: filter by date, then fuzzy-match prefix against event title via acronym scoring.
Returns S3 key (e.g. "NAIC_LATF_2026-05-21.mp3") or None.
"""

import logging
import re

import boto3

log = logging.getLogger(__name__)

_BUCKET = "recordings-bucket-1"
_DATE_RE = re.compile(r"_(\d{4}-\d{2}-\d{2})\.mp3$")

_recording_keys: list[str] | None = None


def _load_keys() -> list[str]:
    global _recording_keys
    if _recording_keys is not None:
        return _recording_keys
    try:
        s3 = boto3.client("s3")
        keys: list[str] = []
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=_BUCKET):
            for obj in page.get("Contents", []):
                if obj["Key"].endswith(".mp3"):
                    keys.append(obj["Key"])
        _recording_keys = keys
        log.debug("recording_matcher: loaded %d keys from %s", len(keys), _BUCKET)
        return keys
    except Exception as exc:
        log.warning("recording_matcher: failed to list %s: %s", _BUCKET, exc)
        return []


def _extract_date(key: str) -> str | None:
    m = _DATE_RE.search(key)
    return m.group(1) if m else None


def _extract_abbreviation(key: str) -> str:
    """Extract title abbreviation from key. 'NAIC_LATF_2026-05-21.mp3' → 'LATF'"""
    stem = _DATE_RE.sub("", key).removesuffix(".mp3")
    return re.sub(r"^NAIC_", "", stem)


def _acronym_score(abbreviation: str, title: str) -> float:
    """Score 0.0–1.0 for how well abbreviation matches title. Acronym match is primary."""
    abbrev_alpha = re.sub(r"[^A-Za-z]", "", abbreviation).upper()
    if not abbrev_alpha:
        return 0.0

    title_words = [w for w in re.split(r"[\s\-&/,]+", title) if w]
    initials = "".join(w[0].upper() for w in title_words)

    if abbrev_alpha == initials:
        return 1.0

    # Subsequence: all abbreviation letters appear in order among title initials
    idx = 0
    for ch in abbrev_alpha:
        while idx < len(initials) and initials[idx] != ch:
            idx += 1
        if idx >= len(initials):
            break
        idx += 1
    else:
        return 0.9

    # Token overlap: abbreviation segments vs title word prefixes
    abbrev_tokens = [t.upper() for t in re.split(r"[-_]", abbreviation) if t]
    title_upper = [w.upper() for w in title_words]
    matched = sum(
        1 for tok in abbrev_tokens
        if any(tw.startswith(tok) or tok.startswith(tw[:3]) for tw in title_upper)
    )
    if abbrev_tokens:
        return 0.5 * matched / len(abbrev_tokens)

    return 0.0


def find_recording(event_title: str, event_start_date_time: str) -> str | None:
    """
    Return the S3 key of the best-matching mp3 in recordings-bucket-1, or None.

    Filters by date extracted from event_start_date_time (ISO 8601), then uses
    acronym scoring to pick the best match when multiple recordings share a date.
    """
    if not event_title or not event_start_date_time:
        return None
    if event_start_date_time.strip().upper() in ("N/A", "N/A.", "-"):
        return None

    date_str = event_start_date_time[:10]
    if not re.match(r"\d{4}-\d{2}-\d{2}", date_str):
        return None

    keys = _load_keys()
    date_keys = [k for k in keys if _extract_date(k) == date_str]

    if not date_keys:
        log.debug("recording_matcher: no recordings for date %s", date_str)
        return None

    if len(date_keys) == 1:
        log.info(
            "recording_matcher: unique match '%s' on %s → %s",
            event_title[:50], date_str, date_keys[0],
        )
        return date_keys[0]

    best_key = None
    best_score = -1.0
    for key in date_keys:
        abbrev = _extract_abbreviation(key)
        score = _acronym_score(abbrev, event_title)
        log.debug("recording_matcher: %.2f  %s vs '%s'", score, key, event_title[:50])
        if score > best_score:
            best_score = score
            best_key = key

    if best_score > 0.3:
        log.info(
            "recording_matcher: matched '%s' → %s (score=%.2f)",
            event_title[:50], best_key, best_score,
        )
        return best_key

    log.warning(
        "recording_matcher: no confident match for '%s' on %s (best=%.2f, candidates=%s)",
        event_title[:50], date_str, best_score, date_keys,
    )
    return None
