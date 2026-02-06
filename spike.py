#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Minimal change detection spike: fetch -> extract -> diff (per resource type) -> report."""

import argparse
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

# -----------------------------------------------------------------------------
# Logging & config
# -----------------------------------------------------------------------------


def _get_state_backend():
    """Select backend via STATE_BACKEND=local|dynamodb. Default: local if STATE_TABLE unset, else dynamodb."""
    backend = os.environ.get("STATE_BACKEND", "").strip().lower()
    has_table = bool(os.environ.get("STATE_TABLE", "").strip())
    if backend == "dynamodb" or (has_table and backend != "local"):
        from storage.state_store_dynamodb import load_target_state, save_target_state

        return ("dynamodb", load_target_state, save_target_state)
    from storage.state_store_local import load_target_state, save_target_state

    return ("local", load_target_state, save_target_state)


logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

DEFAULT_TARGETS_FILE = Path(__file__).parent / "targets.json"
_STATE_BACKEND_NAME, _load_target_state, _save_target_state = _get_state_backend()
REPORT_FILE = Path(__file__).parent / "last_report.txt"
TARGET_URL = "https://example.com"
USE_PLAYWRIGHT = os.environ.get("USE_PLAYWRIGHT", "1") != "0"  # Set USE_PLAYWRIGHT=0 to use requests only


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


MAX_RETRIES = _int_env("MAX_RETRIES", 3)
BACKOFF_SECONDS = _int_env("BACKOFF_SECONDS", 2)
DELAY_BETWEEN_PAGES = _int_env("DELAY_BETWEEN_PAGES", 1)

# -----------------------------------------------------------------------------
# URL filtering: global denylist + per-extractor allow/deny
# -----------------------------------------------------------------------------

# Domains to always exclude (common utilities, social, add-to-calendar, etc.)
GLOBAL_DENY_DOMAINS = [
    "translate.google.com",
    "add-to-calendar-pro.com",
    "www.google.com",
    "google.com",
    "facebook.com",
    "twitter.com",
    "x.com",
    "linkedin.com",
    "youtube.com",
    "instagram.com",
    "accounts.google.com",
    "login.microsoftonline.com",
    "bit.ly",
    "tinyurl.com",
]

# Path substrings that exclude a URL (e.g. /translate, /share)
GLOBAL_DENY_PATH_PATTERNS = ["/translate?", "/intl/", "/share?", "javascript:", "mailto:"]

# Default regex patterns for keyword_links "meaningful link" filtering (social, generic nav)
DEFAULT_DENY_URL_PATTERNS = [
    r"/facebook",
    r"/twitter",
    r"/linkedin",
    r"/youtube",
    r"/instagram",
    r"/x\.com",
    r"/connect",
    r"/share",
    r"/login",
    r"/signin",
    r"/register",
    r"/subscribe",
    r"/newsletter",
    r"/cookie",
    r"/privacy",
]


def _domain_matches(netloc: str, domain: str) -> bool:
    """True if netloc equals domain or is a subdomain of it."""
    domain = domain.lower().strip().lstrip(".")
    netloc = netloc.lower()
    return netloc == domain or netloc.endswith("." + domain)


def _url_matches_deny_patterns(url: str, patterns: list[str]) -> bool:
    """True if url matches any regex in patterns (path or full url)."""
    parsed = urlparse(url)
    path = (parsed.path or "").lower()
    url_lower = url.lower()
    for pat in patterns:
        try:
            if re.search(pat, path, re.I) or re.search(pat, url_lower):
                return True
        except re.error:
            continue
    return False


def _url_should_hide_from_report(url: str | None) -> bool:
    """True if URL should not appear in report output (denied domains or patterns)."""
    if not url:
        return False
    if not _url_passes_filter(url, {}):
        return True
    if _url_matches_deny_patterns(url, DEFAULT_DENY_URL_PATTERNS):
        return True
    return False


def _item_should_hide_from_report(item: dict, rtype: str) -> bool:
    """True if item (from added/removed) should not be shown in report."""
    if rtype in ("docs", "event_links", "events"):
        return _url_should_hide_from_report(item.get("url"))
    if rtype == "meetings":
        for key in ("webex_url", "agenda_url", "materials_url"):
            if _url_should_hide_from_report(item.get(key)):
                return True
        return False
    return False


def _url_passes_filter(full_url: str, params: dict) -> bool:
    """
    Apply global denylist and optional params.allow_domains / params.deny_domains.
    Returns True if URL should be kept. Applied AFTER urljoin absolute normalization.
    """
    parsed = urlparse(full_url)
    scheme = (parsed.scheme or "").lower()
    netloc = (parsed.netloc or "").lower()
    path = parsed.path or ""

    if scheme not in ("http", "https"):
        return False

    url_lower = full_url.lower()
    for pat in GLOBAL_DENY_PATH_PATTERNS:
        if pat in url_lower:
            return False

    for d in GLOBAL_DENY_DOMAINS:
        if _domain_matches(netloc, d):
            return False

    deny = params.get("deny_domains") or []
    for d in deny:
        if _domain_matches(netloc, d):
            return False

    allow = params.get("allow_domains")
    if allow:
        if not any(_domain_matches(netloc, a) for a in allow):
            return False

    return True


# -----------------------------------------------------------------------------
# Extractors: map name -> callable(soup, base_url, params) -> list[dict]
# Each extractor returns a list of dicts with stable keys for diffing (url for links, triple for events).
# -----------------------------------------------------------------------------

_IGNORED_HREF_SCHEMES = ("mailto:", "javascript:", "tel:")

# Query params to strip for canonical URL (tracking, etc.)
_CANONICAL_STRIP_PARAMS = ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid", "ref")


def _canonical_url(url: str) -> str:
    """Canonical URL for dedup/diff: lower scheme/host, strip trailing slash, remove tracking params."""
    if not url:
        return ""
    parsed = urlparse(url)
    scheme = (parsed.scheme or "https").lower()
    netloc = (parsed.netloc or "").lower()
    path = (parsed.path or "/").rstrip("/") or "/"
    # Filter query params (strip tracking)
    query_dict = parse_qs(parsed.query, keep_blank_values=False)
    strip_lower = {p.lower() for p in _CANONICAL_STRIP_PARAMS}
    filtered = {k: v for k, v in query_dict.items()
                if not (k.lower().startswith("utm") or k.lower() in strip_lower)}
    query = urlencode(sorted(filtered.items()), doseq=True) if filtered else ""
    fragment = ""  # strip fragment for canonical
    return urlunparse((scheme, netloc, path, parsed.params or "", query, fragment))


def _normalize_href_to_absolute(href: str, base_url: str) -> str | None:
    """Normalize href: strip whitespace, skip mailto/javascript/tel, return absolute URL or None."""
    href = href.strip()
    if not href or href.startswith("#"):
        return None
    href_lower = href.lower()
    if any(href_lower.startswith(s) for s in _IGNORED_HREF_SCHEMES):
        return None
    return urljoin(base_url, href)


def _path_matches_extension(path: str, extensions: list[str]) -> bool:
    """Check if path ends with any extension. Path is from urlparse (no query). E.g. /doc.pdf?x=y -> path /doc.pdf."""
    path_lower = (path or "").lower()
    return any(path_lower.endswith(ext.lower()) for ext in extensions)


