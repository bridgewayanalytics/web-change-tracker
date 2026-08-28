"""
Backfill all four JSONL files to normalize field keys to stable IDs.

Applies field_normalizer.normalize_row_keys() to every data row in:
  - alerts/alerts_table.jsonl          (alert rows)
  - alerts/document_extractions_table.jsonl (doc extraction rows)
  - alerts/eval_results_table.jsonl    (QA eval rows — normalizes eval_scores keys)
  - alerts/doc_eval_results_table.jsonl (doc QA eval rows — normalizes eval_scores keys)

Idempotent: stable IDs map to themselves, so running twice is safe.

Usage:
    python3 scripts/backfill_normalize_storage.py [--tables all|alerts|doc-extractions|eval|doc-eval] [--dry-run] [--limit N]
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

_TABLES = {
    "alerts": "alerts/alerts_table.jsonl",
    "doc-extractions": "alerts/document_extractions_table.jsonl",
    "eval": "alerts/eval_results_table.jsonl",
    "doc-eval": "alerts/doc_eval_results_table.jsonl",
}


def s3_client():
    import boto3
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _load_jsonl(client, key: str) -> list[dict]:
    try:
        body = client.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode("utf-8")
    except client.exceptions.NoSuchKey:
        log.warning("Key not found: %s", key)
        return []
    rows = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as e:
            log.warning("Skipping malformed line: %s", e)
    return rows


def _write_jsonl(client, key: str, rows: list[dict]) -> None:
    body = "\n".join(json.dumps(r, default=str) for r in rows)
    client.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=body.encode("utf-8"),
        ContentType="application/x-ndjson",
    )


def _report_changes(original: dict, normalized: dict) -> dict[str, str]:
    """Return {old_key: new_key} for every key that changed."""
    return {
        k: normalized_k
        for k, normalized_k in (
            (k, normalized.get(k, k)) for k in original
            if k in normalized and normalized[k] != k
        )
        if k != normalized_k
    }


def backfill_data_rows(
    client,
    s3_key: str,
    norm_map: dict,
    skip_keys: frozenset,
    dry_run: bool,
    limit: int | None,
) -> None:
    from storage.field_normalizer import normalize_row_keys

    log.info("Loading %s ...", s3_key)
    rows = _load_jsonl(client, s3_key)
    if not rows:
        log.info("  No rows found, skipping.")
        return

    if limit:
        rows = rows[-limit:]

    changed_count = 0
    changed_keys: dict[str, int] = {}
    normalized_rows = []

    for row in rows:
        norm = normalize_row_keys(row, norm_map, skip_keys)
        normalized_rows.append(norm)

        # Track which keys changed
        for old_key, val in row.items():
            if old_key in skip_keys:
                continue
            new_key = norm_map.get(old_key, old_key)
            if new_key != old_key and old_key in norm or new_key in norm:
                if new_key != old_key:
                    changed_keys[f"{old_key} → {new_key}"] = changed_keys.get(f"{old_key} → {new_key}", 0) + 1

        if norm != row:
            changed_count += 1

    log.info("  %d total rows, %d rows have key changes", len(rows), changed_count)
    if changed_keys:
        log.info("  Key renames seen:")
        for rename, count in sorted(changed_keys.items(), key=lambda x: -x[1]):
            log.info("    %s  (%d rows)", rename, count)

    if dry_run:
        log.info("  [dry-run] Would rewrite %s", s3_key)
    else:
        _write_jsonl(client, s3_key, normalized_rows)
        log.info("  Wrote %d rows to %s", len(normalized_rows), s3_key)


def backfill_eval_rows(
    client,
    s3_key: str,
    norm_map: dict,
    dry_run: bool,
    limit: int | None,
) -> None:
    from storage.field_normalizer import normalize_score_keys

    log.info("Loading %s ...", s3_key)
    rows = _load_jsonl(client, s3_key)
    if not rows:
        log.info("  No rows found, skipping.")
        return

    if limit:
        rows = rows[-limit:]

    changed_count = 0
    changed_keys: dict[str, int] = {}

    for row in rows:
        scores = row.get("eval_scores")
        if not isinstance(scores, dict):
            continue
        norm_scores = normalize_score_keys(scores, norm_map)
        for old_key in scores:
            if old_key == "overall_summary":
                continue
            new_key = norm_map.get(old_key) or norm_map.get(old_key.strip()) or old_key
            if new_key != old_key:
                changed_keys[f"{old_key} → {new_key}"] = changed_keys.get(f"{old_key} → {new_key}", 0) + 1
        if norm_scores != scores:
            row["eval_scores"] = norm_scores
            changed_count += 1

    log.info("  %d total rows, %d rows have eval_scores key changes", len(rows), changed_count)
    if changed_keys:
        log.info("  Score key renames seen:")
        for rename, count in sorted(changed_keys.items(), key=lambda x: -x[1]):
            log.info("    %s  (%d rows)", rename, count)

    if dry_run:
        log.info("  [dry-run] Would rewrite %s", s3_key)
    else:
        _write_jsonl(client, s3_key, rows)
        log.info("  Wrote %d rows to %s", len(rows), s3_key)


def main():
    parser = argparse.ArgumentParser(description="Backfill JSONL storage to use stable field IDs")
    parser.add_argument(
        "--tables",
        default="all",
        help="Comma-separated list of tables to backfill: all, alerts, doc-extractions, eval, doc-eval",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report changes without writing")
    parser.add_argument("--limit", type=int, default=None, help="Only process last N rows per file")
    args = parser.parse_args()

    if args.tables.strip() == "all":
        selected = list(_TABLES.keys())
    else:
        selected = [t.strip() for t in args.tables.split(",")]
        unknown = [t for t in selected if t not in _TABLES]
        if unknown:
            log.error("Unknown table(s): %s. Valid: %s", unknown, list(_TABLES.keys()))
            sys.exit(1)

    # Load creds
    try:
        from config.run_spec import RunSpec
        spec = RunSpec.from_env()
        if hasattr(spec, "_load_secrets"):
            spec._load_secrets()
    except Exception:
        pass
    try:
        from eval.run_eval import _load_secrets
        _load_secrets()
    except Exception:
        pass

    from storage.field_normalizer import get_alert_norm_map, get_doc_norm_map
    from storage.alert_schema import ALERT_PIPELINE_FIELDS
    from storage.doc_schema import DOC_PIPELINE_FIELDS

    alert_norm = get_alert_norm_map()
    doc_norm = get_doc_norm_map()

    client = s3_client()

    if args.dry_run:
        log.info("=== DRY RUN — no files will be written ===")

    for table in selected:
        s3_key = _TABLES[table]
        log.info("")
        log.info("=== %s (%s) ===", table, s3_key)

        if table == "alerts":
            backfill_data_rows(client, s3_key, alert_norm, ALERT_PIPELINE_FIELDS, args.dry_run, args.limit)
        elif table == "doc-extractions":
            backfill_data_rows(client, s3_key, doc_norm, DOC_PIPELINE_FIELDS, args.dry_run, args.limit)
        elif table == "eval":
            backfill_eval_rows(client, s3_key, alert_norm, args.dry_run, args.limit)
        elif table == "doc-eval":
            backfill_eval_rows(client, s3_key, doc_norm, args.dry_run, args.limit)

    log.info("")
    log.info("Done.")


if __name__ == "__main__":
    main()
