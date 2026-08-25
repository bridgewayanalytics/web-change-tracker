"""
Field registry for document_extractions_table.jsonl.

DOC_PIPELINE_FIELDS — the complete set of keys the pipeline stamps on a
document extraction row. These are never produced by the document agent;
they are injected by _build_doc_extraction_rows(), document_agent.py,
spike.py, and the eval/bubble_sync/ingest pipelines.

Consumers:
  - eval/doc_eval_agent.py  → exclude from QA prompts and field_names lists
  - bubble/document_agent.py → strip from agent output before stamping
"""

DOC_PIPELINE_FIELDS: frozenset[str] = frozenset({
    # Row anchors stamped by _build_doc_extraction_rows() in storage/alert_s3.py
    "run_id",
    "run_timestamp",
    "target_id",
    "source_url",
    "agent_call_id",
    "library_item_title",
    "library_item_url",
    "library_item_file_name",
    "config_hash",
    # Stamped by document_agent.extract_document_data() after the LLM call
    "doc_agent_context_key",
    "data_extraction_datetime",
    # Stamped by spike.py post-extraction
    "extraction_source",
    "ingest_status",
    # Rerun metadata
    "last_rerun_at",
    # Patched by bubble_sync.py / ingest_actions.py after storage
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
