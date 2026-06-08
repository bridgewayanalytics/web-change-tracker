"""
Backfill recording_s3_key on existing alerts_table.jsonl rows.

Reads alerts_table.jsonl from S3, runs find_recording() on rows that have
an event_title and event_start_date_time but no recording_s3_key, then
patches any matches back into the JSONL.

Pass 1 (existing): match rows that have both event_title and event_start_date_time
via the standard find_recording() logic (date + acronym scoring).

Pass 2 (new): for each recording still unmatched after pass 1, extract its date
and abbreviation from the filename, then find alert rows where:
  - No recording_s3_key set yet
  - target_id contains the abbreviation (acronym scoring > 0.3 against
    space-tokenised target_id)
  - run_timestamp date is within ±4 days of the recording date

This handles "Updated Materials" rows where event_start_date_time is N/A but
the target_id encodes the committee (e.g. naic.a.latf → LATF recordings).

Usage:
    AWS_PROFILE=bridgeway python scripts/backfill_recordings.py [--dry-run] [--limit 50]

Options:
    --dry-run   Print matches without writing to S3
    --limit N   Only process the first N unmatched rows in pass 1 (0=all). Pass 2 is not limited.
"""

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

_DEFAULT_BUCKET = "web-change-tracker-prod-artifacts-815039343351"
BUCKET = os.environ.get("CHANGELOG_BUCKET") or os.environ.get("BUBBLE_ARTIFACT_BUCKET") or _DEFAULT_BUCKET
ALERTS_KEY = "alerts/alerts_table.jsonl"
RECORDINGS_BUCKET = "recordings-bucket-1"

_DATE_RE = re.compile(r"_(\d{4}-\d{2}-\d{2})\.mp3$")


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


def _is_na(val: str) -> bool:
    return not val or val.strip().upper() in ("N/A", "N/A.", "-", "")


def _list_all_recordings(client) -> list[str]:
    """List all .mp3 keys in the recordings bucket."""
    keys: list[str] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=RECORDINGS_BUCKET):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".mp3"):
                keys.append(obj["Key"])
    return keys


def _extract_date(key: str) -> str | None:
    m = _DATE_RE.search(key)
    return m.group(1) if m else None


def _extract_abbreviation(key: str) -> str:
    """'NAIC_LATF_2026-05-21.mp3' → 'LATF', 'NAIC_LRBC-WG_2026-04-23.mp3' → 'LRBC-WG'"""
    stem = _DATE_RE.sub("", key).removesuffix(".mp3")
    return re.sub(r"^NAIC_", "", stem)


def _acronym_score(abbreviation: str, title: str) -> float:
    """Score 0.0–1.0 for how well abbreviation matches title (re-implemented locally
    to avoid importing recording_matcher with its module-level S3 state)."""
    abbrev_alpha = re.sub(r"[^A-Za-z]", "", abbreviation).upper()
    if not abbrev_alpha:
        return 0.0

    raw_words = re.split(r"[\s\-&/,().]+", title)
    title_words = [re.sub(r"^[^A-Za-z]+|[^A-Za-z]+$", "", w) for w in raw_words if w]
    title_words = [w for w in title_words if w]
    initials = "".join(w[0].upper() for w in title_words)

    if abbrev_alpha == initials:
        return 1.0

    # Subsequence check
    idx = 0
    for ch in abbrev_alpha:
        while idx < len(initials) and initials[idx] != ch:
            idx += 1
        if idx >= len(initials):
            break
        idx += 1
    else:
        return 0.9

    # Exact token match
    abbrev_tokens = [t.upper() for t in re.split(r"[-_]", abbreviation) if t]
    title_upper = [w.upper() for w in title_words]
    exact_hits = sum(1 for tok in abbrev_tokens if tok in title_upper)
    if exact_hits == len(abbrev_tokens):
        return 0.85
    if exact_hits > 0:
        return 0.5 + 0.3 * exact_hits / len(abbrev_tokens)

    # Prefix overlap
    matched = sum(
        1 for tok in abbrev_tokens
        if any(tw.startswith(tok) or tok.startswith(tw[:3]) for tw in title_upper)
    )
    if abbrev_tokens:
        return 0.5 * matched / len(abbrev_tokens)

    return 0.0


def _target_id_as_title(target_id: str) -> str:
    """Convert target_id to a space-separated string suitable for acronym scoring.

    'naic.a.latf' → 'naic a latf'
    'naic.e.life_rbc_wg' → 'naic e life rbc wg'
    """
    return re.sub(r"[._]", " ", target_id)


def _parse_run_date(run_timestamp: str) -> datetime | None:
    """Parse ISO timestamp and return the date part as a datetime (midnight UTC)."""
    try:
        dt = datetime.fromisoformat(run_timestamp.rstrip("Z").replace("Z", "+00:00"))
        return datetime(dt.year, dt.month, dt.day)
    except Exception:
        return None