def _normalize_title_text(s: str) -> str:
    """Normalize whitespace: strip newlines, collapse repeated spaces."""
    if not s:
        return ""
    return " ".join((s or "").split())


def _link_context(anchor) -> str | None:
    """
    Best-effort context for a link: nearest preceding heading (H2/H3/H4/H5) or parent section title.
    Returns normalized text up to 80 chars, or None.
    """
    # Closest preceding heading in document order (find_all_previous returns reverse order)
    for h in anchor.find_all_previous(["h2", "h3", "h4", "h5"]):
        text = _normalize_title_text(h.get_text()).strip()
        if text:
            return (text[:80] + "…") if len(text) > 80 else text
    # Fallback: walk up parents, look for heading in same container (e.g. sibling or ancestor child)
    parent = anchor.parent
    for _ in range(8):
        if not parent or parent.name == "body":
            break
        heading = parent.find(["h2", "h3", "h4", "h5"])
        if heading:
            text = _normalize_title_text(heading.get_text()).strip()
            if text:
                return (text[:80] + "…") if len(text) > 80 else text
        parent = getattr(parent, "parent", None)
    return None


def _best_title(url: str, anchor_text: str | None, extensions: list[str]) -> str:
    """
    Best-effort title for a link item.
    Prefer normalized anchor text if non-empty; else last path segment (filename) for PDFs;
    else short host+path summary.
    """
    anchor = _normalize_title_text(anchor_text or "").strip()
    if anchor:
        return (anchor[:80] + "…") if len(anchor) > 80 else anchor
    parsed = urlparse(url)
    path = (parsed.path or "").strip("/")
    filename = path.split("/")[-1] if path else ""
    path_lower = path.lower()
    if filename and any(path_lower.endswith(ext.lower()) for ext in extensions):
        return filename
    host = parsed.netloc or ""
    short_path = (path[:40] + "…") if len(path) > 40 else path
    return f"{host}/{short_path}" if host or short_path else url[:60]


def _link_collector_v1(soup: BeautifulSoup, base_url: str, params: dict) -> list[dict]:
    """Collect links matching params.extensions (e.g. ['.pdf']). Returns [{title, url}]."""
    extensions = params.get("extensions", [".pdf"])
    anchors = soup.find_all("a", href=True)
    raw_count = len(anchors)

    absolute_urls: list[str] = []
    for a in anchors:
        url = _normalize_href_to_absolute(a["href"], base_url)
        if url:
            absolute_urls.append(url)

    after_norm = len(absolute_urls)
    after_domain = [u for u in absolute_urls if _url_passes_filter(u, params)]
    domain_count = len(after_domain)

    results: list[dict] = []
    seen_canonical: set[str] = set()
    for a in anchors:
        url = _normalize_href_to_absolute(a["href"], base_url)
        if not url or url not in after_domain:
            continue
        canonical = _canonical_url(url)
        if canonical in seen_canonical:
            continue
        parsed = urlparse(url)
        path = (parsed.path or "").lower()
        if not _path_matches_extension(path, extensions):
            continue
        seen_canonical.add(canonical)
        anchor_text = a.get_text(separator=" ", strip=False)
        title = _best_title(url, anchor_text, extensions)
        context = _link_context(a)
        item: dict = {"title": title, "url": url}
        if context:
            item["context"] = context
        results.append(item)

    log.debug(
        "link_collector_v1: raw=%d after_norm=%d after_domain=%d after_ext=%d",
        raw_count, after_norm, domain_count, len(results),
    )
    return results


def _keyword_links_v1(soup: BeautifulSoup, base_url: str, params: dict) -> list[dict]:
    """Collect links whose visible text contains any params.keywords. Returns [{title, url}]."""
    keywords = [k.lower() for k in params.get("keywords", [])]
    if not keywords:
        return []
    anchors = [a for a in soup.find_all("a", href=True) if a.get_text(strip=True)]
    raw_count = len(anchors)

    absolute_urls: list[tuple] = []  # (anchor, url, anchor_text)
    for a in anchors:
        url = _normalize_href_to_absolute(a["href"], base_url)
        if url:
            text = a.get_text(separator=" ", strip=False)
            if any(kw in text.lower() for kw in keywords):
                absolute_urls.append((a, url, text))

    after_norm = len(absolute_urls)
    after_domain = [(a, u, t) for a, u, t in absolute_urls if _url_passes_filter(u, params)]
    domain_count = len(after_domain)

    # Meaningful link filter: deny_url_patterns (default excludes social/nav paths)
    deny_patterns = params.get("deny_url_patterns")
    if deny_patterns is None:
        deny_patterns = DEFAULT_DENY_URL_PATTERNS
    after_meaningful = [(a, u, t) for a, u, t in after_domain if not _url_matches_deny_patterns(u, deny_patterns)]

    # Dedup by canonical URL; keep best title when duplicates exist
    by_canonical: dict[str, dict] = {}
    extensions = params.get("extensions", [])  # fallback for _best_title when no extension filter
    for a, url, anchor_text in after_meaningful:
        canonical = _canonical_url(url)
        title = _best_title(url, anchor_text, extensions or [".pdf", ".htm", ".html"])
        context = _link_context(a)
        item: dict = {"title": title, "url": url}
        if context:
            item["context"] = context
        existing = by_canonical.get(canonical)
        if existing is None or len(title) > len(existing.get("title", "")):
            by_canonical[canonical] = item
    results = list(by_canonical.values())

    log.debug(
        "keyword_links_v1: raw=%d after_norm=%d after_domain=%d after_keyword=%d",
        raw_count, after_norm, domain_count, len(results),
    )
    return results


def _find_events_listing_root(soup: BeautifulSoup) -> BeautifulSoup:
    """
    Find the main content / events listing container to exclude header, nav, footer.
    NAIC uses main, #content, [role="main"], or .region-content. Returns scope or full soup.
    """
    for selector in ("main", "#content", '[role="main"]', ".region-content", "#block-mainpagecontent"):
        root = soup.select_one(selector)
        if root:
            return root
    return soup


