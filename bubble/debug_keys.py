"""
Helpers for deterministic debug keys for Resource and Calendar Item payloads.

These keys are used only in debug artifacts (e.g. last_bubble_report.json),
not in Bubble JSON payloads. Schema validation strips unknown fields when
building the actual Bubble payloads.
"""

from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import parse_qs, urlparse, urlunparse, urlencode


def _canonical_url(url: str) -> str:
    """
    Best-effort canonical URL for debug keys:
    - lower-case scheme and host
    - strip fragment
    - strip common tracking query params (utm_*, gclid, fbclid)
    """
    if not url:
        return ""
    parsed = urlparse(url.strip())
    scheme = (parsed.scheme or "https").lower()
    netloc = (parsed.netloc or "").lower()
    path = parsed.path or ""

    # Strip tracking params
    query_dict = parse_qs(parsed.query, keep_blank_values=False)
    filtered: dict[str, list[str]] = {}
    for k, v in query_dict.items():
        kl = k.lower()
        if kl.startswith("utm_") or kl in ("gclid", "fbclid"):
            continue
        filtered[k] = v
    query = urlencode(sorted(filtered.items()), doseq=True) if filtered else ""

    return urlunparse((scheme, netloc, path, parsed.params or "", query, ""))  # no fragment


def _hash_components(*parts: str) -> str:
    """Stable hex digest from joined parts (for fallback key)."""
    text = "|".join(p or "" for p in parts)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_resource_key(resource: dict[str, Any], ctx: dict[str, Any]) -> str:
    """
    Deterministic key for a Resource payload:
    - Prefer canonical URL if available.
    - Else hash(title + date + source_page + target_label).
    """
    url = (resource.get("URL") or "").strip()
    canon = _canonical_url(url)
    if canon:
        return canon
    title = (resource.get("Name") or "").strip()
    date = (resource.get("date") or "").strip() if isinstance(resource.get("date"), str) else ""
    source_page = (ctx.get("url") or "").strip()
    label = (ctx.get("label") or "").strip()
    return _hash_components(title, date, source_page, label)


def compute_calendar_item_key(item: dict[str, Any], ctx: dict[str, Any]) -> str:
    """
    Deterministic key for a Calendar Item payload:
    - Prefer canonical URL from any agenda/materials links if present.
    - Else hash(title + date + source_page + target_label).
    """
    # Try to find a URL from Agenda entries (first one wins)
    url = ""
    agenda = item.get("Agenda") or []
    if isinstance(agenda, list):
        for a in agenda:
            if isinstance(a, dict):
                u = (a.get("url") or "").strip()
                if u:
                    url = u
                    break
    canon = _canonical_url(url)
    if canon:
        return canon
    title = (item.get("title") or "").strip()
    date = (item.get("date") or "").strip() if isinstance(item.get("date"), str) else ""
    source_page = (ctx.get("url") or "").strip()
    label = (ctx.get("label") or "").strip()
    return _hash_components(title, date, source_page, label)


def make_resource_debug_entry(resource: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """
    Return a shallow copy of resource with __key and __source added for debug use.
    Does not modify the original dict.
    """
    out = dict(resource)
    out["__key"] = compute_resource_key(resource, ctx)
    out["__source"] = {
        "source_page_url": (ctx.get("url") or "").strip(),
        "target_label": (ctx.get("label") or "").strip(),
    }
    return out


def make_calendar_debug_entry(item: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """
    Return a shallow copy of calendar item with __key and __source added for debug use.
    Does not modify the original dict.
    """
    out = dict(item)
    out["__key"] = compute_calendar_item_key(item, ctx)
    out["__source"] = {
        "source_page_url": (ctx.get("url") or "").strip(),
        "target_label": (ctx.get("label") or "").strip(),
    }
    return out

