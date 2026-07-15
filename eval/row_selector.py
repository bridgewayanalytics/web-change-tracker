"""
Select alert rows eligible for QA evaluation.

Eligibility uses a time-window approach:
  1. Query Bubble for all calendar items and library items in the space.
  2. Match alert rows against those records (the "anchor" rows) — these are rows
     whose corresponding Bubble record actually exists, meaning a human has made
     editorial decisions (chronicle topics, newsreel relevance, etc.).
  3. The anchor rows define a date range (earliest to latest alert_date_time).
  4. ALL rows within that date range qualify — including "No Meaningful Change"
     alerts, irrelevant alerts, and resources intentionally not published to Bubble.
     These rows represent the full agent output during the period that Bubble
     records exist, giving a complete picture of evaluation quality.

Transcript rows (alert_type == "New Meeting Transcript Available") are always
excluded — they share HTML with their parent alert and have no independent content.

Rows specified by --agent-call-ids bypass this check entirely.
"""

import json
import logging
import os

log = logging.getLogger(__name__)

_ALERTS_KEY = "alerts/alerts_table.jsonl"
_DEFAULT_BUCKET = "web-change-tracker-prod-artifacts-815039343351"


def _get_bucket() -> str:
    return (
        os.environ.get("CHANGELOG_BUCKET", "").strip()
        or os.environ.get("BUBBLE_ARTIFACT_BUCKET", "").strip()
        or _DEFAULT_BUCKET
    )


def _s3_client():
    import boto3
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _build_bubble_match_sets() -> tuple[set[str], set[str]]:
    """
    Query Bubble for all calendar items and library items in the space.

    Returns:
        event_keys: set of "org|YYYY-MM-DD" strings for existing calendar items
        lib_urls:   set of library item URL strings for existing library items
    """
    from bubble.bridgemind import get_client, TYPE_CALENDAR_ITEM, TYPE_LIBRARY_ITEM, SPACE_CONSTRAINT

    client = get_client()
    event_keys: set[str] = set()
    lib_urls: set[str] = set()

    try:
        for item in client.list_all(TYPE_CALENDAR_ITEM, constraints=SPACE_CONSTRAINT):
            # Build org|date key — calendar items have a date field and orgs list
            date = str(item.get("date") or item.get("start_datetime") or "")[:10]
            orgs = item.get("orgs") or item.get("organizations") or []
            if not isinstance(orgs, list):
                orgs = [orgs]
            # Also index just by date for partial matching
            if date:
                event_keys.add(f"|{date}")
            for org in orgs:
                org_name = org.get("Name") or org if isinstance(org, str) else ""
                if org_name and date:
                    event_keys.add(f"{org_name}|{date}")
        log.info("row_selector: loaded %d event keys from Bubble", len(event_keys))
    except Exception as e:
        log.warning("row_selector: could not fetch calendar items from Bubble: %s", e)

    try:
        for item in client.list_all(TYPE_LIBRARY_ITEM, constraints=SPACE_CONSTRAINT):
            url = (item.get("url_text") or item.get("url") or "").strip()
            if url:
                lib_urls.add(url)
        log.info("row_selector: loaded %d library item URLs from Bubble", len(lib_urls))
    except Exception as e:
        log.warning("row_selector: could not fetch library items from Bubble: %s", e)

    return event_keys, lib_urls


def _row_matches_bubble(row: dict, event_keys: set[str], lib_urls: set[str]) -> bool:
    """Return True if this alert row has a matching record in Bubble."""
    ba = row.get("bubble_action") or {}

    # Check library item match by URL
    lp = ba.get("library_item_preview") or {}
    ms_lib = lp.get("match_search") or {}
    lib_url = (ms_lib.get("url") or lp.get("url") or row.get("library_item_url") or "").strip()
    if lib_url and lib_url.lower() not in ("n/a", "") and lib_url in lib_urls:
        return True

    # Check calendar item match by org + date
    ep = ba.get("event_preview") or {}
    ms_ev = ep.get("match_search") or {}
    date = (ms_ev.get("date") or "")[:10]
    org = (ms_ev.get("org") or "").strip()
    if date:
        if f"{org}|{date}" in event_keys or f"|{date}" in event_keys:
            return True

    return False


