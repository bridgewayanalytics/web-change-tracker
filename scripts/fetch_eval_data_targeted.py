"""
Fetch targeted evaluation data for field-specific accuracy testing.

Unlike the general stratified sample, each mode pulls rows where the target
field is actually exercised — eliminating the N/A inflation problem.

Modes
-----
chronicle_topics
    10 rows from agenda-item alert types (New/Updated Agenda, New/Updated
    Agenda & Materials). Split ~5/5 between rows where the agent assigned at
    least one specific topic vs. rows where the agent said all N/A (to catch
    both correct assignments AND potential misses).

events
    10 rows from meeting-type alerts where event fields are populated
    (event_title and event_start_date_time are not N/A). Tests: event title
    format, start/end times, Webex URL, call-in number.

documents
    10 rows where a real document was detected (library_item_url and
    library_items_file_name are not N/A). Tests: document URL accuracy,
    filename extraction, preliminary title.

Usage
-----
    AWS_PROFILE=bridgeway python3 scripts/fetch_eval_data_targeted.py --mode chronicle_topics
    AWS_PROFILE=bridgeway python3 scripts/fetch_eval_data_targeted.py --mode events
    AWS_PROFILE=bridgeway python3 scripts/fetch_eval_data_targeted.py --mode documents
"""

import argparse
import json
import logging
import os
import random
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

BUCKET = os.environ.get("CHANGELOG_BUCKET") or "web-change-tracker-prod-artifacts-815039343351"
ALERTS_KEY = "alerts/alerts_table.jsonl"

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

# Alert types that produce real agenda items
AGENDA_TYPES = {
    "New Agenda",
    "Updated Agenda",
    "New Agenda & Materials",
    "Updated Agenda & Materials",
}

# Alert types where event fields (time, location, Webex) are populated
EVENT_TYPES = {
    "New Meeting",
    "Updated Meeting",
    "New Agenda",
    "Updated Agenda",
    "New Agenda & Materials",
    "Updated Agenda & Materials",
}


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


def _has_specific_topics(row: dict) -> bool:
    """True if any agenda item has at least one non-N/A chronicle topic."""
    items = row.get("agenda_item_title_chronicle_topics") or []
    if not isinstance(items, list):
        return False
    for item in items:
        if not isinstance(item, dict):
            continue
        for t in (item.get("chronicle_topics") or []):
            if t and str(t).strip().upper() != "N/A":
                return True
    return False


def _has_real_event(row: dict) -> bool:
    title = str(row.get("event_title") or "").strip()
    start = str(row.get("event_start_date_time") or "").strip()
    return title not in ("", "N/A") and start not in ("", "N/A")


def _has_real_document(row: dict) -> bool:
    url = str(row.get("library_item_url") or "").strip()
    fname = str(row.get("library_items_file_name") or "").strip()
    return url not in ("", "N/A") and fname not in ("", "N/A")


def _is_new_schema(row: dict) -> bool:
    """Flat schema rows have library_item_url as a top-level string (not nested)."""
    return "library_item_url" in row


def sample_chronicle_topics(rows: list[dict], n: int = 10) -> list[dict]:
    """
    Pull rows from agenda-type alerts, split ~50/50 between rows where the
    agent assigned specific topics (tests correct classification) vs. all N/A
    (tests whether the agent missed topics it should have caught).
    """
    eligible = [
        r for r in rows
        if r.get("alert_url")
        and str(r.get("alert_type", "")) in AGENDA_TYPES
        and str(r.get("alert_type", "")) not in SKIP_TYPES
        and _is_new_schema(r)
    ]
    eligible.sort(key=lambda r: str(r.get("alert_date_time") or ""), reverse=True)

    with_topics    = [r for r in eligible if _has_specific_topics(r)]
    without_topics = [r for r in eligible if not _has_specific_topics(r)]

    log.info("chronicle_topics pool: %d with specific topics, %d all-N/A",
             len(with_topics), len(without_topics))

    half = n // 2
    sample = with_topics[:half] + without_topics[:n - half]

    # Deduplicate by agent_call_id, shuffle for variety
    seen: set[str] = set()
    deduped = []
    for r in sample:
        cid = str(r.get("agent_call_id") or id(r))
        if cid not in seen:
            seen.add(cid)
            deduped.append(r)

    random.shuffle(deduped)
    return deduped[:n]


