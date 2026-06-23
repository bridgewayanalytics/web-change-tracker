"""
Data accuracy evaluation — LLM-as-judge against live NAIC pages.

For each sampled alert row:
  1. Fetches the alert_url live page (requests + BeautifulSoup)
  2. Sends page content + agent output to Claude claude-opus-4-7 for field-by-field scoring
  3. Produces a stakeholder-facing markdown report + raw JSON results

Uses Claude as judge (different model family from the OpenAI agents that generated alerts,
avoiding same-model bias in evaluation).

Scoring rubric (per field):
  2 = Correct        — accurate and complete
  1 = Partial        — mostly right, minor error or omission
  0 = Incorrect      — wrong, missing, or hallucinated
  N = Not applicable — field not expected for this alert type

Usage:
    AWS_PROFILE=bridgeway ANTHROPIC_API_KEY=sk-ant-... python scripts/accuracy_eval.py [--n 20] [--dry-run]
"""

import argparse
import json
import logging
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

BUCKET = os.environ.get("CHANGELOG_BUCKET") or "web-change-tracker-prod-artifacts-815039343351"
ALERTS_KEY = "alerts/alerts_table.jsonl"
OUTPUT_DIR = Path("analysis/accuracy_eval")

FIELDS_TO_EVALUATE = [
    "alert_type",
    "alert_title",
    "alert_description",
    "organization",
    "event_title",
    "event_start_date_time",
    "event_end_date_time",
    "event_url",
    "event_call_in_number_access_code",
    "library_item_preliminary_title",
    "library_item_url",
    "library_items_file_name",
    "agenda_item_title_chronicle_topics",
    "is_the_alert_relevant_for_an_art_newsreel_article",
]

FIELD_LABELS = {
    "alert_type":                                   "Alert Type",
    "alert_title":                                  "Alert Title",
    "alert_description":                            "Alert Description",
    "organization":                                 "Organization",
    "event_title":                                  "Event Title",
    "event_start_date_time":                        "Event Start Date/Time",
    "event_end_date_time":                          "Event End Date/Time",
    "event_url":                                    "Event URL",
    "event_call_in_number_access_code":             "Call-In Number",
    "library_item_preliminary_title":               "Document Title",
    "library_item_url":                             "Document URL",
    "library_items_file_name":                      "Document Filename",
    "agenda_item_title_chronicle_topics":           "Agenda Items & Topics",
    "is_the_alert_relevant_for_an_art_newsreel_article": "Newsreel Relevance",
}

