"""
Backfill document_extractions_table.jsonl from already-stored S3 agent_output.json files.

Reads the agent_output.json files the live pipeline already wrote, re-runs the
document-data-extraction agent on any library items found, and appends rows to
alerts/document_extractions_table.jsonl.

SAFE: never touches alerts_table.jsonl, alerts_table.xlsx, or any other existing data.

Usage:
    python scripts/backfill_document_extractions.py [--limit 5] [--dry-run]

Requires: AWS credentials with access to the artifacts bucket.
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

_DEFAULT_BUCKET = "web-change-tracker-prod-artifacts-815039343351"
BUCKET = os.environ.get("CHANGELOG_BUCKET") or os.environ.get("BUBBLE_ARTIFACT_BUCKET") or _DEFAULT_BUCKET


def s3_client():
    import boto3
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def list_agent_outputs(client, limit: int) -> list[dict]:
    """
    List the most recent `limit` agent_output.json files from S3 pages/ prefix.
    Returns list of dicts with: target_id, run_id, key, last_modified.
    """
    paginator = client.get_paginator("list_objects_v2")
    entries: list[dict] = []

    for page in paginator.paginate(Bucket=BUCKET, Prefix="pages/", Delimiter=""):
        for obj in page.get("Contents") or []:
            key = obj["Key"]
            if key.endswith("/agent_output.json"):
                # pages/<target_id>/YYYY/MM/DD/<run_id>/agent_output.json
                parts = key.split("/")
                if len(parts) >= 6:
                    entries.append({
                        "target_id": parts[1],
                        "run_id": parts[5],
                        "key": key,
                        "prefix": "/".join(parts[:6]),
                        "last_modified": obj["LastModified"],
                    })

    entries.sort(key=lambda x: x["last_modified"], reverse=True)
    return entries[:limit]


def fetch_json(client, key: str) -> dict | list | None:
    try:
        resp = client.get_object(Bucket=BUCKET, Key=key)
        return json.loads(resp["Body"].read().decode("utf-8"))
    except client.exceptions.NoSuchKey:
        return None
    except Exception as e:
        log.warning("Failed to fetch %s: %s", key, e)
        return None


def run_backfill(limit: int = 5, dry_run: bool = False):
    os.environ.setdefault("PAGE_CHANGE_AGENT_ENABLED", "true")
    os.environ.setdefault("PGVECTOR_ENABLED", "true")
    os.environ.setdefault("OPENAI_FETCH_FROM_SSM", "true")
    os.environ.setdefault("AWS_REGION", "us-east-1")

    from bubble.ssm_loader import load_openai_env_from_ssm, load_db_env_from_ssm
    load_openai_env_from_ssm()
    load_db_env_from_ssm()

    from bubble.document_agent import should_run_for_alert, extract_document_data
    from storage.alert_s3 import _build_doc_extraction_rows, _get_bucket, _put, _s3_client as alert_s3_client

    client = s3_client()

    log.info("Scanning S3 for the %d most recent agent_output.json files...", limit)
    entries = list_agent_outputs(client, limit)

    if not entries:
        log.info("No agent_output.json files found in S3.")
        return

    log.info("Found %d file(s):", len(entries))
    for e in entries:
        log.info("  %s / %s (%s)", e["target_id"], e["run_id"], e["last_modified"].date())

    doc_table_rows: list[dict] = []

    for entry in entries:
        target_id = entry["target_id"]
        run_id = entry["run_id"]
        agent_output = fetch_json(client, entry["key"])

        if not agent_output:
            log.warning("Could not load agent_output for %s/%s, skipping", target_id, run_id)
            continue

        # Unwrap to list of alert dicts
        # agent_output.json can be: a list (multi-alert), a dict with "alerts" key, or a single dict
        if isinstance(agent_output, list):
            alert_dicts = agent_output
        elif isinstance(agent_output, dict) and "alerts" in agent_output and isinstance(agent_output["alerts"], list):
            alert_dicts = agent_output["alerts"]
        elif isinstance(agent_output, dict):
            alert_dicts = [agent_output]
        else:
            log.warning("Unexpected agent_output type for %s/%s: %s", target_id, run_id, type(agent_output))
            continue

        # Load meta for run_timestamp and source_url
        meta = fetch_json(client, f"{entry['prefix']}/meta.json") or {}
        run_timestamp = meta.get("run_timestamp") or 0
        source_url = meta.get("url") or ""

        from datetime import datetime, timezone
        run_timestamp_iso = (
            datetime.fromtimestamp(run_timestamp, tz=timezone.utc).isoformat()
            if run_timestamp else entry["last_modified"].isoformat()
        )

        doc_extractions: list[dict] = []
        for alert_dict in alert_dicts:
            alert_type = alert_dict.get("alert_type", "")
            if alert_type == "No Meaningful Change":
                log.info("  %s/%s: no meaningful change, skipping", target_id, run_id)
                continue

            if not should_run_for_alert(alert_dict):
                log.info("  %s/%s: alert_type=%r — not a document alert, skipping", target_id, run_id, alert_type)
                continue

            # Old schema: library_items array; new flat schema: single library_item_* fields
            library_items = alert_dict.get("library_items") or []
            if not library_items:
                raw_title = alert_dict.get("library_item_preliminary_title") or {}
                if isinstance(raw_title, dict):
                    lib_name = raw_title.get("library_item_title") or raw_title.get("title") or ""
                else:
                    lib_name = str(raw_title)
                lib_url = alert_dict.get("library_item_url") or ""
                lib_file = alert_dict.get("library_items_file_name") or ""
                if lib_name and lib_name.strip().upper() not in ("N/A", "N/A.", "-", ""):
                    library_items = [{"preliminary_title": lib_name, "url": lib_url, "file_name": lib_file}]

            if not library_items:
                log.info("  %s/%s: alert_type=%r — no library items", target_id, run_id, alert_type)
                continue

            log.info("  %s/%s: alert_type=%r  %d library item(s)", target_id, run_id, alert_type, len(library_items))

            for item in library_items:
                name = item.get("preliminary_title") or item.get("title") or item.get("file_name") or ""
                url = item.get("url") or ""
                if not name or name.strip().upper() in ("N/A", "N/A.", "-"):
                    log.info("    -> skipping item with no title")
                    continue
                log.info("    -> document agent: %s", name[:80])
                result = extract_document_data(name, url)
                if result:
                    doc_extractions.append({"item": item, "extraction": result})
                    log.info("       extracted %d field(s)", len(result))
                else:
                    log.info("       no output")

        rows = _build_doc_extraction_rows(
            doc_extractions, run_id, run_timestamp_iso, target_id, source_url,
        )
        doc_table_rows.extend(rows)
        log.info("  -> %d extraction row(s) built", len(rows))

    if not doc_table_rows:
        log.info("No document extraction rows produced — nothing to write.")
        return

    log.info("Total rows to write: %d", len(doc_table_rows))

    if dry_run:
        log.info("[DRY RUN] Would replace/append %d row(s) to alerts/document_extractions_table.jsonl", len(doc_table_rows))
        for r in doc_table_rows:
            print(json.dumps(r, indent=2, ensure_ascii=False))
        return

    alert_bucket = _get_bucket()
    if not alert_bucket:
        log.error("No bucket configured (CHANGELOG_BUCKET / BUBBLE_ARTIFACT_BUCKET)")
        return

    a_client = alert_s3_client()
    doc_table_key = "alerts/document_extractions_table.jsonl"

    # Build set of (run_id, target_id) pairs being backfilled so we can replace them
    backfill_keys = {(r["run_id"], r["target_id"]) for r in doc_table_rows}

    existing_rows: list[dict] = []
    try:
        resp = a_client.get_object(Bucket=alert_bucket, Key=doc_table_key)
        existing_body = resp["Body"].read()
        log.info("Existing document_extractions_table.jsonl: %d bytes", len(existing_body))
        for line in existing_body.decode("utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                key = (row.get("run_id"), row.get("target_id"))
                if key not in backfill_keys:
                    existing_rows.append(row)
            except Exception:
                pass
        log.info("Keeping %d existing rows (removed %d being replaced)", len(existing_rows), len(existing_body.splitlines()) - len(existing_rows))
    except Exception:
        log.info("document_extractions_table.jsonl does not exist yet — will create it.")

    combined_rows = existing_rows + doc_table_rows
    combined_rows.sort(key=lambda r: str(r.get("run_timestamp") or ""), reverse=True)

    combined = "\n".join(json.dumps(r, ensure_ascii=False) for r in combined_rows).encode("utf-8")

    _put(a_client, alert_bucket, doc_table_key, combined, "application/x-ndjson", "backfill-doc-extractions")
    log.info(
        "Done — wrote %d total rows to s3://%s/%s (%d new/replaced)",
        len(combined_rows), alert_bucket, doc_table_key, len(doc_table_rows),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill document_extractions_table.jsonl from stored agent outputs")
    parser.add_argument("--limit", type=int, default=5, help="Number of recent agent outputs to process (default: 5)")
    parser.add_argument("--dry-run", action="store_true", help="Print rows without writing to S3")
    args = parser.parse_args()
    run_backfill(limit=args.limit, dry_run=args.dry_run)
