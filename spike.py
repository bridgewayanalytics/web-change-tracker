#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Minimal change detection spike: fetch -> extract -> diff (per resource type) -> report."""

import argparse
import copy
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

# Load .env from cwd if present (dev-only; not required)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

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
LAST_EMAIL_REPORT_FILE = Path(__file__).parent / "last_email_report.txt"
BUBBLE_RESOURCES_FILE = Path(__file__).parent / "last_bubble_resources.json"
BUBBLE_CALENDAR_ITEMS_FILE = Path(__file__).parent / "last_bubble_calendar_items.json"
BUBBLE_REPORT_FILE = Path(__file__).parent / "last_bubble_report.json"
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


def _upload_bubble_report_to_s3(
    run_timestamp: int,
    run_spec,  # RunSpec, but keep untyped here to avoid circular import
    targets_file: Path,
) -> None:
    """
    If BUBBLE_ARTIFACT_BUCKET is set and last_bubble_report.json exists, upload it to S3:
      - s3://$BUBBLE_ARTIFACT_BUCKET/bubble_reports/latest.json
      - s3://$BUBBLE_ARTIFACT_BUCKET/bubble_reports/runs/YYYY/MM/DD/<run_id>.json

    Includes S3 object metadata for run_id, image_tag/git_sha (if present), bubble_mode,
    dry_run_bubble, and targets_file. Logs warnings on failure, but never raises.
    """
    bucket = (os.environ.get("BUBBLE_ARTIFACT_BUCKET") or "").strip()
    if not bucket:
        return

    if not BUBBLE_REPORT_FILE.exists():
        log.warning(
            "Bubble report upload skipped: %s does not exist (nothing to upload)",
            BUBBLE_REPORT_FILE,
        )
        return

    try:
        import boto3
        from datetime import datetime, timezone
    except Exception as e:  # pragma: no cover - boto3/import issues are non-fatal
        log.warning("Bubble report upload skipped: boto3 not available (%s)", e)
        return

    try:
        region = os.environ.get("AWS_REGION", "us-east-1")
        client = boto3.client("s3", region_name=region)

        dt = datetime.fromtimestamp(run_timestamp, tz=timezone.utc)

        # Prefer an explicit RUN_ID if provided; fall back to timestamp-based ID.
        run_id = (os.environ.get("RUN_ID") or "").strip() or f"run-{run_timestamp}"

        # Try to capture an image tag / git sha if deployment has set one.
        image_tag = (
            (os.environ.get("IMAGE_TAG") or "").strip()
            or (os.environ.get("GIT_SHA") or "").strip()
        )

        bubble_mode = getattr(run_spec, "bubble_mode", None)
        dry_run_bubble = getattr(run_spec, "dry_run_bubble", None)

        metadata: dict[str, str] = {
            "run_id": str(run_id),
            "bubble_mode": str(bubble_mode),
            "dry_run_bubble": str(dry_run_bubble).lower(),
            "targets_file": str(targets_file),
        }
        if image_tag:
            metadata["image_tag"] = image_tag

        body = BUBBLE_REPORT_FILE.read_bytes()

        latest_key = "bubble_reports/latest.json"
        versioned_key = (
            f"bubble_reports/runs/{dt.year:04d}/{dt.month:02d}/{dt.day:02d}/{run_id}.json"
        )

        for key in (latest_key, versioned_key):
            try:
                client.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=body,
                    ContentType="application/json",
                    Metadata=metadata,
                )
                log.info("Uploaded Bubble report to s3://%s/%s", bucket, key)
            except Exception as e:
                log.warning("Bubble report upload failed for s3://%s/%s: %s", bucket, key, e)
    except Exception as e:  # pragma: no cover - defensive catch-all
        log.warning("Bubble report upload encountered an unexpected error: %s", e)


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


def save_state(
    key: str,
    page_hash: str,
    extracted: dict[str, list[dict]],
    skip: bool = False,
    content_html: str | None = None,
) -> None:
    if skip:
        return
    state: dict = {"page_hash": page_hash, "extracted": extracted}
    if content_html is not None:
        state["content_html"] = content_html
    _save_target_state(key, state)


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


def _is_meeting_materials(item: dict) -> bool:
    """True if URL contains /call_materials/ or label includes Agenda, Materials, or Additional Materials."""
    url = item.get("url", "") or ""
    if "/call_materials/" in url:
        return True
    label = (item.get("title") or item.get("label", "")).strip().lower()
    for kw in ("agenda", "materials", "additional materials"):
        if kw in label:
            return True
    return False


def _short_title(rtype: str, item: dict) -> str:
    """Short title for report: [context] title when context present and not redundant, else title/label, filename, or hostname/path."""
    url = item.get("url", "")
    suffix = " (meeting materials)" if _is_meeting_materials(item) else ""
    if rtype in ("docs", "event_links"):
        label = (item.get("title") or item.get("label", "")).strip()
        if label:
            base = (label[:60] + "…") if len(label) > 60 else label
            ctx = (item.get("context") or "").strip()
            if ctx and not _context_redundant_with_title(ctx, label):
                ctx_trunc = (ctx[:60] + "…") if len(ctx) > 60 else ctx
                return f"[{ctx_trunc}] {base}{suffix}"
            return f"{base}{suffix}"
        if url:
            parsed = urlparse(url)
            path = (parsed.path or "").strip("/")
            filename = path.split("/")[-1] if path else ""
            if filename:
                return f"{filename}{suffix}"
            return f"{parsed.netloc or ''}/{path[:40]}{suffix}"
    if rtype == "events":
        title = item.get("title", "").strip()
        if title:
            return (title[:60] + "…" if len(title) > 60 else title) + suffix
        if url:
            return (urlparse(url).path.split("/")[-1] or url[:50]) + suffix
    if rtype == "meetings":
        return _format_meeting_compact(item)
    return (url[:60] if url else "") + suffix


def _org_group_key(e: dict) -> tuple[str, tuple[str, ...]]:
    """Return (org_id, org_path_tuple) for grouping. Uses '_' and () for missing."""
    org_id = e.get("org_id") or "_"
    org_path = e.get("org_path")
    path_tuple = tuple(org_path) if isinstance(org_path, list) else ()
    return (org_id, path_tuple)


_RTYPE_LABELS = {"docs": "Docs", "event_links": "Meeting Links", "events": "Meeting Links", "meetings": "Meetings"}

_SECTION_LABELS = {
    "docs": ("New documents", "Removed documents"),
    "event_links": ("New/updated meeting links", "Removed meeting links"),
    "events": ("New/updated meeting links", "Removed meeting links"),
    "meetings": ("New meetings", "Removed meetings"),
}

_MEETING_LINK_DISTINCT_KEYWORDS = ("agenda", "materials", "minutes", "call")


def _meeting_link_has_distinct_metadata(item: dict) -> bool:
    """True if meeting_link title/label contains Agenda, Materials, Minutes, Call (implies distinct from generic doc)."""
    label = (item.get("title") or item.get("label", "")).strip().lower()
    return any(kw in label for kw in _MEETING_LINK_DISTINCT_KEYWORDS)


def _dedupe_cross_section(by_type: dict) -> dict:
    """
    Cross-section dedupe: URLs in both docs and meeting_links.
    If meeting_link has distinct metadata (Agenda/Materials/etc), keep in Meeting Links, remove from Docs.
    Otherwise keep in Docs, remove from Meeting Links.
    Returns new by_type with filtered lists (does not mutate input).
    """
    result = {}
    for k, v in by_type.items():
        result[k] = {"added": list(v.get("added", [])), "removed": list(v.get("removed", []))}

    docs_added = result.get("docs", {}).get("added", [])
    docs_removed = result.get("docs", {}).get("removed", [])
    el_added = result.get("event_links", {}).get("added", [])
    el_removed = result.get("event_links", {}).get("removed", [])
    ev_added = result.get("events", {}).get("added", [])
    ev_removed = result.get("events", {}).get("removed", [])

    # Build canonical URL -> meeting_link item (prefer one with distinct metadata)
    def _ml_items():
        for item in el_added + el_removed + ev_added + ev_removed:
            u = item.get("url", "")
            if u:
                yield _canonical_url(u), item

    meeting_links_by_url: dict[str, dict] = dict(_ml_items())

    docs_urls = {
        _canonical_url(x.get("url", ""))
        for x in docs_added + docs_removed
        if x.get("url")
    }
    ml_urls = set(meeting_links_by_url.keys())
    overlap = docs_urls & ml_urls

    urls_keep_in_ml = set()
    urls_keep_in_docs = set()
    for url in overlap:
        ml_item = meeting_links_by_url.get(url)
        if ml_item and _meeting_link_has_distinct_metadata(ml_item):
            urls_keep_in_ml.add(url)
        else:
            urls_keep_in_docs.add(url)

    def _filter_docs(items: list[dict], exclude_urls: set[str]) -> list[dict]:
        return [x for x in items if _canonical_url(x.get("url", "")) not in exclude_urls]

    def _filter_ml(items: list[dict], exclude_urls: set[str]) -> list[dict]:
        return [x for x in items if _canonical_url(x.get("url", "")) not in exclude_urls]

    result["docs"] = {
        "added": _filter_docs(docs_added, urls_keep_in_ml),
        "removed": _filter_docs(docs_removed, urls_keep_in_ml),
    }
    result["event_links"] = {
        "added": _filter_ml(el_added, urls_keep_in_docs),
        "removed": _filter_ml(el_removed, urls_keep_in_docs),
    }
    result["events"] = {
        "added": _filter_ml(ev_added, urls_keep_in_docs),
        "removed": _filter_ml(ev_removed, urls_keep_in_docs),
    }
    return result


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


def _event_with_deduped_by_type(e: dict) -> dict:
    """Return event with change.by_type replaced by cross-section deduped version."""
    if "error" in e or "change" not in e:
        return e
    ch = e["change"]
    by_type = ch.get("by_type", {})
    if not by_type:
        return e
    deduped = _dedupe_cross_section(by_type)
    ch_copy = dict(ch)
    ch_copy["by_type"] = deduped
    out = dict(e)
    out["change"] = ch_copy
    return out


