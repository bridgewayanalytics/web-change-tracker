#!/usr/bin/env python3
"""
PDF Agenda Content Analysis

Downloads PDFs from Bubble resources that have topic suggestion or agenda item
associations, extracts text, and analyzes whether agenda structures, topic names,
and reference numbers are present in the PDF content.

Usage:
    # Requires BUBBLE_API_URL and BUBBLE_API_KEY in environment
    python analysis/pdf_agenda_detection/analyze_pdf_content.py

Outputs:
    analysis/pdf_agenda_detection/pdf_sample_dataset.json
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("analyze_pdf_content")

OUTPUT_DIR = Path(__file__).resolve().parent
DATASET_FILE = OUTPUT_DIR / "pdf_sample_dataset.json"

# Load previous analysis data
AGENDA_TOPIC_DIR = PROJECT_ROOT / "analysis" / "agenda_topic_mapping"
CHRONICLES_FILE = AGENDA_TOPIC_DIR / "chronicles_tree.json"
HISTORICAL_SAMPLES_FILE = AGENDA_TOPIC_DIR / "historical_samples.json"


# ---------------------------------------------------------------------------
# Bubble client
# ---------------------------------------------------------------------------

def get_client():
    from bubble.client import get_client as _get_client
    return _get_client(use_cache=True)


def take(gen, limit: int) -> list[dict]:
    out = []
    for i, item in enumerate(gen):
        if i >= limit:
            break
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# PDF text extraction (reuse existing infrastructure)
# ---------------------------------------------------------------------------

def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract text from PDF bytes using the project's existing extractors."""
    from scrape.pdf_meeting_meta import _extract_plain_text
    return _extract_plain_text(pdf_bytes)


def fetch_pdf_bytes(url: str, timeout: int = 20) -> bytes | None:
    """Download PDF from URL."""
    import requests
    try:
        r = requests.get(url.strip(), timeout=timeout, headers={
            "User-Agent": "Mozilla/5.0 (compatible; BridgewayAnalytics/1.0)"
        })
        r.raise_for_status()
        if len(r.content) < 100:
            return None
        return r.content
    except Exception as e:
        log.debug("PDF download failed for %s: %s", url[:80], e)
        return None


# ---------------------------------------------------------------------------
# Agenda detection patterns
# ---------------------------------------------------------------------------

# Numbered agenda item patterns
RE_NUMBERED_ITEM = re.compile(
    r"^\s*(\d{1,3})\s*[.)]\s+(.+)",
    re.MULTILINE,
)

# Roman numeral items
RE_ROMAN_ITEM = re.compile(
    r"^\s*((?:I{1,3}|IV|V|VI{0,3}|IX|X{0,3}|XI{0,3}|XII|XIII|XIV|XV))\s*[.)]\s+(.+)",
    re.MULTILINE,
)

# Lettered items
RE_LETTER_ITEM = re.compile(
    r"^\s*([A-Z])\s*[.)]\s+(.+)",
    re.MULTILINE,
)

# "AGENDA" header
RE_AGENDA_HEADER = re.compile(
    r"(?:^|\n)\s*(AGENDA|Agenda|Meeting\s+Agenda|MEETING\s+AGENDA)\s*(?:\n|$)",
    re.IGNORECASE,
)

# Reference number patterns (e.g., SAPWG#2024-04, VOSTF#2023-005, Ref#2024-01, #2024-XX)
RE_REF_NUMBER = re.compile(
    r"(?:(?:SAPWG|VOSTF|LATF|BWG|LRBCWG|RBC-IRE|E-Committee|CATF|EIOPA|IAIS)"
    r"[#\s]*(?:Ref\s*#?\s*)?(\d{4}[-–]\d{1,3}))"
    r"|(?:(?:Ref|Reference|Item)\s*#?\s*(\d{4}[-–]\d{1,3}))"
    r"|(?:#(\d{4}[-–]\d{1,3}))",
    re.IGNORECASE,
)

# SSAP reference patterns
RE_SSAP_REF = re.compile(
    r"SSAP\s+(?:No\.?\s*)?(\d+[A-Z]?)",
    re.IGNORECASE,
)

# Group/committee name in header
RE_GROUP_HEADER = re.compile(
    r"((?:[\w\s\-&',/()]+)\s*(?:TASK\s+FORCE|WORKING\s+GROUP|COMMITTEE|SUBGROUP|TEAM))",
    re.IGNORECASE,
)

