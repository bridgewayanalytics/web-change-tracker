"""
Transcribe mp3 recordings from recordings-bucket-1 using OpenAI Whisper.

Downloads the mp3 from S3, submits to whisper-1, and stores the plain-text
transcript in the artifacts bucket under transcripts/{stem}.txt.

Returns the S3 key of the stored transcript, or None on failure.
Skips if the transcript already exists in S3 (idempotent).
"""

import io
import logging
import os

import boto3

log = logging.getLogger(__name__)

_RECORDINGS_BUCKET = "recordings-bucket-1"
_TRANSCRIPT_PREFIX = "transcripts/"
_WHISPER_SIZE_LIMIT = 25 * 1024 * 1024  # 25 MB


def _artifacts_bucket() -> str:
    return os.environ.get("CHANGELOG_BUCKET", "")


def _transcript_key(recording_key: str) -> str:
    """'NAIC_LATF_2026-05-21.mp3' → 'transcripts/NAIC_LATF_2026-05-21.txt'"""
    return f"{_TRANSCRIPT_PREFIX}{recording_key.removesuffix('.mp3')}.txt"


def format_with_timestamps(segments) -> str:
    """
    Format Whisper segments as timestamped lines: '[HH:MM:SS] text'.

    Accepts both API (TranscriptionSegment objects) and local-model (dicts).
    """
    lines = []
    for seg in segments:
        if isinstance(seg, dict):
            start = float(seg.get("start", 0))
            text = str(seg.get("text", "")).strip()
        else:
            start = float(getattr(seg, "start", 0))
            text = str(getattr(seg, "text", "")).strip()
        if not text:
            continue
        h = int(start // 3600)
        m = int((start % 3600) // 60)
        s = int(start % 60)
        lines.append(f"[{h:02d}:{m:02d}:{s:02d}] {text}")
    return "\n".join(lines)


def transcribe_recording(recording_s3_key: str) -> str | None:
    """
    Transcribe an mp3 from recordings-bucket-1.

    Stores the result in the artifacts bucket (CHANGELOG_BUCKET) under transcripts/.
    Returns the transcript S3 key on success, None on failure or skip.
    """
    bucket = _artifacts_bucket()
    if not bucket:
        log.warning("transcriber: CHANGELOG_BUCKET not set — cannot store transcript")
        return None

    transcript_key = _transcript_key(recording_s3_key)
    s3 = boto3.client("s3")

    # Skip if already transcribed
    try:
        s3.head_object(Bucket=bucket, Key=transcript_key)
        log.info("transcriber: already exists at s3://%s/%s", bucket, transcript_key)
        return transcript_key
    except s3.exceptions.ClientError:
        pass
    except Exception as exc:
        log.warning("transcriber: head_object failed: %s", exc)

    # Download mp3 from recordings bucket
    try:
        resp = s3.get_object(Bucket=_RECORDINGS_BUCKET, Key=recording_s3_key)
        audio_bytes = resp["Body"].read()
    except Exception as exc:
        log.warning("transcriber: failed to download %s: %s", recording_s3_key, exc)
        return None

    if len(audio_bytes) > _WHISPER_SIZE_LIMIT:
        log.warning(
            "transcriber: %s is %.1f MB — exceeds Whisper 25 MB limit, skipping",
            recording_s3_key, len(audio_bytes) / 1024 / 1024,
        )
        return None

    # Call Whisper
    try:
        from bubble.openai_client import _get_client
        client = _get_client()

        audio_file = io.BytesIO(audio_bytes)
        audio_file.name = recording_s3_key  # Whisper uses filename for format detection

        log.info(
            "transcriber: submitting %s (%.1f MB) to whisper-1",
            recording_s3_key, len(audio_bytes) / 1024 / 1024,
        )
        result = client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            response_format="verbose_json",
        )
        segments = getattr(result, "segments", None) or []
        if segments:
            transcript_text = format_with_timestamps(segments)
        else:
            transcript_text = getattr(result, "text", str(result))
    except Exception as exc:
        log.warning("transcriber: Whisper API failed for %s: %s", recording_s3_key, exc)
        return None

    # Store transcript
    try:
        s3.put_object(
            Bucket=bucket,
            Key=transcript_key,
            Body=transcript_text.encode("utf-8"),
            ContentType="text/plain; charset=utf-8",
        )
        log.info(
            "transcriber: stored %d chars at s3://%s/%s",
            len(transcript_text), bucket, transcript_key,
        )
        return transcript_key
    except Exception as exc:
        log.warning("transcriber: failed to store transcript: %s", exc)
        return None