def load_eligible_rows(
    *,
    limit: int | None = None,
    since_run_timestamp: int | None = None,
    agent_call_ids: list[str] | None = None,
) -> list[dict]:
    """
    Load alert rows eligible for evaluation.

    When agent_call_ids is provided, those rows are returned directly without
    any Bubble check (used for targeted re-evaluation).

    Otherwise: queries Bubble to find anchor rows (those with matching records),
    derives a date window from those anchors, and returns ALL rows in that window
    (including not-relevant alerts and unpublished resources).

    Args:
        limit: max agent calls to return (most recent first)
        since_run_timestamp: only include rows at or after this Unix timestamp
        agent_call_ids: if provided, return only these rows (bypasses Bubble check)

    Returns list of alert row dicts, most recent first.
    """
    bucket = _get_bucket()
    client = _s3_client()

    try:
        resp = client.get_object(Bucket=bucket, Key=_ALERTS_KEY)
        lines = resp["Body"].read().decode("utf-8").strip().split("\n")
    except Exception as e:
        log.error("Failed to load alerts_table.jsonl: %s", e)
        return []

    all_rows: list[dict] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            all_rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    # ── agent_call_ids bypass ────────────────────────────────────────────────
    if agent_call_ids is not None:
        result = [r for r in all_rows if r.get("agent_call_id") in agent_call_ids]
        result.sort(key=lambda r: r.get("run_timestamp", 0), reverse=True)
        log.info("Selected %d row(s) by agent_call_id", len(result))
        return result

    # ── Time-window eligibility ──────────────────────────────────────────────
    # Step 1: find anchor rows — those with a matching Bubble record
    event_keys, lib_urls = _build_bubble_match_sets()

    candidate_rows = [
        r for r in all_rows
        if r.get("alert_type") != "New Meeting Transcript Available"
        and (
            since_run_timestamp is None
            or (r.get("run_timestamp") is not None and int(r.get("run_timestamp", 0)) >= since_run_timestamp)
        )
    ]

    anchor_rows = [r for r in candidate_rows if r.get("bubble_action") and _row_matches_bubble(r, event_keys, lib_urls)]
    log.info("row_selector: %d anchor rows matched in Bubble", len(anchor_rows))

    if not anchor_rows:
        log.info("row_selector: no anchor rows found — no rows eligible")
        return []

    # Step 2: determine date window from anchor rows
    anchor_dates = [r.get("alert_date_time", "") for r in anchor_rows if r.get("alert_date_time")]
    anchor_dates.sort()
    window_start = anchor_dates[0][:10]   # YYYY-MM-DD of earliest anchor
    window_end = anchor_dates[-1][:10]    # YYYY-MM-DD of latest anchor
    log.info("row_selector: date window %s → %s", window_start, window_end)

    # Step 3: include all rows whose alert_date_time falls within the window
    def _in_window(row: dict) -> bool:
        dt = (row.get("alert_date_time") or "")[:10]
        return bool(dt) and window_start <= dt <= window_end

    eligible = [r for r in candidate_rows if _in_window(r)]
    log.info("row_selector: %d rows fall within the date window", len(eligible))

    # Most recent first
    eligible.sort(key=lambda r: r.get("alert_date_time", ""), reverse=True)

    # Deduplicate by agent_call_id, apply limit at call level,
    # then return all sibling rows for each selected call
    seen: set[str] = set()
    selected_call_ids: list[str] = []
    for row in eligible:
        cid = row.get("agent_call_id", "")
        if cid and cid not in seen:
            seen.add(cid)
            selected_call_ids.append(cid)

    if limit is not None:
        selected_call_ids = selected_call_ids[:limit]

    selected_set = set(selected_call_ids)
    result = [r for r in eligible if r.get("agent_call_id", "") in selected_set]

    log.info(
        "Selected %d agent call(s) (%d total rows including siblings) for evaluation",
        len(selected_call_ids), len(result),
    )
    return result