def render_report(change_events: list[dict], verbose: bool = False) -> str:
    """Compact report: summary at top, per-target sections, diff counts + samples."""
    events_with_changes = [e for e in change_events if "error" not in e and _has_changes(e["change"])]
    displayable = [e for e in events_with_changes if _has_displayable_changes(e)]
    events_with_errors = [e for e in change_events if "error" in e]
    all_relevant = events_with_changes + events_with_errors

    if not displayable and not events_with_errors:
        return "No changes detected.\n"

    # Apply cross-section dedupe (docs vs meeting_links) before counts and formatting
    displayable = [_event_with_deduped_by_type(e) for e in displayable]
    all_relevant = [_event_with_deduped_by_type(e) if "error" not in e else e for e in all_relevant]

    # Summary totals (exclude denied items) — use deduped sets
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
        if da:
            parts.append(f"{da} new document{'s' if da != 1 else ''}")
        if dr:
            parts.append(f"{dr} removed document{'s' if dr != 1 else ''}")
        el = by_type.get("event_links", {})
        ev = by_type.get("events", {})
        ea = _count_non_denied(el.get("added", []), "event_links") + _count_non_denied(ev.get("added", []), "events")
        er = _count_non_denied(el.get("removed", []), "event_links") + _count_non_denied(ev.get("removed", []), "events")
        if ea:
            parts.append(f"{ea} new meeting link{'s' if ea != 1 else ''}")
        if er:
            parts.append(f"{er} removed meeting link{'s' if er != 1 else ''}")
        m = by_type.get("meetings", {})
        ma, mr = _count_non_denied(m.get("added", []), "meetings"), _count_non_denied(m.get("removed", []), "meetings")
        if ma:
            parts.append(f"{ma} new meeting{'s' if ma != 1 else ''}")
        if mr:
            parts.append(f"{mr} removed meeting{'s' if mr != 1 else ''}")
        if parts:
            highlights.append(f"{label}: {'; '.join(parts)}")
    highlights = highlights[:3]

    lines = ["Web Change Report", "=" * 40, ""]
    lines.append("Summary")
    lines.append("-" * 20)
    lines.append(f"Identified website updates: {len(displayable)}{' (+errors)' if events_with_errors else ''}")
    if total_docs_added:
        lines.append(f"New documents: {total_docs_added}")
    if total_docs_removed:
        lines.append(f"Removed documents: {total_docs_removed}")
    if total_events_added:
        lines.append(f"New/updated meeting links: {total_events_added}")
    if total_events_removed:
        lines.append(f"Removed meeting links: {total_events_removed}")
    if total_meetings_added:
        lines.append(f"New meetings: {total_meetings_added}")
    if total_meetings_removed:
        lines.append(f"Removed meetings: {total_meetings_removed}")
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
            path_segments = list(org_path) if isinstance(org_path, list) and org_path else []
            hierarchy_parts = path_segments + [label]
            hierarchy_str = " › ".join(hierarchy_parts) if hierarchy_parts else label

            if "error" in e:
                lines.append(f"Hierarchy: {hierarchy_str}")
                lines.append(url)
                lines.append(f"  Error: {e['error']}")
                continue

            ch = e["change"]
            include_hash = e.get("include_hash_changes", False)

            # Build compact diffs per type
            by_type = ch.get("by_type", {})
            if ch["first_run"]:
                lines.append(f"Hierarchy: {hierarchy_str}")
                lines.append(url)
                lines.append("  Initial baseline recorded")
                continue

            has_any_diff = bool(by_type) or (include_hash and ch["page_changed"])
            if not has_any_diff:
                continue

            lines.append(f"Hierarchy: {hierarchy_str}")
            lines.append(url)

            if include_hash and ch["page_changed"]:
                lines.append("  Page content changed")

            for rtype in ("docs", "event_links", "events", "meetings"):
                diff = by_type.get(rtype, {"added": [], "removed": []})
                added = [x for x in diff.get("added", []) if not _item_should_hide_from_report(x, rtype)]
                removed = [x for x in diff.get("removed", []) if not _item_should_hide_from_report(x, rtype)]
                if not added and not removed:
                    continue
                new_label, rem_label = _SECTION_LABELS[rtype]
                n_add, n_rem = len(added), len(removed)
                if n_add:
                    lines.append(f"  {new_label}: {n_add}")
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
                if n_rem:
                    lines.append(f"  {rem_label}: {n_rem}")
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
    skip_persist: bool = False,
    inject_fakes: bool = False,
    run_id: str | None = None,
    run_timestamp: int | None = None,
    org_path: list | None = None,
    group: str | None = None,
) -> dict:
    """Process one target: fetch, extract, diff, save, print."""
    log.info("--- %s ---", label)
    log.info("Fetching %s...", url)
    html = fetch_page(url)
    # Archive raw HTML to S3 (non-blocking, skipped if HTML_SNAPSHOT_BUCKET not set)
    if run_id and run_timestamp:
        from storage.html_snapshot_s3 import store_html_snapshot
        store_html_snapshot(html, url, run_id, run_timestamp, target_id=target_id)
    # dump-html-snapshot and debug-extract stage 1 use this exact html (no modifications)
    if dump_html_snapshot:
        debug_dir = Path("debug")
        debug_dir.mkdir(parents=True, exist_ok=True)
        html_path = debug_dir / f"{target_id}.html"
        html_path.write_text(html, encoding="utf-8")
        log.info("Saved HTML snapshot to %s", html_path)
    page_hash, soup = parse_html(html)

    from scrape.html_content_extractor import strip_to_content
    content_html: str = strip_to_content(html)

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
    prev_content_html: str | None = (prev or {}).get("content_html")

    curr_for_diff = extracted
    if inject_fakes:
        curr_for_diff = copy.deepcopy(extracted)
        curr_for_diff.setdefault("docs", []).append({"title": "[SIMULATED] Test Document", "url": "https://example.com/simulated-doc.pdf"})
        curr_for_diff.setdefault("event_links", []).append({"title": "[SIMULATED] Test Event", "url": "https://example.com/simulated-event"})
        if any((r or {}).get("type") == "meetings" for r in (extract_rules or [])):
            curr_for_diff.setdefault("meetings", []).append({
                "title": "[SIMULATED] Test Meeting",
                "date_text": "Tuesday, January 15, 2025",
                "time_text": "1:00 PM ET",
                "expected_duration": "1 hr",
                "webex_url": "https://example.com/simulated-webex",
                "agenda_url": "https://example.com/simulated-agenda.pdf",
                "materials_url": "https://example.com/simulated-materials.pdf",
                "notes": None,
            })
    change = compute_change(prev, page_hash, curr_for_diff)

    if run_id and run_timestamp and (change.get("page_changed") or change.get("first_run")):
        from storage.page_change_s3 import store_page_change
        store_page_change(
            target_id=target_id,
            run_id=run_id,
            run_timestamp=run_timestamp,
            label=label,
            url=url,
            before_html=prev_content_html or "",
            after_html=content_html,
            before_hash=change.get("before_hash"),
            after_hash=change.get("after_hash"),
            first_run=bool(change.get("first_run")),
        )

        try:
            from scrape.page_chunker import chunk_page
            from storage.chunk_s3 import store_page_chunks
            target_context = {
                "target_id": target_id,
                "label": label,
                "url": url,
                "group": group or target_id,
                "org_path": org_path or [],
            }
            chunks = chunk_page(content_html, target_context, run_timestamp=run_timestamp)
            store_page_chunks(chunks, target_id=target_id, run_id=run_id, run_timestamp=run_timestamp)
        except Exception as e:
            log.warning("Page chunking failed for %s (non-fatal): %s", target_id, e)

    save_state(
        target_id,
        page_hash,
        extracted,
        skip=compare_snapshot or skip_persist,
        content_html=content_html,
    )
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

    return {
        "target_id": target_id,
        "label": label,
        "url": url,
        "change": change,
        "prev_content_html": prev_content_html,
        "content_html": content_html,
    }


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
    p.add_argument(
        "--simulate-change-all",
        action="store_true",
        help="Test-only: run extraction on all targets, inject fake changes into first N, produce combined report (no persist)",
    )
    p.add_argument(
        "--simulate-change-n",
        type=int,
        default=5,
        metavar="N",
        help="Number of targets to inject fake changes when using --simulate-change-all (default: 5)",
    )
    p.add_argument(
        "--print-bubble-schema",
        action="store_true",
        help="Print Bubble Resources field list and exit",
    )
    p.add_argument(
        "--emit-bubble-json",
        action="store_true",
        help="Write Bubble Resource and Calendar Item payloads to last_bubble_resources.json, last_bubble_calendar_items.json, and last_bubble_report.json",
    )
    p.add_argument(
        "--bubble-enrich",
        action="store_true",
        default=False,
        help="Run Bubble reference enrichment (trees, nodes, deterministic + optional AI). Default on if AI_ENRICHMENT_ENABLED=true.",
    )
    p.add_argument(
        "--no-ai",
        action="store_true",
        default=False,
        help="Disable AI enrichment (refs) even when AI_ENRICHMENT_ENABLED is set",
    )
    p.add_argument(
        "--ai-enrich",
        action="store_true",
        default=False,
        help="Run AI enrichment on Bubble payloads (requires OPENAI_API_KEY) before writing JSON",
    )
    p.add_argument(
        "--bubble-report",
        action="store_true",
        default=False,
        help="Use Bubble JSON format for report and email (summary + Calendar Items + Resources)",
    )
    p.add_argument(
        "--e2e-bubble",
        action="store_true",
        default=False,
        help="E2E Bubble: build snapshot, pass into payload mapping/enrichment; debug artifacts under debug/; no write endpoints.",
    )
    p.add_argument(
        "--e2e-bubble-verify",
        action="store_true",
        default=False,
        dest="e2e_bubble_verify",
        help="After enrich_refs, verify all reference fields against snapshot; exit non-zero if any invalid IDs.",
    )
    p.add_argument(
        "--bubble-snapshot-limit",
        type=int,
        default=200,
        metavar="N",
        help="Max items per type when building Bubble snapshot (default: 200)",
    )
    p.add_argument(
        "--dry-run-bubble",
        action="store_true",
        default=True,
        dest="dry_run_bubble",
        help="Do not call Bubble write endpoints (default: True)",
    )
    p.add_argument(
        "--no-dry-run-bubble",
        action="store_false",
        dest="dry_run_bubble",
        help="Allow Bubble write endpoints (still none implemented in this app)",
    )
    p.add_argument(
        "--pdf-meeting-meta",
        action="store_true",
        dest="pdf_meeting_meta",
        help="Extract meeting metadata from PDF resources (date, group, times); default ON when PROD_OBSERVE_MODE=true",
    )
    p.add_argument(
        "--no-pdf-meeting-meta",
        action="store_true",
        dest="no_pdf_meeting_meta",
        help="Disable PDF meeting metadata extraction",
    )
    p.add_argument(
        "--smoke-bubble-resolvers",
        action="store_true",
        default=False,
        dest="smoke_bubble_resolvers",
        help="Run resolver smoke tests against LIVE Bubble and exit 0 (pass) / 1 (fail).",
    )
    args = p.parse_args()
    # Default bubble_enrich on when AI_ENRICHMENT_ENABLED is set
    if os.environ.get("AI_ENRICHMENT_ENABLED", "").strip().lower() in ("1", "true", "yes"):
        args.bubble_enrich = True
    if args.dump_html_snapshot and not args.target_id:
        p.error("--dump-html-snapshot requires --target-id")
    if args.debug_extract and not args.target_id:
        p.error("--debug-extract requires --target-id")
    if args.simulate_change and not args.target_id:
        p.error("--simulate-change requires --target-id")
    return args


