#!/usr/bin/env python3
"""Minimal change detection spike: fetch → normalize → hash → diff → report."""

import hashlib
import json
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

STATE_FILE = Path(__file__).parent / "state.json"
TARGETS_FILE = Path(__file__).parent / "targets.json"
REPORT_FILE = Path(__file__).parent / "last_report.txt"
TARGET_URL = "https://example.com"
USE_PLAYWRIGHT = True  # Falls back to requests if Playwright unavailable; run `playwright install chromium` for JS pages


def fetch_with_playwright(url: str) -> str:
    """Fetch page HTML with Playwright (handles JS-rendered content)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="networkidle", timeout=15000)
        html = page.content()
        browser.close()
    return html


def fetch_with_requests(url: str) -> str:
    """Fetch page HTML with requests (simple pages only)."""
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


def normalize(html: str, base_url: str) -> tuple[str, list[str]]:
    """Parse with BeautifulSoup; return (page_text_hash, sorted_pdf_links)."""
    soup = BeautifulSoup(html, "html.parser")
    # Strip script/style
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    # Normalize whitespace for stable hash
    text = " ".join(text.split())

    pdf_links: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        full_url = urljoin(base_url, href)
        parsed = urlparse(full_url)
        path = parsed.path.lower()
        if path.endswith(".pdf"):
            pdf_links.append(full_url)

    pdf_links = sorted(set(pdf_links))
    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return text_hash, pdf_links


def load_targets() -> list[dict] | None:
    """Load targets from targets.json. Returns None if file missing (use single URL fallback)."""
    if not TARGETS_FILE.exists():
        return None
    with open(TARGETS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else data.get("targets", data)


def load_state(key: str) -> dict | None:
    if not STATE_FILE.exists():
        return None
    with open(STATE_FILE, encoding="utf-8") as f:
        raw = json.load(f)
    # New format: { "targets": { id: {...} } }
    if "targets" in raw:
        return raw["targets"].get(key)
    # Old format: flat { "page_hash", "pdf_links" }
    if "page_hash" in raw and key == "default":
        return {"page_hash": raw["page_hash"], "pdf_links": raw.get("pdf_links", [])}
    return None


def save_state(key: str, page_hash: str, pdf_links: list[str]) -> None:
    raw = {}
    if STATE_FILE.exists():
        with open(STATE_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    # Migrate old format to new
    if "page_hash" in raw and "targets" not in raw:
        raw = {"targets": {"default": {"page_hash": raw["page_hash"], "pdf_links": raw.get("pdf_links", [])}}}
    if "targets" not in raw:
        raw["targets"] = {}
    raw["targets"][key] = {"page_hash": page_hash, "pdf_links": pdf_links}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2)


def compute_change(prev_state: dict | None, curr_state: dict) -> dict:
    """Compare prev and curr state; return structured change event."""
    curr_hash = curr_state.get("page_hash", "")
    curr_pdfs = set(curr_state.get("pdf_links") or [])
    prev_hash = prev_state.get("page_hash") if prev_state else None
    prev_pdfs = set(prev_state.get("pdf_links") or []) if prev_state else set()

    new_pdfs = sorted(curr_pdfs - prev_pdfs)
    removed_pdfs = sorted(prev_pdfs - curr_pdfs)
    page_changed = prev_hash != curr_hash if prev_hash else bool(curr_hash)
    first_run = prev_state is None

    return {
        "first_run": first_run,
        "page_changed": page_changed,
        "new_pdfs": new_pdfs,
        "removed_pdfs": removed_pdfs,
        "before_hash": prev_hash,
        "after_hash": curr_hash,
    }


def render_report(change_events: list[dict]) -> str:
    """Produce concise plain-text summary grouped by target label (only targets with changes)."""
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
                lines.append(f"  - Page content changed")
            for u in ch["new_pdfs"]:
                lines.append(f"  + New PDF: {u}")
            for u in ch["removed_pdfs"]:
                lines.append(f"  - Removed PDF: {u}")
        lines.append("")
    return "\n".join(lines)


def _has_changes(ch: dict) -> bool:
    return ch["first_run"] or ch["page_changed"] or ch["new_pdfs"] or ch["removed_pdfs"]


def process_target(target_id: str, label: str, url: str) -> dict:
    """Process one target: fetch, normalize, diff, save, print. Returns change event for report."""
    print(f"\n--- {label} ---")
    print(f"Fetching {url}...")
    html = fetch_page(url)
    page_hash, pdf_links = normalize(html, url)

    prev = load_state(target_id)
    curr = {"page_hash": page_hash, "pdf_links": pdf_links}
    change = compute_change(prev, curr)

    save_state(target_id, page_hash, pdf_links)

    # Console output
    if change["first_run"]:
        print("[FIRST RUN] No previous state. Recording baseline.")
    elif _has_changes(change):
        print("[CHANGE DETECTED]")
        if change["page_changed"]:
            print(f"  Page content changed (hash: {change['before_hash'][:8] if change['before_hash'] else '?'}... → {change['after_hash'][:8]}...)")
        for u in change["new_pdfs"]:
            print(f"  + {u}")
        for u in change["removed_pdfs"]:
            print(f"  - {u}")
    else:
        print("[NO CHANGE] Page and PDF links match previous run.")

    print(f"Current PDF links: {len(pdf_links)}")
    for link in pdf_links:
        print(f"  {link}")

    return {"target_id": target_id, "label": label, "url": url, "change": change}


def main() -> None:
    targets = load_targets()
    change_events: list[dict] = []

    if targets:
        for t in targets:
            target_id = t.get("id", t.get("url", "unknown"))
            label = t.get("label", target_id)
            url = t.get("url")
            if url:
                change_events.append(process_target(target_id, label, url))
            else:
                print(f"\n--- {label} --- Skipping: no URL")
    else:
        change_events.append(process_target("default", "default", TARGET_URL))

    report = render_report(change_events)
    print("\n" + report)
    REPORT_FILE.write_text(report, encoding="utf-8")
    print(f"Report written to {REPORT_FILE}")


if __name__ == "__main__":
    main()
