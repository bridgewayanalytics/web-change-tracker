"""
Backfill alerts_table.jsonl from already-stored S3 page change snapshots.

Finds the most recent N page changes in S3 (pages/<target_id>/YYYY/MM/DD/<run_id>/),
re-runs them through the page change agent + document agent using current DynamoDB config,
and appends results to alerts/alerts_table.jsonl.

Usage:
    python scripts/backfill_alerts.py [--limit 5] [--dry-run]

Requires: AWS credentials with access to the artifacts bucket.
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

# Make repo root importable
sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

_DEFAULT_BUCKET = "web-change-tracker-prod-artifacts-815039343351"
BUCKET = os.environ.get("CHANGELOG_BUCKET") or os.environ.get("BUBBLE_ARTIFACT_BUCKET") or _DEFAULT_BUCKET


def s3_client():
    import boto3
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def list_recent_page_changes(client, limit: int) -> list[dict]:
    """
    List the most recent `limit` page change entries from S3 pages/ prefix.
    Returns list of dicts with: target_id, run_id, prefix, last_modified.
    """
    paginator = client.get_paginator("list_objects_v2")
    entries: list[dict] = []

    # Collect all meta.json objects under pages/
    for page in paginator.paginate(Bucket=BUCKET, Prefix="pages/", Delimiter=""):
        for obj in page.get("Contents") or []:
            key = obj["Key"]
            if key.endswith("/meta.json"):
                # pages/<target_id>/YYYY/MM/DD/<run_id>/meta.json
                parts = key.split("/")
                if len(parts) >= 6:
                    entries.append({
                        "target_id": parts[1],
                        "run_id": parts[5],
                        "prefix": "/".join(parts[:6]),
                        "last_modified": obj["LastModified"],
                        "meta_key": key,
                    })

    # Sort by most recent and take top N
    entries.sort(key=lambda x: x["last_modified"], reverse=True)
    return entries[:limit]


def fetch_text(client, key: str) -> str:
    try:
        resp = client.get_object(Bucket=BUCKET, Key=key)
        return resp["Body"].read().decode("utf-8")
    except client.exceptions.NoSuchKey:
        return ""


def run_backfill(limit: int = 5, dry_run: bool = False):
    # Enable agents and force SSM credential loading (same as prod ECS environment)
    os.environ.setdefault("PAGE_CHANGE_AGENT_ENABLED", "true")
    os.environ.setdefault("PGVECTOR_ENABLED", "true")
    os.environ.setdefault("OPENAI_FETCH_FROM_SSM", "true")
    os.environ.setdefault("AWS_REGION", "us-east-1")

    from bubble.ssm_loader import load_openai_env_from_ssm, load_db_env_from_ssm
    load_openai_env_from_ssm()
    load_db_env_from_ssm()

    client = s3_client()

    log.info("Scanning S3 for the %d most recent page changes...", limit)
    changes = list_recent_page_changes(client, limit)

    if not changes:
        log.info("No page changes found in S3.")
        return

    log.info("Found %d change(s) to backfill:", len(changes))
    for c in changes:
        log.info("  %s / %s (%s)", c["target_id"], c["run_id"], c["last_modified"].date())

    from bubble.page_change_agent import extract_page_change
    from bubble.document_agent import should_run_for_alert, extract_document_data
    from storage.alert_s3 import _build_table_rows, _get_bucket, _put, _s3_client as alert_s3_client

    table_rows: list[dict] = []

    for entry in changes:
        prefix = entry["prefix"]
        target_id = entry["target_id"]
        run_id = entry["run_id"]

        # Load meta
        meta_raw = fetch_text(client, entry["meta_key"])
        if not meta_raw:
            log.warning("No meta.json for %s/%s, skipping", target_id, run_id)
            continue
        meta = json.loads(meta_raw)

        run_timestamp = meta.get("run_timestamp") or 0
        from datetime import datetime, timezone
        run_timestamp_iso = datetime.fromtimestamp(run_timestamp, tz=timezone.utc).isoformat() \
            if run_timestamp else entry["last_modified"].isoformat()

        label = meta.get("label") or target_id
        source_url = meta.get("url") or ""
        first_run = meta.get("first_run", False)

        # Load before/after HTML
        before_html = fetch_text(client, f"{prefix}/before.html")
        after_html = fetch_text(client, f"{prefix}/after.html")

        if not after_html:
            log.warning("No after.html for %s/%s, skipping", target_id, run_id)
            continue

        log.info("Running page change agent on %s / %s...", target_id, run_id)

        target_context = {
            "label": label,
            "url": source_url,
            "org_path": [],
            "group": "",
            "tags": [],
        }

        agent_output = extract_page_change(before_html, after_html, target_context)

        if not agent_output:
            log.info("  -> No agent output (skipping)")
            continue
        if agent_output.get("alert_type") == "No Meaningful Change":
            log.info("  -> No meaningful change (skipping)")
            continue

        log.info("  -> alert_type=%s  library_items=%d  events=%d",
                 agent_output.get("alert_type"),
                 len(agent_output.get("library_items") or []),
                 len(agent_output.get("events") or []))

        # Run document agent on library items if applicable
        doc_extractions: list[dict] = []
        if should_run_for_alert(agent_output):
            for item in (agent_output.get("library_items") or []):
                name = item.get("preliminary_title") or item.get("title") or item.get("file_name") or ""
                url = item.get("url") or ""
                if not name:
                    continue
                log.info("  -> document agent: %s", name[:60])
                result = extract_document_data(name, url)
                if result:
                    doc_extractions.append({"item": item, "extraction": result})
                    log.info("     topics=%s  agenda=%s",
                             result.get("topic_ids"), result.get("agenda_item_ids"))

        rows = _build_table_rows(
            agent_output, doc_extractions,
            run_id, run_timestamp_iso, target_id, source_url,
        )
        table_rows.extend(rows)
        log.info("  -> %d table row(s) built", len(rows))

    if not table_rows:
        log.info("No alert rows produced — nothing to write.")
        return

    log.info("Total rows to append: %d", len(table_rows))

    if dry_run:
        log.info("[DRY RUN] Would append %d row(s) to alerts/alerts_table.jsonl", len(table_rows))
        for r in table_rows:
            print(json.dumps(r, indent=2, ensure_ascii=False))
        return

    # Append to the global alerts_table.jsonl
    alert_bucket = _get_bucket()
    if not alert_bucket:
        log.error("No bucket configured (CHANGELOG_BUCKET / BUBBLE_ARTIFACT_BUCKET)")
        return

    a_client = alert_s3_client()
    global_key = "alerts/alerts_table.jsonl"

    existing_body = b""
    try:
        resp = a_client.get_object(Bucket=alert_bucket, Key=global_key)
        existing_body = resp["Body"].read()
        log.info("Existing alerts_table.jsonl: %d bytes", len(existing_body))
    except Exception:
        log.info("alerts_table.jsonl does not exist yet — will create it.")

    new_lines = "\n".join(json.dumps(r, ensure_ascii=False) for r in table_rows)
    if existing_body:
        combined = existing_body.rstrip(b"\n") + b"\n" + new_lines.encode("utf-8")
    else:
        combined = new_lines.encode("utf-8")

    _put(a_client, alert_bucket, global_key, combined, "application/x-ndjson", "backfill")
    log.info("Done — appended %d row(s) to s3://%s/%s", len(table_rows), alert_bucket, global_key)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill alerts_table.jsonl from S3 snapshots")
    parser.add_argument("--limit", type=int, default=5, help="Number of recent changes to backfill (default: 5)")
    parser.add_argument("--dry-run", action="store_true", help="Print rows without writing to S3")
    args = parser.parse_args()
    run_backfill(limit=args.limit, dry_run=args.dry_run)
