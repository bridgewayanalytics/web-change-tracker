"""
Fetch evaluation data: sample 20 alert rows + scrape their live NAIC pages.
Saves everything to analysis/accuracy_eval/eval_data.json for analysis in Claude Code session.

Usage:
    AWS_PROFILE=bridgeway python3 scripts/fetch_eval_data.py
"""

import json
import logging
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

BUCKET = os.environ.get("CHANGELOG_BUCKET") or "web-change-tracker-prod-artifacts-815039343351"
ALERTS_KEY = "alerts/alerts_table.jsonl"
OUTPUT_PATH = Path("analysis/accuracy_eval/eval_data.json")

SKIP_TYPES = {
    "No Meaningful Change",
    "Alert not relevant - the change was limited to carrousel or reordering of content",
}

FIELDS_TO_INCLUDE = [
    "agent_call_id", "run_id", "target_id", "alert_date_time",
    "alert_type", "alert_title", "alert_description", "alert_url",
    "organization",
    "event_title", "event_start_date_time", "event_end_date_time",
    "event_duration", "event_is_full_day", "event_url",
    "event_call_in_number_access_code",
    "agenda_item_title_chronicle_topics",
    "agenda_item_title_official",
    "library_item_preliminary_title", "library_item_url", "library_items_file_name",
    "is_the_alert_relevant_for_an_art_newsreel_article",
    "bubble_action",
]


def s3_client():
    import boto3
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def load_rows() -> list[dict]:
    body = s3_client().get_object(Bucket=BUCKET, Key=ALERTS_KEY)["Body"].read().decode("utf-8")
    rows = []
    for line in body.splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def stratified_sample(rows: list[dict], n: int) -> list[dict]:
    eligible = [r for r in rows if r.get("alert_url") and str(r.get("alert_type", "")) not in SKIP_TYPES]

    by_type: dict[str, list[dict]] = defaultdict(list)
    for r in eligible:
        at = str(r.get("alert_type") or "Unknown")
        by_type[at].append(r)

    for at in by_type:
        by_type[at].sort(key=lambda r: str(r.get("alert_date_time") or ""), reverse=True)

    sample = []
    types = sorted(by_type.keys(), key=lambda t: -len(by_type[t]))
    i = 0
    while len(sample) < n and any(by_type[t] for t in types):
        t = types[i % len(types)]
        if by_type[t]:
            sample.append(by_type[t].pop(0))
        i += 1

    return sample[:n]


def fetch_page(url: str) -> str:
    if not url or url == "N/A":
        return ""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; BridgewayBot/1.0)"}
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()
        return " ".join(soup.get_text(" ", strip=True).split())
    except Exception as e:
        log.warning("  Could not fetch %s: %s", url, e)
        return ""


def main():
    log.info("Loading alerts from S3...")
    rows = load_rows()
    log.info("Total rows: %d", len(rows))

    sample = stratified_sample(rows, 20)
    log.info("Sampled %d rows across %d alert types", len(sample),
             len({r.get("alert_type") for r in sample}))

    records = []
    for i, row in enumerate(sample, 1):
        alert_url = str(row.get("alert_url") or "")
        lib_url   = str((row.get("library_item_url") or ""))

        log.info("[%d/20] %s", i, str(row.get("alert_title") or "")[:70])
        log.info("  Fetching alert_url: %s", alert_url)
        alert_page = fetch_page(alert_url)

        # Also fetch document URL if different from alert URL
        doc_page = ""
        if lib_url and lib_url not in ("N/A", "", alert_url) and lib_url.startswith("http"):
            log.info("  Fetching library_item_url: %s", lib_url)
            doc_page = fetch_page(lib_url)[:3000]  # shorter for docs

        records.append({
            "index": i,
            "alert": {f: row.get(f) for f in FIELDS_TO_INCLUDE},
            "alert_page_content": alert_page,
            "doc_page_content": doc_page,
            "alert_page_length": len(alert_page),
        })

        log.info("  Page: %d chars | Doc: %d chars", len(alert_page), len(doc_page))
        time.sleep(0.5)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)

    log.info("Saved %d records → %s", len(records), OUTPUT_PATH)
    print(f"\nData ready at: {OUTPUT_PATH}")
    print(f"Alert types covered: {sorted({r['alert']['alert_type'] for r in records})}")


if __name__ == "__main__":
    main()