# Common NAIC meeting structure markers
RE_ROLL_CALL = re.compile(r"\b(?:Roll\s+Call|Call\s+to\s+Order|Opening\s+Remarks)\b", re.IGNORECASE)
RE_ADJOURNMENT = re.compile(r"\b(?:Adjournment|Adjourn|Any\s+Other\s+Business)\b", re.IGNORECASE)
RE_DISCUSSION = re.compile(r"\b(?:Discussion|Consider|Consideration|Receive|Adopt|Hear|Review)\b", re.IGNORECASE)


def analyze_pdf_text(text: str, chronicle_names: list[str]) -> dict:
    """Analyze extracted PDF text for agenda structures and topic signals."""
    if not text or not text.strip():
        return {"has_text": False}

    lines = text.split("\n")
    first_page_text = "\n".join(lines[:80])  # Approximate first page
    full_lower = text.lower()

    # 1. Agenda header detection
    agenda_headers = RE_AGENDA_HEADER.findall(text)
    has_agenda_header = bool(agenda_headers)

    # 2. Numbered items
    numbered_items = RE_NUMBERED_ITEM.findall(text)
    # Filter: only items where number is sequential-ish (1-30)
    numbered_items = [(n, t.strip()) for n, t in numbered_items if 1 <= int(n) <= 30]

    # 3. Roman numeral items
    roman_items = RE_ROMAN_ITEM.findall(text)

    # 4. Lettered items
    letter_items = RE_LETTER_ITEM.findall(text)

    # 5. Reference numbers
    ref_matches = RE_REF_NUMBER.findall(text)
    ref_numbers = []
    for groups in ref_matches:
        for g in groups:
            if g:
                ref_numbers.append(g.strip())
    ref_numbers = list(set(ref_numbers))

    # 6. SSAP references
    ssap_refs = list(set(RE_SSAP_REF.findall(text)))

    # 7. Group name in header
    group_matches = RE_GROUP_HEADER.findall(first_page_text)
    group_in_header = [g.strip() for g in group_matches[:3]] if group_matches else []

    # 8. Meeting structure markers
    has_roll_call = bool(RE_ROLL_CALL.search(text))
    has_adjournment = bool(RE_ADJOURNMENT.search(text))
    has_discussion = bool(RE_DISCUSSION.search(text))

    # 9. Chronicle topic name detection
    matched_chronicles = []
    for name in chronicle_names:
        # Strip BBCode
        clean_name = re.sub(r"\[/?[^\]]*\]", "", name).strip()
        if not clean_name or len(clean_name) < 4:
            continue
        # Check if topic name (or substantial portion) appears in PDF
        if clean_name.lower() in full_lower:
            matched_chronicles.append(clean_name)
        else:
            # Try key phrase (first 3+ significant words)
            words = [w for w in clean_name.split() if len(w) > 2 and w.lower() not in
                     ("the", "and", "for", "with", "from", "into")]
            if len(words) >= 2:
                phrase = " ".join(words[:3]).lower()
                if phrase in full_lower:
                    matched_chronicles.append(clean_name)

    # 10. Determine agenda structure type
    structure_type = "none"
    if has_agenda_header and (numbered_items or roman_items):
        structure_type = "formal_agenda"
    elif numbered_items and has_discussion:
        structure_type = "numbered_list"
    elif has_roll_call and has_adjournment:
        structure_type = "meeting_minutes"
    elif numbered_items:
        structure_type = "numbered_list"
    elif roman_items or letter_items:
        structure_type = "outline"
    elif has_discussion or has_agenda_header:
        structure_type = "informal"

    # 11. Extract agenda items (best effort)
    extracted_items = []
    if numbered_items:
        for num, title in numbered_items[:20]:
            extracted_items.append({"number": int(num), "title": title[:200]})
    elif roman_items:
        for num, title in roman_items[:20]:
            extracted_items.append({"number": num, "title": title[:200]})

    return {
        "has_text": True,
        "text_length": len(text),
        "line_count": len(lines),
        "has_agenda_header": has_agenda_header,
        "agenda_header_text": agenda_headers[:2] if agenda_headers else [],
        "structure_type": structure_type,
        "numbered_item_count": len(numbered_items),
        "roman_item_count": len(roman_items),
        "letter_item_count": len(letter_items),
        "extracted_items": extracted_items[:15],
        "ref_numbers": ref_numbers[:10],
        "ssap_refs": ssap_refs[:10],
        "group_in_header": group_in_header,
        "has_roll_call": has_roll_call,
        "has_adjournment": has_adjournment,
        "has_discussion_keywords": has_discussion,
        "matched_chronicle_topics": matched_chronicles[:10],
        "first_page_preview": first_page_text[:500],
    }


