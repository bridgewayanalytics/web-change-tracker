#!/usr/bin/env python3
"""Minimal change detection spike: fetch → extract → diff (per resource type) → report."""

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from state_store import LocalStateStore

TARGETS_FILE = Path(__file__).parent / "targets.json"
STATE_STORE = LocalStateStore(Path(__file__).parent / "state.json")
REPORT_FILE = Path(__file__).parent / "last_report.txt"
TARGET_URL = "https://example.com"
USE_PLAYWRIGHT = os.environ.get("USE_PLAYWRIGHT", "1") != "0"  # Set USE_PLAYWRIGHT=0 to use requests only

# -----------------------------------------------------------------------------
# Extractors: map name -> callable(soup, base_url, params) -> list[dict]
# Each extractor returns a list of dicts with stable keys for diffing (url for links, triple for events).
# -----------------------------------------------------------------------------


def _link_collector_v1(soup: BeautifulSoup, base_url: str, params: dict) -> list[dict]:
    """Collect links matching params.extensions (e.g. ['.pdf']). Returns [{label, url}]."""
    extensions = params.get("extensions", [".pdf"])
    results: list[dict] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        full_url = urljoin(base_url, href)
        parsed = urlparse(full_url)
        path = parsed.path.lower()
        if any(path.endswith(ext) for ext in extensions):
            label = (a.get_text(strip=True) or path.split("/")[-1] or full_url)[:80]
            results.append({"label": label, "url": full_url})
    return results


def _keyword_links_v1(soup: BeautifulSoup, base_url: str, params: dict) -> list[dict]:
    """Collect links whose visible text contains any params.keywords. Returns [{label, url}]."""
    keywords = [k.lower() for k in params.get("keywords", [])]
    if not keywords:
        return []
    results: list[dict] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        if not text:
            continue
        text_lower = text.lower()
        if any(kw in text_lower for kw in keywords):
            full_url = urljoin(base_url, a["href"])
            if full_url not in seen:
                seen.add(full_url)
                results.append({"label": text[:80], "url": full_url})
    return results


def _naic_events_v1(soup: BeautifulSoup, base_url: str, params: dict) -> list[dict]:
    """NAIC-specific: extract event/meeting entries. Returns [{title, datetime_text, url}]."""
    # Placeholder: links with nearby date-like text. Real NAIC pages would use specific selectors.
    results: list[dict] = []
    for a in soup.find_all("a", href=True):
        parent = a.parent
        text = (parent.get_text(separator=" ", strip=True) if parent else a.get_text(strip=True))[:200]
        full_url = urljoin(base_url, a["href"])
        title = a.get_text(strip=True) or full_url.split("/")[-1] or full_url
        # Look for date-like pattern in surrounding text
        dt_match = re.search(r"\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}|\d{4}[/\-]\d{1,2}[/\-]\d{1,2}", text)
        datetime_text = dt_match.group(0) if dt_match else ""
        results.append({"title": title[:100], "datetime_text": datetime_text, "url": full_url})
    return results[:20]  # Cap to avoid noise; real NAIC pages would use specific selectors


EXTRACTOR_REGISTRY: dict[str, Callable[..., list[dict]]] = {
    "link_collector_v1": _link_collector_v1,
    "keyword_links_v1": _keyword_links_v1,
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


# -----------------------------------------------------------------------------
# Stable keys per resource type for diffing
# -----------------------------------------------------------------------------


def _stable_key(rtype: str, item: dict) -> str:
    """Return a stable string key for diffing."""
    if rtype in ("docs", "event_links"):
        return item.get("url", "")
    if rtype == "events":
        t = item.get("title", "")
        dt = item.get("datetime_text", "")
        u = item.get("url", "")
        return f"{t}|{dt}|{u}"
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


def fetch_with_playwright(url: str) -> str:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="networkidle", timeout=15000)
        html = page.content()
        browser.close()
    return html


def fetch_with_requests(url: str) -> str:
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    r.encoding = r.apparent_encoding
    return r.text


def fetch_page(url: str) -> str:
    if USE_PLAYWRIGHT:
        try:
            return fetch_with_playwright(url)
        except Exception as e:
            print(f"[Playwright failed: {e}] Falling back to requests...")
    return fetch_with_requests(url)


def parse_html(html: str) -> tuple[str, BeautifulSoup]:
    """Parse HTML; return (page_hash, soup)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    text = " ".join(text.split())
    page_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return page_hash, soup


# -----------------------------------------------------------------------------
# State (via store abstraction)
# -----------------------------------------------------------------------------


def load_targets() -> list[dict] | None:
    if not TARGETS_FILE.exists():
        return None
    with open(TARGETS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else data.get("targets", data)


def _get_target_state(raw: dict, key: str) -> dict | None:
    """Extract one target's state from raw store output; migrate old formats."""
    if "targets" in raw:
        s = raw["targets"].get(key)
        return _migrate_state(s)
    if "page_hash" in raw and key == "default":
        return {
            "page_hash": raw["page_hash"],
            "extracted": {"docs": [{"label": "?", "url": u} for u in raw.get("pdf_links", [])]},
        }
    return None


