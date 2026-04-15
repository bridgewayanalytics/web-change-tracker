"""
Store pipeline alert output to S3 in a UI-ready structure.

Structure (all under CHANGELOG_BUCKET, falling back to BUBBLE_ARTIFACT_BUCKET):

  runs/YYYY/MM/DD/<run_id>/alerts.json
      Array of alert objects — one per changed page that produced agent output.
      This is the primary file for the Alerts Table dashboard UI.

  alerts/alerts_table.jsonl
      Single growing flat JSONL — every run appends its rows here.
      One line per alert row using the exact field names from the Bubble field
      inventory Alerts table. One row per library item detected (or one row per
      alert when no library items). This is the primary stakeholder-facing file.

  pages/<target_id>/YYYY/MM/DD/<run_id>/agent_output.json
      Full web tracking agent output for one page.

  pages/<target_id>/YYYY/MM/DD/<run_id>/doc_extractions.json
      Document agent results for library items found on that page.

Never raises — all failures are logged as warnings.
"""

import json
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger(__name__)


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


def _date_prefix(run_timestamp: int) -> str:
    dt = datetime.fromtimestamp(run_timestamp, tz=timezone.utc)
    return f"{dt.year:04d}/{dt.month:02d}/{dt.day:02d}"


def _put(client, bucket: str, key: str, body: bytes, content_type: str, run_id: str) -> None:
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType=content_type,
        Metadata={"run_id": run_id},
    )


def _put_json(client, bucket: str, key: str, obj: object, run_id: str) -> None:
    _put(client, bucket, key,
         json.dumps(obj, indent=2, ensure_ascii=False).encode("utf-8"),
         "application/json", run_id)


def _build_table_rows(
    agent_output: dict,
    doc_extractions: list[dict],
    run_id: str,
    run_timestamp_iso: str,
    target_id: str,
    source_url: str,
) -> list[dict]:
    """
    Build flat alert_table rows using the field names from the Bubble field inventory
    Alerts table. One row per library item; if no library items, one row for the alert.

    Field names follow the ODS inventory exactly:
      alert_type, alert_title, alert_description, alert_url_title, organization,
      alert_date_time, event_title, event_start_date_time, event_end_date_time,
      event_timezone, event_duration, event_is_full_day, event_url,
      event_call_in_access_code, agenda_item_title, agenda_item_title_official,
      agenda_item_standardized_id, agenda_item_official_id, chronicle_topics,
      library_item_preliminary_title, library_item_url, new_library_item_file_name,
      candidate_chronicles, candidate_agenda_items
    """
    # Shared alert-level fields
    first_event = (agent_output.get("events") or [None])[0] or {}
    first_agenda = (agent_output.get("agenda_items") or [None])[0] or {}

    base: dict = {
        # --- Pipeline metadata ---
        "run_id": run_id,
        "run_timestamp": run_timestamp_iso,
        "target_id": target_id,
        "source_url": source_url,
        # --- Alert fields (ODS: A. UI Table Columns) ---
        "alert_type": agent_output.get("alert_type") or "",
        "alert_title": agent_output.get("alert_title") or "",
        "alert_description": agent_output.get("alert_description") or "",
        "alert_url_title": agent_output.get("alert_title") or "",   # title tied to the URL
        "alert_url": agent_output.get("alert_url") or "",
        "organization": agent_output.get("organization") or "",
        "alert_date_time": agent_output.get("alert_date_time") or "",
        # --- Event fields (first event if present) ---
        "event_title": first_event.get("title") or "",
        "event_start_date_time": first_event.get("start_datetime") or "",
        "event_end_date_time": first_event.get("end_datetime") or "",
        "event_timezone": first_event.get("timezone") or "",
        "event_duration": first_event.get("duration") or "",
        "event_is_full_day": first_event.get("is_full_day") or False,
        "event_url": first_event.get("url") or "",
        "event_call_in_access_code": first_event.get("call_in_access_code") or "",
        # --- Agenda item fields (first agenda item if present) ---
        "agenda_item_title": first_agenda.get("title") or "",
        "agenda_item_title_official": first_agenda.get("official_title") or "",
        "agenda_item_standardized_id": first_agenda.get("standardized_id") or "",
        "agenda_item_official_id": first_agenda.get("official_id") or "",
        "chronicle_topics": ", ".join(first_agenda.get("chronicle_topics") or []),
    }

    # Build a lookup: library_item url → doc extraction result
    doc_by_url: dict[str, dict] = {}
    doc_by_title: dict[str, dict] = {}
    for entry in doc_extractions:
        item = entry.get("item") or {}
        extraction = entry.get("extraction") or {}
        if item.get("url"):
            doc_by_url[item["url"]] = extraction
        title = item.get("preliminary_title") or item.get("title") or ""
        if title:
            doc_by_title[title] = extraction

    library_items = agent_output.get("library_items") or []
    if not library_items:
        # No library items — emit one row for the alert itself
        base["library_item_preliminary_title"] = ""
        base["library_item_url"] = ""
        base["new_library_item_file_name"] = ""
        base["candidate_chronicles"] = ""
        base["candidate_agenda_items"] = ""
        return [base]

    rows: list[dict] = []
    for item in library_items:
        title = item.get("preliminary_title") or item.get("title") or ""
        url = item.get("url") or ""
        file_name = item.get("file_name") or ""

        extraction = doc_by_url.get(url) or doc_by_title.get(title) or {}
        candidate_chronicles = ", ".join(str(x) for x in (extraction.get("topic_ids") or []))
        candidate_agenda_items = ", ".join(str(x) for x in (extraction.get("agenda_item_ids") or []))

        row = dict(base)
        row["library_item_preliminary_title"] = title
        row["library_item_url"] = url
        row["new_library_item_file_name"] = file_name
        row["candidate_chronicles"] = candidate_chronicles
        row["candidate_agenda_items"] = candidate_agenda_items
        rows.append(row)

    return rows


