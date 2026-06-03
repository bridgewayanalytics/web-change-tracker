"""
Chunk meeting transcripts into agenda-item-aligned segments with rich metadata.

Takes a plain-text transcript from S3 and the alert dict (which carries all
agenda item arrays, event metadata, and org context), produces a JSONL file
where each line is one chunk with full NAIC classification metadata attached.

The LLM agent reads the full transcript and the agenda item list, then assigns
each portion of the transcript to the agenda item being discussed. Code merges
the assignments with the rich metadata from the alert.

JSONL stored at: transcripts/chunks/{stem}.jsonl in the artifacts bucket.
Returns the S3 key on success, None on failure.
"""

import json
import logging
import os

import boto3

log = logging.getLogger(__name__)

_CHUNK_PREFIX = "transcripts/chunks/"
_MAX_WORDS_PER_CHUNK = 800


def _artifacts_bucket() -> str:
    return os.environ.get("CHANGELOG_BUCKET", "")


def _chunk_key(transcript_s3_key: str) -> str:
    """'transcripts/NAIC_LATF_2026-05-21.txt' → 'transcripts/chunks/NAIC_LATF_2026-05-21.jsonl'"""
    stem = transcript_s3_key.removeprefix("transcripts/").removesuffix(".txt")
    return f"{_CHUNK_PREFIX}{stem}.jsonl"


def _download_transcript(s3, bucket: str, key: str) -> str | None:
    try:
        resp = s3.get_object(Bucket=bucket, Key=key)
        return resp["Body"].read().decode("utf-8")
    except Exception as exc:
        log.warning("transcript_chunker: failed to download %s: %s", key, exc)
        return None


def _build_agenda_items(alert: dict) -> list[dict]:
    """
    Zip the four parallel agenda arrays into a unified list.
    Skips entries where every field is N/A.
    """
    titles   = alert.get("agenda_item_title_and_chronicle_topics") or []
    officials = alert.get("agenda_item_title_official") or []
    std_ids  = alert.get("agenda_item_standardized_id") or []
    off_ids  = alert.get("agenda_item_official_id") or []

    items = []
    for i, title_entry in enumerate(titles):
        if not isinstance(title_entry, dict):
            continue
        agenda_title    = title_entry.get("agenda_item_title", "N/A")
        status          = title_entry.get("status", "N/A")
        chronicle_topics = [
            t for t in (title_entry.get("chronicle_topics") or [])
            if t and t != "N/A"
        ]

        def _get(arr, idx, field):
            entry = arr[idx] if idx < len(arr) else {}
            return entry.get(field, "N/A") if isinstance(entry, dict) else "N/A"

        official_title  = _get(officials, i, "official_title")
        standardized_id = _get(std_ids,   i, "standardized_id")
        official_id     = _get(off_ids,   i, "official_id")

        all_na = all(
            v in ("N/A", "", None)
            for v in [agenda_title, official_title, standardized_id, official_id]
        )
        if all_na:
            continue

        items.append({
            "index": i,
            "agenda_item_title":          agenda_title,
            "agenda_item_status":         status,
            "agenda_item_official_title": official_title,
            "agenda_item_standardized_id": standardized_id,
            "agenda_item_official_id":    official_id,
            "chronicle_topics":           chronicle_topics,
        })

    return items


def _call_chunker_agent(transcript_text: str, agenda_items: list[dict]) -> list[dict] | None:
    """
    Ask an LLM to segment the transcript by agenda item.
    Returns a list of {agenda_item_index: int, text: str}, or None on failure.
    agenda_item_index == -1 means general/unattributed content.
    """
    from bubble.openai_client import chat_json

    if agenda_items:
        agenda_summary = "\n".join(
            f"{i + 1}. {item['agenda_item_title']}"
            f" | official: {item['agenda_item_official_title']}"
            f" | std_id: {item['agenda_item_standardized_id']}"
            f" | topics: {', '.join(item['chronicle_topics']) or 'none'}"
            for i, item in enumerate(agenda_items)
        )
    else:
        agenda_summary = "No structured agenda items identified — label all content as General Discussion."

    system_prompt = (
        "You are a meeting transcript segmentation expert. "
        "You receive a meeting transcript and an agenda item list. "
        "Split the transcript into chunks aligned to the agenda items being discussed. "
        "\n\nRules:"
        "\n- Assign each portion of the transcript to the agenda item being discussed at that point."
        "\n- If a portion does not clearly belong to any agenda item (opening remarks, roll call, "
        "procedural discussion, closing), assign it agenda_item_index -1 (General Discussion)."
        f"\n- Keep each chunk under {_MAX_WORDS_PER_CHUNK} words. If an agenda item discussion is "
        "longer, split it into multiple sequential chunks with the same agenda_item_index."
        "\n- Preserve the exact transcript text — do not paraphrase, summarize, or omit anything."
        "\n- Output a JSON object with a 'chunks' array. "
        'Each element: {"agenda_item_index": <int>, "text": <string>}.'
        "\n- agenda_item_index is 0-based matching the agenda list (0 = first item). "
        "Use -1 for general/unattributed content."
    )

    user_content = (
        f"## Agenda Items\n{agenda_summary}"
        f"\n\n## Transcript\n{transcript_text}"
    )

    output_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "chunks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "agenda_item_index": {"type": "integer"},
                        "text": {"type": "string"},
                    },
                    "required": ["agenda_item_index", "text"],
                },
            }
        },
        "required": ["chunks"],
    }

    try:
        result = chat_json(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            model="gpt-5.4",
            reasoning_effort="low",
            json_schema=output_schema,
            json_schema_name="transcript_chunks",
            json_schema_strict=True,
        )
    except Exception as exc:
        log.warning("transcript_chunker: agent call failed: %s", exc)
        return None

    if not isinstance(result, dict):
        return None
    chunks = result.get("chunks")
    return chunks if isinstance(chunks, list) else None