def _naic_meetings_v1(soup: BeautifulSoup, base_url: str, params: dict) -> list[dict]:
    """
    Extract meeting blocks from NAIC committee/series pages.
    Identifies 'Public Webex Meeting' / 'Public Conference Call' sections.
    Returns [{title, date_text, time_text, expected_duration, webex_url, agenda_url, materials_url, notes}].
    Best-effort: missing fields set to null.
    """
    results: list[dict] = []
    root = _find_events_listing_root(soup)
    # Find meeting blocks: div.node with committee__calendar or national_meeting__calendar
    blocks = root.find_all("div", class_=lambda c: c and ("committee__calendar" in c or "national_meeting__calendar" in c))
    no_materials_phrase = "There are no meeting materials at this time."

    for block in blocks:
        text = block.get_text(separator=" ", strip=False)
        if "Public Webex Meeting" not in text and "Public Conference Call" not in text:
            continue

        title: str | None = None
        date_text: str | None = None
        time_text: str | None = None
        expected_duration: str | None = None
        webex_url: str | None = None
        agenda_url: str | None = None
        materials_url: str | None = None
        notes: str | None = None

        # Title: from hidden span or preceding heading
        hidden = block.select_one('div[style*="display: none"] span')
        if hidden:
            title = _normalize_title_text(hidden.get_text()).strip()[:80] or None
        if not title:
            for h in block.find_all(["h3", "h4", "h5"]):
                t = _normalize_title_text(h.get_text()).strip()
                if t and "Upcoming Meeting" not in t and len(t) > 3:
                    title = t[:80]
                    break
        if not title:
            title = "Public Webex Meeting" if "Public Webex Meeting" in text else "Public Conference Call"

        # Date: e.g. "Tuesday, February 10, 2026" or "Wednesday, February 11, 2026"
        date_m = re.search(
            r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s*\d{4}",
            text,
        )
        if date_m:
            date_text = date_m.group(0).strip()[:80]

        # Time: e.g. "12:00 PM ET, 11:00 AM CT, ..." or "1:00 PM ET, ..."
        time_m = re.search(
            r"\d{1,2}:\d{2}\s*[AP]M\s+ET[^<\n]{0,80}",
            text,
        )
        if time_m:
            time_text = time_m.group(0).strip()[:80]

        # Expected Duration: e.g. "Expected Duration: 1 hr" or "1 hr"
        dur_m = re.search(r"Expected\s+Duration:\s*([^\n<]+)|(\d+\s*(?:hr|hour)s?)", text, re.I)
        if dur_m:
            expected_duration = (dur_m.group(1) or dur_m.group(2) or "").strip()[:40] or None

        # Webex Link (filter denied domains)
        for a in block.find_all("a", href=True):
            link_text = (a.get_text() or "").strip().lower()
            href = _normalize_href_to_absolute(a["href"], base_url)
            if href and "webex" in href.lower() and ("webex" in link_text or "link" in link_text):
                if _url_passes_filter(href, params):
                    webex_url = href
                break

        # Agenda & Materials links (filter denied domains)
        for a in block.find_all("a", href=True):
            link_text = (a.get_text() or "").strip().lower()
            href = _normalize_href_to_absolute(a["href"], base_url)
            if not href or "webex.com" in href or not _url_passes_filter(href, params):
                continue
            if "agenda" in link_text and "materials" in link_text:
                agenda_url = href
                materials_url = href
                break
            elif "agenda" in link_text and not agenda_url:
                agenda_url = href
            elif ("meeting materials" in link_text or "materials" in link_text) and "agenda" not in link_text:
                materials_url = href
        if agenda_url and not materials_url:
            materials_url = agenda_url

        # Notes: "There are no meeting materials at this time."
        if no_materials_phrase.lower() in text.lower():
            notes = no_materials_phrase[:80]

        results.append({
            "title": title,
            "date_text": date_text,
            "time_text": time_text,
            "expected_duration": expected_duration,
            "webex_url": webex_url,
            "agenda_url": agenda_url,
            "materials_url": materials_url,
            "notes": notes,
        })

    return results[:30]


def _naic_events_v1(soup: BeautifulSoup, base_url: str, params: dict) -> list[dict]:
    """NAIC-specific: extract event/meeting entries from the events listing only (excludes nav/header/footer). Returns [{title, datetime_text, url}]."""
    root = _find_events_listing_root(soup)
    results: list[dict] = []
    for a in root.find_all("a", href=True):
        full_url = _normalize_href_to_absolute(a["href"], base_url)
        if not full_url or not _url_passes_filter(full_url, params):
            continue
        parent = a.parent
        text = (parent.get_text(separator=" ", strip=True) if parent else a.get_text(strip=True))[:200]
        title = a.get_text(strip=True) or full_url.split("/")[-1] or full_url
        # Look for date-like pattern in surrounding text
        dt_match = re.search(r"\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}|\d{4}[/\-]\d{1,2}[/\-]\d{1,2}", text)
        datetime_text = dt_match.group(0) if dt_match else ""
        results.append({"title": title[:100], "datetime_text": datetime_text, "url": full_url})
    # Cap to avoid noise; prefer links that look like events (committees, events paths)
    return results[:50]


EXTRACTOR_REGISTRY: dict[str, Callable[..., list[dict]]] = {
    "link_collector_v1": _link_collector_v1,
    "keyword_links_v1": _keyword_links_v1,
    "naic_meetings_v1": _naic_meetings_v1,
    "naic_events_v1": _naic_events_v1,
}


def run_extractors(soup: BeautifulSoup, base_url: str, extract_rules: list[dict]) -> dict[str, list[dict]]:
    """Run extract rules; return {resource_type: [items]}."""
    out: dict[str, list[dict]] = {}
    for rule in extract_rules:
        rtype = rule.get("type", "unknown")
        name = rule.get("extractor")
        params = rule.get("params") or {}
        if name in EXTRACTOR_REGISTRY:
            items = EXTRACTOR_REGISTRY[name](soup, base_url, params)
            out[rtype] = items
        else:
            out[rtype] = []  # unknown extractor
    return out


def _href_looks_pdf(href: str) -> bool:
    """True if href path (before query) ends with .pdf."""
    path = urlparse(urljoin("https://x/", href)).path or ""
    return path.lower().endswith(".pdf")


def _print_debug_extract(
    raw_html: str,
    soup: BeautifulSoup,
    base_url: str,
    extract_rules: list[dict],
    html_after_dom_removal: str | None = None,
) -> None:
    """Print extraction pipeline stages with counts and 5 sample URLs (for --debug-extract)."""
    def _sample(lst: list, n: int = 5) -> list:
        return list(lst)[:n]

    # Stage 1: Immediately after page.content() (before any cleanup)
    soup_raw = BeautifulSoup(raw_html, "html.parser")
    raw_before = [a.get("href", "") for a in soup_raw.find_all("a", href=True)]
    pdf_before = [h for h in raw_before if _href_looks_pdf(h)]
    print("raw_links_before_cleanup:", len(raw_before))
    print("pdf_links_before_cleanup:", len(pdf_before))
    for h in _sample(raw_before):
        print(f"  {h}")
    print()

    # Stage 2: After page.evaluate DOM removals (if used)
    if html_after_dom_removal is not None and html_after_dom_removal != raw_html:
        soup_dom = BeautifulSoup(html_after_dom_removal, "html.parser")
        raw_after_dom = [a.get("href", "") for a in soup_dom.find_all("a", href=True)]
        print("raw_links_after_dom_removal:", len(raw_after_dom))
        for h in _sample(raw_after_dom):
            print(f"  {h}")
        print()
    else:
        print("raw_links_after_dom_removal: (no DOM removal)")
        print()

    # Stage 3: After BeautifulSoup cleanup (script/style/consent removal)
    raw_after_bs4 = [a.get("href", "") for a in soup.find_all("a", href=True)]
    print("raw_links_after_bs4_cleanup:", len(raw_after_bs4))
    for h in _sample(raw_after_bs4):
        print(f"  {h}")
    print()

    # Downstream stages (abs, domain, extension, keyword)
    anchors = soup.find_all("a", href=True)
    raw = [a.get("href", "") for a in anchors]
    abs_urls = []
    for a in anchors:
        u = _normalize_href_to_absolute(a["href"], base_url)
        if u:
            abs_urls.append(u)

    merged: dict = {}
    for rule in extract_rules or []:
        p = rule.get("params") or {}
        if "allow_domains" in p and "allow_domains" not in merged:
            merged["allow_domains"] = p["allow_domains"]
        if "deny_domains" in p and "deny_domains" not in merged:
            merged["deny_domains"] = p["deny_domains"]

    after_domain = [u for u in abs_urls if _url_passes_filter(u, merged)]

    ext_params = {}
    for rule in extract_rules or []:
        if rule.get("extractor") == "link_collector_v1":
            ext_params = rule.get("params") or {}
            break
    extensions = ext_params.get("extensions", [".pdf"])
    after_ext = []
    for u in after_domain:
        path = (urlparse(u).path or "").lower()
        if _path_matches_extension(path, extensions):
            after_ext.append(u)

    kw_params = {}
    for rule in extract_rules or []:
        if rule.get("extractor") == "keyword_links_v1":
            kw_params = rule.get("params") or {}
            break
    keywords = [k.lower() for k in kw_params.get("keywords", [])]
    after_kw = []
    for a in anchors:
        text = a.get_text(strip=True)
        if not text or not keywords:
            continue
        u = _normalize_href_to_absolute(a["href"], base_url)
        if u and u in after_domain and any(kw in text.lower() for kw in keywords):
            after_kw.append(u)

    stages = [
        ("abs_a_links (after urljoin)", abs_urls),
        ("after_domain_filter", after_domain),
        ("after_extension_filter (docs)", after_ext),
        ("after_keyword_filter (event_links)", after_kw),
    ]
    for name, lst in stages:
        print(f"{name}: {len(lst)}")
        for u in _sample(lst):
            print(f"  {u}")
        print()