# ---------------------------------------------------------------------------
# Data pulling
# ---------------------------------------------------------------------------

def pull_pdf_resources_with_enrichment(client, limit=100) -> list[dict]:
    """Pull resources with PDF URLs that also have topic suggestion or that we can cross-reference."""
    from bubble.lookups import TYPE_RESOURCE

    log.info("Fetching resources with topic suggestion (for PDF analysis)...")
    constraints = [{"key": "topic suggestion", "constraint_type": "is_not_empty"}]
    all_resources = take(client.list_all(TYPE_RESOURCE, constraints=constraints, page_size=100), 200)

    # Filter to PDFs
    pdf_resources = [r for r in all_resources if (r.get("URL") or "").lower().endswith(".pdf")]
    log.info("  -> %d PDF resources with topic suggestion (from %d total)", len(pdf_resources), len(all_resources))

    # Also pull resources referenced by known agenda items
    historical = json.loads(HISTORICAL_SAMPLES_FILE.read_text(encoding="utf-8")) if HISTORICAL_SAMPLES_FILE.exists() else {}
    agenda_resource_ids = set()
    for ai in historical.get("resolved_agenda_items", []):
        for rid in ai.get("Resources_ids", []):
            agenda_resource_ids.add(rid)

    log.info("Fetching %d resources referenced by agenda items...", len(agenda_resource_ids))
    agenda_resources = []
    for rid in agenda_resource_ids:
        try:
            r = client.get(TYPE_RESOURCE, rid)
            if (r.get("URL") or "").lower().endswith(".pdf"):
                agenda_resources.append(r)
        except Exception:
            pass
    log.info("  -> %d PDF resources from agenda items", len(agenda_resources))

    # Merge, deduplicate by _id
    seen_ids = set()
    merged = []
    for r in pdf_resources + agenda_resources:
        rid = r.get("_id")
        if rid and rid not in seen_ids:
            seen_ids.add(rid)
            merged.append(r)

    # Cap at limit
    merged = merged[:limit]
    log.info("Total unique PDF resources for analysis: %d", len(merged))
    return merged


