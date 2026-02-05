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
from urllib.parse import urljoin, urlparse

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


def load_targets(targets_file: Path) -> list[dict] | None:
    path = Path(targets_file)
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else data.get("targets", data)


def _migrate_state(s: dict | None) -> dict | None:
    """Migrate old extracted format to new extracted[resource_type]."""
    if not s:
        return s
    if "pdf_links" in s and "extracted" not in s:
        s = dict(s)
        s["extracted"] = {"docs": [{"label": u, "url": u} for u in s.get("pdf_links", [])]}
        del s["pdf_links"]
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
        return f"{item.get('label', '')} -> {item.get('url', '')}"
    if rtype == "events":
        return f"{item.get('title', '')} ({item.get('datetime_text', '')}) {item.get('url', '')}"
    return str(item)


def render_report(change_events: list[dict]) -> str:
    """Group by target, then by resource type. Include targets with changes and errors."""
    events_with_changes = [e for e in change_events if "error" not in e and _has_changes(e["change"])]
    events_with_errors = [e for e in change_events if "error" in e]

    lines = ["Web Change Report", "=" * 40, ""]

    for e in events_with_errors:
        lines.append(f"## ERROR: {e.get('label', 'unknown')}")
        lines.append(f"URL: {e.get('url', '')}")
        lines.append(f"  {e['error']}")
        lines.append("")

    if events_with_changes:
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

    if not events_with_changes and not events_with_errors:
        return "No changes detected.\n"

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
) -> dict:
    """Process one target: fetch, extract, diff, save, print."""
    log.info("--- %s ---", label)
    log.info("Fetching %s...", url)
    html = fetch_page(url)
    page_hash, soup = parse_html(html)

    rules = extract_rules if extract_rules else DEFAULT_EXTRACT
    extracted = run_extractors(soup, url, rules)

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
    return p.parse_args()


def main() -> None:
    from datetime import datetime, timezone

    args = parse_args()
    run_timestamp = int(datetime.now(timezone.utc).timestamp())
    targets = load_targets(args.targets_file)

    # Filter by target_ids if provided
    target_ids_filter: set[str] | None = None
    if args.target_ids:
        target_ids_filter = {s.strip() for s in args.target_ids.split(",") if s.strip()}

    if targets and target_ids_filter is not None:
        targets = [t for t in targets if t.get("id", t.get("url", "unknown")) in target_ids_filter]
        if not targets:
            log.warning("No targets match --target-ids %s", args.target_ids)
            return

    change_events: list[dict] = []

    # For --compare-snapshot: load from snapshot_dir or default snapshots/
    compare_snapshot_dir = args.snapshot_dir if args.snapshot_dir else (Path("snapshots") if args.compare_snapshot else None)

    def process_one(target_id: str, label: str, url: str, extract_rules: list[dict] | None) -> dict:
        try:
            return process_target(
                target_id,
                label,
                url,
                extract_rules,
                snapshot_dir=args.snapshot_dir,
                compare_snapshot=args.compare_snapshot,
                compare_snapshot_dir=compare_snapshot_dir,
            )
        except Exception as e:
            log.error("Target %s failed: %s", label, e, exc_info=False)
            return {"target_id": target_id, "label": label, "url": url, "error": str(e)}

    if targets is not None and targets:
        for i, t in enumerate(targets):
            if i > 0 and DELAY_BETWEEN_PAGES > 0:
                time.sleep(DELAY_BETWEEN_PAGES)
            target_id = t.get("id", t.get("url", "unknown"))
            label = t.get("label", target_id)
            url = t.get("url")
            extract_rules = t.get("extract")
            if url:
                change_events.append(process_one(target_id, label, url, extract_rules))
            else:
                log.warning("--- %s --- Skipping: no URL", label)
    else:
        change_events.append(process_one("default", "default", TARGET_URL, None))

    report = render_report(change_events)
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