# -----------------------------------------------------------------------------
# Stable keys per resource type for diffing
# -----------------------------------------------------------------------------


def _stable_key(rtype: str, item: dict) -> str:
    """Return a stable string key for diffing (canonical URL for docs/event_links)."""
    if rtype in ("docs", "event_links"):
        return _canonical_url(item.get("url", ""))
    if rtype == "events":
        t = item.get("title", "")
        dt = item.get("datetime_text", "")
        u = item.get("url", "")
        return f"{t}|{dt}|{u}"
    if rtype == "meetings":
        t = item.get("title", "")
        d = item.get("date_text", "")
        tm = item.get("time_text", "")
        w = item.get("webex_url", "")
        return f"{t}|{d}|{tm}|{w}"
    return json.dumps(item, sort_keys=True)


def _diff_extracted(prev_list: list[dict], curr_list: list[dict], rtype: str) -> dict:
    """Compare prev vs curr by stable keys. Returns {added: [...], removed: [...]}."""
    prev_keys = {_stable_key(rtype, x): x for x in (prev_list or [])}
    curr_keys = {_stable_key(rtype, x): x for x in (curr_list or [])}
    added = [curr_keys[k] for k in curr_keys if k not in prev_keys]
    removed = [prev_keys[k] for k in prev_keys if k not in curr_keys]
    return {"added": added, "removed": removed}


# -----------------------------------------------------------------------------
# Fetch & parse
# -----------------------------------------------------------------------------


def _dismiss_cookie_banner(page) -> None:
    """Try to dismiss cookie/consent banners. Wrapped in try/except; no-op if nothing found."""
    try:
        time.sleep(0.5)  # Short wait for banner to render after goto

        # Common selectors (prefer Reject; OneTrust etc.)
        selectors = [
            "button#onetrust-reject-all-handler",
            "button#onetrust-accept-btn-handler",
            '[aria-label*="Reject"]',
            '[aria-label*="reject"]',
            '[aria-label*="Accept"]',
            '[aria-label*="accept"]',
        ]

        # Button text matches, prefer Reject All (case-insensitive)
        button_texts = ["Reject All", "Accept All", "I Agree", "Accept", "Continue", "OK"]

        clicked = False

        # Try selectors first
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                loc.click(timeout=2000)
                clicked = True
                break
            except Exception:
                pass

        # Try button text if no selector matched
        if not clicked:
            for text in button_texts:
                try:
                    page.get_by_role("button", name=re.compile(re.escape(text), re.I)).first.click(timeout=2000)
                    clicked = True
                    break
                except Exception:
                    pass

        if clicked:
            time.sleep(0.75)  # 500-1000ms after click
    except Exception:
        pass  # Continue; page may have no banner


def fetch_with_playwright(url: str) -> str:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="networkidle", timeout=15000)
        _dismiss_cookie_banner(page)
        html = page.content()
        browser.close()
    return html


def fetch_with_requests(url: str) -> str:
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    r.encoding = r.apparent_encoding
    return r.text


def _fetch_with_retry(get_html: Callable[[], str], url: str) -> str:
    """Retry fetch with exponential backoff."""
    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            return get_html()
        except Exception as e:
            last_err = e
            if attempt < MAX_RETRIES - 1:
                delay = BACKOFF_SECONDS * (2**attempt)
                log.warning("Fetch failed (attempt %d/%d): %s; retrying in %.1fs", attempt + 1, MAX_RETRIES, e, delay)
                time.sleep(delay)
            else:
                log.error("Fetch failed after %d attempts: %s", MAX_RETRIES, e)
    raise last_err  # type: ignore[misc]


def fetch_page(url: str) -> str:
    if USE_PLAYWRIGHT:
        try:
            return _fetch_with_retry(lambda: fetch_with_playwright(url), url)
        except Exception as e:
            log.warning("Playwright failed, falling back to requests: %s", e)
    return _fetch_with_retry(lambda: fetch_with_requests(url), url)


# Safe allowlist of consent/cookie UI selectors (exact id or class match only)
_CONSENT_REMOVE_IDS = {"onetrust-banner-sdk", "onetrust-consent-sdk", "ot-sdk-btn", "cybotcookiebotdialog"}
_CONSENT_REMOVE_CLASSES = {"ot-sdk-container", "otfloatingroundedcorner", "cookie-banner", "cookie-consent", "consent-banner"}


def _should_remove_consent_element(tag) -> bool:
    """True if element's id or any class is in the safe allowlist (exact match, case-insensitive)."""
    if not tag.name:
        return False
    id_val = (tag.get("id") or "").strip().lower()
    if id_val and id_val in _CONSENT_REMOVE_IDS:
        return True
    class_val = tag.get("class")
    if isinstance(class_val, str):
        class_val = class_val.split()
    for c in class_val or []:
        if c.strip().lower() in _CONSENT_REMOVE_CLASSES:
            return True
    return False


def _remove_consent_ui(soup: BeautifulSoup) -> None:
    """Remove known consent UI elements using safe allowlist only."""
    for tag in sorted(soup.find_all(_should_remove_consent_element), key=lambda t: len(list(t.parents)), reverse=True):
        if tag.parent:
            tag.decompose()