def resolve_resource_context(resource: dict, client, node_cache: dict, agenda_items_by_resource: dict) -> dict:
    """Build context for a resource including resolved names."""
    rid = resource.get("_id", "")

    # Topic suggestion
    topic_id = resource.get("topic suggestion")
    if isinstance(topic_id, dict):
        topic_id = topic_id.get("_id") or topic_id.get("id")
    topic_name = None
    if topic_id:
        if topic_id not in node_cache:
            try:
                node = client.get("Tree node", topic_id)
                node_cache[topic_id] = (node.get("name") or node.get("Name") or "").strip()
            except Exception:
                node_cache[topic_id] = None
        topic_name = node_cache.get(topic_id)

    # Organization / NAIC group from parent
    parent = resource.get("parent")
    parent_name = None
    if isinstance(parent, str) and not parent.startswith("http"):
        if " › " in parent or ">" in parent:
            parent_name = parent
        elif parent not in node_cache:
            try:
                node = client.get("Tree node", parent)
                node_cache[parent] = (node.get("name") or node.get("Name") or "").strip()
            except Exception:
                node_cache[parent] = None
            parent_name = node_cache.get(parent)
        else:
            parent_name = node_cache.get(parent)

    # Related calendar items
    cal_ids = resource.get("Related calendar items") or []
    calendar_titles = []
    for cid in (cal_ids[:3] if isinstance(cal_ids, list) else []):
        if isinstance(cid, dict):
            cid = cid.get("_id") or cid.get("id")
        if cid:
            try:
                cal = client.get("Calendar item", cid)
                calendar_titles.append((cal.get("title") or "").strip())
            except Exception:
                pass

    # Agenda items that reference this resource
    linked_agenda_items = agenda_items_by_resource.get(rid, [])

    return {
        "_id": rid,
        "Name": (resource.get("Name") or "").strip(),
        "URL": (resource.get("URL") or "").strip(),
        "notes": (resource.get("notes") or "").strip(),
        "date": resource.get("date"),
        "parent": parent,
        "parent_name": parent_name,
        "topic_suggestion_id": topic_id,
        "topic_suggestion_name": topic_name,
        "calendar_titles": calendar_titles,
        "linked_agenda_items": linked_agenda_items,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    client = get_client()
    node_cache: dict = {}

    # Load chronicles for topic matching
    chronicle_names = []
    if CHRONICLES_FILE.exists():
        chronicles = json.loads(CHRONICLES_FILE.read_text(encoding="utf-8"))
        chronicle_names = [c["name"] for c in chronicles if c.get("name")]
        log.info("Loaded %d chronicle topic names", len(chronicle_names))

    # Build agenda items by resource ID (from previous analysis)
    agenda_items_by_resource: dict[str, list[dict]] = {}
    if HISTORICAL_SAMPLES_FILE.exists():
        historical = json.loads(HISTORICAL_SAMPLES_FILE.read_text(encoding="utf-8"))
        for ai in historical.get("resolved_agenda_items", []):
            for rid in ai.get("Resources_ids", []):
                agenda_items_by_resource.setdefault(rid, []).append({
                    "BA_title": ai.get("BA_title", ""),
                    "BA_ref": ai.get("BA_ref", ""),
                    "Ref": ai.get("Ref", ""),
                    "Topics_names": ai.get("Topics_names", []),
                })

    # Pull resources
    resources = pull_pdf_resources_with_enrichment(client)

    # Process each PDF
    results = []
    download_failures = 0
    for i, r in enumerate(resources):
        url = (r.get("URL") or "").strip()
        name = (r.get("Name") or "").strip()
        log.info("[%d/%d] Processing: %s", i + 1, len(resources), name[:60])

        ctx = resolve_resource_context(r, client, node_cache, agenda_items_by_resource)

        # Download PDF
        pdf_bytes = fetch_pdf_bytes(url)
        if not pdf_bytes:
            download_failures += 1
            ctx["pdf_analysis"] = {"download_failed": True}
            results.append(ctx)
            continue

        # Extract text
        text = extract_pdf_text(pdf_bytes)
        if not text or not text.strip():
            ctx["pdf_analysis"] = {"has_text": False, "download_failed": False}
            results.append(ctx)
            continue

        # Analyze
        analysis = analyze_pdf_text(text, chronicle_names)
        ctx["pdf_analysis"] = analysis
        results.append(ctx)

        # Rate limit
        if (i + 1) % 10 == 0:
            time.sleep(0.5)

    # Write results
    dataset = {
        "metadata": {
            "total_resources": len(resources),
            "download_failures": download_failures,
            "with_text": sum(1 for r in results if r.get("pdf_analysis", {}).get("has_text")),
            "chronicle_topics_loaded": len(chronicle_names),
        },
        "results": results,
    }
    DATASET_FILE.write_text(json.dumps(dataset, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Wrote %d results to %s", len(results), DATASET_FILE)

    # Print summary
    print("\n" + "=" * 70)
    print("PDF CONTENT ANALYSIS SUMMARY")
    print("=" * 70)

    with_text = [r for r in results if r.get("pdf_analysis", {}).get("has_text")]
    print(f"Total resources:          {len(resources)}")
    print(f"Download failures:        {download_failures}")
    print(f"With extractable text:    {len(with_text)}")
    print()

    # Structure type distribution
    from collections import Counter
    structure_dist = Counter(r["pdf_analysis"]["structure_type"] for r in with_text)
    print("Agenda structure types:")
    for st, count in structure_dist.most_common():
        pct = count / len(with_text) * 100 if with_text else 0
        print(f"  {st:25s} {count:4d}  ({pct:.1f}%)")
    print()

    # Key signals
    has_agenda = sum(1 for r in with_text if r["pdf_analysis"]["has_agenda_header"])
    has_numbered = sum(1 for r in with_text if r["pdf_analysis"]["numbered_item_count"] > 0)
    has_roman = sum(1 for r in with_text if r["pdf_analysis"]["roman_item_count"] > 0)
    has_refs = sum(1 for r in with_text if r["pdf_analysis"]["ref_numbers"])
    has_ssap = sum(1 for r in with_text if r["pdf_analysis"]["ssap_refs"])
    has_group = sum(1 for r in with_text if r["pdf_analysis"]["group_in_header"])
    has_chronicle = sum(1 for r in with_text if r["pdf_analysis"]["matched_chronicle_topics"])
    has_roll_call = sum(1 for r in with_text if r["pdf_analysis"]["has_roll_call"])
    has_discussion = sum(1 for r in with_text if r["pdf_analysis"]["has_discussion_keywords"])

    n = len(with_text)
    print(f"Signal presence (of {n} PDFs with text):")
    print(f"  Agenda header:           {has_agenda:4d}  ({has_agenda/n*100:.1f}%)" if n else "")
    print(f"  Numbered items:          {has_numbered:4d}  ({has_numbered/n*100:.1f}%)" if n else "")
    print(f"  Roman numeral items:     {has_roman:4d}  ({has_roman/n*100:.1f}%)" if n else "")
    print(f"  Reference numbers:       {has_refs:4d}  ({has_refs/n*100:.1f}%)" if n else "")
    print(f"  SSAP references:         {has_ssap:4d}  ({has_ssap/n*100:.1f}%)" if n else "")
    print(f"  Group name in header:    {has_group:4d}  ({has_group/n*100:.1f}%)" if n else "")
    print(f"  Chronicle topic match:   {has_chronicle:4d}  ({has_chronicle/n*100:.1f}%)" if n else "")
    print(f"  Roll call / opening:     {has_roll_call:4d}  ({has_roll_call/n*100:.1f}%)" if n else "")
    print(f"  Discussion keywords:     {has_discussion:4d}  ({has_discussion/n*100:.1f}%)" if n else "")
    print()

    # Chronicle topic match details
    if has_chronicle:
        all_matched = []
        for r in with_text:
            all_matched.extend(r["pdf_analysis"]["matched_chronicle_topics"])
        topic_counts = Counter(all_matched)
        print(f"Most frequently matched Chronicle topics in PDFs:")
        for topic, count in topic_counts.most_common(15):
            print(f"  {topic[:60]:60s} {count:3d}")
    print()

    # Compare with Bubble topic suggestion
    topic_match_count = 0
    topic_total = 0
    for r in with_text:
        topic_name = r.get("topic_suggestion_name")
        if topic_name:
            topic_total += 1
            clean_topic = re.sub(r"\[/?[^\]]*\]", "", topic_name).strip()
            if clean_topic.lower() in [t.lower() for t in r["pdf_analysis"]["matched_chronicle_topics"]]:
                topic_match_count += 1

    print(f"Assigned Bubble topic found in PDF text: {topic_match_count}/{topic_total}")
    if topic_total:
        print(f"  ({topic_match_count/topic_total*100:.1f}% of resources with topic suggestion)")
    print()

    # Agenda item match (for resources linked to agenda items)
    ai_match_title = 0
    ai_match_ref = 0
    ai_total = 0
    for r in with_text:
        for ai in r.get("linked_agenda_items", []):
            ai_total += 1
            ba_title = ai.get("BA_title", "").lower()
            ba_ref = ai.get("BA_ref", "") or ai.get("Ref", "")
            pdf_lower = r["pdf_analysis"].get("first_page_preview", "").lower()
            full_text_lower = ""  # We don't store full text, check first page

            # Title match: check if significant words appear
            title_words = set(ba_title.split()) - {"the", "of", "and", "&", "a", "an", "in", "on", "for", "with", "-", "–"}
            if len(title_words) >= 2:
                overlap = sum(1 for w in title_words if w in pdf_lower)
                if overlap >= min(3, len(title_words)):
                    ai_match_title += 1

            # Ref match
            if ba_ref and ba_ref.lower() in pdf_lower:
                ai_match_ref += 1

    print(f"Agenda item signals in PDF (of {ai_total} resource-agenda item pairs):")
    print(f"  BA title keywords in PDF:  {ai_match_title}")
    print(f"  BA Ref # in PDF:           {ai_match_ref}")
    print()

    print("Files written:")
    print(f"  {DATASET_FILE}")


if __name__ == "__main__":
    main()