EVALUATOR_PROMPT = """You are an expert data quality auditor evaluating AI-generated alerts about changes to NAIC (National Association of Insurance Commissioners) web pages.

You will be given:
1. LIVE PAGE CONTENT: the current content of the NAIC page that was monitored (fetched today)
2. AGENT OUTPUT: the structured alert the AI produced when it detected a change on this page

Important context: the agent ran weeks or months ago. The live page may have changed since then. Where a field references historical content (e.g. a past meeting date), evaluate whether it is plausible and internally consistent. Where the live page still shows the same content, check for exact accuracy.

SCORING RUBRIC:
- 2 = Correct: accurate and complete
- 1 = Partial: mostly right but minor error, omission, or imprecision
- 0 = Incorrect: wrong, missing when it should be present, or hallucinated (not plausible from this page)
- "N/A" = Not applicable: this field is genuinely not expected for this alert type

FIELDS TO EVALUATE:
- alert_type: Is the classification correct? Valid: New Meeting, Updated Meeting, New Agenda, New Materials, New Agenda & Materials, Updated Agenda, Updated Materials, Updated Agenda & Materials, New Request for Comment, Updated Request for Comment, New Effective Date, Updated Effective Date, New or Updated Report or Other Resource, No Meaningful Change, Other, Alert not relevant - the change was limited to carrousel or reordering of content
- alert_title: Does it accurately and concisely describe what changed?
- alert_description: Is it an accurate summary? Any hallucinations (claims not supported by the page)?
- organization: Is the correct NAIC org identified? Check the page header/title.
- event_title: If a meeting is involved, is the title correct?
- event_start_date_time: Is the date/time accurate?
- event_end_date_time: Is the end date/time accurate?
- event_url: Does the URL point to the correct meeting/event page?
- event_call_in_number_access_code: Is call-in info correct or correctly marked N/A?
- library_item_preliminary_title: Is the document title accurate?
- library_item_url: Does the URL point to the document described?
- library_items_file_name: Is the filename plausible for the document described?
- agenda_item_title_chronicle_topics: Are agenda items real? Are chronicle topics (insurance regulatory taxonomy) appropriate?
- is_the_alert_relevant_for_an_art_newsreel_article: Should this content be used to generate an insurance regulatory newsreel article? Yes = substantive regulatory change/document, No = administrative/housekeeping only, Additional review needed = borderline

Return ONLY a JSON object with this exact structure:
{
  "scores": {
    "alert_type": <2|1|0|"N/A">,
    "alert_title": <2|1|0|"N/A">,
    "alert_description": <2|1|0|"N/A">,
    "organization": <2|1|0|"N/A">,
    "event_title": <2|1|0|"N/A">,
    "event_start_date_time": <2|1|0|"N/A">,
    "event_end_date_time": <2|1|0|"N/A">,
    "event_url": <2|1|0|"N/A">,
    "event_call_in_number_access_code": <2|1|0|"N/A">,
    "library_item_preliminary_title": <2|1|0|"N/A">,
    "library_item_url": <2|1|0|"N/A">,
    "library_items_file_name": <2|1|0|"N/A">,
    "agenda_item_title_chronicle_topics": <2|1|0|"N/A">,
    "is_the_alert_relevant_for_an_art_newsreel_article": <2|1|0|"N/A">
  },
  "issues": ["<concise description of each error found>"],
  "hallucinations": ["<each claim in agent output not supported by the page>"],
  "page_changed_since_alert": true|false,
  "overall_notes": "<1-2 sentence summary of this row's quality>"
}"""


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
    """Sample rows stratified by alert_type, excluding No Meaningful Change / carousel."""
    skip = {"No Meaningful Change", "Alert not relevant - the change was limited to carrousel or reordering of content"}
    eligible = [r for r in rows if r.get("alert_url") and str(r.get("alert_type", "")) not in skip]

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


def fetch_page(url: str, timeout: int = 15) -> str:
    """Fetch and strip a URL to plain text."""
    if not url or url == "N/A":
        return ""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; BridgewayBot/1.0)"}
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        return " ".join(soup.get_text(" ", strip=True).split())
    except Exception as e:
        log.warning("  Could not fetch %s: %s", url, e)
        return ""


def truncate(text: str, limit: int = 10000) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + "\n\n[... truncated ...]\n\n" + text[-half:]


def evaluate_row(row: dict, page_text: str, client) -> dict:
    agent_output = {f: row.get(f) for f in FIELDS_TO_EVALUATE}

    user_msg = f"""LIVE PAGE CONTENT (from {row.get('alert_url', 'unknown URL')}):
{truncate(page_text or '(could not fetch page)', 10000)}

---

AGENT OUTPUT:
{json.dumps(agent_output, indent=2, ensure_ascii=False)}"""

    resp = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=2048,
        system=EVALUATOR_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    # Extract JSON from response
    content = resp.content[0].text.strip()
    # Strip markdown fences if present
    if content.startswith("```"):
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
    return json.loads(content.strip())


def score_pct(scores: list) -> float | None:
    numeric = [s for s in scores if isinstance(s, (int, float))]
    if not numeric:
        return None
    return round(sum(numeric) / (len(numeric) * 2) * 100, 1)


def grade(pct: float | None) -> str:
    if pct is None:
        return "—"
    if pct >= 90:
        return "✅"
    if pct >= 70:
        return "⚠️"
    return "❌"