def parse_html(html: str) -> tuple[str, BeautifulSoup]:
    """Parse HTML; return (page_hash, soup)."""
    soup = BeautifulSoup(html, "html.parser")
    # Remove only script, style, noscript (safe)
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    a_links_before_cleanup = len(soup.find_all("a", href=True))
    _remove_consent_ui(soup)
    a_links_after_cleanup = len(soup.find_all("a", href=True))

    threshold = max(20, int(a_links_before_cleanup * 0.1))
    if a_links_after_cleanup < threshold:
        log.warning(
            "Cleanup removed too much; skipping cleanup for this target "
            "(before=%d after=%d threshold=%d)",
            a_links_before_cleanup, a_links_after_cleanup, threshold,
        )
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

    text = soup.get_text(separator=" ", strip=True)
    text = " ".join(text.split())
    page_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return page_hash, soup


# -----------------------------------------------------------------------------
# State (via store abstraction)
# -----------------------------------------------------------------------------


def load_targets(targets_file: Path) -> list[dict] | None:
    path = Path(targets_file)
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else data.get("targets", data)


def _normalize_extracted_item(item: dict) -> dict:
    """Normalize docs/event_links item to {title, url}; migrate label -> title; preserve context."""
    url = item.get("url", "")
    title = item.get("title") or item.get("label", "")
    if not title and url:
        title = urlparse(url).path.split("/")[-1] or url[:60]
    out: dict = {"title": _normalize_title_text(str(title)), "url": url}
    if item.get("context"):
        out["context"] = (item["context"])[:80]
    return out


def _migrate_state(s: dict | None) -> dict | None:
    """Migrate old extracted format to new extracted[resource_type] with {title, url} items."""
    if not s:
        return s
    s = dict(s)
    if "pdf_links" in s and "extracted" not in s:
        s["extracted"] = {"docs": [{"title": u.split("/")[-1] or u[:60], "url": u} for u in s.get("pdf_links", [])]}
        del s["pdf_links"]
    # Normalize docs/event_links items to {title, url}; leave events unchanged
    extracted = s.get("extracted", {})
    if extracted:
        s["extracted"] = {
            rtype: [_normalize_extracted_item(it) for it in items] if rtype in ("docs", "event_links") else items
            for rtype, items in extracted.items()
        }
    return s


def load_state(key: str, from_snapshot_dir: Path | None = None) -> dict | None:
    if from_snapshot_dir:
        path = from_snapshot_dir / f"{key}.json"
        if path.exists():
            with open(path, encoding="utf-8") as f:
                return _migrate_state(json.load(f))
        return None
    return _migrate_state(_load_target_state(key))


def save_snapshot(key: str, page_hash: str, extracted: dict[str, list[dict]], snapshot_dir: Path) -> None:
    """Save normalized content and extracted lists to snapshot file per target."""
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    path = snapshot_dir / f"{key}.json"
    data = {"page_hash": page_hash, "extracted": extracted}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    log.info("Snapshot saved to %s", path)


def save_state(key: str, page_hash: str, extracted: dict[str, list[dict]], skip: bool = False) -> None:
    if skip:
        return
    _save_target_state(key, {"page_hash": page_hash, "extracted": extracted})


# -----------------------------------------------------------------------------
# Change computation & reporting
# -----------------------------------------------------------------------------


def compute_change(prev_state: dict | None, curr_page_hash: str, curr_extracted: dict[str, list[dict]]) -> dict:
    """Compare prev vs curr; return change event per resource type."""
    prev_extracted = (prev_state or {}).get("extracted", {})
    prev_hash = prev_state.get("page_hash") if prev_state else None
    first_run = prev_state is None
    page_changed = prev_hash != curr_page_hash if prev_hash else bool(curr_page_hash)

    by_type: dict[str, dict] = {}
    all_types = set(prev_extracted) | set(curr_extracted)
    for rtype in sorted(all_types):
        prev_list = prev_extracted.get(rtype, [])
        curr_list = curr_extracted.get(rtype, [])
        diff = _diff_extracted(prev_list, curr_list, rtype)
        if diff["added"] or diff["removed"]:
            by_type[rtype] = diff

    return {
        "first_run": first_run,
        "page_changed": page_changed,
        "before_hash": prev_hash,
        "after_hash": curr_page_hash,
        "by_type": by_type,
    }


def _has_changes(ch: dict) -> bool:
    return ch["first_run"] or ch["page_changed"] or bool(ch.get("by_type"))


def _format_item(rtype: str, item: dict) -> str:
    if rtype == "docs":
        return item.get("url", "")
    if rtype == "event_links":
        t = item.get("title") or item.get("label", "")
        return f"{t} -> {item.get('url', '')}"
    if rtype == "events":
        return f"{item.get('title', '')} ({item.get('datetime_text', '')}) {item.get('url', '')}"
    if rtype == "meetings":
        return _format_meeting_compact(item)
    return str(item)


def _format_meeting_compact(item: dict) -> str:
    """Compact meeting line: title — date — time (webex: yes/no, agenda: yes/no, materials: yes/no) (notes)."""
    title = (item.get("title") or "Meeting").strip()
    date_text = (item.get("date_text") or "").strip()
    time_text = (item.get("time_text") or "").strip()
    parts = [title]
    if date_text:
        parts.append(date_text[:50])
    if time_text:
        parts.append(time_text[:50])
    main = " — ".join(parts)
    tokens = [
        f"webex: {'yes' if item.get('webex_url') else 'no'}",
        f"agenda: {'yes' if item.get('agenda_url') else 'no'}",
        f"materials: {'yes' if item.get('materials_url') else 'no'}",
    ]
    main += " (" + ", ".join(tokens) + ")"
    notes = (item.get("notes") or "").strip()
    if notes:
        main += f" ({notes[:80]}{'…' if len(notes) > 80 else ''})"
    return main


def _format_meeting_report_lines(item: dict, verbose: bool) -> list[str]:
    """Meeting report: main line + optional indented URL lines when verbose."""
    lines = [_format_meeting_compact(item)]
    if verbose:
        for label, url in [("webex", item.get("webex_url")), ("agenda", item.get("agenda_url")), ("materials", item.get("materials_url"))]:
            if url:
                lines.append(f"      {label}: {url}")
    return lines


def _context_redundant_with_title(context: str, title: str) -> bool:
    """True if context is empty or redundant with title (e.g. same, or title contains context)."""
    ctx = (context or "").strip().lower()
    tit = (title or "").strip().lower()
    if not ctx:
        return True
    if ctx == tit:
        return True
    # Title already contains context (e.g. "Agenda & Materials" in both)
    if len(ctx) <= len(tit) + 5 and ctx in tit:
        return True
    return False


def _short_title(rtype: str, item: dict) -> str:
    """Short title for report: [context] title when context present and not redundant, else title/label, filename, or hostname/path."""
    url = item.get("url", "")
    if rtype in ("docs", "event_links"):
        label = (item.get("title") or item.get("label", "")).strip()
        if label:
            base = (label[:60] + "…") if len(label) > 60 else label
            ctx = (item.get("context") or "").strip()
            if ctx and not _context_redundant_with_title(ctx, label):
                ctx_trunc = (ctx[:60] + "…") if len(ctx) > 60 else ctx
                return f"[{ctx_trunc}] {base}"
            return base
        if url:
            parsed = urlparse(url)
            path = (parsed.path or "").strip("/")
            filename = path.split("/")[-1] if path else ""
            if filename:
                return filename
            return f"{parsed.netloc or ''}/{path[:40]}"
    if rtype == "events":
        title = item.get("title", "").strip()
        if title:
            return (title[:60] + "…") if len(title) > 60 else title
        if url:
            return urlparse(url).path.split("/")[-1] or url[:50]
    if rtype == "meetings":
        return _format_meeting_compact(item)
    return url[:60] if url else ""


