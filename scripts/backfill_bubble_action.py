"""
Backfill bubble_action on existing alerts_table.jsonl rows.

Runs classify_alert() on every row that lacks a bubble_action field and stamps
the result on applicable rows. Idempotent — rows with bubble_action already set
are skipped unless --force is passed.

Usage:
    AWS_PROFILE=bridgeway python scripts/backfill_bubble_action.py [--dry-run] [--force]
"""

import argparse
import json
import logging
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

_DEFAULT_BUCKET = "web-change-tracker-prod-artifacts-815039343351"
BUCKET = os.environ.get("CHANGELOG_BUCKET") or os.environ.get("BUBBLE_ARTIFACT_BUCKET") or _DEFAULT_BUCKET
ALERTS_KEY = "alerts/alerts_table.jsonl"
DOC_EXTRACTIONS_KEY = "alerts/document_extractions_table.jsonl"


def s3_client():
    import boto3
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def load_rows(client) -> list[dict]:
    body = client.get_object(Bucket=BUCKET, Key=ALERTS_KEY)["Body"].read().decode("utf-8")
    rows = []
    for line in body.splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def save_rows(client, rows: list[dict]) -> None:
    combined = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows).encode("utf-8")
    client.put_object(Bucket=BUCKET, Key=ALERTS_KEY, Body=combined, ContentType="application/x-ndjson")


def load_doc_extractions(client) -> dict[str, dict]:
    """Load document_extractions_table.jsonl and return a dict keyed by agent_call_id."""
    try:
        body = client.get_object(Bucket=BUCKET, Key=DOC_EXTRACTIONS_KEY)["Body"].read().decode("utf-8")
    except Exception as exc:
        log.warning("Could not load doc extractions (skipping enrichment): %s", exc)
        return {}
    result: dict[str, dict] = {}
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            call_id = row.get("agent_call_id") or ""
            if call_id and call_id not in result:
                result[call_id] = row
        except Exception:
            pass
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Re-classify even rows that already have bubble_action")
    args = parser.parse_args()

    from bubble.bubble_sync_classifier import classify_alert, enrich_with_doc_extraction

    client = s3_client()
    log.info("Loading %s from s3://%s", ALERTS_KEY, BUCKET)
    rows = load_rows(client)
    log.info("Loaded %d rows", len(rows))

    log.info("Loading doc extractions from s3://%s/%s", BUCKET, DOC_EXTRACTIONS_KEY)
    doc_by_call_id = load_doc_extractions(client)
    log.info("Loaded %d doc extraction rows", len(doc_by_call_id))

    counts: Counter = Counter()
    changed = 0

    for row in rows:
        if not args.force and row.get("bubble_action"):
            counts["already_set"] += 1
            continue

        plan = classify_alert(row)
        if plan.applicable:
            ba = plan.to_dict()
            call_id = row.get("agent_call_id") or ""
            doc_row = doc_by_call_id.get(call_id)
            if doc_row:
                enrich_with_doc_extraction(ba, doc_row)
            row["bubble_action"] = ba
            counts[f"set:{row.get('alert_type', '?')}"] += 1
            changed += 1
        else:
            counts["not_applicable"] += 1

    log.info("Summary:")
    for key, n in sorted(counts.items()):
        log.info("  %-45s %d", key, n)
    log.info("Rows to update: %d", changed)

    if args.dry_run:
        log.info("Dry run — nothing written.")
        return

    if changed == 0:
        log.info("Nothing to write.")
        return

    log.info("Writing updated rows to s3://%s/%s", BUCKET, ALERTS_KEY)
    save_rows(client, rows)
    log.info("Done.")


if __name__ == "__main__":
    main()
