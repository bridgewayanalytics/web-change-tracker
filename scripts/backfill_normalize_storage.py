"""
Backfill all four JSONL files to normalize field keys to stable IDs.

Applies field_normalizer.normalize_row_keys() to every data row in:
  - alerts/alerts_table.jsonl
  - alerts/document_extractions_table.jsonl
  - alerts/eval_results_table.jsonl    (normalizes eval_scores keys only)
  - alerts/doc_eval_results_table.jsonl

Idempotent: stable IDs map to themselves, so running twice is safe.

Three modes:
  --dry-run   Report what would change; write nothing at all.
  --preview   Write normalized output to <key>.preview in S3 so you can
              inspect the result before committing. Does NOT touch the real files.
  (default)   Back up originals to <key>.bak first, then overwrite with
              normalized output.

Usage:
    # 1. Inspect what will change (no S3 writes at all)
    python3 scripts/backfill_normalize_storage.py --dry-run

    # 2. Write preview files to S3 for manual inspection
    python3 scripts/backfill_normalize_storage.py --preview

    # 3. Run for real (auto-backs up originals to .bak before overwriting)
    python3 scripts/backfill_normalize_storage.py
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
        log.warning("  Key not found: %s", key)
        return []
    rows = []
    bad = 0
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            bad += 1
    if bad:
        log.warning("  %d malformed lines skipped", bad)
    return rows


def _write_jsonl(client, key: str, rows: list[dict]) -> None:
    body = "\n".join(json.dumps(r, default=str) for r in rows)
    client.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=body.encode("utf-8"),
        ContentType="application/x-ndjson",
    )


def _backup(client, key: str) -> str:
    """Copy key → key.bak in S3. Returns the backup key."""
    bak_key = key + ".bak"
    client.copy_object(
        Bucket=BUCKET,
        CopySource={"Bucket": BUCKET, "Key": key},
        Key=bak_key,
    )
    return bak_key


_NESTED_ALERT_KEYS = frozenset({"events", "library_items", "agenda_items"})


def _nested_safe_skip_keys(row: dict, base_skip: frozenset) -> frozenset:
    """
    For nested-schema rows, add any list-valued keys to skip_keys so they pass
    through untouched. This lets us rename other top-level keys (alert_datetime_et,
    art_newsreel_relevance, etc.) while leaving the nested arrays as-is.
    """
    extra = {k for k in _NESTED_ALERT_KEYS if isinstance(row.get(k), list) and row.get(k)}
    return base_skip | extra if extra else base_skip


def _normalize_data_rows(
    rows: list[dict],
    norm_map: dict,
    skip_keys: frozenset,
    skip_nested_alerts: bool,
) -> tuple[list[dict], int, int, dict[str, int]]:
    """
    Normalize rows in memory. Returns (normalized_rows, changed_count, skipped_nested, changed_keys).
    Does not touch S3.

    For nested-schema alert rows (pre-May 2026): normalizes all top-level scalar keys
    but leaves list-valued nested arrays (events, library_items, agenda_items) untouched.
    This avoids the agenda_items → agenda_item_title_chronicle_topics rename for the array
    while still cleaning up old keys like alert_datetime_et.
    """
    from storage.field_normalizer import normalize_row_keys

    normalized_rows = []
    changed_count = 0
    skipped_nested = 0
    changed_keys: dict[str, int] = {}

    for row in rows:
        is_nested = skip_nested_alerts and any(
            isinstance(row.get(k), list) and row.get(k) for k in _NESTED_ALERT_KEYS
        )
        effective_skip = _nested_safe_skip_keys(row, skip_keys) if is_nested else skip_keys
        if is_nested:
            skipped_nested += 1
        norm = normalize_row_keys(row, norm_map, effective_skip)
        normalized_rows.append(norm)
        for old_key in row:
            if old_key in effective_skip:
                continue
            new_key = norm_map.get(old_key, old_key)
            if new_key != old_key:
                label = f"{old_key} → {new_key}"
                changed_keys[label] = changed_keys.get(label, 0) + 1
        if norm != row:
            changed_count += 1

    return normalized_rows, changed_count, skipped_nested, changed_keys


def _normalize_eval_rows(
    rows: list[dict],
    norm_map: dict,
) -> tuple[list[dict], int, dict[str, int]]:
    """Normalize eval_scores keys in memory. Returns (rows_mutated, changed_count, changed_keys)."""
    from storage.field_normalizer import normalize_score_keys

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
                label = f"{old_key} → {new_key}"
                changed_keys[label] = changed_keys.get(label, 0) + 1
        if norm_scores != scores:
            row["eval_scores"] = norm_scores
            changed_count += 1

    return rows, changed_count, changed_keys


def _log_summary(total: int, changed: int, skipped: int, changed_keys: dict, key_label: str = "Key") -> None:
    if skipped:
        log.info("  %d nested-schema rows (pre-May 2026): scalar keys normalized, list arrays preserved", skipped)
    log.info("  %d total rows, %d rows have changes", total, changed)
    if changed_keys:
        log.info("  %s renames:", key_label)
        for rename, count in sorted(changed_keys.items(), key=lambda x: -x[1]):
            log.info("    %-60s  (%d rows)", rename, count)
    else:
        log.info("  No renames needed — all keys already stable.")


def process_data_table(
    client, s3_key: str, norm_map: dict, skip_keys: frozenset,
    dry_run: bool, preview: bool, limit: int | None, skip_nested_alerts: bool = False,
) -> None:
    log.info("Loading %s ...", s3_key)
    rows = _load_jsonl(client, s3_key)
    if not rows:
        return
    if limit:
        rows = rows[-limit:]

    normalized, changed, skipped, changed_keys = _normalize_data_rows(
        rows, norm_map, skip_keys, skip_nested_alerts
    )
    _log_summary(len(rows), changed, skipped, changed_keys)

    if dry_run:
        log.info("  [dry-run] No writes.")
        return

    if preview:
        preview_key = s3_key + ".preview"
        _write_jsonl(client, preview_key, normalized)
        log.info("  [preview] Written to s3://%s/%s", BUCKET, preview_key)
        log.info("  [preview] Real file untouched.")
        return

    # Real run: back up first, then overwrite
    bak = _backup(client, s3_key)
    log.info("  Backed up original → s3://%s/%s", BUCKET, bak)
    _write_jsonl(client, s3_key, normalized)
    log.info("  Wrote %d normalized rows to %s", len(normalized), s3_key)


def process_eval_table(
    client, s3_key: str, norm_map: dict,
    dry_run: bool, preview: bool, limit: int | None,
) -> None:
    log.info("Loading %s ...", s3_key)
    rows = _load_jsonl(client, s3_key)
    if not rows:
        return
    if limit:
        rows = rows[-limit:]

    rows, changed, changed_keys = _normalize_eval_rows(rows, norm_map)
    _log_summary(len(rows), changed, 0, changed_keys, key_label="Score key")

    if dry_run:
        log.info("  [dry-run] No writes.")
        return

    if preview:
        preview_key = s3_key + ".preview"
        _write_jsonl(client, preview_key, rows)
        log.info("  [preview] Written to s3://%s/%s", BUCKET, preview_key)
        log.info("  [preview] Real file untouched.")
        return

    bak = _backup(client, s3_key)
    log.info("  Backed up original → s3://%s/%s", BUCKET, bak)
    _write_jsonl(client, s3_key, rows)
    log.info("  Wrote %d normalized rows to %s", len(rows), s3_key)


def main():
    parser = argparse.ArgumentParser(description="Backfill JSONL storage to use stable field IDs")
    parser.add_argument(
        "--tables", default="all",
        help="Comma-separated: all, alerts, doc-extractions, eval, doc-eval",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change; write nothing to S3")
    parser.add_argument("--preview", action="store_true",
                        help="Write normalized output to <key>.preview in S3; leave real files untouched")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only process last N rows per file (useful for spot-checks)")
    args = parser.parse_args()

    if args.dry_run and args.preview:
        log.error("--dry-run and --preview are mutually exclusive")
        sys.exit(1)

    if args.tables.strip() == "all":
        selected = list(_TABLES.keys())
    else:
        selected = [t.strip() for t in args.tables.split(",")]
        unknown = [t for t in selected if t not in _TABLES]
        if unknown:
            log.error("Unknown table(s): %s. Valid: %s", unknown, list(_TABLES.keys()))
            sys.exit(1)

    from storage.field_normalizer import get_alert_norm_map, get_doc_norm_map
    from storage.alert_schema import ALERT_PIPELINE_FIELDS
    from storage.doc_schema import DOC_PIPELINE_FIELDS

    alert_norm = get_alert_norm_map()
    doc_norm = get_doc_norm_map()
    client = s3_client()

    if args.dry_run:
        log.info("=== DRY RUN — no S3 writes at all ===")
    elif args.preview:
        log.info("=== PREVIEW — writing to .preview keys; real files untouched ===")
    else:
        log.info("=== REAL RUN — originals backed up to .bak before overwrite ===")

    for table in selected:
        s3_key = _TABLES[table]
        log.info("")
        log.info("=== %s (%s) ===", table, s3_key)

        if table == "alerts":
            process_data_table(
                client, s3_key, alert_norm, ALERT_PIPELINE_FIELDS,
                args.dry_run, args.preview, args.limit, skip_nested_alerts=True,
            )
        elif table == "doc-extractions":
            process_data_table(
                client, s3_key, doc_norm, DOC_PIPELINE_FIELDS,
                args.dry_run, args.preview, args.limit,
            )
        elif table == "eval":
            process_eval_table(client, s3_key, alert_norm, args.dry_run, args.preview, args.limit)
        elif table == "doc-eval":
            process_eval_table(client, s3_key, doc_norm, args.dry_run, args.preview, args.limit)

    log.info("")
    log.info("Done.")


if __name__ == "__main__":
    main()