def _run_pass2(rows: list[dict], all_recording_keys: list[str], dry_run: bool) -> int:
    """Pass 2: match recordings by target_id acronym + ±4 day timestamp window."""
    # Build set of keys already claimed by any row
    claimed = {str(r["recording_s3_key"]) for r in rows if r.get("recording_s3_key")}

    unmatched_recordings = [k for k in all_recording_keys if k not in claimed]
    log.info("[pass2] %d recordings unmatched after pass 1", len(unmatched_recordings))

    # Build list of alert rows that still have no recording_s3_key
    unmatched_rows = [(i, r) for i, r in enumerate(rows) if not r.get("recording_s3_key")]
    log.info("[pass2] %d alert rows without recording_s3_key", len(unmatched_rows))

    total_matched = 0

    for rec_key in unmatched_recordings:
        rec_date_str = _extract_date(rec_key)
        if not rec_date_str:
            log.debug("[pass2] Skipping %s — no date in filename", rec_key)
            continue

        abbreviation = _extract_abbreviation(rec_key)
        try:
            rec_date = datetime.strptime(rec_date_str, "%Y-%m-%d")
        except ValueError:
            continue

        # Find target_ids that match this abbreviation
        candidate_target_ids: dict[str, float] = {}
        for _, row in unmatched_rows:
            target_id = str(row.get("target_id", "") or "")
            if not target_id or target_id in candidate_target_ids:
                continue
            title_form = _target_id_as_title(target_id)
            score = _acronym_score(abbreviation, title_form)
            if score > 0.3:
                candidate_target_ids[target_id] = score

        if not candidate_target_ids:
            log.debug("[pass2] %s → no target_id matches abbreviation '%s'", rec_key, abbreviation)
            continue

        best_target = max(candidate_target_ids, key=lambda t: candidate_target_ids[t])
        log.info(
            "[pass2] Recording %s → abbreviation '%s' → best target '%s' (score=%.2f)",
            rec_key, abbreviation, best_target, candidate_target_ids[best_target],
        )

        # Match alert rows in that target within ±4 days
        row_matched = 0
        for row_i, row in unmatched_rows:
            if str(row.get("target_id", "")) != best_target:
                continue
            run_ts = str(row.get("run_timestamp", "") or "")
            run_date = _parse_run_date(run_ts)
            if run_date is None:
                continue
            if abs((run_date - rec_date).days) <= 4:
                log.info(
                    "[pass2]   → stamping row i=%d target=%s run_date=%s",
                    row_i, best_target, run_date.date(),
                )
                if not dry_run:
                    rows[row_i]["recording_s3_key"] = rec_key
                row_matched += 1

        log.info(
            "[pass2] Recording %s → target %s → matched %d alert row(s) (±4d of %s)",
            rec_key, best_target, row_matched, rec_date_str,
        )
        total_matched += row_matched

    return total_matched


def main():
    from bubble.recording_matcher import find_recording

    parser = argparse.ArgumentParser(description="Backfill recording_s3_key on alerts")
    parser.add_argument("--dry-run", action="store_true", help="Print matches without writing")
    parser.add_argument("--limit", type=int, default=0, help="Max rows to attempt in pass 1 (0=all)")
    parser.add_argument("--pass2-only", action="store_true", help="Skip pass 1, run only pass 2")
    args = parser.parse_args()

    client = s3_client()
    log.info("Loading %s from s3://%s", ALERTS_KEY, BUCKET)
    rows = load_rows(client)
    log.info("Loaded %d rows", len(rows))

    # ── Pass 1: existing logic — event_title + event_start_date_time ─────────────
    pass1_matched = 0
    if not args.pass2_only:
        candidates = [
            (i, r) for i, r in enumerate(rows)
            if not r.get("recording_s3_key")
            and not _is_na(str(r.get("event_title", "") or ""))
            and not _is_na(str(r.get("event_start_date_time", "") or ""))
        ]
        log.info("[pass1] %d rows eligible (have event but no recording_s3_key)", len(candidates))

        if args.limit:
            candidates = candidates[: args.limit]

        for idx, (row_i, row) in enumerate(candidates):
            event_title = str(row.get("event_title", "") or "")
            event_start = str(row.get("event_start_date_time", "") or "")
            key = find_recording(event_title, event_start)
            if key:
                log.info(
                    "[pass1] [%d/%d] MATCH: '%s' (%s) → %s",
                    idx + 1, len(candidates),
                    event_title[:60], event_start[:10], key,
                )
                if not args.dry_run:
                    rows[row_i]["recording_s3_key"] = key
                pass1_matched += 1
            else:
                log.debug("[pass1] [%d/%d] no match: '%s' (%s)", idx + 1, len(candidates), event_title[:60], event_start[:10])

        log.info("[pass1] Matched %d / %d candidates", pass1_matched, len(candidates))

    # ── Pass 2: target_id acronym + ±4 day timestamp window ──────────────────────
    log.info("Listing all recordings from s3://%s for pass 2", RECORDINGS_BUCKET)
    try:
        all_recording_keys = _list_all_recordings(client)
        log.info("Found %d total recordings in bucket", len(all_recording_keys))
    except Exception as exc:
        log.warning("Could not list recordings bucket: %s — skipping pass 2", exc)
        all_recording_keys = []

    pass2_matched = 0
    if all_recording_keys:
        pass2_matched = _run_pass2(rows, all_recording_keys, dry_run=args.dry_run)
        log.info("[pass2] Matched %d rows total", pass2_matched)

    total_matched = pass1_matched + pass2_matched
    log.info("Total matched: %d (pass1=%d, pass2=%d)", total_matched, pass1_matched, pass2_matched)

    if total_matched == 0:
        log.info("Nothing to write.")
        return

    if args.dry_run:
        log.info("Dry run — not writing to S3.")
        return

    log.info("Writing updated rows to s3://%s/%s", BUCKET, ALERTS_KEY)
    save_rows(client, rows)
    log.info("Done.")


if __name__ == "__main__":
    main()
