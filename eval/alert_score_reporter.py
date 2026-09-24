"""
Generate a summary accuracy report from eval_results_table.jsonl and write it
to alerts/alert_eval_score_report.json in S3.

Called automatically at the end of each web tracking eval run. Safe to call
multiple times — each call overwrites the previous report.
"""

import json
import logging
import os
import time
from collections import defaultdict

log = logging.getLogger(__name__)

_DEFAULT_BUCKET = "web-change-tracker-prod-artifacts-815039343351"
_RESULTS_KEY = "alerts/eval_results_table.jsonl"
_REPORT_KEY = "alerts/alert_eval_score_report.json"

_SCORE_CORRECT = "Correct"
_SCORE_PARTIALLY = "Partially Correct"
_SCORE_INCORRECT = "Incorrect"

# Fields that should never appear in the score report — pipeline metadata,
# eval bookkeeping, Bubble sync fields, recording/transcript fields.
_EXCLUDE_SCORE_FIELDS = {
    # Pipeline metadata
    "run_id", "run_timestamp", "target_id", "source_url", "config_hash", "agent_call_id",
    "extraction_source", "ingest_status", "last_rerun_at",
    # Eval bookkeeping
    "eval_run_id", "eval_timestamp", "eval_row_key", "eval_scores", "overall_summary",
    # Bubble sync fields
    "bubble_action", "bubble_sync_status", "bubble_sync_error",
    "bubble_event_id", "bubble_library_item_id", "eidarix_agenda_item_ids",
    # Recording / transcript
    "recording_s3_key", "transcript_s3_key", "transcript_chunks_s3_key",
    "manual_transcript_s3_key",
}

# Suppress fields with fewer than this many scored rows (likely legacy schema rows).
_MIN_FIELD_SAMPLES = 10


def _get_valid_fields() -> set[str] | None:
    """Return the set of current output schema field names from DynamoDB.
    Only these fields are shown in the report — filters out old schema names
    from historical eval runs. Returns None if config is unavailable."""
    try:
        from config.chatkit_config import get_chat_config
        cfg = get_chat_config("web-tracking-agent")
        schema = cfg.get("output_json_schema") or {}
        props = schema.get("properties") or {}
        if "alerts" in props:
            inner = (props["alerts"].get("items") or {})
            fields = set(inner.get("required") or [])
        else:
            fields = set(schema.get("required") or [])
        return fields or None
    except Exception as e:
        log.warning("alert_score_reporter: could not load schema fields: %s", e)
        return None


def _get_bucket() -> str:
    return (
        os.environ.get("CHANGELOG_BUCKET", "").strip()
        or os.environ.get("BUBBLE_ARTIFACT_BUCKET", "").strip()
        or _DEFAULT_BUCKET
    )


def _s3_client():
    import boto3
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _normalize_score(score: str) -> str:
    s = str(score).strip()
    if s.lower().startswith("correct"):
        return _SCORE_CORRECT
    if "partial" in s.lower():
        return _SCORE_PARTIALLY
    return _SCORE_INCORRECT


def generate_score_report(triggered_by_run: str | None = None) -> dict:
    """Read eval_results_table.jsonl, compute per-field accuracy stats,
    and write the report to S3. Returns the report dict."""
    bucket = _get_bucket()
    client = _s3_client()

    try:
        resp = client.get_object(Bucket=bucket, Key=_RESULTS_KEY)
        body = resp["Body"].read().decode("utf-8")
    except client.exceptions.NoSuchKey:
        log.warning("alert_score_reporter: %s not found — no report generated", _RESULTS_KEY)
        return {}
    except Exception as e:
        log.error("alert_score_reporter: failed to read results: %s", e)
        return {}

    valid_fields = _get_valid_fields()

    field_counts: dict[str, dict[str, int]] = defaultdict(lambda: {
        _SCORE_CORRECT: 0, _SCORE_PARTIALLY: 0, _SCORE_INCORRECT: 0
    })
    total_rows = 0

    for line in body.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue

        scores = row.get("eval_scores")
        if not isinstance(scores, dict) or not scores:
            continue

        total_rows += 1
        for field, entry in scores.items():
            if field in _EXCLUDE_SCORE_FIELDS:
                continue
            if valid_fields and field not in valid_fields:
                continue
            if not isinstance(entry, dict):
                continue
            score_val = entry.get("score", "")
            bucket_name = _normalize_score(str(score_val))
            field_counts[field][bucket_name] += 1

    overall = {_SCORE_CORRECT: 0, _SCORE_PARTIALLY: 0, _SCORE_INCORRECT: 0}
    for counts in field_counts.values():
        for bucket_name, n in counts.items():
            overall[bucket_name] += n
    total_scores = sum(overall.values())
    overall_accuracy = round(overall[_SCORE_CORRECT] / total_scores * 100, 1) if total_scores else 0.0

    by_field = []
    for field, counts in field_counts.items():
        total = sum(counts.values())
        if total < _MIN_FIELD_SAMPLES:
            log.debug(
                "alert_score_reporter: suppressing field %r — only %d samples (< %d)",
                field, total, _MIN_FIELD_SAMPLES,
            )
            continue
        acc = round(counts[_SCORE_CORRECT] / total * 100, 1) if total else 0.0
        by_field.append({
            "field": field,
            "correct": counts[_SCORE_CORRECT],
            "partially_correct": counts[_SCORE_PARTIALLY],
            "incorrect": counts[_SCORE_INCORRECT],
            "total": total,
            "accuracy_pct": acc,
        })
    by_field.sort(key=lambda x: (x["accuracy_pct"], x["field"]))

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "triggered_by_run": triggered_by_run or "",
        "total_rows": total_rows,
        "overall": {
            "correct": overall[_SCORE_CORRECT],
            "partially_correct": overall[_SCORE_PARTIALLY],
            "incorrect": overall[_SCORE_INCORRECT],
            "total_scores": total_scores,
            "accuracy_pct": overall_accuracy,
        },
        "by_field": by_field,
    }

    try:
        client.put_object(
            Bucket=bucket,
            Key=_REPORT_KEY,
            Body=json.dumps(report, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        log.info(
            "alert_score_reporter: wrote %s — %d rows, %d fields, overall %.1f%%",
            _REPORT_KEY, total_rows, len(by_field), overall_accuracy,
        )
    except Exception as e:
        log.error("alert_score_reporter: failed to write report: %s", e)

    return report