def generate_report(results: list[dict], sample: list[dict]) -> str:
    lines = []

    lines.append("# Alert Data Accuracy Evaluation")
    lines.append(f"\n**Date:** {datetime.now().strftime('%B %d, %Y')}  ")
    lines.append(f"**Rows evaluated:** {len(results)}  ")
    lines.append(f"**Method:** Claude Opus 4 evaluating agent output against live NAIC page content  ")
    lines.append(f"**Alert types covered:** {len({r.get('alert_type') for r in sample[:len(results)]})}\n")

    lines.append("---\n")

    # Overall score
    all_scores_flat = []
    for r in results:
        for s in r["eval"]["scores"].values():
            if isinstance(s, (int, float)):
                all_scores_flat.append(s)
    overall = score_pct(all_scores_flat)
    lines.append(f"## Overall Score: {overall}%\n")
    lines.append(f"> {grade(overall)} Based on {len(all_scores_flat)} field evaluations across {len(results)} alerts.\n")

    # Field accuracy table
    lines.append("## Accuracy by Field\n")
    lines.append("| Field | Score | Correct | Partial | Incorrect | N/A |")
    lines.append("|-------|-------|---------|---------|-----------|-----|")

    field_scores: dict[str, list] = defaultdict(list)
    for r in results:
        for field, score in r["eval"]["scores"].items():
            field_scores[field].append(score)

    for field in FIELDS_TO_EVALUATE:
        scores = field_scores[field]
        pct = score_pct(scores)
        correct   = scores.count(2)
        partial   = scores.count(1)
        incorrect = scores.count(0)
        na        = sum(1 for s in scores if s == "N/A")
        label     = FIELD_LABELS.get(field, field)
        g         = grade(pct)
        pct_str   = f"{pct}%" if pct is not None else "—"
        lines.append(f"| {label} | {g} {pct_str} | {correct} | {partial} | {incorrect} | {na} |")

    lines.append("")

    # Alert type breakdown
    lines.append("## Score by Alert Type\n")
    type_counts: Counter = Counter()
    type_scores: dict[str, list] = defaultdict(list)
    for r, row in zip(results, sample):
        at = str(row.get("alert_type") or "Unknown")
        type_counts[at] += 1
        for s in r["eval"]["scores"].values():
            if isinstance(s, (int, float)):
                type_scores[at].append(s)

    lines.append("| Alert Type | Count | Score |")
    lines.append("|-----------|-------|-------|")
    for at, count in type_counts.most_common():
        pct = score_pct(type_scores[at])
        g = grade(pct)
        pct_str = f"{pct}%" if pct is not None else "—"
        lines.append(f"| {at} | {count} | {g} {pct_str} |")
    lines.append("")

    # Hallucinations
    hallucinations = []
    for r, row in zip(results, sample):
        for h in r["eval"].get("hallucinations") or []:
            hallucinations.append((str(row.get("alert_type") or "?"), str(row.get("alert_title") or "?")[:60], h))

    if hallucinations:
        lines.append(f"## Hallucinations ({len(hallucinations)})\n")
        lines.append("*Claims in agent output not supported by the source page.*\n")
        for at, title, h in hallucinations:
            lines.append(f"- **[{at}]** _{title}_  \n  → {h}")
        lines.append("")

    # Issues
    all_issues = []
    for r, row in zip(results, sample):
        for issue in r["eval"].get("issues") or []:
            all_issues.append((str(row.get("alert_type") or "?"), str(row.get("alert_title") or "?")[:60], issue))

    if all_issues:
        lines.append(f"## Issues Found ({len(all_issues)})\n")
        for at, title, issue in all_issues:
            lines.append(f"- **[{at}]** _{title}_  \n  → {issue}")
        lines.append("")

    # Page-changed count
    changed = sum(1 for r in results if r["eval"].get("page_changed_since_alert"))
    lines.append(f"## Notes\n")
    lines.append(f"- {changed} of {len(results)} pages appeared to have changed since the alert was generated (evaluator flagged uncertainty on those rows).\n")

    # Row-by-row
    lines.append("---\n")
    lines.append("## Row-by-Row Detail\n")
    for i, (r, row) in enumerate(zip(results, sample), 1):
        at    = str(row.get("alert_type") or "Unknown")
        title = str(row.get("alert_title") or "—")[:90]
        org   = str(row.get("organization") or "—")
        if isinstance(org, list):
            org = ", ".join(org)
        aid   = str(row.get("agent_call_id") or "")[-8:]
        url   = str(row.get("alert_url") or "")
        row_scores = [s for s in r["eval"]["scores"].values() if isinstance(s, (int, float))]
        row_pct = score_pct(row_scores)
        g = grade(row_pct)

        lines.append(f"### {i}. {g} {title}")
        lines.append(f"**Type:** {at}  |  **Org:** {org}  |  **ID:** `{aid}`  |  **Score:** {row_pct}%")
        if url:
            lines.append(f"**URL:** {url}\n")

        # Per-field scores on one line
        score_parts = []
        for field in FIELDS_TO_EVALUATE:
            s = r["eval"]["scores"].get(field)
            if s == "N/A":
                continue
            icon = {2: "✅", 1: "⚠️", 0: "❌"}.get(s, "?")
            score_parts.append(f"{icon} {FIELD_LABELS[field]}")
        lines.append("  ".join(score_parts) + "\n")

        notes = r["eval"].get("overall_notes", "")
        if notes:
            lines.append(f"_{notes}_\n")

        row_issues = r["eval"].get("issues") or []
        if row_issues:
            for issue in row_issues:
                lines.append(f"- {issue}")
            lines.append("")

    lines.append("---")
    lines.append(f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}_")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=20)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    import anthropic
    oai = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    log.info("Loading alerts from S3...")
    rows = load_rows()
    log.info("Total rows: %d", len(rows))

    sample = stratified_sample(rows, args.n)
    log.info("Sampled %d rows across %d alert types", len(sample),
             len({r.get("alert_type") for r in sample}))

    if args.dry_run:
        for i, r in enumerate(sample, 1):
            print(f"{i:2}. [{r.get('alert_type')}] {str(r.get('alert_title') or '')[:70]}")
            print(f"     URL: {r.get('alert_url', '')}")
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    results = []
    for i, row in enumerate(sample, 1):
        title = str(row.get("alert_title") or "")[:60]
        url   = str(row.get("alert_url") or "")
        log.info("[%d/%d] Fetching page: %s", i, len(sample), url)
        page_text = fetch_page(url)
        log.info("  Page length: %d chars", len(page_text))

        log.info("[%d/%d] Evaluating: %s", i, len(sample), title)
        try:
            eval_result = evaluate_row(row, page_text, oai)
            results.append({
                "row_index": i,
                "agent_call_id": row.get("agent_call_id"),
                "alert_type": row.get("alert_type"),
                "alert_title": row.get("alert_title"),
                "alert_url": url,
                "eval": eval_result,
            })
            log.info("  Issues: %d  Hallucinations: %d  Page changed: %s",
                     len(eval_result.get("issues") or []),
                     len(eval_result.get("hallucinations") or []),
                     eval_result.get("page_changed_since_alert"))
        except Exception as e:
            log.error("  Evaluation failed: %s", e)
            results.append({
                "row_index": i,
                "agent_call_id": row.get("agent_call_id"),
                "eval": {"scores": {f: "N/A" for f in FIELDS_TO_EVALUATE},
                         "issues": [str(e)], "hallucinations": [],
                         "page_changed_since_alert": False,
                         "overall_notes": "Evaluation failed"},
            })

        time.sleep(0.5)

    ts = datetime.now().strftime("%Y%m%d_%H%M")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    raw_path = OUTPUT_DIR / f"results_{ts}.json"
    with open(raw_path, "w") as f:
        json.dump({"sample": sample, "results": results}, f, indent=2, ensure_ascii=False)
    log.info("Raw results → %s", raw_path)

    report = generate_report(results, sample)
    report_path = OUTPUT_DIR / f"report_{ts}.md"
    with open(report_path, "w") as f:
        f.write(report)
    log.info("Report → %s", report_path)

    print("\n" + "=" * 60 + "\n")
    print(report)


if __name__ == "__main__":
    main()