def _build_base_metadata(alert: dict) -> dict:
    """Extract semantically meaningful metadata from the alert for stamping on every chunk."""
    lib = alert.get("library_item_preliminary_title")
    lib_title = lib.get("title") if isinstance(lib, dict) else None

    return {
        # Event context
        "event_title":           alert.get("event_title"),
        "event_start_date_time": alert.get("event_start_date_time"),
        "event_duration":        alert.get("event_duration"),
        "event_url":             alert.get("event_url"),
        # Organization + alert classification
        "organization":          alert.get("organization"),
        "alert_type":            alert.get("alert_type"),
        "alert_title":           alert.get("alert_title"),
        "alert_description":     alert.get("alert_description"),
        "alert_url":             alert.get("alert_url"),
        # Associated library item (if any)
        "library_item_title":    lib_title,
        "library_item_url":      alert.get("library_item_url"),
    }


def chunk_transcript(alert: dict, run_id: str, target_id: str) -> str | None:
    """
    Chunk a meeting transcript into agenda-item-aligned segments with rich metadata.

    Downloads the plain-text transcript from S3, runs the chunker agent,
    merges with alert metadata, and stores JSONL at
    transcripts/chunks/{stem}.jsonl in the artifacts bucket.

    Returns the S3 key on success, None on failure or skip.
    """
    transcript_key = alert.get("transcript_s3_key")
    if not transcript_key:
        return None

    bucket = _artifacts_bucket()
    if not bucket:
        log.warning("transcript_chunker: CHANGELOG_BUCKET not set")
        return None

    chunks_key = _chunk_key(transcript_key)
    s3 = boto3.client("s3")

    # Idempotent: skip if already chunked
    try:
        s3.head_object(Bucket=bucket, Key=chunks_key)
        log.info("transcript_chunker: already exists at s3://%s/%s", bucket, chunks_key)
        return chunks_key
    except Exception:
        pass

    transcript_text = _download_transcript(s3, bucket, transcript_key)
    if not transcript_text:
        return None

    agenda_items = _build_agenda_items(alert)
    base_metadata = _build_base_metadata(alert)

    log.info(
        "transcript_chunker: segmenting transcript (%d chars, %d agenda items, run=%s)",
        len(transcript_text), len(agenda_items), run_id,
    )

    raw_chunks = _call_chunker_agent(transcript_text, agenda_items)
    if raw_chunks is None:
        log.warning("transcript_chunker: agent returned no chunks for %s", transcript_key)
        return None

    agenda_by_index = {item["index"]: item for item in agenda_items}
    lines = []

    for chunk_index, raw in enumerate(raw_chunks):
        text = (raw.get("text") or "").strip()
        if not text:
            continue

        agenda_idx = raw.get("agenda_item_index", -1)
        ai = agenda_by_index.get(agenda_idx, {})

        row = {
            **base_metadata,
            # Chunk position
            "chunk_index": chunk_index,
            # Agenda item classification
            "agenda_item_title":           ai.get("agenda_item_title", "General Discussion"),
            "agenda_item_status":          ai.get("agenda_item_status", "N/A"),
            "agenda_item_official_title":  ai.get("agenda_item_official_title", "N/A"),
            "agenda_item_standardized_id": ai.get("agenda_item_standardized_id", "N/A"),
            "agenda_item_official_id":     ai.get("agenda_item_official_id", "N/A"),
            "chronicle_topics":            ai.get("chronicle_topics", []),
            # Chunk text
            "text": text,
        }
        lines.append(json.dumps(row, ensure_ascii=False))

    if not lines:
        log.warning("transcript_chunker: no non-empty chunks produced for %s", transcript_key)
        return None

    jsonl_body = "\n".join(lines) + "\n"

    try:
        s3.put_object(
            Bucket=bucket,
            Key=chunks_key,
            Body=jsonl_body.encode("utf-8"),
            ContentType="application/x-ndjson",
        )
        log.info(
            "transcript_chunker: stored %d chunks at s3://%s/%s",
            len(lines), bucket, chunks_key,
        )
        return chunks_key
    except Exception as exc:
        log.warning("transcript_chunker: failed to store chunks: %s", exc)
        return None