def _build_bubble_payloads(
    change_events: list[dict],
    *,
    ai_enrich: bool = False,
    bubble_enrich: bool = False,
    no_ai: bool = False,
    bubble_snapshot: dict | None = None,
    run_id: str = "",
) -> tuple[list[dict], list[dict], dict[str, list[dict]]]:
    """Build Bubble Resource and Calendar Item payloads. Optionally run AI enrichment and reference enrichment."""
    from bubble.payload import (
        build_calendar_item_context,
        build_calendar_item_payload,
        build_resource_context,
        build_resource_payload,
    )
    from bubble.ai_enrichment import enrich_payloads

    from bubble.page_change_agent import (
        PAGE_CHANGE_AGENT_ENABLED,
        agent_output_to_by_type,
        extract_page_change,
    )

    # When PAGE_CHANGE_AGENT_ENABLED, run LLM agent on each changed event and
    # merge agent-extracted by_type data into the change event before payload building.
    if PAGE_CHANGE_AGENT_ENABLED:
        for ev in change_events:
            if "error" in ev:
                continue
            change = ev.get("change", {})
            if not change.get("page_changed") and not change.get("first_run"):
                continue
            before_html = ev.get("prev_content_html") or ""
            after_html = ev.get("content_html") or ""
            if not after_html:
                continue
            target_context = {
                "label": ev.get("label", ""),
                "url": ev.get("url", ""),
                "org_path": ev.get("org_path", []),
                "group": ev.get("group", ""),
                "tags": ev.get("tags", []),
            }
            agent_alerts = extract_page_change(before_html, after_html, target_context)
            # Filter out "No Meaningful Change" alerts (handles both alert_type and Alert Type1 keys)
            from bubble.page_change_agent import _is_no_meaningful_change
            agent_alerts = [a for a in agent_alerts if not _is_no_meaningful_change(a)]
            if agent_alerts:
                # Store the list of alert dicts for downstream storage
                ev["__agent_output"] = agent_alerts
                # Use first alert for by_type merging and metadata (backward compat)
                first_alert = agent_alerts[0]
                agent_by_type = agent_output_to_by_type(first_alert)
                if agent_by_type:
                    existing = ev["change"].get("by_type") or {}
                    merged = dict(existing)
                    merged.update(agent_by_type)
                    ev["change"] = dict(ev["change"])
                    ev["change"]["by_type"] = merged
                # Attach alert metadata for payload builders (from first alert)
                # Check both snake_case and human-readable label field names
                from bubble.page_change_agent import _get_alert_type
                ev["__agent_alert_type"] = _get_alert_type(first_alert)
                for snake, label in (("alert_title", "Alert Title"), ("alert_description", "Alert Description")):
                    val = first_alert.get(snake) or first_alert.get(label) or ""
                    if val:
                        ev[f"__agent_{snake}"] = val

    # Run document agent for events where the alert type indicates new/updated materials
    from bubble.document_agent import should_run_for_alert, extract_document_data as _extract_doc
    for ev in change_events:
        agent_alerts = ev.get("__agent_output") or []
        if not agent_alerts:
            continue
        # Normalize to list (backward compat if somehow a dict)
        if isinstance(agent_alerts, dict):
            agent_alerts = [agent_alerts]
        doc_results: list[dict] = []
        for agent_output in agent_alerts:
            if not should_run_for_alert(agent_output):
                continue
            # Old schema: library_items array; new flat schema: single library_item_* fields
            library_items = agent_output.get("library_items") or []
            if not library_items:
                # New flat schema — library_item_preliminary_title is {status, title}
                raw_title = agent_output.get("library_item_preliminary_title") or {}
                if isinstance(raw_title, dict):
                    lib_name = raw_title.get("library_item_title") or raw_title.get("title") or ""
                else:
                    lib_name = str(raw_title)
                lib_url = agent_output.get("library_item_url") or ""
                lib_file = agent_output.get("library_items_file_name") or ""
                if lib_name and lib_name.strip().upper() not in ("N/A", "N/A.", "-", ""):
                    library_items = [{"preliminary_title": lib_name, "url": lib_url, "file_name": lib_file}]

            for item in library_items:
                raw = item.get("preliminary_title") or item.get("title") or item.get("file_name") or ""
                if isinstance(raw, dict):
                    name = raw.get("library_item_title") or raw.get("title") or ""
                else:
                    name = str(raw)
                url = item.get("url") or ""
                if not name or name.strip().upper() in ("N/A", "N/A.", "-", ""):
                    continue
                doc_result = _extract_doc(name, url)
                if doc_result:
                    doc_results.append({"item": item, "extraction": doc_result})
                    log.info(
                        "document_agent: %s -> topics=%s agenda=%s",
                        name[:60],
                        doc_result.get("topic_ids"),
                        doc_result.get("agenda_item_ids"),
                    )
                    relevance = doc_result.get("newsreel_relevance")
                    if isinstance(relevance, dict) and relevance.get("status") == "Yes":
                        doc_result["ingest_status"] = "pending"
        if doc_results:
            ev["__doc_extraction"] = doc_results

    # Match meeting alerts to audio recordings, transcribe, and chunk by agenda item
    try:
        from bubble.recording_matcher import find_recording as _find_recording
        from bubble.transcriber import transcribe_recording as _transcribe
        from bubble.transcript_chunker import chunk_transcript as _chunk_transcript
        for ev in change_events:
            ev_target_id = ev.get("target_id", "")
            for alert in (ev.get("__agent_output") or []):
                title = alert.get("event_title") or ""
                dt = alert.get("event_start_date_time") or ""
                if not title or title.strip().upper() in ("N/A", "N/A.", "-", ""):
                    continue
                if not dt or dt.strip().upper() in ("N/A", "N/A.", "-", ""):
                    continue
                rec_key = _find_recording(title, dt)
                if not rec_key:
                    continue
                alert["recording_s3_key"] = rec_key
                t_key = _transcribe(rec_key)
                if t_key:
                    alert["transcript_s3_key"] = t_key
                    chunks_key = _chunk_transcript(alert, run_id=run_id, target_id=ev_target_id)
                    if chunks_key:
                        alert["transcript_chunks_s3_key"] = chunks_key
                        alert["ingest_status"] = "pending"
    except Exception as _rec_exc:
        log.warning("recording_matcher: non-fatal error: %s", _rec_exc)

    # Stamp bubble_action on each alert — pure classification, no API calls.
    # Irrelevant alerts (No Meaningful Change, carousel) are skipped.
    try:
        from bubble.bubble_sync_classifier import classify_alert as _classify_alert
        for ev in change_events:
            for alert in (ev.get("__agent_output") or []):
                plan = _classify_alert(alert)
                if plan.applicable:
                    alert["bubble_action"] = plan.to_dict()
    except Exception as _cls_exc:
        log.warning("bubble_sync_classifier: non-fatal error: %s", _cls_exc)

    resources = build_resource_payload(change_events)
    calendar_items = build_calendar_item_payload(change_events)

    has_changes = any(
        _has_displayable_changes(e) for e in change_events if "error" not in e
    )
    resource_ctx = build_resource_context(change_events)
    calendar_ctx = build_calendar_item_context(change_events)

    resources, calendar_items = enrich_payloads(
        resources,
        calendar_items,
        resource_ctx,
        calendar_ctx,
        has_changes=has_changes,
        force=ai_enrich,
        bubble_snapshot=bubble_snapshot,
    )

    # PDF agenda signals: download PDFs and extract ref numbers, numbered items,
    # group hints, and structure type. Stores as __pdf_agenda_signals debug key
    # for use by enrich_refs agenda item matching.
    try:
        from bubble.payload import apply_pdf_agenda_signals
        apply_pdf_agenda_signals(resources, artifact_output_dir=None)
    except Exception as e:
        log.warning("PDF agenda signal extraction failed (non-fatal): %s", e)

    if bubble_enrich:
        from bubble.enrich_refs import enrich_refs
        use_ai = (
            not no_ai
            and os.environ.get("AI_ENRICHMENT_ENABLED", "").strip().lower() in ("1", "true", "yes")
            and bool(os.environ.get("OPENAI_API_KEY", "").strip())
        )
        try:
            resources, calendar_items = enrich_refs(
                resources,
                calendar_items,
                resource_ctx,
                calendar_ctx,
                use_ai=use_ai,
                bubble_snapshot=bubble_snapshot,
            )
        except Exception as e:
            log.warning("Bubble reference enrichment failed, using payloads as-is: %s", e)

    # Build calendar alerts from newly-detected resources and attach to calendar items.
    # Alerts are built as Bubble Alert objects and attached to calendar items
    # via the "alerts" list field.
    alerts_by_cal: dict[str, list[dict]] = {}
    try:
        from bubble.calendar_alerts import attach_alerts_to_calendar_items, build_calendar_alerts
        alerts_by_cal = build_calendar_alerts(resources, resource_context=resource_ctx)
        if alerts_by_cal:
            calendar_items = attach_alerts_to_calendar_items(calendar_items, alerts_by_cal)
    except Exception as e:
        log.warning("Calendar alert generation failed, continuing without alerts: %s", e)

    return (resources, calendar_items, alerts_by_cal)