def _org_group_key(e: dict) -> tuple[str, tuple[str, ...]]:
    """Return (org_id, org_path_tuple) for grouping. Uses '_' and () for missing."""
    org_id = e.get("org_id") or "_"
    org_path = e.get("org_path")
    path_tuple = tuple(org_path) if isinstance(org_path, list) else ()
    return (org_id, path_tuple)


_RTYPE_LABELS = {"docs": "Docs", "event_links": "Event Links", "events": "Meetings", "meetings": "Meetings"}


def _doc_is_high_priority(item: dict) -> bool:
    """True if doc URL/filename suggests agenda, materials, minutes, or call_materials."""
    url = (item.get("url") or "").lower()
    if "/call_materials/" in url:
        return True
    path = urlparse(url).path.lower()
    filename = path.split("/")[-1] if path else ""
    for kw in ("agenda", "materials", "minutes"):
        if kw in filename or kw in path:
            return True
    return False


def _change_priority_score(e: dict) -> int:
    """Priority score for Highlights: meetings > high-priority docs > event links > generic docs."""
    score = 0
    ch = e.get("change", {})
    by_type = ch.get("by_type", {})

    def count(changes: list[dict], rtype: str) -> int:
        return sum(1 for x in (changes or []) if not _item_should_hide_from_report(x, rtype))

    for item in by_type.get("docs", {}).get("added", []) + by_type.get("docs", {}).get("removed", []):
        if _item_should_hide_from_report(item, "docs"):
            continue
        score += 3 if _doc_is_high_priority(item) else 1

    for rtype in ("event_links", "events"):
        for item in by_type.get(rtype, {}).get("added", []) + by_type.get(rtype, {}).get("removed", []):
            if not _item_should_hide_from_report(item, rtype):
                score += 1

    for item in by_type.get("meetings", {}).get("added", []) + by_type.get("meetings", {}).get("removed", []):
        if not _item_should_hide_from_report(item, "meetings"):
            score += 5

    return score


def _has_displayable_changes(e: dict) -> bool:
    """True if this event has something to show (by_type diffs, first_run, or page_changed with include_hash)."""
    ch = e["change"]
    if ch.get("first_run"):
        return True
    by_type = ch.get("by_type", {})
    if by_type:
        return True
    if e.get("include_hash_changes") and ch.get("page_changed"):
        return True
    return False


def render_report(change_events: list[dict], verbose: bool = False) -> str:
    """Compact report: summary at top, per-target sections, diff counts + samples."""
    events_with_changes = [e for e in change_events if "error" not in e and _has_changes(e["change"])]
    displayable = [e for e in events_with_changes if _has_displayable_changes(e)]
    events_with_errors = [e for e in change_events if "error" in e]
    all_relevant = events_with_changes + events_with_errors

    if not displayable and not events_with_errors:
        return "No changes detected.\n"

    # Summary totals (exclude denied items)
    def _count_non_denied(changes: list[dict], rtype: str) -> int:
        return sum(1 for x in changes if not _item_should_hide_from_report(x, rtype))

    total_docs_added = sum(
        _count_non_denied(e["change"].get("by_type", {}).get("docs", {}).get("added", []), "docs")
        for e in displayable
    )
    total_docs_removed = sum(
        _count_non_denied(e["change"].get("by_type", {}).get("docs", {}).get("removed", []), "docs")
        for e in displayable
    )
    total_events_added = sum(
        _count_non_denied(e["change"].get("by_type", {}).get("event_links", {}).get("added", []), "event_links")
        + _count_non_denied(e["change"].get("by_type", {}).get("events", {}).get("added", []), "events")
        for e in displayable
    )
    total_events_removed = sum(
        _count_non_denied(e["change"].get("by_type", {}).get("event_links", {}).get("removed", []), "event_links")
        + _count_non_denied(e["change"].get("by_type", {}).get("events", {}).get("removed", []), "events")
        for e in displayable
    )
    total_meetings_added = sum(
        _count_non_denied(e["change"].get("by_type", {}).get("meetings", {}).get("added", []), "meetings")
        for e in displayable
    )
    total_meetings_removed = sum(
        _count_non_denied(e["change"].get("by_type", {}).get("meetings", {}).get("removed", []), "meetings")
        for e in displayable
    )

    limit = 9999 if verbose else 5

    # Build Highlights: top 3 targets by priority (meetings > agenda/materials/minutes docs > event links > generic docs)
    highlights: list[str] = []
    top_for_highlights = sorted(displayable, key=_change_priority_score, reverse=True)[:3]
    for e in top_for_highlights:
        label = e.get("label", "unknown")
        ch = e["change"]
        by_type = ch.get("by_type", {})
        parts: list[str] = []
        d = by_type.get("docs", {})
        da, dr = _count_non_denied(d.get("added", []), "docs"), _count_non_denied(d.get("removed", []), "docs")
        if da or dr:
            s = f"{da} doc{'s' if da != 1 else ''} added"
            if dr:
                s += f", {dr} doc{'s' if dr != 1 else ''} removed"
            parts.append(s)
        el = by_type.get("event_links", {})
        ev = by_type.get("events", {})
        ea = _count_non_denied(el.get("added", []), "event_links") + _count_non_denied(ev.get("added", []), "events")
        er = _count_non_denied(el.get("removed", []), "event_links") + _count_non_denied(ev.get("removed", []), "events")
        if ea or er:
            s = f"{ea} event{'s' if ea != 1 else ''} added"
            if er:
                s += f", {er} removed"
            parts.append(s)
        m = by_type.get("meetings", {})
        ma, mr = _count_non_denied(m.get("added", []), "meetings"), _count_non_denied(m.get("removed", []), "meetings")
        if ma or mr:
            s = f"{ma} meeting{'s' if ma != 1 else ''} added"
            if mr:
                s += f", {mr} removed"
            parts.append(s)
        if parts:
            highlights.append(f"{label}: {'; '.join(parts)}")
    highlights = highlights[:3]

    lines = ["Web Change Report", "=" * 40, ""]
    lines.append("Summary")
    lines.append("-" * 20)
    lines.append(f"Targets changed: {len(displayable)}{' (+errors)' if events_with_errors else ''}")
    lines.append(f"Docs: +{total_docs_added} / -{total_docs_removed}")
    lines.append(f"Events: +{total_events_added} / -{total_events_removed}")
    lines.append(f"Meetings: +{total_meetings_added} / -{total_meetings_removed}")
    if highlights:
        lines.append("")
        lines.append("Highlights")
        lines.append("-" * 20)
        for h in highlights:
            lines.append(f"• {h}")
    lines.append("")

    # Group by (org_id, org_path)
    groups: dict[tuple[str, tuple[str, ...]], list[dict]] = {}
    for e in all_relevant:
        key = _org_group_key(e)
        groups.setdefault(key, []).append(e)

    first_target = True
    for (org_id, path_tuple) in sorted(groups.keys()):
        group_events = groups[(org_id, path_tuple)]
        for e in group_events:
            if not first_target:
                lines.append("")
            first_target = False
            label = e.get("label", "unknown")
            url = e.get("url", "")
            org_path = e.get("org_path")
            path_str = " › ".join(org_path) if isinstance(org_path, list) and org_path else ""
            header = f"{path_str} › {label}" if path_str else label

            if "error" in e:
                lines.append(header)
                lines.append(url)
                lines.append(f"  Error: {e['error']}")
                continue

            ch = e["change"]
            include_hash = e.get("include_hash_changes", False)

            # Build compact diffs per type
            by_type = ch.get("by_type", {})
            if ch["first_run"]:
                lines.append(header)
                lines.append(url)
                lines.append("  Initial baseline recorded")
                continue

            has_any_diff = bool(by_type) or (include_hash and ch["page_changed"])
            if not has_any_diff:
                continue

            lines.append(header)
            lines.append(url)

            if include_hash and ch["page_changed"]:
                lines.append("  Page content changed")

            for rtype in ("docs", "event_links", "events", "meetings"):
                diff = by_type.get(rtype, {"added": [], "removed": []})
                added = [x for x in diff.get("added", []) if not _item_should_hide_from_report(x, rtype)]
                removed = [x for x in diff.get("removed", []) if not _item_should_hide_from_report(x, rtype)]
                if not added and not removed:
                    continue
                label_r = _RTYPE_LABELS.get(rtype, rtype)
                n_add, n_rem = len(added), len(removed)
                lines.append(f"  {label_r}: +{n_add} / -{n_rem}")
                for x in added[:limit]:
                    if rtype == "meetings":
                        report_lines = _format_meeting_report_lines(x, verbose)
                        lines.append(f"    + {report_lines[0]}")
                        for extra in report_lines[1:]:
                            lines.append(extra)
                    else:
                        title = _short_title(rtype, x)
                        url = x.get("url") or ""
                        lines.append(f"    + {title} — {url}" if url else f"    + {title}")
                if len(added) > limit:
                    lines.append(f"    (+{len(added) - limit} more)")
                for x in removed[:limit]:
                    if rtype == "meetings":
                        report_lines = _format_meeting_report_lines(x, verbose)
                        lines.append(f"    - {report_lines[0]}")
                        for extra in report_lines[1:]:
                            lines.append(extra)
                    else:
                        title = _short_title(rtype, x)
                        url = x.get("url") or ""
                        lines.append(f"    - {title} — {url}" if url else f"    - {title}")
                if len(removed) > limit:
                    lines.append(f"    (-{len(removed) - limit} more)")

    return "\n".join(lines)