def sample_events(rows: list[dict], n: int = 10) -> list[dict]:
    """
    Pull rows from meeting/agenda alert types where event fields are populated.
    Stratify by alert type to cover both New Meeting and Updated Meeting etc.
    """
    eligible = [
        r for r in rows
        if r.get("alert_url")
        and str(r.get("alert_type", "")) in EVENT_TYPES
        and str(r.get("alert_type", "")) not in SKIP_TYPES
        and _is_new_schema(r)
        and _has_real_event(r)
    ]
    eligible.sort(key=lambda r: str(r.get("alert_date_time") or ""), reverse=True)

    # Stratify by alert type so we get variety
    by_type: dict[str, list[dict]] = {}
    for r in eligible:
        t = str(r.get("alert_type") or "Unknown")
        by_type.setdefault(t, []).append(r)

    log.info("events pool by type: %s",
             {t: len(v) for t, v in sorted(by_type.items())})

    sample: list[dict] = []
    types = sorted(by_type.keys(), key=lambda t: -len(by_type[t]))
    i = 0
    while len(sample) < n and any(by_type[t] for t in types):
        t = types[i % len(types)]
        if by_type[t]:
            sample.append(by_type[t].pop(0))
        i += 1

    return sample[:n]


def sample_documents(rows: list[dict], n: int = 10) -> list[dict]:
    """
    Pull rows where a real document (URL + filename) was detected.
    Stratify by alert type to cover materials, agendas, RFCs, reports.
    """
    eligible = [
        r for r in rows
        if r.get("alert_url")
        and str(r.get("alert_type", "")) not in SKIP_TYPES
        and _is_new_schema(r)
        and _has_real_document(r)
    ]
    eligible.sort(key=lambda r: str(r.get("alert_date_time") or ""), reverse=True)

    by_type: dict[str, list[dict]] = {}
    for r in eligible:
        t = str(r.get("alert_type") or "Unknown")
        by_type.setdefault(t, []).append(r)

    log.info("documents pool by type: %s",
             {t: len(v) for t, v in sorted(by_type.items())})

    sample: list[dict] = []
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
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        required=True,
        choices=["chronicle_topics", "events", "documents"],
        help="Which field group to sample for",
    )
    args = parser.parse_args()
    mode = args.mode

    output_path = Path(f"analysis/accuracy_eval/eval_data_{mode}.json")

    log.info("Loading alerts from S3...")
    rows = load_rows()
    log.info("Total rows: %d", len(rows))

    if mode == "chronicle_topics":
        sample = sample_chronicle_topics(rows)
    elif mode == "events":
        sample = sample_events(rows)
    else:
        sample = sample_documents(rows)

    log.info("Sampled %d rows for mode=%s", len(sample), mode)

    records = []
    for i, row in enumerate(sample, 1):
        alert_url = str(row.get("alert_url") or "")
        lib_url   = str(row.get("library_item_url") or "")

        log.info("[%d/%d] %s", i, len(sample), str(row.get("alert_title") or "")[:70])
        log.info("  alert_type: %s", row.get("alert_type"))
        log.info("  Fetching alert_url: %s", alert_url)
        alert_page = fetch_page(alert_url)

        doc_page = ""
        if lib_url and lib_url not in ("N/A", "", alert_url) and lib_url.startswith("http"):
            log.info("  Fetching library_item_url: %s", lib_url)
            doc_page = fetch_page(lib_url)[:3000]

        records.append({
            "index": i,
            "alert": {f: row.get(f) for f in FIELDS_TO_INCLUDE},
            "alert_page_content": alert_page,
            "doc_page_content": doc_page,
            "alert_page_length": len(alert_page),
        })

        log.info("  Page: %d chars | Doc: %d chars", len(alert_page), len(doc_page))
        time.sleep(0.5)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)

    log.info("Saved %d records → %s", len(records), output_path)
    print(f"\nData ready at: {output_path}")
    print(f"Alert types covered: {sorted({r['alert']['alert_type'] for r in records})}")

    # Print a quick preview of what was sampled
    print(f"\nSample preview ({mode}):")
    for rec in records:
        a = rec["alert"]
        print(f"  {rec['index']:2d}. [{a.get('alert_type')}] {str(a.get('alert_title') or '')[:60]}")
        if mode == "chronicle_topics":
            items = a.get("agenda_item_title_chronicle_topics") or []
            has_topics = _has_specific_topics(a)
            print(f"      topics assigned: {has_topics} | agenda items: {len(items) if isinstance(items, list) else '?'}")
        elif mode == "events":
            print(f"      event: {a.get('event_title', '')[:50]} @ {a.get('event_start_date_time', '')[:20]}")
        else:
            title = a.get("library_item_preliminary_title")
            if isinstance(title, dict):
                title = title.get("title", "")
            print(f"      doc: {str(title or '')[:50]} | file: {a.get('library_items_file_name', '')[:30]}")


if __name__ == "__main__":
    main()