def _migrate_state(s: dict | None) -> dict | None:
    """Migrate old extracted format to new extracted[resource_type]."""
    if not s:
        return s
    if "pdf_links" in s and "extracted" not in s:
        s["extracted"] = {"docs": [{"label": u, "url": u} for u in s.get("pdf_links", [])]}
        del s["pdf_links"]
    return s


def load_state(key: str) -> dict | None:
    raw = STATE_STORE.load_state()
    return _get_target_state(raw, key)


def save_state(key: str, page_hash: str, extracted: dict[str, list[dict]]) -> None:
    raw = STATE_STORE.load_state()
    if "page_hash" in raw and "targets" not in raw:
        raw = {"targets": {"default": {"page_hash": raw["page_hash"], "extracted": raw.get("extracted", {})}}}
    if "targets" not in raw:
        raw["targets"] = {}
    raw["targets"][key] = {"page_hash": page_hash, "extracted": extracted}
    STATE_STORE.save_state(raw)


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
        return f"{item.get('label', '')} -> {item.get('url', '')}"
    if rtype == "events":
        return f"{item.get('title', '')} ({item.get('datetime_text', '')}) {item.get('url', '')}"
    return str(item)


def render_report(change_events: list[dict]) -> str:
    """Group by target, then by resource type. Only include targets with changes."""
    events_with_changes = [e for e in change_events if _has_changes(e["change"])]
    if not events_with_changes:
        return "No changes detected.\n"

    lines = ["Web Change Report", "=" * 40, ""]
    for e in events_with_changes:
        label = e["label"]
        url = e["url"]
        ch = e["change"]
        lines.append(f"## {label}")
        lines.append(f"URL: {url}")

        if ch["first_run"]:
            lines.append("  - Initial baseline recorded")
        else:
            if ch["page_changed"]:
                lines.append("  - Page content changed")

            for rtype, diff in sorted(ch.get("by_type", {}).items()):
                lines.append(f"  [{rtype}]")
                for x in diff.get("added", []):
                    lines.append(f"    + {_format_item(rtype, x)}")
                for x in diff.get("removed", []):
                    lines.append(f"    - {_format_item(rtype, x)}")
        lines.append("")
    return "\n".join(lines)


# -----------------------------------------------------------------------------
# Default extract rules (backwards compat when target has no extract array)
# -----------------------------------------------------------------------------

DEFAULT_EXTRACT = [
    {"type": "docs", "extractor": "link_collector_v1", "params": {"extensions": [".pdf"]}, "_purpose": "Collect PDFs."},
]


def process_target(target_id: str, label: str, url: str, extract_rules: list[dict] | None) -> dict:
    """Process one target: fetch, extract, diff, save, print."""
    print(f"\n--- {label} ---")
    print(f"Fetching {url}...")
    html = fetch_page(url)
    page_hash, soup = parse_html(html)

    rules = extract_rules if extract_rules else DEFAULT_EXTRACT
    extracted = run_extractors(soup, url, rules)

    prev = load_state(target_id)
    change = compute_change(prev, page_hash, extracted)

    save_state(target_id, page_hash, extracted)

    # Console output
    if change["first_run"]:
        print("[FIRST RUN] Recording baseline.")
    elif _has_changes(change):
        print("[CHANGE DETECTED]")
        if change["page_changed"]:
            print("  Page content changed")
        for rtype, diff in change.get("by_type", {}).items():
            print(f"  [{rtype}]")
            for x in diff.get("added", []):
                print(f"    + {_format_item(rtype, x)}")
            for x in diff.get("removed", []):
                print(f"    - {_format_item(rtype, x)}")
    else:
        print("[NO CHANGE]")

    for rtype, items in sorted(extracted.items()):
        print(f"  {rtype}: {len(items)} items")

    return {"target_id": target_id, "label": label, "url": url, "change": change}


def main() -> None:
    targets = load_targets()
    change_events: list[dict] = []

    if targets:
        for t in targets:
            target_id = t.get("id", t.get("url", "unknown"))
            label = t.get("label", target_id)
            url = t.get("url")
            extract_rules = t.get("extract")
            if url:
                change_events.append(process_target(target_id, label, url, extract_rules))
            else:
                print(f"\n--- {label} --- Skipping: no URL")
    else:
        change_events.append(process_target("default", "default", TARGET_URL, None))

    report = render_report(change_events)
    print("\n" + report)
    REPORT_FILE.write_text(report, encoding="utf-8")
    print(f"Report written to {REPORT_FILE}")

    has_changes = any(_has_changes(e["change"]) for e in change_events)
    if has_changes:
        from emailer import send_report
        send_report(report, has_changes)


if __name__ == "__main__":
    main()