def store_run_alerts(
    change_events: list[dict],
    run_id: str,
    run_timestamp: int,
) -> str | None:
    """
    Write:
      runs/<date>/<run_id>/alerts.json       — structured alert objects (one per page)
      alerts/alerts_table.jsonl              — growing flat JSONL, appended each run
      pages/<target_id>/<date>/<run_id>/agent_output.json
      pages/<target_id>/<date>/<run_id>/doc_extractions.json

    Only events with __agent_output and alert_type != "No Meaningful Change" are included.
    Never raises — all failures are logged as warnings.

    Returns the S3 URI of alerts.json on success, None if skipped or failed.
    """
    bucket = _get_bucket()
    if not bucket:
        return None

    date_prefix = _date_prefix(run_timestamp)
    run_timestamp_iso = datetime.fromtimestamp(run_timestamp, tz=timezone.utc).isoformat()

    alert_rows: list[dict] = []
    table_rows: list[dict] = []

    try:
        client = _s3_client()
    except Exception as e:
        log.warning("alert_s3: could not create S3 client: %s", e)
        return None

    for ev in change_events:
        if "error" in ev:
            continue
        agent_output = ev.get("__agent_output") or {}
        if not agent_output:
            continue
        if agent_output.get("alert_type") == "No Meaningful Change":
            continue

        target_id = ev.get("target_id") or "unknown"
        source_url = ev.get("url") or ""
        page_key_prefix = f"pages/{target_id}/{date_prefix}/{run_id}"

        # Write per-page agent_output.json
        agent_key = f"{page_key_prefix}/agent_output.json"
        try:
            _put_json(client, bucket, agent_key, agent_output, run_id)
        except Exception as e:
            log.warning("alert_s3: failed to write agent_output for %s: %s", target_id, e)
            agent_key = None

        # Write per-page doc_extractions.json (if present)
        doc_extractions = ev.get("__doc_extraction") or []
        if doc_extractions:
            doc_key = f"{page_key_prefix}/doc_extractions.json"
            try:
                _put_json(client, bucket, doc_key, doc_extractions, run_id)
            except Exception as e:
                log.warning("alert_s3: failed to write doc_extractions for %s: %s", target_id, e)

        # Structured alert row (alerts.json)
        row: dict = {
            "run_id": run_id,
            "run_timestamp": run_timestamp_iso,
            "target_id": target_id,
            "source_url": source_url,
            "alert_type": agent_output.get("alert_type") or "",
            "alert_title": agent_output.get("alert_title") or "",
            "alert_description": agent_output.get("alert_description") or "",
            "alert_url": agent_output.get("alert_url"),
            "organization": agent_output.get("organization"),
            "alert_date_time": agent_output.get("alert_date_time"),
            "events": agent_output.get("events") or [],
            "library_items": agent_output.get("library_items") or [],
            "agenda_items": agent_output.get("agenda_items") or [],
            "doc_extractions": [e.get("extraction") or {} for e in doc_extractions],
        }
        if agent_key:
            row["detail_s3_key"] = agent_key
        alert_rows.append(row)

        # Flat table rows (alerts_table.jsonl) — one per library item
        table_rows.extend(_build_table_rows(
            agent_output, doc_extractions,
            run_id, run_timestamp_iso, target_id, source_url,
        ))

    # Write alerts.json (per-run structured output)
    alerts_key = f"runs/{date_prefix}/{run_id}/alerts.json"
    try:
        _put_json(client, bucket, alerts_key, alert_rows, run_id)
        uri = f"s3://{bucket}/{alerts_key}"
        log.info("alert_s3: wrote %d alert(s) to %s", len(alert_rows), uri)
    except Exception as e:
        log.warning("alert_s3: failed to write alerts.json: %s", e)
        uri = None

    # Append new rows to the single growing alerts/alerts_table.jsonl
    # S3 has no native append — download existing, append, re-upload.
    # Then regenerate alerts_table.xlsx from the full JSONL.
    if table_rows:
        global_table_key = "alerts/alerts_table.jsonl"
        xlsx_key = "alerts/alerts_table.xlsx"
        try:
            existing_body = b""
            try:
                resp = client.get_object(Bucket=bucket, Key=global_table_key)
                existing_body = resp["Body"].read()
            except client.exceptions.NoSuchKey:
                pass
            except Exception:
                pass

            new_lines = "\n".join(json.dumps(r, ensure_ascii=False) for r in table_rows)
            if existing_body:
                combined = existing_body.rstrip(b"\n") + b"\n" + new_lines.encode("utf-8")
            else:
                combined = new_lines.encode("utf-8")

            _put(client, bucket, global_table_key, combined, "application/x-ndjson", run_id)
            log.info(
                "alert_s3: appended %d row(s) to s3://%s/%s",
                len(table_rows), bucket, global_table_key,
            )

            # Regenerate Excel from the full updated JSONL
            try:
                all_rows = [json.loads(ln) for ln in combined.decode("utf-8").splitlines() if ln.strip()]
                xlsx_bytes = _build_xlsx(all_rows)
                _put(client, bucket, xlsx_key, xlsx_bytes,
                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", run_id)
                log.info("alert_s3: updated s3://%s/%s (%d rows)", bucket, xlsx_key, len(all_rows))
            except Exception as e:
                log.warning("alert_s3: failed to write alerts_table.xlsx: %s", e)

        except Exception as e:
            log.warning("alert_s3: failed to update alerts_table.jsonl: %s", e)

    return uri


def _build_xlsx(rows: list[dict]) -> bytes:
    """Convert a list of alert table row dicts to an Excel workbook (bytes)."""
    import io
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    if not rows:
        rows = []

    COLS = [
        "run_id", "run_timestamp", "target_id", "source_url",
        "alert_type", "alert_title", "alert_description",
        "alert_url_title", "alert_url", "organization", "alert_date_time",
        "event_title", "event_start_date_time", "event_end_date_time",
        "event_timezone", "event_duration", "event_is_full_day",
        "event_url", "event_call_in_access_code",
        "agenda_item_title", "agenda_item_title_official",
        "agenda_item_standardized_id", "agenda_item_official_id", "chronicle_topics",
        "library_item_preliminary_title", "library_item_url", "new_library_item_file_name",
        "candidate_chronicles", "candidate_agenda_items",
    ]

    HEADER_LABELS = {c: c.replace("_", " ").title() for c in COLS}

    def thin_border():
        s = Side(style="thin", color="D0D0D0")
        return Border(top=s, bottom=s, left=s, right=s)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Alerts"

    # Header row
    for ci, col in enumerate(COLS, 1):
        c = ws.cell(1, ci, HEADER_LABELS[col])
        c.font = Font(name="Calibri", bold=True, size=9, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1E40AF")
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = thin_border()
    ws.row_dimensions[1].height = 28

    # Data rows
    ALT = "EBF0FF"
    for ri, row in enumerate(rows, 2):
        bg = ALT if ri % 2 == 0 else "FFFFFF"
        for ci, col in enumerate(COLS, 1):
            val = row.get(col, "")
            if isinstance(val, bool):
                val = "Yes" if val else ""
            c = ws.cell(ri, ci, val)
            c.font = Font(name="Calibri", size=9)
            c.fill = PatternFill("solid", fgColor=bg)
            c.alignment = Alignment(vertical="top", wrap_text=True)
            c.border = thin_border()

    # Column widths
    widths = {
        "run_id": 18, "run_timestamp": 22, "target_id": 22, "source_url": 30,
        "alert_type": 20, "alert_title": 40, "alert_description": 60,
        "alert_url_title": 35, "alert_url": 30, "organization": 30, "alert_date_time": 20,
        "event_title": 35, "event_start_date_time": 20, "event_end_date_time": 20,
        "event_timezone": 16, "event_duration": 14, "event_is_full_day": 12,
        "event_url": 30, "event_call_in_access_code": 22,
        "agenda_item_title": 35, "agenda_item_title_official": 35,
        "agenda_item_standardized_id": 22, "agenda_item_official_id": 18,
        "chronicle_topics": 30,
        "library_item_preliminary_title": 35, "library_item_url": 30,
        "new_library_item_file_name": 25,
        "candidate_chronicles": 25, "candidate_agenda_items": 25,
    }
    from openpyxl.utils import get_column_letter
    for ci, col in enumerate(COLS, 1):
        ws.column_dimensions[get_column_letter(ci)].width = widths.get(col, 20)

    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