# -----------------------------------------------------------------------------
# Default extract rules (backwards compat when target has no extract array)
# -----------------------------------------------------------------------------

DEFAULT_EXTRACT = [
    {"type": "docs", "extractor": "link_collector_v1", "params": {"extensions": [".pdf"]}, "_purpose": "Collect PDFs."},
]


def process_target(
    target_id: str,
    label: str,
    url: str,
    extract_rules: list[dict] | None,
    snapshot_dir: Path | None = None,
    compare_snapshot: bool = False,
    compare_snapshot_dir: Path | None = None,
    dump_extracted: bool = False,
    dump_html_snapshot: bool = False,
    debug_extract: bool = False,
) -> dict:
    """Process one target: fetch, extract, diff, save, print."""
    log.info("--- %s ---", label)
    log.info("Fetching %s...", url)
    html = fetch_page(url)
    # dump-html-snapshot and debug-extract stage 1 use this exact html (no modifications)
    if dump_html_snapshot:
        debug_dir = Path("debug")
        debug_dir.mkdir(parents=True, exist_ok=True)
        html_path = debug_dir / f"{target_id}.html"
        html_path.write_text(html, encoding="utf-8")
        log.info("Saved HTML snapshot to %s", html_path)
    page_hash, soup = parse_html(html)

    rules = extract_rules if extract_rules else DEFAULT_EXTRACT
    if debug_extract:
        _print_debug_extract(raw_html=html, soup=soup, base_url=url, extract_rules=rules)
    extracted = run_extractors(soup, url, rules)

    if dump_extracted:
        from datetime import datetime, timezone

        ts = int(datetime.now(timezone.utc).timestamp())
        out = json.dumps(extracted, indent=2)
        print(out)
        debug_dir = Path("debug")
        debug_dir.mkdir(parents=True, exist_ok=True)
        debug_path = debug_dir / f"{target_id}-{ts}.json"
        debug_path.write_text(out, encoding="utf-8")
        log.info("Extracted JSON saved to %s", debug_path)

    from_snapshot = compare_snapshot_dir if compare_snapshot else None
    prev = load_state(target_id, from_snapshot_dir=from_snapshot)
    change = compute_change(prev, page_hash, extracted)

    save_state(target_id, page_hash, extracted, skip=compare_snapshot)
    if snapshot_dir:
        save_snapshot(target_id, page_hash, extracted, snapshot_dir)

    # Console output
    if change["first_run"]:
        log.info("[FIRST RUN] Recording baseline.")
    elif _has_changes(change):
        log.info("[CHANGE DETECTED]")
        if change["page_changed"]:
            log.info("  Page content changed")
        for rtype, diff in change.get("by_type", {}).items():
            log.info("  [%s]", rtype)
            for x in diff.get("added", []):
                log.info("    + %s", _format_item(rtype, x))
            for x in diff.get("removed", []):
                log.info("    - %s", _format_item(rtype, x))
    else:
        log.info("[NO CHANGE]")

    for rtype, items in sorted(extracted.items()):
        log.info("  %s: %d items", rtype, len(items))

    return {"target_id": target_id, "label": label, "url": url, "change": change}


def parse_args() -> argparse.Namespace:
    targets_file_default = os.environ.get("TARGETS_FILE", "").strip() or str(DEFAULT_TARGETS_FILE)
    target_ids_default = os.environ.get("TARGET_IDS", "").strip()

    p = argparse.ArgumentParser(description="Web change detection: fetch → extract → diff → report")
    p.add_argument(
        "--targets-file",
        type=Path,
        default=targets_file_default,
        metavar="PATH",
        help="Path to targets JSON (default: targets.json or TARGETS_FILE env)",
    )
    p.add_argument(
        "--target-ids",
        type=str,
        default=target_ids_default,
        metavar="ID1,ID2,...",
        help="Comma-separated target IDs to process (default: all; or TARGET_IDS env)",
    )
    p.add_argument(
        "--snapshot-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="Save normalized content and extracted lists to DIR/<target_id>.json per target",
    )
    p.add_argument(
        "--compare-snapshot",
        action="store_true",
        help="Compare current scrape against snapshot files instead of prior state (use with --snapshot-dir)",
    )
    p.add_argument(
        "--target-id",
        type=str,
        default=None,
        metavar="ID",
        help="Run only one target (overrides --target-ids)",
    )
    p.add_argument(
        "--dump-extracted",
        action="store_true",
        help="Print extracted resource JSON (docs/event_links/events) to stdout and save to debug/<id>-<timestamp>.json",
    )
    p.add_argument(
        "--dump-html-snapshot",
        action="store_true",
        help="Save rendered HTML to debug/<target_id>.html (requires --target-id)",
    )
    p.add_argument(
        "--debug-extract",
        action="store_true",
        help="Print extraction pipeline stages with sample URLs (requires --target-id)",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="In reports, list all changed items (no truncation to 5)",
    )
    p.add_argument(
        "--simulate-change",
        action="store_true",
        help="Test-only: simulate diffs from stored state and render report without persisting (requires --target-id)",
    )
    args = p.parse_args()
    if args.dump_html_snapshot and not args.target_id:
        p.error("--dump-html-snapshot requires --target-id")
    if args.debug_extract and not args.target_id:
        p.error("--debug-extract requires --target-id")
    if args.simulate_change and not args.target_id:
        p.error("--simulate-change requires --target-id")
    return args


