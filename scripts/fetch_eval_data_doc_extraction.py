"""
Fetch targeted evaluation data for document extraction agent accuracy testing.

Samples rows from document_extractions_table.jsonl where newsreel_relevance is
a clean Yes/No string (skipping old dict-format rows and N/A rows).

Splits 50/50 between Yes and No rows to test both directions.
Fetches the live document page for each row so accuracy can be assessed.

Usage
-----
    AWS_PROFILE=bridgeway python3 scripts/fetch_eval_data_doc_extraction.py
    AWS_PROFILE=bridgeway python3 scripts/fetch_eval_data_doc_extraction.py --n 10
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
DOC_EXTRACTIONS_KEY = "alerts/document_extractions_table.jsonl"
ALERTS_KEY = "alerts/alerts_table.jsonl"

FIELDS_TO_INCLUDE = [
    "agent_call_id", "run_id", "target_id", "data_extraction_datetime",
    "document_title", "document_type", "document_description",
    "document_url_web_tracking_agent", "library_item_url", "library_item_title",
    "library_item_file_name", "organization_or_publisher",
    "newsreel_relevance",
    "agenda_items",
    "existing_updated_or_new_document",
    "date_published", "meeting_or_last_comment_date",
    "number",
    "ingest_status", "extraction_source",
]


def s3_client():
    import boto3
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def load_doc_rows() -> list[dict]:
    body = s3_client().get_object(Bucket=BUCKET, Key=DOC_EXTRACTIONS_KEY)["Body"].read().decode("utf-8")
    rows = []
    for line in body.splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def load_alert_index(rows: list[dict]) -> dict[str, dict]:
    """Build agent_call_id → alert row lookup for cross-referencing."""
    body = s3_client().get_object(Bucket=BUCKET, Key=ALERTS_KEY)["Body"].read().decode("utf-8")
    idx: dict[str, dict] = {}
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            cid = row.get("agent_call_id")
            if cid:
                idx[cid] = row
        except Exception:
            pass
    return idx


def _clean_newsreel(val) -> str | None:
    """
    Return 'Yes' / 'No' if val is a clean string status, else None.
    Old dict format {'explanation_or_reference': '...'} → None.
    """
    if isinstance(val, dict):
        # New format: {'status': 'Yes'/'No', 'details': '...'}
        s = str(val.get("status") or "").strip()
        if s in ("Yes", "No"):
            return s
        return None
    if isinstance(val, str):
        s = val.strip()
        if s in ("Yes", "No"):
            return s
        return None
    return None


def _get_doc_url(row: dict) -> str:
    """Best document URL to fetch."""
    for key in ("library_item_url", "document_url_web_tracking_agent", "source_url"):
        v = str(row.get(key) or "").strip()
        if v and v not in ("N/A", ""):
            return v
    return ""


def sample_doc_extraction(rows: list[dict], n: int = 10) -> list[dict]:
    """
    Split ~50/50 between Yes and No newsreel_relevance rows.
    New-schema only (skip transcript rows — extraction_source == 'transcript').
    Sort by most recent first, then take evenly.
    """
    eligible = [
        r for r in rows
        if _clean_newsreel(r.get("newsreel_relevance")) is not None
        and str(r.get("extraction_source") or "").strip() != "transcript"
        and _get_doc_url(r)
    ]
    eligible.sort(key=lambda r: str(r.get("data_extraction_datetime") or ""), reverse=True)

    yes_rows = [r for r in eligible if _clean_newsreel(r.get("newsreel_relevance")) == "Yes"]
    no_rows  = [r for r in eligible if _clean_newsreel(r.get("newsreel_relevance")) == "No"]

    log.info("newsreel_relevance pool: %d Yes, %d No", len(yes_rows), len(no_rows))

    half = n // 2
    # Pull extra candidates to survive URL deduplication
    sample = yes_rows[:half * 3] + no_rows[:(n - half) * 3]

    # Deduplicate by URL first (same doc extracted in multiple runs), then by agent_call_id
    seen_urls: set[str] = set()
    seen_ids: set[str] = set()
    deduped = []
    for r in sample:
        url = _get_doc_url(r)
        cid = str(r.get("agent_call_id") or id(r))
        if url and url in seen_urls:
            continue
        if cid in seen_ids:
            continue
        seen_urls.add(url)
        seen_ids.add(cid)
        deduped.append(r)

    random.shuffle(deduped)
    return deduped[:n]


def fetch_page(url: str, max_chars: int = 8000) -> str:
    if not url or url == "N/A":
        return ""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; BridgewayBot/1.0)"}
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        # If it's a PDF, just return a note (can't parse here)
        ct = resp.headers.get("Content-Type", "")
        if "pdf" in ct.lower():
            return "[PDF — binary content, not scraped]"
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()
        text = " ".join(soup.get_text(" ", strip=True).split())
        return text[:max_chars]
    except Exception as e:
        log.warning("  Could not fetch %s: %s", url, e)
        return ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=10, help="Number of rows to sample")
    args = parser.parse_args()

    output_path = Path("analysis/accuracy_eval/eval_data_doc_extraction.json")

    log.info("Loading document extractions from S3...")
    rows = load_doc_rows()
    log.info("Total doc extraction rows: %d", len(rows))

    sample = sample_doc_extraction(rows, n=args.n)
    log.info("Sampled %d rows", len(sample))

    records = []
    for i, row in enumerate(sample, 1):
        doc_url = _get_doc_url(row)
        nr = row.get("newsreel_relevance")
        nr_clean = _clean_newsreel(nr)

        log.info("[%d/%d] %s", i, len(sample), str(row.get("document_title") or row.get("library_item_title") or "")[:70])
        log.info("  newsreel_relevance: %s | url: %s", nr_clean, doc_url[:80])

        page_content = fetch_page(doc_url)

        records.append({
            "index": i,
            "extraction": {f: row.get(f) for f in FIELDS_TO_INCLUDE},
            "newsreel_status": nr_clean,
            "newsreel_details": nr.get("details") if isinstance(nr, dict) else None,
            "doc_url": doc_url,
            "doc_page_content": page_content,
            "doc_page_length": len(page_content),
        })

        log.info("  Page: %d chars", len(page_content))
        time.sleep(0.5)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)

    log.info("Saved %d records → %s", len(records), output_path)
    print(f"\nData ready at: {output_path}")

    yes_count = sum(1 for r in records if r["newsreel_status"] == "Yes")
    no_count  = sum(1 for r in records if r["newsreel_status"] == "No")
    print(f"Split: {yes_count} Yes / {no_count} No")

    print(f"\nSample preview:")
    for rec in records:
        e = rec["extraction"]
        title = str(e.get("document_title") or e.get("library_item_title") or "")[:60]
        doc_type = str(e.get("document_type") or "")
        print(f"  {rec['index']:2d}. [{rec['newsreel_status']}] [{doc_type}] {title}")
        if rec["newsreel_details"]:
            print(f"      details: {rec['newsreel_details'][:80]}")


if __name__ == "__main__":
    main()