def _render_bubble_report(resources: list[dict], calendar_items: list[dict]) -> str:
    """Bubble-style report: 2-3 line summary + pretty-printed JSON sections."""
    import json

    nc, nr = len(calendar_items), len(resources)
    summary_lines = [
        "Web change tracker: Bubble payload report.",
        f"Calendar Items: {nc} | Resources: {nr}",
        "Payloads below are ready for Bubble import.",
    ]
    summary = "\n".join(summary_lines)

    cal_json = json.dumps(calendar_items, indent=2, ensure_ascii=False)
    res_json = json.dumps(resources, indent=2, ensure_ascii=False)

    return f"""{summary}

Bubble: Calendar Items ({nc})
{cal_json}

Bubble: Resources ({nr})
{res_json}
"""


def _build_email_report_links(change_events: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Build links metadata for email report. Returns (resource_links, calendar_item_links).
    Each link: {source_page_url, source_page_title, detected_url}.
    Order matches build_resource_payload and build_calendar_item_payload.
    """
    from bubble.payload import _event_with_deduped_by_type, _item_should_hide

    resource_links: list[dict] = []
    calendar_item_links: list[dict] = []

    for e in change_events:
        if "error" in e:
            continue
        label = e.get("label", "unknown")
        org_path = list(e.get("org_path") or [])
        source_url = (e.get("url") or "").strip()
        source_title = " › ".join(org_path + [label]) if org_path else label

        deduped = _event_with_deduped_by_type(e)
        by_type = deduped.get("change", {}).get("by_type", {})

        for rtype in ("docs", "event_links", "events"):
            for item in by_type.get(rtype, {}).get("added", []):
                if _item_should_hide(item, rtype):
                    continue
                detected_url = (item.get("url") or "").strip()
                resource_links.append({
                    "source_page_url": source_url,
                    "source_page_title": source_title,
                    "detected_url": detected_url,
                })

        visible_meetings = [
            m for m in by_type.get("meetings", {}).get("added", [])
            if not _item_should_hide(m, "meetings")
        ]
        for m in visible_meetings:
            detected_url = (
                (m.get("webex_url") or "").strip()
                or (m.get("agenda_url") or "").strip()
                or (m.get("materials_url") or "").strip()
            )
            calendar_item_links.append({
                "source_page_url": source_url,
                "source_page_title": source_title,
                "detected_url": detected_url,
            })

    return (resource_links, calendar_item_links)


def render_email_report(
    change_events: list[dict],
    resources: list[dict],
    calendar_items: list[dict],
) -> str:
    """
    Email report: per-changed-event agent output (all fields), document extraction results,
    then summary counts. Replaces raw Bubble payload JSON blocks.
    Always produced (empty when no changes).
    """
    events_with_changes = [
        e for e in change_events
        if "error" not in e and _has_displayable_changes(e)
    ]
    nr, ne = len(resources), len(calendar_items)

    lines: list[str] = []
    lines.append("New Library Items (Resources): %d" % nr)
    lines.append("New Calendar Items (Events): %d" % ne)
    lines.append("")

    if not events_with_changes:
        lines.append("No page changes detected.")
        return "\n".join(lines)

    for ev in events_with_changes:
        label = ev.get("label") or ev.get("url") or "Unknown page"
        url = (ev.get("url") or "").strip()
        lines.append("=" * 60)
        lines.append(f"PAGE: {label}")
        if url:
            lines.append(f"URL:  {url}")
        lines.append("")

        raw_output = ev.get("__agent_output")
        if isinstance(raw_output, dict):
            agent_alerts = [raw_output]
        elif isinstance(raw_output, list):
            agent_alerts = raw_output
        else:
            agent_alerts = []

        if agent_alerts:
            for alert_idx, agent_output in enumerate(agent_alerts):
                if len(agent_alerts) > 1:
                    lines.append(f"--- Alert {alert_idx + 1} of {len(agent_alerts)} ---")

                def _val(key: str, *alt_keys: str) -> str:
                    for k in (key,) + alt_keys:
                        v = agent_output.get(k)
                        if v is not None:
                            return str(v)
                    return "(none)"

                from bubble.page_change_agent import _get_alert_type
                lines.append(f"Alert Type:        {_get_alert_type(agent_output) or '(none)'}")
                lines.append(f"Alert Title:       {_val('alert_title', 'Alert Title')}")
                lines.append(f"Alert Description: {_val('alert_description', 'Alert Description')}")
                lines.append(f"Alert URL:         {_val('alert_url', 'Alert URL')}")
                lines.append(f"Organization:      {_val('organization', 'Organization')}")
                lines.append(f"Alert Date/Time:   {_val('alert_date_time', 'Alert Date & Time (ET)')}")
                lines.append("")

                # Legacy nested schema fields
                events_list = agent_output.get("events") or []
                if events_list:
                    lines.append(f"Events ({len(events_list)}):")
                    for ev_item in events_list:
                        lines.append(f"  - {ev_item.get('title') or '(no title)'}")
                        if ev_item.get("start_datetime"):
                            lines.append(f"    Start:    {ev_item['start_datetime']}")
                        if ev_item.get("end_datetime"):
                            lines.append(f"    End:      {ev_item['end_datetime']}")
                        if ev_item.get("timezone"):
                            lines.append(f"    Timezone: {ev_item['timezone']}")
                        if ev_item.get("url"):
                            lines.append(f"    URL:      {ev_item['url']}")
                        if ev_item.get("call_in_access_code"):
                            lines.append(f"    Access:   {ev_item['call_in_access_code']}")
                    lines.append("")

                lib_items = agent_output.get("library_items") or []
                if lib_items:
                    lines.append(f"Library Items ({len(lib_items)}):")
                    for item in lib_items:
                        title = item.get("preliminary_title") or item.get("title") or item.get("file_name") or "(no title)"
                        lines.append(f"  - {title}")
                        if item.get("url"):
                            lines.append(f"    URL: {item['url']}")
                        if item.get("file_name"):
                            lines.append(f"    File: {item['file_name']}")
                    lines.append("")

                agenda_items = agent_output.get("agenda_items") or []
                if agenda_items:
                    lines.append(f"Agenda Items ({len(agenda_items)}):")
                    for ag in agenda_items:
                        title = ag.get("title") or ag.get("official_title") or "(no title)"
                        lines.append(f"  - {title}")
                        if ag.get("standardized_id"):
                            lines.append(f"    ID: {ag['standardized_id']}")
                        if ag.get("chronicle_topics"):
                            lines.append(f"    Topics: {', '.join(ag['chronicle_topics'])}")
                    lines.append("")
        else:
            lines.append("(no agent output)")
            lines.append("")

        # Document extraction results
        doc_extraction = ev.get("__doc_extraction") or []
        if doc_extraction:
            lines.append(f"Document Extraction ({len(doc_extraction)} items):")
            for entry in doc_extraction:
                item = entry.get("item") or {}
                extraction = entry.get("extraction") or {}
                name = item.get("preliminary_title") or item.get("title") or item.get("file_name") or "(unknown)"
                lines.append(f"  {name}:")
                if extraction.get("summary"):
                    lines.append(f"    Summary:       {extraction['summary']}")
                if extraction.get("topic_ids"):
                    lines.append(f"    Topic IDs:     {', '.join(str(x) for x in extraction['topic_ids'])}")
                if extraction.get("agenda_item_ids"):
                    lines.append(f"    Agenda IDs:    {', '.join(str(x) for x in extraction['agenda_item_ids'])}")
            lines.append("")

    return "\n".join(lines)


def _write_bubble_payload(
    change_events: list[dict],
    *,
    ai_enrich: bool = False,
    bubble_enrich: bool = False,
    no_ai: bool = False,
    resources: list[dict] | None = None,
    calendar_items: list[dict] | None = None,
) -> None:
    """Build and write Bubble Resource and Calendar Item payloads, plus last_bubble_report.json."""
    import json

    if resources is None or calendar_items is None:
        resources, calendar_items, _ = _build_bubble_payloads(
            change_events,
            ai_enrich=ai_enrich,
            bubble_enrich=bubble_enrich,
            no_ai=no_ai,
        )

    from bubble.payload import strip_debug_keys
    resources_clean = [strip_debug_keys(r) for r in resources]
    calendar_items_clean = [strip_debug_keys(c) for c in calendar_items]

    BUBBLE_RESOURCES_FILE.write_text(json.dumps(resources_clean, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Wrote Bubble Resource payload to %s (%d items)", BUBBLE_RESOURCES_FILE, len(resources_clean))

    BUBBLE_CALENDAR_ITEMS_FILE.write_text(json.dumps(calendar_items_clean, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Wrote Bubble Calendar Item payload to %s (%d items)", BUBBLE_CALENDAR_ITEMS_FILE, len(calendar_items_clean))

    web_urls = list({(e.get("url") or "").strip() for e in change_events if (e.get("url") or "").strip()})
    for r in resources:
        u = (r.get("URL") or "").strip()
        if u and u not in web_urls:
            web_urls.append(u)
    # Build debug views with __key and __source for report only (not in Bubble payload JSON).
    try:
        from bubble.payload import build_resource_context, build_calendar_item_context
        from bubble.debug_keys import make_calendar_debug_entry, make_resource_debug_entry

        resource_ctx = build_resource_context(change_events)
        calendar_ctx = build_calendar_item_context(change_events)

        debug_resources: list[dict] = []
        for idx, r in enumerate(resources):
            ctx = resource_ctx[idx] if idx < len(resource_ctx) else {"label": "unknown", "url": ""}
            debug_resources.append(make_resource_debug_entry(r, ctx))

        debug_calendar_items: list[dict] = []
        for idx, c in enumerate(calendar_items):
            ctx = calendar_ctx[idx] if idx < len(calendar_ctx) else {"label": "unknown", "url": ""}
            debug_calendar_items.append(make_calendar_debug_entry(c, ctx))
    except Exception:
        # Fallback: no debug keys if context/build fails
        debug_resources = list(resources)
        debug_calendar_items = list(calendar_items)

    report = {
        "counts": {"resources": len(resources), "calendar_items": len(calendar_items)},
        "web_urls": web_urls,
        "resources": debug_resources,
        "calendar_items": debug_calendar_items,
    }
    BUBBLE_REPORT_FILE.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Wrote Bubble report to %s", BUBBLE_REPORT_FILE)


def _run_simulate_change_all(
    targets: list[dict],
    n: int,
    verbose: bool,
    emit_bubble_json: bool = False,
    ai_enrich: bool = False,
    bubble_enrich: bool = False,
    no_ai: bool = False,
    e2e_bubble: bool = False,
    bubble_snapshot_limit: int = 200,
    bubble_report: bool = False,
) -> None:
    """Test-only: run extraction on all targets, inject fake changes into first N, produce combined report. No persist."""
    change_events: list[dict] = []
    for i, t in enumerate(targets or []):
        url = t.get("url")
        if not url:
            log.warning("--- %s --- Skipping: no URL", t.get("label", "unknown"))
            continue
        if i > 0 and DELAY_BETWEEN_PAGES > 0:
            time.sleep(DELAY_BETWEEN_PAGES)
        target_id = t.get("id", t.get("url", "unknown"))
        label = t.get("label", target_id)
        extract_rules = t.get("extract")
        inject_fakes = i < n
        try:
            ev = process_target(
                target_id,
                label,
                url,
                extract_rules,
                skip_persist=True,
                inject_fakes=inject_fakes,
            )
            for k in ("org_id", "org_path", "group", "tags", "include_hash_changes"):
                if k in t:
                    ev[k] = t[k]
            change_events.append(ev)
        except Exception as e:
            log.error("Target %s failed: %s", label, e, exc_info=False)
            change_events.append({
                "target_id": target_id,
                "label": label,
                "url": url,
                "error": str(e),
                **{k: t[k] for k in ("org_id", "org_path", "group", "tags", "include_hash_changes") if k in t},
            })
    bubble_snapshot = None
    if e2e_bubble:
        try:
            from bubble.client import get_client
            from bubble.snapshot import build_bubble_snapshot
            client = get_client(use_cache=True)
            bubble_snapshot = build_bubble_snapshot(client, limit=bubble_snapshot_limit)
        except Exception as e:
            log.warning("E2E Bubble snapshot build failed, continuing without snapshot: %s", e)

    resources, calendar_items, _ = _build_bubble_payloads(
        change_events,
        ai_enrich=ai_enrich,
        bubble_enrich=bubble_enrich,
        no_ai=no_ai,
        bubble_snapshot=bubble_snapshot,
    )
    email_report = render_email_report(change_events, resources, calendar_items)
    LAST_EMAIL_REPORT_FILE.write_text(email_report, encoding="utf-8")
    log.info("Email report written to %s", LAST_EMAIL_REPORT_FILE)

    if bubble_report or emit_bubble_json:
        if bubble_report:
            report = _render_bubble_report(resources, calendar_items)
            log.info("\n[SIMULATE-CHANGE-ALL - Bubble report]\n%s", report[:500] + ("..." if len(report) > 500 else ""))
        else:
            report = render_report(change_events, verbose=verbose)
            log.info("\n[SIMULATE-CHANGE-ALL - not persisted]\n%s", report)
        if emit_bubble_json:
            _write_bubble_payload(
                change_events,
                ai_enrich=ai_enrich,
                bubble_enrich=bubble_enrich,
                no_ai=no_ai,
                resources=resources,
                calendar_items=calendar_items,
            )
    else:
        report = render_report(change_events, verbose=verbose)
        log.info("\n[SIMULATE-CHANGE-ALL - not persisted]\n%s", report)
    REPORT_FILE.write_text(report, encoding="utf-8")
    log.info("Report written to %s", REPORT_FILE)
    from bubble.reference_resolution import write_reference_resolution_report
    write_reference_resolution_report()


def _run_simulate_change(
    target_id: str,
    targets: list[dict],
    verbose: bool,
    emit_bubble_json: bool = False,
    ai_enrich: bool = False,
    bubble_enrich: bool = False,
    no_ai: bool = False,
    e2e_bubble: bool = False,
    bubble_snapshot_limit: int = 200,
    bubble_report: bool = False,
) -> None:
    """Test-only: simulate diffs from stored state, render report, do NOT persist."""
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
    bubble_snapshot = None
    if e2e_bubble:
        try:
            from bubble.client import get_client
            from bubble.snapshot import build_bubble_snapshot
            client = get_client(use_cache=True)
            bubble_snapshot = build_bubble_snapshot(client, limit=bubble_snapshot_limit)
        except Exception as e:
            log.warning("E2E Bubble snapshot build failed, continuing without snapshot: %s", e)

    resources, calendar_items, _ = _build_bubble_payloads(
        [change_event],
        ai_enrich=ai_enrich,
        bubble_enrich=bubble_enrich,
        no_ai=no_ai,
        bubble_snapshot=bubble_snapshot,
    )
    email_report = render_email_report([change_event], resources, calendar_items)
    LAST_EMAIL_REPORT_FILE.write_text(email_report, encoding="utf-8")
    log.info("Email report written to %s", LAST_EMAIL_REPORT_FILE)

    if bubble_report or emit_bubble_json:
        if bubble_report:
            report = _render_bubble_report(resources, calendar_items)
            log.info("\n[SIMULATED CHANGE - Bubble report]\n%s", report[:500] + ("..." if len(report) > 500 else ""))
        else:
            report = render_report([change_event], verbose=verbose)
            log.info("\n[SIMULATED CHANGE - not persisted]\n%s", report)
        if emit_bubble_json:
            _write_bubble_payload(
                [change_event],
                ai_enrich=ai_enrich,
                bubble_enrich=bubble_enrich,
                no_ai=no_ai,
                resources=resources,
                calendar_items=calendar_items,
            )
    else:
        report = render_report([change_event], verbose=verbose)
        log.info("\n[SIMULATED CHANGE - not persisted]\n%s", report)
    REPORT_FILE.write_text(report, encoding="utf-8")
    log.info("Report written to %s", REPORT_FILE)
    from bubble.reference_resolution import write_reference_resolution_report
    write_reference_resolution_report()


def _run_rerun(rerun_run_id: str, rerun_target_id: str, rerun_mode: str = "alerts", rerun_library_item_url: str = "") -> None:
    """
    Re-evaluate a single stored alert using the current DynamoDB agent config.

    rerun_mode:
      "alerts"  — re-run page_change_agent only (default, triggered from Alerts page)
      "docs"    — re-run document_agent only (triggered from Document Extractions page)
      "both"    — re-run both agents

    Fetches before/after HTML from S3 at:
      pages/<target_id>/YYYY/MM/DD/<run_id>/before.html
      pages/<target_id>/YYYY/MM/DD/<run_id>/after.html
      pages/<target_id>/YYYY/MM/DD/<run_id>/meta.json

    Writes result to:
      alerts/reruns/<run_id>/<target_id>/result.json

    Does NOT modify alerts_table.jsonl — the dashboard handles accept/discard.
    """
    import boto3
    from datetime import datetime, timezone

    log.info("rerun: starting for run_id=%s target_id=%s", rerun_run_id, rerun_target_id)

    bucket = (
        os.environ.get("CHANGELOG_BUCKET", "").strip()
        or os.environ.get("BUBBLE_ARTIFACT_BUCKET", "").strip()
        or "web-change-tracker-prod-artifacts-815039343351"
    )
    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))

    def _fetch(key: str) -> str:
        try:
            return s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
        except Exception:
            return ""

    # Locate the stored snapshot — search under pages/<target_id>/
    prefix = f"pages/{rerun_target_id}/"
    meta_key = None
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents") or []:
            key = obj["Key"]
            if key.endswith(f"/{rerun_run_id}/meta.json"):
                meta_key = key
                break
        if meta_key:
            break

    if not meta_key:
        log.error("rerun: could not find meta.json for run_id=%s target_id=%s", rerun_run_id, rerun_target_id)
        raise SystemExit(1)

    run_prefix = meta_key[: -len("/meta.json")]
    meta = json.loads(_fetch(meta_key))
    before_html = _fetch(f"{run_prefix}/before.html")
    after_html = _fetch(f"{run_prefix}/after.html")

    if not after_html:
        log.error("rerun: no after.html for run_id=%s target_id=%s", rerun_run_id, rerun_target_id)
        raise SystemExit(1)

    target_context = {
        "label": meta.get("label") or rerun_target_id,
        "url": meta.get("url") or "",
        "org_path": [],
        "group": "",
        "tags": [],
    }

    from bubble.page_change_agent import extract_page_change, get_config_hash, PAGE_CHANGE_AGENT_ENABLED
    if not PAGE_CHANGE_AGENT_ENABLED:
        log.error("rerun: PAGE_CHANGE_AGENT_ENABLED is false — cannot rerun")
        raise SystemExit(1)

    log.info("rerun: running page_change_agent...")
    agent_alerts = extract_page_change(before_html, after_html, target_context)
    # Keep "No Meaningful Change" for docs mode (still need to identify library items from the
    # result); filter it only for alerts-only reruns where it would produce no rows.
    from bubble.page_change_agent import _is_no_meaningful_change
    agent_alerts_for_rows = [a for a in agent_alerts if not _is_no_meaningful_change(a)]

    if not agent_alerts_for_rows and rerun_mode == "alerts":
        log.warning("rerun: agent returned no meaningful output — writing empty result")
        # Write an empty result so the dashboard can show "no change" rather than a hard failure
        config_hash = get_config_hash()
        rerun_timestamp = datetime.now(timezone.utc).isoformat()
        # Fetch original rows for comparison
        original_rows: list[dict] = []
        try:
            jsonl_body = s3.get_object(Bucket=bucket, Key="alerts/alerts_table.jsonl")["Body"].read().decode("utf-8")
            for line in jsonl_body.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    if row.get("run_id") == rerun_run_id and row.get("target_id") == rerun_target_id:
                        original_rows.append(row)
                except Exception:
                    pass
        except Exception:
            pass
        result = {
            "run_id": rerun_run_id,
            "target_id": rerun_target_id,
            "rerun_timestamp": rerun_timestamp,
            "config_hash": config_hash,
            "original_rows": original_rows,
            "rerun_rows": [],
            "doc_original_rows": [],
            "doc_rerun_rows": [],
            "error": "page_change_agent returned no meaningful output — the agent may have determined no change occurred",
        }
        result_key = f"alerts/reruns/{rerun_run_id}/{rerun_target_id}/result.json"
        s3.put_object(
            Bucket=bucket,
            Key=result_key,
            Body=json.dumps(result, indent=2, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
        )
        log.info("rerun: wrote empty result to s3://%s/%s", bucket, result_key)
        return

    agent_call_id = agent_alerts_for_rows[0].get("agent_call_id", "") if agent_alerts_for_rows else ""

    # For docs/both mode: if page_change_agent failed, fall back to original alert rows
    # so document extraction can still proceed independently.
    if rerun_mode in ("docs", "both") and not agent_alerts_for_rows:
        log.warning("rerun: page_change_agent returned no output — falling back to original rows for doc extraction")
        # Fetch original rows from S3 to use as the agent_alerts source
        try:
            jsonl_body = s3.get_object(Bucket=bucket, Key="alerts/alerts_table.jsonl")["Body"].read().decode("utf-8")
            for line in jsonl_body.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    if row.get("run_id") == rerun_run_id and row.get("target_id") == rerun_target_id:
                        agent_alerts_for_rows.append(row)
                except Exception:
                    pass
        except Exception as exc:
            log.error("rerun: could not fetch original rows as fallback: %s", exc)
            raise SystemExit(1) from exc
        if not agent_alerts_for_rows:
            log.error("rerun: no original rows found for fallback")
            raise SystemExit(1)

    # Run document agent on library items if applicable (only in "docs" or "both" mode)
    doc_extractions: list[dict] = []
    if rerun_mode in ("docs", "both"):
        from bubble.document_agent import should_run_for_alert, extract_document_data
        for agent_output in agent_alerts_for_rows:
            if not should_run_for_alert(agent_output):
                continue
            # Old schema: library_items array; new flat schema: single library_item_* fields
            library_items = agent_output.get("library_items") or []
            if not library_items:
                # New flat schema — library_item_preliminary_title is {status, library_item_title}
                raw_title = agent_output.get("library_item_preliminary_title") or {}
                if isinstance(raw_title, dict):
                    lib_name = raw_title.get("library_item_title") or raw_title.get("title") or ""
                else:
                    lib_name = str(raw_title)
                lib_url = agent_output.get("library_item_url") or ""
                lib_file = agent_output.get("library_items_file_name") or ""
                # Only add if there's a meaningful title (not N/A)
                if lib_name and lib_name.strip().upper() not in ("N/A", "N/A.", "-", ""):
                    library_items = [{"preliminary_title": lib_name, "url": lib_url, "file_name": lib_file}]

            for item in library_items:
                raw = item.get("preliminary_title") or item.get("title") or item.get("file_name") or ""
                # preliminary_title may be a flat string or a new-schema {status, library_item_title} dict
                if isinstance(raw, dict):
                    name = raw.get("library_item_title") or raw.get("title") or ""
                else:
                    name = str(raw)
                url = item.get("url") or ""
                if not name or name.strip().upper() in ("N/A", "N/A.", "-", ""):
                    continue
                # Per-document scoping: skip items that don't match the target URL
                if rerun_library_item_url and url != rerun_library_item_url:
                    log.info("rerun: skipping document (URL mismatch): %s", name[:60])
                    continue
                log.info("rerun: document agent: %s", name[:60])
                doc_result = extract_document_data(name, url)
                if doc_result:
                    doc_extractions.append({"item": item, "extraction": doc_result})
                    relevance = doc_result.get("newsreel_relevance")
                    if isinstance(relevance, dict) and relevance.get("status") == "Yes":
                        doc_result["ingest_status"] = "pending"

    config_hash = get_config_hash()
    rerun_timestamp = datetime.now(timezone.utc).isoformat()

    from storage.alert_s3 import _build_table_rows, _build_doc_extraction_rows
    new_rows = _build_table_rows(
        agent_alerts_for_rows, doc_extractions,
        rerun_run_id, rerun_timestamp, rerun_target_id, meta.get("url") or "",
        config_hash=config_hash,
    )

    # Build doc extraction rerun rows
    doc_rerun_rows = _build_doc_extraction_rows(
        doc_extractions,
        rerun_run_id, rerun_timestamp, rerun_target_id, meta.get("url") or "",
        agent_call_id=agent_call_id,
    )

    # Fetch original rows for diff
    original_rows: list[dict] = []
    try:
        jsonl = s3.get_object(Bucket=bucket, Key="alerts/alerts_table.jsonl")["Body"].read().decode("utf-8")
        for line in jsonl.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                if row.get("run_id") == rerun_run_id and row.get("target_id") == rerun_target_id:
                    original_rows.append(row)
            except Exception:
                pass
    except Exception:
        pass

    # Fetch original doc extraction rows for diff
    doc_original_rows: list[dict] = []
    try:
        doc_jsonl = s3.get_object(Bucket=bucket, Key="alerts/document_extractions_table.jsonl")["Body"].read().decode("utf-8")
        for line in doc_jsonl.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                if row.get("run_id") == rerun_run_id and row.get("target_id") == rerun_target_id:
                    # Per-document scoping: only include the target document
                    if rerun_library_item_url and row.get("library_item_url") != rerun_library_item_url:
                        continue
                    doc_original_rows.append(row)
            except Exception:
                pass
    except Exception:
        pass

    result = {
        "run_id": rerun_run_id,
        "target_id": rerun_target_id,
        "rerun_timestamp": rerun_timestamp,
        "config_hash": config_hash,
        "original_rows": original_rows,
        "rerun_rows": new_rows,
        "doc_original_rows": doc_original_rows,
        "doc_rerun_rows": doc_rerun_rows,
    }

    result_key = f"alerts/reruns/{rerun_run_id}/{rerun_target_id}/result.json"
    s3.put_object(
        Bucket=bucket,
        Key=result_key,
        Body=json.dumps(result, indent=2, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json",
    )
    log.info("rerun: wrote result to s3://%s/%s", bucket, result_key)


def _run_smoke_bubble_resolvers() -> int:
    """Run resolver smoke tests against LIVE Bubble. Returns exit code 0 (pass) or 1 (fail)."""
    from bubble import lookups
    from bubble.enrich_refs import (
        ORGANIZATION_TREE_NAME,
        TYPE1_TREE_NAME,
        TOPIC_TREE_NAME,
        _normalize_for_matching,
        _build_naic_group_node_map,
        _resolve_naic_group_node,
        _resolve_organization_naic_node,
        _build_type1_nodes_by_name,
        _build_topic_candidates,
        _node_name,
        _obj_id,
    )

    results: list[tuple[str, bool, str]] = []

    def _record(name: str, passed: bool, detail: str = "") -> None:
        tag = "PASS" if passed else "FAIL"
        results.append((name, passed, detail))
        print(f"  [{tag}] {name}" + (f"  — {detail}" if detail else ""))

    print("\n=== Bubble Resolver Smoke Suite ===\n")

    # --- 1. Load trees and print node counts ---
    tree_checks = {
        "Organization": ORGANIZATION_TREE_NAME,
        "Resources Types": TYPE1_TREE_NAME,
        "Chronicles": TOPIC_TREE_NAME,
    }
    tree_ids: dict[str, str | None] = {}
    tree_node_counts: dict[str, int] = {}

    for label, tree_name in tree_checks.items():
        tree = lookups.get_tree_by_name(tree_name)
        if not tree:
            _record(f"Load tree: {label}", False, f"tree '{tree_name}' not found")
            tree_ids[label] = None
            tree_node_counts[label] = 0
            continue
        tid = tree.get("_id") or tree.get("id")
        tree_ids[label] = tid
        nodes = lookups.get_tree_nodes_in_tree(tid)
        tree_node_counts[label] = len(nodes)
        ok = len(nodes) > 0
        _record(f"Load tree: {label}", ok, f"tree_id={tid}  nodes={len(nodes)}")

    # --- 2. Organization: resolve NAIC node (normalized name matching) ---
    naic_node_id: str | None = None
    naic_nid, naic_ev = _resolve_organization_naic_node(ORGANIZATION_TREE_NAME)
    if naic_nid:
        naic_node_id = naic_nid
        raw_name = naic_ev.get("resolved_name", "NAIC")
        _record("Organization: NAIC node", True, f"'{raw_name}' node_id={naic_node_id}")
    else:
        failure = naic_ev.get("failure", "unknown")
        candidates = naic_ev.get("naic_candidates", [])
        detail = f"failure={failure}"
        if candidates:
            detail += f"  candidates={candidates}"
        _record("Organization: NAIC node", False, detail)

    # --- 3. NAIC Group normalized label matching ---
    test_labels = [
        "Statutory Accounting Principles Working Group",
        "Life Actuarial Task Force",
    ]
    for label in test_labels:
        nid, evidence = _resolve_naic_group_node(ORGANIZATION_TREE_NAME, [label])
        if nid:
            raw = evidence.get("chosen_raw_name", "?")
            _record(f"NAIC Group: \"{label}\"", True, f"→ \"{raw}\" (id={nid})")
        else:
            failure = evidence.get("failure", "unknown")
            _record(f"NAIC Group: \"{label}\"", False, f"failure={failure}")

    # --- 4. Calendar lookup by group ---
    for label in test_labels:
        nid, _ = _resolve_naic_group_node(ORGANIZATION_TREE_NAME, [label])
        if not nid:
            _record(f"Calendar for \"{label}\"", True, "skipped (group not resolved)")
            continue
        cal_items, cal_meta = lookups.search_calendar_items_by_naic_group(nid)
        count = len(cal_items)
        constraints_json = cal_meta.get("constraints", [])
        if count == 0:
            print(f"  [WARN] Calendar for \"{label}\": 0 items (group_id={nid})")
            print(f"         constraints: {constraints_json}")
            results.append((f"Calendar for \"{label}\"", True, f"0 items (warning)"))
        else:
            _record(f"Calendar for \"{label}\"", True, f"{count} item(s)")

    # --- 5. Type1: verify "Agenda & Materials" ---
    type1_map = _build_type1_nodes_by_name(TYPE1_TREE_NAME)
    agenda_id = type1_map.get("Agenda & Materials") or type1_map.get("agenda & materials")
    if agenda_id:
        _record("Type1: Agenda & Materials", True, f"node_id={agenda_id}")
    else:
        available = sorted(k for k in type1_map if k == k.lower())[:10]
        _record("Type1: Agenda & Materials", False, f"not found; available: {available}")

    # --- 6. Topic: Chronicles nodes loaded, check for known node ---
    topic_map = _build_topic_candidates(TOPIC_TREE_NAME)
    topic_count = len(topic_map)
    if topic_count > 0:
        known = "NAIC Investments"
        known_lower = known.lower()
        tid = topic_map.get(known) or topic_map.get(known_lower)
        if tid:
            _record("Topic: Chronicles", True, f"{topic_count} candidates; '{known}' id={tid}")
        else:
            sample = list(topic_map.keys())[:8]
            _record("Topic: Chronicles", True, f"{topic_count} candidates; '{known}' not present; sample: {sample}")
    else:
        _record("Topic: Chronicles", False, "0 candidates loaded")

    # --- Summary ---
    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    failed = total - passed
    print(f"\n{'='*40}")
    print(f"  {passed}/{total} passed, {failed} failed")
    print(f"{'='*40}\n")

    hard_fail = (
        tree_node_counts.get("Organization", 0) == 0
        or tree_node_counts.get("Resources Types", 0) == 0
        or tree_node_counts.get("Chronicles", 0) == 0
        or naic_node_id is None
        or agenda_id is None
        or topic_count == 0
    )
    return 1 if hard_fail else 0


def _run_manual_chunk(agent_call_id: str, transcript_s3_key: str) -> None:
    """
    Chunk a manually uploaded transcript and mark the alert row pending for ingest.

    Triggered via ECS RunTask override with:
      MANUAL_CHUNK_AGENT_CALL_ID  — agent_call_id of the alert row to update
      MANUAL_CHUNK_TRANSCRIPT_S3_KEY — S3 key of the uploaded .txt transcript

    Reads the alert row from alerts_table.jsonl to get event/agenda metadata,
    runs chunk_transcript(), writes the JSONL to S3, then patches the row with
    transcript_chunks_s3_key and ingest_status: "pending".
    """
    import boto3
    log.info("manual_chunk: starting for agent_call_id=%s transcript=%s", agent_call_id, transcript_s3_key)

    bucket = (
        os.environ.get("CHANGELOG_BUCKET", "").strip()
        or os.environ.get("BUBBLE_ARTIFACT_BUCKET", "").strip()
    )
    if not bucket:
        log.error("manual_chunk: CHANGELOG_BUCKET not set")
        raise SystemExit(1)

    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))

    # Find the alert row by agent_call_id
    alert_row: dict | None = None
    try:
        body = s3.get_object(Bucket=bucket, Key="alerts/alerts_table.jsonl")["Body"].read().decode("utf-8")
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                if row.get("agent_call_id") == agent_call_id:
                    alert_row = row
                    break
            except Exception:
                pass
    except Exception as exc:
        log.error("manual_chunk: could not read alerts_table.jsonl: %s", exc)
        raise SystemExit(1)

    if not alert_row:
        log.error("manual_chunk: no row found for agent_call_id=%s", agent_call_id)
        raise SystemExit(1)

    # Override the transcript_s3_key on the alert so chunk_transcript uses the manual file
    alert_row["transcript_s3_key"] = transcript_s3_key
    alert_row["manual_transcript_s3_key"] = transcript_s3_key

    run_id = alert_row.get("run_id") or "manual"
    target_id = alert_row.get("target_id") or "manual"

    from bubble.transcript_chunker import chunk_transcript as _chunk_transcript
    chunks_key = _chunk_transcript(alert_row, run_id=run_id, target_id=target_id)
    if not chunks_key:
        log.error("manual_chunk: chunking failed for agent_call_id=%s", agent_call_id)
        raise SystemExit(1)

    log.info("manual_chunk: chunks written to s3://%s/%s", bucket, chunks_key)

    from storage.alert_s3 import patch_jsonl_row
    patched = patch_jsonl_row(
        jsonl_key="alerts/alerts_table.jsonl",
        match_fields={"agent_call_id": agent_call_id},
        update_fields={
            "transcript_s3_key": transcript_s3_key,
            "manual_transcript_s3_key": transcript_s3_key,
            "transcript_chunks_s3_key": chunks_key,
            "ingest_status": "pending",
        },
        bucket=bucket,
    )
    log.info("manual_chunk: patched %d row(s) for agent_call_id=%s", patched, agent_call_id)


def _run_manual_doc(agent_call_id: str) -> None:
    """
    Run document extraction on a manually added document row and update it with results.

    Triggered via ECS RunTask override with:
      MANUAL_DOC_AGENT_CALL_ID — agent_call_id of the stub row in document_extractions_table.jsonl

    Reads the stub row (written by /api/ingest/add-document), runs extract_document_data(),
    then patches the row with the extracted fields and ingest_status: "pending".
    """
    import boto3
    log.info("manual_doc: starting for agent_call_id=%s", agent_call_id)

    bucket = (
        os.environ.get("CHANGELOG_BUCKET", "").strip()
        or os.environ.get("BUBBLE_ARTIFACT_BUCKET", "").strip()
    )
    if not bucket:
        log.error("manual_doc: CHANGELOG_BUCKET not set")
        raise SystemExit(1)

    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    doc_key = "alerts/document_extractions_table.jsonl"

    stub_row: dict | None = None
    try:
        body = s3.get_object(Bucket=bucket, Key=doc_key)["Body"].read().decode("utf-8")
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                if row.get("agent_call_id") == agent_call_id:
                    stub_row = row
                    break
            except Exception:
                pass
    except Exception as exc:
        log.error("manual_doc: could not read %s: %s", doc_key, exc)
        raise SystemExit(1)

    if not stub_row:
        log.error("manual_doc: no row found for agent_call_id=%s", agent_call_id)
        raise SystemExit(1)

    document_name = str(stub_row.get("library_item_file_name") or stub_row.get("library_item_title") or "")
    document_url = str(stub_row.get("library_item_url") or stub_row.get("source_url") or "")
    manual_doc_s3_key = str(stub_row.get("manual_doc_s3_key") or "")

    # For S3-uploaded files, generate a presigned URL so the agent can fetch the PDF
    if manual_doc_s3_key and not document_url:
        try:
            document_url = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": manual_doc_s3_key},
                ExpiresIn=900,
            )
            log.info("manual_doc: generated presigned URL for %s", manual_doc_s3_key)
        except Exception as exc:
            log.warning("manual_doc: could not generate presigned URL: %s", exc)

    if not document_name or not document_url:
        log.error("manual_doc: missing document_name or document_url in stub row")
        raise SystemExit(1)

    log.info("manual_doc: extracting '%s' from %s", document_name[:60], document_url[:80])

    from bubble.document_agent import extract_document_data
    result = extract_document_data(document_name=document_name, document_url=document_url)

    if not result:
        log.error("manual_doc: extraction returned empty result for agent_call_id=%s", agent_call_id)
        raise SystemExit(1)

    log.info("manual_doc: extracted %d field(s) for agent_call_id=%s", len(result), agent_call_id)

    update_fields = {
        **result,
        "run_id": stub_row.get("run_id") or "manual",
        "ingest_status": "pending",
        # Preserve stub metadata
        "library_item_url": document_url if not manual_doc_s3_key else stub_row.get("library_item_url", ""),
        "library_item_file_name": document_name,
        "manual_doc_s3_key": manual_doc_s3_key,
        "source_url": str(stub_row.get("source_url") or ""),
    }

    from storage.alert_s3 import patch_jsonl_row
    patched = patch_jsonl_row(
        jsonl_key=doc_key,
        match_fields={"agent_call_id": agent_call_id},
        update_fields=update_fields,
        bucket=bucket,
    )
    log.info("manual_doc: patched %d row(s) for agent_call_id=%s", patched, agent_call_id)


def main() -> None:
    args = parse_args()

    from config.run_spec import (
        compute_run_spec,
        render_run_spec_summary,
        validate_run_spec,
    )
    run_spec = compute_run_spec(args)
    try:
        validate_run_spec(run_spec)
    except ValueError as e:
        log.error("RunSpec validation failed: %s", e)
        raise SystemExit(1) from e
    human_summary, _ = render_run_spec_summary(run_spec)
    log.info("RunSpec:\n%s", human_summary)

    if run_spec.prod_observe_mode:
        from bubble.enrich_refs import ORGANIZATION_TREE_NAME, TYPE1_TREE_NAME, TOPIC_TREE_NAME
        log.info(
            "Bubble tree config: Organization=%r  ResourceTypes=%r  Topic=%r",
            ORGANIZATION_TREE_NAME, TYPE1_TREE_NAME, TOPIC_TREE_NAME,
        )

    # Bubble LIVE healthcheck at startup when enrichment is on and mode is LIVE
    if run_spec.bubble_enrich_enabled and run_spec.bubble_mode == "LIVE":
        from bubble.healthcheck import bubble_healthcheck
        bubble_ok, _ = bubble_healthcheck()
        run_spec.bubble_live_ok = bubble_ok
        if not bubble_ok:
            log.warning("bubble_live_ok=false: Bubble API healthcheck failed; ref resolution may fail")

    from bubble.reference_resolution import clear_records
    clear_records()

    # Load OpenAI and DB settings from SSM in AWS/prod mode (before any bubble/ai code)
    try:
        from bubble.ssm_loader import load_openai_env_from_ssm, load_db_env_from_ssm
        load_openai_env_from_ssm()
        load_db_env_from_ssm()
    except Exception as e:
        log.debug("SSM loader skipped or failed: %s", e)

    if args.print_bubble_schema:
        from bubble_resources import BUBBLE_RESOURCE_FIELDS
        for f in BUBBLE_RESOURCE_FIELDS:
            print(f)
        raise SystemExit(0)

    if args.smoke_bubble_resolvers:
        raise SystemExit(_run_smoke_bubble_resolvers())

    # Rerun mode: re-evaluate a single stored alert with current agent config.
    # Triggered via RERUN_RUN_ID + RERUN_TARGET_ID env vars (set by ECS RunTask override).
    rerun_run_id = os.environ.get("RERUN_RUN_ID", "").strip()
    rerun_target_id = os.environ.get("RERUN_TARGET_ID", "").strip()
    if rerun_run_id and rerun_target_id:
        rerun_mode = os.environ.get("RERUN_MODE", "alerts").strip()
        rerun_library_item_url = os.environ.get("RERUN_LIBRARY_ITEM_URL", "").strip()
        _run_rerun(rerun_run_id, rerun_target_id, rerun_mode=rerun_mode, rerun_library_item_url=rerun_library_item_url)
        return

    # Manual chunk mode: chunk a manually uploaded transcript and mark it pending for ingest.
    # Triggered via MANUAL_CHUNK_AGENT_CALL_ID + MANUAL_CHUNK_TRANSCRIPT_S3_KEY env vars.
    manual_chunk_call_id = os.environ.get("MANUAL_CHUNK_AGENT_CALL_ID", "").strip()
    manual_chunk_transcript_key = os.environ.get("MANUAL_CHUNK_TRANSCRIPT_S3_KEY", "").strip()
    if manual_chunk_call_id and manual_chunk_transcript_key:
        _run_manual_chunk(manual_chunk_call_id, manual_chunk_transcript_key)
        return

    # Manual doc mode: run document extraction on a manually added document row.
    # Triggered via MANUAL_DOC_AGENT_CALL_ID env var.
    manual_doc_call_id = os.environ.get("MANUAL_DOC_AGENT_CALL_ID", "").strip()
    if manual_doc_call_id:
        _run_manual_doc(manual_doc_call_id)
        return

    from datetime import datetime, timezone
    run_timestamp = int(datetime.now(timezone.utc).timestamp())
    run_id = (os.environ.get("RUN_ID") or "").strip() or f"run-{run_timestamp}"
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

    # Simulate-change mode: no fetch, no persist (single target)
    if args.simulate_change:
        _run_simulate_change(
            args.target_id.strip(),
            targets or [],
            args.verbose,
            args.emit_bubble_json,
            args.ai_enrich,
            args.bubble_enrich,
            args.no_ai,
            getattr(args, "e2e_bubble", False),
            getattr(args, "bubble_snapshot_limit", 200),
            args.bubble_report,
        )
        return

    # Simulate-change-all: fetch + extract on all targets, inject fakes into first N, no persist
    if args.simulate_change_all:
        _run_simulate_change_all(
            targets or [],
            args.simulate_change_n,
            args.verbose,
            args.emit_bubble_json,
            args.ai_enrich,
            args.bubble_enrich,
            args.no_ai,
            getattr(args, "e2e_bubble", False),
            getattr(args, "bubble_snapshot_limit", 200),
            args.bubble_report,
        )
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
                run_id=run_id,
                run_timestamp=run_timestamp,
                org_path=t.get("org_path"),
                group=t.get("group"),
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

    # Build Bubble snapshot when E2E or verify so mapping/enrichment (and verification) can use real Bubble objects
    bubble_snapshot = None
    if run_spec.bubble_mode == "SNAPSHOT":
        try:
            from bubble.client import get_client
            from bubble.snapshot import build_bubble_snapshot
            client = get_client(use_cache=True)
            bubble_snapshot = build_bubble_snapshot(client, limit=getattr(args, "bubble_snapshot_limit", 200))
        except Exception as e:
            log.warning("Bubble snapshot build failed, continuing without snapshot: %s", e)

    # Snapshot stats for RunSpec summary and SNAPSHOT low-count warning
    snapshot_stats = None
    if bubble_snapshot is not None:
        snapshot_stats = {
            "calendar_items": len(bubble_snapshot.get("calendar_items") or []),
            "resources": len(bubble_snapshot.get("resources") or []),
            "tree_nodes": len(bubble_snapshot.get("tree_nodes") or []),
        }
        from config.run_spec import add_snapshot_warnings
        add_snapshot_warnings(run_spec, snapshot_stats)

    # Always build bubble payloads for the email report (and optional bubble output)
    resources, calendar_items, alerts_by_cal = _build_bubble_payloads(
        change_events,
        ai_enrich=run_spec.ai_enrich_enabled,
        bubble_enrich=run_spec.bubble_enrich_enabled,
        no_ai=args.no_ai,
        bubble_snapshot=bubble_snapshot,
        run_id=run_id,
    )

    # Upload alerts to S3 (same bucket as bubble reports, under alerts/ prefix).
    if alerts_by_cal:
        try:
            from bubble.calendar_alerts import upload_alerts_to_s3
            upload_alerts_to_s3(alerts_by_cal, run_timestamp)
        except Exception as e:
            log.warning("Alert upload to S3 failed (non-fatal): %s", e)

    # Verify reference fields against snapshot: drop invalid IDs + warn, or exit non-zero in --e2e-bubble-verify
    if bubble_snapshot is not None:
        from bubble.mapping_pipeline import verify_all_references
        verify_mode = "e2e_verify" if run_spec.e2e_bubble_verify else "normal"
        resources, calendar_items = verify_all_references(
            resources, calendar_items, bubble_snapshot, mode=verify_mode,
            artifact_output_dir=run_spec.artifact_output_dir or None,
        )

    # PDF meeting metadata: download PDFs, extract date/group/times; set date if null, __meeting_meta for audit
    if run_spec.pdf_meeting_meta_enabled:
        from bubble.payload import apply_pdf_meeting_metadata
        apply_pdf_meeting_metadata(
            resources,
            pdf_meeting_meta_enabled=True,
            artifact_output_dir=run_spec.artifact_output_dir or None,
        )

    # RunSpec summary (with snapshot_stats and warnings) for logs and email header
    human_summary, _ = render_run_spec_summary(run_spec, snapshot_stats)
    log.info("RunSpec (final):\n%s", human_summary)

    # Debug metric summary: snapshot counts, resolved vs unresolved per field, calendar-too-small warning
    from bubble.reference_resolution import get_resolution_summary
    from config.run_spec import render_debug_metric_summary
    resolution_by_field = get_resolution_summary()
    debug_metric_text, _ = render_debug_metric_summary(snapshot_stats, resolution_by_field, run_spec.bubble_live_ok)
    log.info("Debug metric summary:\n%s", debug_metric_text)

    # Build and write the email report: RunSpec + debug metric summary + report body
    email_report = render_email_report(change_events, resources, calendar_items)
    full_email = human_summary + "\n\n" + debug_metric_text + "\n\n" + email_report
    LAST_EMAIL_REPORT_FILE.write_text(full_email, encoding="utf-8")
    log.info("Email report written to %s", LAST_EMAIL_REPORT_FILE)

    if args.bubble_report or args.emit_bubble_json:
        if args.bubble_report:
            from bubble.payload import strip_debug_keys
            report = _render_bubble_report(
                [strip_debug_keys(r) for r in resources],
                [strip_debug_keys(c) for c in calendar_items],
            )
            log.info("Bubble report: %d Calendar Items, %d Resources", len(calendar_items), len(resources))
        else:
            report = render_report(change_events, verbose=args.verbose)
            log.info("\n%s", report)
        if args.emit_bubble_json:
            _write_bubble_payload(
                change_events,
                ai_enrich=run_spec.ai_enrich_enabled,
                bubble_enrich=run_spec.bubble_enrich_enabled,
                no_ai=args.no_ai,
                resources=resources,
                calendar_items=calendar_items,
            )
    else:
        report = render_report(change_events, verbose=args.verbose)
        log.info("\n%s", report)

    REPORT_FILE.write_text(report, encoding="utf-8")
    log.info("Report written to %s", REPORT_FILE)

    # After a successful run (even if no changes), optionally upload last_bubble_report.json to S3
    # when BUBBLE_ARTIFACT_BUCKET is set. This is read-only: no Bubble writes are performed.
    _upload_bubble_report_to_s3(run_timestamp, run_spec, args.targets_file)

    # Write UI-ready alerts output: runs/<date>/<run_id>/alerts.json + per-page agent_output/doc_extractions
    try:
        from storage.alert_s3 import store_run_alerts
        store_run_alerts(change_events, run_id, run_timestamp)
    except Exception as e:
        log.warning("Alert S3 write failed (non-fatal): %s", e)

    # Only send email when EMAIL_ENABLED=true, there are meaningful changes, and at least one
    # Resource or Calendar Item in the Bubble payload (avoid empty payload emails).
    has_payload = bool(resources or calendar_items)

    # "Meaningful changes" at the target level (diffs / first_run / include_hash_changes)
    events_with_meaningful_changes = [
        e for e in change_events if "error" not in e and _has_displayable_changes(e)
    ]
    targets_changed = len(events_with_meaningful_changes)
    if targets_changed > 0 and has_payload:
        from emailer import send_report

        if LAST_EMAIL_REPORT_FILE.exists():
            email_body = LAST_EMAIL_REPORT_FILE.read_text(encoding="utf-8")
            log.info("Email body source: last_email_report.txt")
        else:
            email_body = REPORT_FILE.read_text(encoding="utf-8")
            log.info("Email body source: last_report.txt")
        send_report(email_body, targets_changed)

    if os.environ.get("CHANGELOG_BUCKET", "").strip():
        from storage.changelog_s3 import append_change_events as s3_append

        _HTML_KEYS = {"content_html", "prev_content_html"}
        changelog_events = [{k: v for k, v in e.items() if k not in _HTML_KEYS} for e in change_events]
        uri = s3_append(run_timestamp, changelog_events)
        if uri:
            log.info("Change events appended to %s", uri)

    # Always write resolution and verify artifacts to RunSpec artifact dir; upload to S3 when enabled
    artifact_dir = run_spec.artifact_output_dir or "debug"
    from bubble.reference_resolution import write_reference_resolution_report
    write_reference_resolution_report(path=Path(artifact_dir) / "reference_resolution_report.json")
    if run_spec.s3_artifact_upload_enabled:
        from config.run_spec import upload_artifacts_to_s3
        upload_artifacts_to_s3(artifact_dir, run_timestamp)


if __name__ == "__main__":
    main()