def _run_simulate_change(target_id: str, targets: list[dict], verbose: bool) -> None:
    """Test-only: simulate diffs from stored state, render report, do NOT persist."""
    import copy

    target = next((t for t in targets if t.get("id", t.get("url", "unknown")) == target_id), None)
    if not target:
        log.error("Target %s not found in targets file", target_id)
        return
    label = target.get("label", target_id)
    url = target.get("url", "")
    extract_rules = target.get("extract") or []

    prev_state = load_state(target_id)
    if prev_state is None:
        prev_state = {
            "page_hash": "simulated",
            "extracted": {"docs": [{"title": "Placeholder for removal", "url": "https://example.com/placeholder.pdf"}]},
        }
        if any(r.get("type") == "event_links" for r in extract_rules):
            prev_state["extracted"]["event_links"] = []
        if any(r.get("type") == "events" for r in extract_rules):
            prev_state["extracted"]["events"] = []

    curr_extracted = copy.deepcopy(prev_state.get("extracted", {}))
    page_hash = prev_state.get("page_hash", "simulated")

    # Remove 1 doc item (if any)
    if curr_extracted.get("docs"):
        curr_extracted["docs"] = curr_extracted["docs"][1:]

    # Add 1 fake doc
    curr_extracted.setdefault("docs", []).append({"title": "FAKE TEST DOC", "url": "https://example.com/fake.pdf"})

    # Add 1 fake event_link
    curr_extracted.setdefault("event_links", []).append({"title": "FAKE TEST EVENT", "url": "https://example.com/fake-event"})

    # Add 1 fake meeting if events extractor exists
    if any(r.get("type") == "events" for r in extract_rules):
        curr_extracted.setdefault("events", []).append({
            "title": "FAKE TEST MEETING",
            "datetime_text": "01/15/2025",
            "url": "https://example.com/fake-meeting",
        })
    # Add 1 fake meeting block if meetings extractor exists
    if any(r.get("type") == "meetings" for r in extract_rules):
        curr_extracted.setdefault("meetings", []).append({
            "title": "FAKE TEST MEETING",
            "date_text": "Tuesday, January 15, 2025",
            "time_text": "1:00 PM ET",
            "expected_duration": "1 hr",
            "webex_url": "https://example.com/fake-webex",
            "agenda_url": "https://example.com/fake-agenda.pdf",
            "materials_url": "https://example.com/fake-materials.pdf",
            "notes": None,
        })

    change = compute_change(prev_state, page_hash, curr_extracted)
    change_event = {
        "target_id": target_id,
        "label": label,
        "url": url,
        "org_id": target.get("org_id"),
        "org_path": target.get("org_path"),
        "include_hash_changes": target.get("include_hash_changes", False),
        "change": change,
    }
    report = render_report([change_event], verbose=verbose)
    log.info("\n[SIMULATED CHANGE - not persisted]\n%s", report)
    REPORT_FILE.write_text(report, encoding="utf-8")
    log.info("Report written to %s", REPORT_FILE)


def main() -> None:
    from datetime import datetime, timezone

    args = parse_args()
    run_timestamp = int(datetime.now(timezone.utc).timestamp())
    targets = load_targets(args.targets_file)

    # Filter by target_id (single) or target_ids if provided
    target_ids_filter: set[str] | None = None
    if args.target_id:
        target_ids_filter = {args.target_id.strip()}
    elif args.target_ids:
        target_ids_filter = {s.strip() for s in args.target_ids.split(",") if s.strip()}

    if targets and target_ids_filter is not None:
        targets = [t for t in targets if t.get("id", t.get("url", "unknown")) in target_ids_filter]
        if not targets:
            log.warning("No targets match --target-id/--target-ids %s", args.target_id or args.target_ids)
            return

    # Simulate-change mode: no fetch, no persist
    if args.simulate_change:
        _run_simulate_change(args.target_id.strip(), targets or [], args.verbose)
        return

    change_events: list[dict] = []

    # For --compare-snapshot: load from snapshot_dir or default snapshots/
    compare_snapshot_dir = args.snapshot_dir if args.snapshot_dir else (Path("snapshots") if args.compare_snapshot else None)

    def process_one(t: dict) -> dict:
        target_id = t.get("id", t.get("url", "unknown"))
        label = t.get("label", target_id)
        url = t.get("url")
        extract_rules = t.get("extract")
        try:
            ev = process_target(
                target_id,
                label,
                url,
                extract_rules,
                snapshot_dir=args.snapshot_dir,
                compare_snapshot=args.compare_snapshot,
                compare_snapshot_dir=compare_snapshot_dir,
                dump_extracted=args.dump_extracted,
                dump_html_snapshot=args.dump_html_snapshot,
                debug_extract=args.debug_extract,
            )
            # Include org grouping and report options for reporting
            for k in ("org_id", "org_path", "group", "tags", "include_hash_changes"):
                if k in t:
                    ev[k] = t[k]
            return ev
        except Exception as e:
            log.error("Target %s failed: %s", label, e, exc_info=False)
            ev = {"target_id": target_id, "label": label, "url": url, "error": str(e)}
            for k in ("org_id", "org_path", "group", "tags", "include_hash_changes"):
                if k in t:
                    ev[k] = t[k]
            return ev

    if targets is not None and targets:
        for i, t in enumerate(targets):
            if i > 0 and DELAY_BETWEEN_PAGES > 0:
                time.sleep(DELAY_BETWEEN_PAGES)
            url = t.get("url")
            if url:
                change_events.append(process_one(t))
            else:
                log.warning("--- %s --- Skipping: no URL", t.get("label", "unknown"))
    else:
        change_events.append(process_one({"id": "default", "label": "default", "url": TARGET_URL}))

    report = render_report(change_events, verbose=args.verbose)
    log.info("\n%s", report)
    REPORT_FILE.write_text(report, encoding="utf-8")
    log.info("Report written to %s", REPORT_FILE)

    has_changes = any(
        _has_changes(e["change"]) for e in change_events if "error" not in e
    ) or any("error" in e for e in change_events)
    if has_changes:
        from emailer import send_report

        send_report(report, has_changes)

    if os.environ.get("CHANGELOG_BUCKET", "").strip():
        from storage.changelog_s3 import append_change_events as s3_append

        uri = s3_append(run_timestamp, change_events)
        if uri:
            log.info("Change events appended to %s", uri)


if __name__ == "__main__":
    main()
