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

from botocore.exceptions import ClientError

log = logging.getLogger(__name__)

_DEFAULT_BUCKET = "web-change-tracker-prod-artifacts-815039343351"
_RESULTS_KEY = "alerts/doc_eval_results_table.jsonl"
_EXTRACTIONS_KEY = "alerts/document_extractions_table.jsonl"

_STAMP_MAX_RETRIES = 4


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


def _load_existing_with_etag(client, bucket: str, key: str) -> tuple[dict[str, dict], str]:
    """Return (existing_rows_dict, etag). etag is '' when the file doesn't exist yet."""
    try:
        resp = client.get_object(Bucket=bucket, Key=key)
        etag = resp.get("ETag", "")
        body = resp["Body"].read().decode("utf-8")
    except client.exceptions.NoSuchKey:
        return {}, ""
    except Exception:
        raise  # don't swallow S3 errors — caller must not write if we can't read
    rows: dict[str, dict] = {}
    for line in body.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            k = _row_key(r)
            if k:
                rows[k] = r
        except json.JSONDecodeError:
            pass
    return rows, etag


def _load_existing(client, bucket: str) -> dict[str, dict]:
    rows, _ = _load_existing_with_etag(client, bucket, _RESULTS_KEY)
    return rows


def _put_conditional(client, bucket: str, s3_key: str, body: bytes,
                     content_type: str, etag: str, extra_meta: dict | None = None) -> bool:
    """PUT with If-Match: etag for optimistic concurrency.

    Returns True on success, False on 412 PreconditionFailed (caller retries).
    Falls back to unconditional PUT if boto3 doesn't accept IfMatch.
    """
    kwargs = dict(Bucket=bucket, Key=s3_key, Body=body, ContentType=content_type)
    if extra_meta:
        kwargs["Metadata"] = extra_meta
    if etag:
        kwargs["IfMatch"] = etag
    try:
        client.put_object(**kwargs)
        return True
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("PreconditionFailed", "ConditionalRequestConflict"):
            return False
        raise
    except TypeError:
        # boto3 version doesn't accept IfMatch — fall back to unconditional put
        kwargs.pop("IfMatch", None)
        client.put_object(**kwargs)
        return True


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

    Uses ETag-based conditional PUT with retry to prevent concurrent writers
    (e.g. another eval task or dashboard patch_jsonl_row) from clobbering stamps.
    """
    if not eval_rows:
        return
    try:
        from eval.doc_eval_agent import make_doc_eval_row_key
        # Map computed_key → eval_row_key for every row we just evaluated.
        key_map: dict[str, str] = {}
        # Fallback: (call_id, discriminator) → eval_row_key for old 2-part keys
        # that predate the url_filename segment (call_id|discriminator vs call_id|filename|discriminator).
        alt_key_map: dict[tuple[str, str], str] = {}
        for r in eval_rows:
            erk = r.get("eval_row_key")
            if erk:
                key_map[erk] = erk
                parts = erk.split("|")
                if len(parts) >= 2:
                    alt_key_map[(parts[0], parts[-1])] = erk
    except Exception as e:
        log.warning("doc_result_store: could not build stamp key_map: %s", e)
        return

    for attempt in range(_STAMP_MAX_RETRIES):
        try:
            resp = client.get_object(Bucket=bucket, Key=_EXTRACTIONS_KEY)
            etag = resp.get("ETag", "")
            body = resp["Body"].read().decode("utf-8")
        except client.exceptions.NoSuchKey:
            return
        except Exception as e:
            log.warning("doc_result_store: could not read extractions for stamping: %s", e)
            return

        lines = [l for l in body.split("\n") if l.strip()]
        updated_count = 0
        new_lines = []
        for line in lines:
            try:
                row = json.loads(line)
                computed = make_doc_eval_row_key(row)
                stored_erk = row.get("eval_row_key")
                target_erk = key_map.get(computed)
                if target_erk is None:
                    # Fallback: match by (call_id, last discriminator segment) for old-format keys
                    c_parts = computed.split("|")
                    target_erk = alt_key_map.get((c_parts[0], c_parts[-1])) if len(c_parts) >= 2 else None
                if target_erk and stored_erk != target_erk:
                    row["eval_row_key"] = target_erk
                    updated_count += 1
                new_lines.append(json.dumps(row, default=str))
            except (json.JSONDecodeError, Exception):
                new_lines.append(line)  # preserve original line verbatim on any error

        if not updated_count:
            return  # nothing to write

        success = _put_conditional(
            client, bucket, _EXTRACTIONS_KEY,
            "\n".join(new_lines).encode("utf-8"),
            "application/x-ndjson",
            etag,
        )
        if success:
            log.info("doc_result_store: stamped eval_row_key onto %d extraction row(s) (attempt %d)", updated_count, attempt + 1)
            return
        # 412 — another writer changed the file between our read and write; retry from read
        log.warning("doc_result_store: stamp conditional PUT conflict — retrying (attempt %d/%d)", attempt + 1, _STAMP_MAX_RETRIES)

    log.error("doc_result_store: stamp failed after %d retries — eval_row_keys may be unstamped", _STAMP_MAX_RETRIES)


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

    # Count valid rows upfront to avoid a read when nothing would be written.
    valid_rows = [r for r in eval_rows if r.get("eval_scores") and _row_key(r)]
    if not valid_rows:
        log.warning(
            "doc_result_store: no rows with valid eval_scores — skipping S3 write to prevent data loss "
            "(%d row(s) had empty scores; existing S3 file is unchanged)", len(eval_rows)
        )
        # Still stamp even if we have nothing to write — some rows may already have scores
        # from a prior run and the extraction rows may need key stamps from this batch.
        _stamp_eval_row_keys(client, bucket, eval_rows)
        return

    # Upsert with ETag-based retry to prevent concurrent writes from losing rows.
    for attempt in range(_STAMP_MAX_RETRIES):
        try:
            existing, etag = _load_existing_with_etag(client, bucket, _RESULTS_KEY)
            for row in valid_rows:
                existing[_row_key(row)] = row

            body = "\n".join(json.dumps(r, default=str) for r in existing.values()).encode("utf-8")
            success = _put_conditional(client, bucket, _RESULTS_KEY, body,
                                       "application/x-ndjson", etag,
                                       {"eval_run_id": eval_run_id})
            if success:
                log.info("Upserted %d/%d doc eval rows into %s (skipped %d empty)",
                         len(valid_rows), len(eval_rows), _RESULTS_KEY, len(eval_rows) - len(valid_rows))
                break
            log.warning("doc_result_store: eval results PUT conflict — retrying (attempt %d/%d)", attempt + 1, _STAMP_MAX_RETRIES)
        except Exception as e:
            log.error("Failed to write doc_eval_results_table.jsonl: %s", e)
            break

    # Stamp eval_row_key directly onto source extraction rows so the dashboard
    # can do an exact field lookup instead of recomputing the key from row fields.
    # This means the mapping never breaks when key generation logic changes.
    _stamp_eval_row_keys(client, bucket, eval_rows)
