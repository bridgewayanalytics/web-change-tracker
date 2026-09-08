"""
Store document extraction QA results to S3.

Mirrors result_store.py but writes to doc_eval_results_table.jsonl.
Upsert keyed by eval_row_key, which is also stamped back onto the source
extraction row in document_extractions_table.jsonl so the dashboard can do
a direct field lookup instead of recomputing the key from row fields.
"""

import json
import logging
import os

log = logging.getLogger(__name__)

_DEFAULT_BUCKET = "web-change-tracker-prod-artifacts-815039343351"
_RESULTS_KEY = "alerts/doc_eval_results_table.jsonl"
_EXTRACTIONS_KEY = "alerts/document_extractions_table.jsonl"


def _get_bucket() -> str:
    return (
        os.environ.get("CHANGELOG_BUCKET", "").strip()
        or os.environ.get("BUBBLE_ARTIFACT_BUCKET", "").strip()
        or _DEFAULT_BUCKET
    )


def _s3_client():
    import boto3
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _row_key(row: dict) -> str:
    return row.get("eval_row_key") or row.get("agent_call_id") or ""


def _load_existing(client, bucket: str) -> dict[str, dict]:
    try:
        body = client.get_object(Bucket=bucket, Key=_RESULTS_KEY)["Body"].read().decode("utf-8")
    except Exception:
        return {}
    existing: dict[str, dict] = {}
    for line in body.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            key = _row_key(r)
            if key:
                existing[key] = r
        except json.JSONDecodeError:
            pass
    return existing


def _write(client, bucket: str, rows: dict[str, dict], eval_run_id: str) -> None:
    if not rows:
        return
    body = "\n".join(json.dumps(r, default=str) for r in rows.values())
    client.put_object(
        Bucket=bucket,
        Key=_RESULTS_KEY,
        Body=body.encode("utf-8"),
        ContentType="application/x-ndjson",
        Metadata={"eval_run_id": eval_run_id},
    )


def _stamp_eval_row_keys(client, bucket: str, eval_rows: list[dict]) -> None:
    """Stamp eval_row_key onto each source extraction row in document_extractions_table.jsonl.

    Builds a lookup from computed key → stored eval_row_key using the same
    make_doc_eval_row_key function used during the eval run, then patches any
    extraction row whose stored eval_row_key differs (or is absent).
    """
    if not eval_rows:
        return
    try:
        from eval.doc_eval_agent import make_doc_eval_row_key
        # Map computed_key → eval_row_key for every row we just evaluated.
        # They are identical today but diverge if key logic ever changes again.
        key_map: dict[str, str] = {}
        for r in eval_rows:
            erk = r.get("eval_row_key")
            if erk:
                key_map[erk] = erk  # self-reference: computed == stored

        body = client.get_object(Bucket=bucket, Key=_EXTRACTIONS_KEY)["Body"].read().decode("utf-8")
        lines = [l for l in body.split("\n") if l.strip()]
        updated_count = 0
        new_lines = []
        for line in lines:
            try:
                row = json.loads(line)
                computed = make_doc_eval_row_key(row)
                if computed in key_map and row.get("eval_row_key") != key_map[computed]:
                    row["eval_row_key"] = key_map[computed]
                    updated_count += 1
            except (json.JSONDecodeError, Exception):
                pass
            new_lines.append(json.dumps(row, default=str))

        if updated_count:
            client.put_object(
                Bucket=bucket,
                Key=_EXTRACTIONS_KEY,
                Body="\n".join(new_lines).encode("utf-8"),
                ContentType="application/x-ndjson",
            )
            log.info("doc_result_store: stamped eval_row_key onto %d extraction row(s)", updated_count)
    except Exception as e:
        log.warning("doc_result_store: could not stamp eval_row_keys onto extractions: %s", e)


def store_doc_eval_results(eval_rows: list[dict], eval_run_id: str) -> None:
    if not eval_rows:
        return
    bucket = _get_bucket()
    client = _s3_client()

    # Normalize score keys — non-fatal; if field registry is unavailable, write un-normalized.
    try:
        from storage.field_normalizer import get_doc_norm_map, normalize_score_keys
        norm_map = get_doc_norm_map()
        for row in eval_rows:
            if isinstance(row.get("eval_scores"), dict):
                row["eval_scores"] = normalize_score_keys(row["eval_scores"], norm_map)
    except Exception as e:
        log.warning("doc_result_store: score key normalization skipped: %s", e)

    try:
        existing = _load_existing(client, bucket)

        # Before inserting new results, remove ALL existing entries for the same
        # (agent_call_id, library_item_url) pairs — regardless of key format.
        # This prevents stale entries under old key formats from persisting alongside
        # new ones and causing the dashboard to display the wrong result.
        being_replaced = {
            (r.get("agent_call_id", ""), r.get("library_item_url") or "")
            for r in eval_rows
        }
        removed = sum(
            1 for v in existing.values()
            if (v.get("agent_call_id", ""), v.get("library_item_url") or "") in being_replaced
        )
        existing = {
            k: v for k, v in existing.items()
            if (v.get("agent_call_id", ""), v.get("library_item_url") or "") not in being_replaced
        }
        if removed:
            log.info("doc_result_store: removed %d stale entry/entries before upsert", removed)

        for row in eval_rows:
            key = _row_key(row)
            if key:
                existing[key] = row
        _write(client, bucket, existing, eval_run_id)
        log.info("Upserted %d doc eval rows into %s", len(eval_rows), _RESULTS_KEY)
    except Exception as e:
        log.error("Failed to write doc_eval_results_table.jsonl: %s", e)

    # Stamp eval_row_key directly onto source extraction rows so the dashboard
    # can do an exact field lookup instead of recomputing the key from row fields.
    # This means the mapping never breaks when key generation logic changes.
    _stamp_eval_row_keys(client, bucket, eval_rows)
