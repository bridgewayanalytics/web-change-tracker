"""
Field registry for alerts_table.jsonl.

ALERT_PIPELINE_FIELDS — the complete set of keys the pipeline stamps on an
alert row. These are never produced by the page_change_agent; they are
injected by _build_rows_for_single_alert(), spike.py, and the
eval/bubble_sync/ingest pipelines.

Consumers:
  - eval/eval_agent.py → exclude from QA prompts
"""

ALERT_PIPELINE_FIELDS: frozenset[str] = frozenset({
    # Row anchors stamped by _build_rows_for_single_alert() in storage/alert_s3.py
    "run_id",
    "run_timestamp",
    "target_id",
    "source_url",
    "config_hash",
    "agent_call_id",
    # Stamped by spike.py post-extraction
    "extraction_source",
    "ingest_status",
    # Rerun metadata
    "last_rerun_at",
    # Patched by bubble_sync.py after storage
    "bubble_sync_status",
    "bubble_sync_error",
    "bubble_event_id",
    "bubble_library_item_id",
    "eidarix_agenda_item_ids",
    "recording_s3_key",
    "transcript_s3_key",
    # Stamped by the eval pipeline
    "eval_run_id",
    "eval_timestamp",
    "eval_scores",
    "eval_row_key",
})
