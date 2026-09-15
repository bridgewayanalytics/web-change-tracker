"""
Purge hollow document extraction rows from document_extractions_table.jsonl.

A "hollow" row is one where no actual document analysis could have occurred
because the pipeline had no readable document content — either the URL was
empty/N/A, or it pointed to a web page / landing page rather than a document.

Safe to purge:
  - Empty or N/A library_item_url
  - Landing pages and article pages (e.g. /resource-center, /article/, /news/)

Not purged (kept):
  - PDF rows       — substantive analysis
  - Transcript rows — different analysis path (transcript_s3_key)
  - Office docs    — .docx/.xlsx etc.; real docs even if pipeline can't read them
  - JIR/research pages — real publications; --include-web-pages flag purges these too

Usage:
  python3 scripts/purge_hollow_extractions.py --dry-run
  python3 scripts/purge_hollow_extractions.py
  python3 scripts/purge_hollow_extractions.py --include-web-pages  # also purge JIR/research pages
"""

import argparse
import json
import logging
import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

_BUCKET = "web-change-tracker-prod-artifacts-815039343351"
_EXTRACTIONS_KEY = "alerts/document_extractions_table.jsonl"
_DOC_EVAL_KEY = "alerts/doc_eval_results_table.jsonl"

_NA = {"n/a", "n/a.", "-", ""}

# URL path patterns that are always web pages, never documents
_WEB_PAGE_PATTERNS = (
    "/resource-center",
    "/article/",
    "/news/",
)

# URL path patterns that are research/journal pages — hollow but arguably real documents
_RESEARCH_PAGE_PATTERNS = (
    "/research/jir/",
    "/research/resilience",
    "/membership-report/",
    "webex.com",
)


def _s3():
    import boto3
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _load(client, key):
    try:
        body = client.get_object(Bucket=_BUCKET, Key=key)["Body"].read().decode("utf-8")
    except client.exceptions.NoSuchKey:
        return []
    rows = []
    for line in body.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return rows


def _put(client, key, rows):
    body = "\n".join(json.dumps(r, default=str) for r in rows)
    client.put_object(
        Bucket=_BUCKET,
        Key=key,
        Body=body.encode("utf-8"),
        ContentType="application/x-ndjson",
    )


def classify(row, include_web_pages: bool = False) -> str:
    """
    Return 'purge' or 'keep', with a reason string.
    """
    src = row.get("extraction_source", "")
    if src == "transcript":
        return "keep"

    url = str(row.get("library_item_url") or "").strip()

    # Empty / N/A
    if not url or url.lower() in _NA:
        return "purge:empty_url"

    path = url.lower().split("?")[0]

    # Office / spreadsheet documents — real files, keep
    for ext in (".docx", ".doc", ".xlsx", ".xls", ".pptx", ".csv", ".zip"):
        if path.split(";")[0].strip().endswith(ext):
            return "keep"

    # PDF (may be semicolon-separated; check each segment)
    for segment in path.split(";"):
        if segment.strip().endswith(".pdf"):
            return "keep"

    # Definite web pages
    for pat in _WEB_PAGE_PATTERNS:
        if pat in url:
            return "purge:web_page"

    # Research / JIR pages
    if include_web_pages:
        for pat in _RESEARCH_PAGE_PATTERNS:
            if pat in url:
                return "purge:research_page"

    return "keep"


def run(dry_run: bool = True, include_web_pages: bool = False) -> None:
    client = _s3()

    log.info("Loading %s ...", _EXTRACTIONS_KEY)
    rows = _load(client, _EXTRACTIONS_KEY)
    log.info("Loaded %d rows", len(rows))

    keep_rows = []
    purge_rows = []
    reason_counts: dict[str, int] = {}

    for row in rows:
        verdict = classify(row, include_web_pages)
        if verdict.startswith("purge"):
            purge_rows.append(row)
            reason_counts[verdict] = reason_counts.get(verdict, 0) + 1
        else:
            keep_rows.append(row)

    log.info("Would keep: %d rows", len(keep_rows))
    log.info("Would purge: %d rows", len(purge_rows))
    for reason, count in sorted(reason_counts.items()):
        log.info("  %s: %d", reason, count)

    # Show purge candidates grouped by url
    from collections import Counter
    purge_urls = Counter(r.get("library_item_url", "") for r in purge_rows)
    print("\nPurge candidates by URL:")
    for url, count in purge_urls.most_common():
        print(f"  [{count:3d} rows]  {url[:100]}")

    if dry_run:
        log.info("\nDry run — no changes made. Re-run without --dry-run to apply.")
        return

    # Also remove orphaned eval results for purged rows
    purged_agent_call_ids = {r.get("agent_call_id") for r in purge_rows if r.get("agent_call_id")}
    purged_doc_ids = {r.get("doc_extraction_id") for r in purge_rows if r.get("doc_extraction_id")}
    purged_lib_urls = {r.get("library_item_url") for r in purge_rows if r.get("library_item_url")}

    log.info("Loading %s ...", _DOC_EVAL_KEY)
    eval_rows = _load(client, _DOC_EVAL_KEY)
    keep_eval = []
    purge_eval = []
    for ev in eval_rows:
        ev_cid = ev.get("agent_call_id")
        ev_did = ev.get("doc_extraction_id")
        ev_url = ev.get("library_item_url")
        if (ev_cid and ev_cid in purged_agent_call_ids and ev_url in purged_lib_urls) or \
           (ev_did and ev_did in purged_doc_ids):
            purge_eval.append(ev)
        else:
            keep_eval.append(ev)

    log.info("Writing %d extraction rows (removing %d) ...", len(keep_rows), len(purge_rows))
    _put(client, _EXTRACTIONS_KEY, keep_rows)

    if purge_eval:
        log.info("Writing %d eval rows (removing %d orphaned) ...", len(keep_eval), len(purge_eval))
        _put(client, _DOC_EVAL_KEY, keep_eval)
    else:
        log.info("No orphaned eval results to remove.")

    log.info("Done.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", default=False)
    parser.add_argument("--include-web-pages", action="store_true", default=False,
                        help="Also purge JIR/research article web pages")
    args = parser.parse_args()

    try:
        from bubble.ssm_loader import load_openai_env_from_ssm, load_db_env_from_ssm
        load_openai_env_from_ssm()
        load_db_env_from_ssm()
    except Exception:
        pass

    run(dry_run=args.dry_run, include_web_pages=args.include_web_pages)


if __name__ == "__main__":
    main()
