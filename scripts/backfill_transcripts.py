"""
Backfill transcript_s3_key on alerts that have a recording_s3_key but no transcript yet.

Transcribes each recording via Whisper, submits the transcript to the newsreel
knowledge base via presigned URL, and stamps transcript_s3_key + ingest_status=approved.

Groups rows by agent_call_id so each recording is transcribed and ingested once,
then all rows in the group are stamped.

Usage:
    AWS_PROFILE=bridgeway python scripts/backfill_transcripts.py [--dry-run] [--limit N] [--local]

Options:
    --dry-run   Print what would be done without writing to S3 or calling APIs
    --limit N   Only process the first N unique recordings (default: all)
    --local     Use local openai-whisper model instead of the Whisper API
"""

import argparse
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

_DEFAULT_BUCKET = "web-change-tracker-prod-artifacts-815039343351"
RECORDINGS_BUCKET = "recordings-bucket-1"
BUCKET = os.environ.get("CHANGELOG_BUCKET") or os.environ.get("BUBBLE_ARTIFACT_BUCKET") or _DEFAULT_BUCKET
ALERTS_KEY = "alerts/alerts_table.jsonl"


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
    client.put_object(
        Bucket=BUCKET,
        Key=ALERTS_KEY,
        Body=combined,
        ContentType="application/x-ndjson",
    )


def _transcript_key(recording_key: str) -> str:
    stem = Path(recording_key).stem
    return f"transcripts/{stem}.txt"


def transcribe_local(client, recording_key: str, transcript_key: str) -> str | None:
    """Download mp3 from S3, transcribe with local Whisper, upload txt to artifacts bucket."""
    import whisper  # type: ignore

    # Skip if transcript already exists
    try:
        client.head_object(Bucket=BUCKET, Key=transcript_key)
        log.info("  Already transcribed, skipping: %s", transcript_key)
        return transcript_key
    except Exception:
        pass

    log.info("  Downloading %s from s3://%s", recording_key, RECORDINGS_BUCKET)
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        client.download_file(RECORDINGS_BUCKET, recording_key, tmp_path)
        size_mb = Path(tmp_path).stat().st_size / 1024 / 1024
        log.info("  Downloaded %.1f MB — loading Whisper model (this may take a moment)…", size_mb)

        model = whisper.load_model("medium")
        log.info("  Transcribing with local Whisper medium model…")
        result = model.transcribe(tmp_path, fp16=False)
        text = result["text"].strip()

        log.info("  Uploading transcript (%d chars) to s3://%s/%s", len(text), BUCKET, transcript_key)
        client.put_object(
            Bucket=BUCKET,
            Key=transcript_key,
            Body=text.encode("utf-8"),
            ContentType="text/plain",
        )
        return transcript_key
    except Exception as e:
        log.error("  Local transcription failed: %s", e)
        return None
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Backfill transcripts on alerts")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be done without writing")
    parser.add_argument("--limit", type=int, default=0, help="Max unique recordings to process (0=all)")
    parser.add_argument("--local", action="store_true", help="Use local Whisper model instead of API")
    args = parser.parse_args()

    os.environ.setdefault("CHANGELOG_BUCKET", BUCKET)
    os.environ.setdefault("BUBBLE_ARTIFACT_BUCKET", BUCKET)

    import boto3
    ssm = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "us-east-1"))

    # Fetch OpenAI key from SSM if not already in env
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        try:
            result = ssm.get_parameter(Name="/web-change-tracker/prod/openai_api_key", WithDecryption=True)
            os.environ["OPENAI_API_KEY"] = result["Parameter"]["Value"].strip()
            log.info("Loaded OpenAI API key from SSM")
        except Exception as e:
            log.error("Could not fetch OpenAI key from SSM: %s", e)
            sys.exit(1)

    # Fetch ChatKit internal API key from SSM if not already in env
    if not os.environ.get("CHATKIT_INTERNAL_API_KEY", "").strip():
        try:
            result = ssm.get_parameter(Name="/web-change-tracker/prod/chatkit_internal_api_key", WithDecryption=True)
            os.environ["CHATKIT_INTERNAL_API_KEY"] = result["Parameter"]["Value"].strip()
            log.info("Loaded ChatKit internal API key from SSM")
        except Exception as e:
            log.warning("Could not fetch ChatKit internal API key from SSM: %s — ingest will be skipped", e)

    client = s3_client()
    log.info("Loading %s from s3://%s", ALERTS_KEY, BUCKET)
    rows = load_rows(client)
    log.info("Loaded %d rows", len(rows))

    eligible = [
        (i, r) for i, r in enumerate(rows)
        if r.get("recording_s3_key") and not r.get("transcript_s3_key")
    ]
    log.info("%d rows eligible (have recording but no transcript)", len(eligible))

    # Group by agent_call_id — transcribe once per group
    groups: dict[str, list[tuple[int, dict]]] = {}
    for i, r in eligible:
        call_id = str(r.get("agent_call_id") or f"__row_{i}")
        groups.setdefault(call_id, []).append((i, r))

    log.info("%d unique recordings to process", len(groups))

    group_list = list(groups.items())
    if args.limit:
        group_list = group_list[: args.limit]

    transcript_count = 0
    chunk_count = 0

    for call_id, group_rows in group_list:
        _, representative = group_rows[0]
        recording_key = str(representative["recording_s3_key"])
        event_title = str(representative.get("event_title") or "")
        run_id = str(representative.get("run_id") or "manual")
        target_id = str(representative.get("target_id") or "manual")

        log.info("Processing: %s  (call_id=…%s, %d row(s))", recording_key, call_id[-8:], len(group_rows))
        log.info("  Event: %s", event_title[:70])

        if args.dry_run:
            mode = "local Whisper" if args.local else "Whisper API"
            log.info("  [dry-run] would transcribe via %s → ingest to newsreel KB", mode)
            continue

        # Step 1: Transcribe
        if args.local:
            t_key = _transcript_key(recording_key)
            transcript_key = transcribe_local(client, recording_key, t_key)
        else:
            from bubble.transcriber import transcribe_recording
            transcript_key = transcribe_recording(recording_key)

        if not transcript_key:
            log.warning("  Transcription failed — skipping ingest")
            continue

        log.info("  Transcript: %s", transcript_key)
        transcript_count += 1

        # Step 2: Ingest transcript to newsreel KB via presigned URL
        try:
            from storage.ingest_actions import generate_presigned_url
            from bubble.newsreel_ingest import ingest_for_newsreel
            presigned_url = generate_presigned_url(transcript_key, expires_in=3600)
            filename = transcript_key.split("/")[-1] or "transcript.txt"
            ingest_for_newsreel(document_url=presigned_url, filename=filename)
            log.info("  Ingested to newsreel KB as '%s'", filename)
            ingest_ok = True
        except Exception as exc:
            log.warning("  Newsreel ingest failed: %s", exc)
            ingest_ok = False

        # Stamp transcript key + ingest_status on all rows in this group
        for row_i, _ in group_rows:
            rows[row_i]["transcript_s3_key"] = transcript_key
            if ingest_ok and not rows[row_i].get("ingest_status"):
                rows[row_i]["ingest_status"] = "approved"

    log.info("Transcribed: %d", transcript_count)

    if args.dry_run:
        log.info("Dry run — nothing written.")
        return

    if transcript_count == 0:
        log.info("Nothing to write.")
        return

    log.info("Writing updated rows to s3://%s/%s", BUCKET, ALERTS_KEY)
    save_rows(client, rows)
    log.info("Done.")


if __name__ == "__main__":
    main()
