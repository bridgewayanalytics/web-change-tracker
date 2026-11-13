# CLAUDE.md — Project Context

## What this project does

Website change-tracking system that monitors configured NAIC web pages on a 6-hour schedule, detects meaningful changes (new PDFs, meetings, agenda items), and runs RAG-based LLM agents on before/after HTML snapshots to produce structured alerts. Alerts feed a downstream dashboard (repo: `NAICDashboard-`, deployed at `https://tracker.bridgewayanalytics.com`). Approved alerts can be synced to Eidarix (Bubble.io) via `bubble_sync.py`.

## Tech stack

- **Language:** Python
- **Scraping:** Playwright (JS-rendered), requests + BeautifulSoup (simple pages)
- **Change detection:** SHA256 fingerprinting, difflib
- **RAG agents:** OpenAI Agents SDK + hosted ChatKit backend knowledge-base API (`POST /knowledge/search` for reference collections, `GET /knowledge/document-extraction/{uuid}/text` for the source document). Direct-DB pgvector access was removed 2026-11-09 — the backend owns hybrid retrieval, reranking (OpenAI Decisions API), and chunking.
- **Structured outputs:** OpenAI Responses API with JSON Schema (`response_format: json_schema`) — schema loaded from DynamoDB, sanitized for API compatibility at runtime
- **Agent config:** DynamoDB `chatkit_production_config` table (system prompts, model, schema, namespaces)
- **Infrastructure:** AWS — ECS Fargate, EventBridge, DynamoDB, S3, SES, CloudWatch
- **IaC:** Terraform (`infra/terraform/`)
- **Data modeling:** pydantic

## Architecture (pipeline order)

1. **Scheduler** (EventBridge cron, 6h) → triggers ECS Fargate task
2. **Runner** (`spike.py`) loads `targets.json`, orchestrates pipeline
3. **Scraper** fetches pages (Playwright or requests fallback)
4. **Extractors** (pluggable): `link_collector_v1`, `keyword_links_v1`, `naic_meetings_v1`, `naic_events_v1`
5. **Diff Engine** — SHA256 fingerprint comparison against DynamoDB state
6. **HTML snapshot** — before/after stripped HTML saved to S3 (`page_change_s3.py`)
7. **page_change_agent** — LLM agent compares before/after HTML, outputs structured alert JSON
   - Config key: `chat:web-tracking-agent` in DynamoDB
   - **Output schema is fully dynamic** — driven by `output_json_schema` (a JSON Schema object) stored in DynamoDB. The Bubble admin UI writes this field. Updating it there changes agent output, S3 storage, and dashboard columns with no code deploy.
   - **Multi-alert support:** `extract_page_change()` returns `list[dict]`. If the DynamoDB schema wraps fields in a top-level `alerts` array, the agent can produce multiple alert rows from one page change. `_unwrap_alerts()` extracts the list; if no `alerts` wrapper, the single dict is wrapped in a one-element list for backward compatibility.
   - **`agent_call_id`:** Each invocation of `extract_page_change()` generates a UUID (`agent_call_id`) stamped on every alert dict. This identifies which specific agent call produced each row (more granular than `run_id`, which is shared across the entire 6-hour pipeline run). Stored in every alert and doc extraction row.
   - **`config_hash`:** MD5 of `system_prompt + model`, computed once per run via `get_config_hash()`. Stored on every alert row. Used by dashboard rerun feature to detect config changes.
   - At runtime, `_sanitize_schema_for_openai()` strips keywords the OpenAI Responses API rejects in strict mode (`$schema`, `oneOf` → replaced with `{"type":"string"}`, unsupported `format` values like `uri`)
   - Falls back to extracting a JSON schema from a ` ```json ``` ` fenced block in `instructions`, then to `_FALLBACK_OUTPUT_SCHEMA` hardcoded in the file
   - **New flat schema** (as of May 2026): all fields are top-level — no nested `events[]`, `library_items[]`, `agenda_items[]` arrays. Event/agenda/library-item fields are prefixed at the top level (`event_title`, `agenda_item_title_chronicle_topics`, `library_item_preliminary_title`, etc.)
8. **document_agent** — For all alert types that produce a library item (12 types, see `DOCUMENT_ALERT_TYPES` in `document_agent.py`; `Other` is intentionally excluded since it typically fires on low-signal label/metadata edits that don't warrant an extraction call), extracts structured metadata via two paths:
   - Config key: `chat:document-data-extraction` in DynamoDB
   - **When `CHATKIT_INTERNAL_API_KEY` is set (two-step enforcement):** Step 1 — `extract_document_data()` ingests the PDF text to the backend via `ingest_document()` (with `ttl_hours=24*30`), waits for ready, then calls `document_text(doc_uuid)` to fetch the stitched source and inlines it in the user prompt under `=== SOURCE DOCUMENT ===`. The Agents SDK runs with a single `search_knowledge_base` tool that POSTs to `/knowledge/search` for the reference collections only (Chronicles, Newsreels, Guidelines, orgs). Step 2 — `chat_json()` with Structured Outputs reformats the free-text into the exact DynamoDB schema.
   - **When backend unreachable:** direct Responses API call with Structured Outputs (does NOT skip/return `{}` — always produces output, just without knowledge base context)
   - Output: fully dynamic — whatever fields the DynamoDB config `output_json_schema` instructs. Stored as `document_extractions_table.jsonl` in S3.
   - N/A guard: items where the library item name is `"N/A"`, `"N/A."`, `"-"`, or `""` are skipped via `_item_has_real_name()` (no extraction attempted)
   - A `## Output Format` JSON suffix is automatically appended to the DynamoDB `instructions` at runtime — do not add JSON format requirements to the DynamoDB config itself
9. **Alert storage** (`alert_s3.py`) — writes `alerts_table.jsonl`, per-run `alerts.json`, `alerts_table.xlsx` to S3. Excel export serializes list/dict cell values to JSON strings before writing to openpyxl cells.
10. **Recording matcher** (`bubble/recording_matcher.py`) — after the document agent loop, matches meeting alerts to mp3 recordings in `recordings-bucket-1` S3 bucket for recordings already present at pipeline time. Stamps `recording_s3_key` on matching alerts. Recordings uploaded after the pipeline run are handled by the event-driven path (step 10b).
10b. **Event-driven recording ingest** — when a new mp3 lands in `recordings-bucket-1`, an S3 event triggers `infra/lambda/recording_ingest_trigger/handler.py`, which fires an ECS RunTask with `RECORDING_S3_KEY` set. `spike.py` enters `_run_recording_ingest()` mode: (1) transcribes the mp3 (idempotent), (2) stamps `recording_s3_key`/`transcript_s3_key`/`ingest_status="pending"` on matching alert rows, (3) runs transcript doc extraction and appends to `document_extractions_table.jsonl`, (4) creates one synthetic "New Meeting Transcript Available" alert row per unique event title with `bubble_action` stamped and appends to `alerts_table.jsonl`. Idempotency guard: if a synthetic row with the same `transcript_s3_key` already exists, step 4 is skipped entirely. Nothing is auto-ingested — all publishing goes through the content gate.
11. **Transcriber** (`bubble/transcriber.py`) — for alerts that got a `recording_s3_key`, converts the mp3 to a timestamped transcript via OpenAI Whisper (`verbose_json`, `[HH:MM:SS] text` per segment) and stores it in the artifacts bucket under `transcripts/`. The transcriber returns the S3 key; `spike.py` stamps `transcript_s3_key` and `ingest_status: "pending"` on the alert after the call returns. Idempotent.
11b. **Transcript document extraction** (`spike.py`) — immediately after transcription, downloads the transcript from S3 and runs it through the `document-data-extraction` agent (same config as library items). The `transcript_s3_key` is passed as `document_url` so `doc_uuid_for()` derives a stable UUID from it, `ingest_document()` uploads the text to the backend, and `document_text()` returns the stitched source for inlining. Stamps `extraction_source: "transcript"` and `transcript_s3_key` on the result. Appended to `ev["__doc_extraction"]` so it lands in `document_extractions_table.jsonl` alongside library item extractions.
11c. **Synthetic transcript alert** (`spike.py`) — after transcript doc extraction, creates a new alert row deterministically for every alert that received a `transcript_s3_key`. Sets `alert_type = "New Meeting Transcript Available"`, copies all event fields from the parent alert, and assigns a fresh `agent_call_id`. Appended to `ev["__agent_output"]` before the bubble_sync_classifier so it receives `bubble_action` (`event: update`, `library_item: create` with type "Meeting Transcript"). `ingest_status = "pending"` so the content gate shows the Transcript → Newsreel section.
12. **Ingest gate** — no auto-ingest into the newsreel knowledge base. `ingest_status` field controls lifecycle: `null` (not eligible), `"pending"` (awaiting dashboard approval), `"approved"` (ingested), `"rejected"` (dismissed). Dashboard shows approve/edit/reject buttons per row. See `storage/ingest_actions.py`.
13. **Bubble sync classifier** (`bubble/bubble_sync_classifier.py`) — after the recording/transcript pipeline, runs `classify_alert()` on every alert and stamps `bubble_action` on applicable rows. Pure function, no API calls. Maps `alert_type` → `{event, library_item, agenda_items}` with full field previews (event_preview, library_item_preview, agenda_item_previews). `enrich_with_doc_extraction()` merges doc extraction metadata into both previews: description/date/type on library item CREATE; chronicle topics (`topics___dt_list_custom_newsreel_update`) on both event and library item previews. Irrelevant alerts (No Meaningful Change, carousel) get no field set.
15. **Notifier** (SES) — email summary of changes

## Rerun mode

When `RERUN_RUN_ID` and `RERUN_TARGET_ID` environment variables are set, `spike.py` enters rerun mode instead of the normal pipeline:

1. Fetches stored before/after HTML from S3: `pages/<target_id>/YYYY/MM/DD/<run_id>/`
2. Re-runs `extract_page_change()` + `extract_document_data()` with current DynamoDB config. Uses the **original row's `run_timestamp`** as the pipeline run time injected into the agent context (not the current wall-clock time) — ensures the agent sees the same temporal context as the original run.
3. Writes result to:
   - `alerts/reruns/<run_id>/<target_id>/result.json` — target-wide reruns (alerts mode, or docs mode with no URL)
   - `alerts/reruns/<run_id>/<target_id>/doc_<url_hash>.json` — single-document doc reruns (when `RERUN_LIBRARY_ITEM_URL` is set)
4. The dashboard handles Accept (patches JSONL) or Discard (deletes result)

Result schema includes: `run_id`, `target_id`, `rerun_timestamp`, `config_hash`, `original_rows`, `rerun_rows`, `doc_original_rows`, `doc_rerun_rows`.

`RERUN_MODE` env var controls which agents run: `"alerts"` (page_change_agent only), `"docs"` (document_agent only), `"both"` (default).

**Per-document scoping** (docs mode): when `RERUN_LIBRARY_ITEM_URL` is set, `_run_rerun` filters `library_items` by exact URL match and only re-extracts that one document. The S3 result key uses `_rerun_result_key()` which switches to a URL-hashed path, so parallel reruns for different documents on the same (run_id, target_id) don't clobber each other. Without the env var, the legacy target-wide behavior + shared `result.json` path is used.

Full spec: `docs/rerun-feature.md`

## Key directories

- `storage/` — State store (DynamoDB prod / `state.json` dev), S3 for HTML snapshots, alerts, changelogs
- `bubble/` — RAG agents (`page_change_agent.py`, `document_agent.py`), recording matcher, transcriber, newsreel ingest, Bubble sync classifier and executor, ChatKit backend client
- `bubble/doc_extraction_ingest.py` — thin HTTP client for the ChatKit backend's `/knowledge/*` endpoints: `ingest_document()`, `wait_until_ready()`, `document_text()`, `search()`
- `bubble/page_snapshot_ingest.py` — ingests before/after HTML snapshots into the knowledge base via `ingest_document` with deterministic uuid per `(agent_call_id, side)`, 30-day TTL. Returns namespaces the agent appends to its search scope so `Updated:` labels in `<a title=...>` attributes are reachable via `search_knowledge_base`. Called from `document_agent.py` and `eval/doc_eval_agent.py`.
- `scrape/` — HTML content extraction, PDF metadata, page chunking
- `config/` — RunSpec (CLI > env > defaults), chatkit DynamoDB config loader
- `scripts/` — Deploy, backfill scripts
- `prompts/` — Prompt context files injected into agent user messages. `org_tree.txt` is a static fallback org hierarchy tree (dash-depth format, 140+ orgs); live data is fetched from Bubble API via `bubble/org_tree.py`
- `infra/terraform/` — ECS Fargate, EventBridge, DynamoDB, S3, IAM
- `tests/` — Unit/integration tests
- `docs/` — Feature specs (rerun feature)
- `debug/` — AI debug artifacts (gitignored)

## Key entry points

- `spike.py` — Main pipeline orchestrator (normal mode + rerun mode + manual_chunk mode)
- `targets.json` — Target URL config with extract rules per URL
- `config/run_spec.py` — RunSpec: single source of truth for runtime behavior
- `config/chatkit_config.py` — Loads agent config from DynamoDB `chatkit_production_config`
- `bubble/page_change_agent.py` — Page change RAG agent (`extract_page_change()`, `_unwrap_alerts()`, `_sanitize_schema_for_openai()`, `get_config_hash()`). Inlines before/after HTML directly in the user message — no truncation, no vectorization (the whole point of the WTA is to diff the HTML). Resolves organizations via `search_knowledge_base` against the `ba:organizations` namespace; the legacy `=== ORGANIZATION REFERENCE ===` injection is gated behind `ORG_TREE_INJECTION_ENABLED` (default **false**). The `=== TARGET CONTEXT ===` block injected into the user message contains only three fields: `Page`, `URL`, and `Pipeline run time (Eastern)`. The `Org path`, `Group`, and `Tags` fields were removed because they contained internal routing codes (e.g. `NAIC > E > Working Groups`, `working_group`, `committee:E`) that leaked into agent context and caused bad org outputs. The hardcoded alert type list that was previously appended to the user message was also removed — alert type definitions belong in the DynamoDB system prompt only.
- `bubble/document_agent.py` — Document extraction RAG agent (`extract_document_data(document_name, document_url, pdf_text=None, text_limit=None, alert_context=None)`, two-step enforcement). The source document is uploaded via `ingest_document(..., ttl_hours=24*30)`, then the stitched text is fetched via `document_text(doc_uuid)` and **inlined** in the user prompt under `=== SOURCE DOCUMENT ===`. Before/after HTML snapshots are ingested via `bubble/page_snapshot_ingest.py` into `page-snapshot:<uuid>` namespaces (deterministic uuid per `(agent_call_id, side)`, 30-day TTL) and **appended to the agent's search scope** — NOT inlined in the prompt. The Agents SDK runs with a single in-closure `search_knowledge_base` tool that calls the backend `search()` endpoint — scoped to the reference namespaces plus the page-snapshot namespaces. `document-extraction:*` is filtered out because the source is inlined. Also used for transcript extraction — `extraction_source: "transcript"` distinguishes those rows in `document_extractions_table.jsonl`. `_fetch_pdf_text(url)` handles semicolon-separated multi-URL `library_item_url` fields; `_render_html_to_pdf_text()` handles newsroom-article URLs via Playwright `page.pdf()` with a `requests`+BeautifulSoup fallback. Resolves organizations via `search_knowledge_base` against `ba:organizations`; the legacy `=== ORGANIZATION REFERENCE ===` injection is gated behind `ORG_TREE_INJECTION_ENABLED` (default **false**). When `alert_context` is provided, injects `=== ALERT CONTEXT ===` with `organization`, `alert_type`, `event_title`, and `event_start_date_time`. The `trace_id` passed to backend `search()` is the extraction's `agent_call_id`, grouping all its searches in the admin dashboard's retrieval-traces view. Degraded path (backend unreachable) inlines the full HTML under `=== PAGE CHANGE CONTEXT ===` since no search tool is available.
- `bubble/openai_client.py` — OpenAI Responses API client; `chat_json()` supports both `json_object` and `json_schema` structured outputs
- `bubble/recording_matcher.py` — `find_recording(event_title, event_start_date_time)` matches alerts to mp3s in `recordings-bucket-1` by date + acronym scoring
- `bubble/transcriber.py` — `transcribe_recording(recording_s3_key)` converts mp3 → timestamped text via Whisper (`verbose_json` format, `[HH:MM:SS] text` per segment), stores under `transcripts/` in artifacts bucket. `format_with_timestamps(segments)` shared with backfill script.
- `bubble/newsreel_ingest.py` — `ingest_for_newsreel(document_url, filename)` pushes documents and transcripts (via presigned URL) to ChatKit newsreel-generation knowledge base
- `bubble/org_tree.py` — `get_org_tree()` fetches the live Bubble org hierarchy (143 orgs) and formats it as dash-depth text. 30-min in-process cache; falls back to `prompts/org_tree.txt`. Only invoked when `ORG_TREE_INJECTION_ENABLED=true` (default `false`); orgs are normally resolved via the `ba:organizations` search namespace.
- `bubble/bubble_sync_classifier.py` — `classify_alert(alert) → BubbleSyncPlan` pure classifier; stamps `bubble_action` on applicable alerts. `enrich_with_doc_extraction(bubble_action, extraction)` merges doc extraction fields (description, date, type, chronicle topics) into previews. No I/O. `_build_agenda_previews()` reads flat-schema `agenda_item_title_chronicle_topics` first, falls back to old nested-schema `agenda_item_title_and_chronicle_topics` for backward compatibility. Includes `status` field on each agenda item preview: `"New"` = create a new Bubble agendaitem object, `"Existing"`/`"Updated"` = look up by title and link. `_parse_doc_extraction_agenda_items()` parses the doc extraction `agenda_items` string — handles both `; ` and `|` as entry separators. See `bubble_action` field docs below.
- `bubble/bubble_sync.py` — Python Bubble sync executor used by the pipeline. **The dashboard does not call this module** — the dashboard runs the TypeScript executor `dashboard/src/lib/bubble-sync-eidarix.ts` in-process in the Next.js ECS task. Both implementations mirror each other. `sync_alert(agent_call_id, action="all")` with `action` ∈ `{"all", "agenda_items", "library_item", "event"}`. Processing order: (1) resolve agenda items — status-aware (`"New"` creates via `wf/create-agenda-item/`; `"Existing"`/`"Updated"` looks up by title); failures raise `RuntimeError`. (2) library item via `wf/create-library-item/`. (3) event via `wf/create-event` or `wf/update-event`; UPDATE payload contains only linking fields (`id, alert_id, space_id, chronicle_topics, agenda_items, agenda`). All writes go through Eidarix workflow endpoints with `space_id = CONTENT_SPACE_ID`. `EIDARIX_VERSION` env var controls Bubble database (`version-test` vs `version-live`; prod defaults to `"live"`). Payload spec: `docs/bubble_sync_payload_spec.json`.
- `storage/alert_s3.py` — Alert storage, flat/nested schema detection, Excel export, `patch_jsonl_row()` utility
- `storage/ingest_actions.py` — Ingest gate: `approve_transcript_ingest()`, `approve_document_ingest()`, `ingest_manual_document_url()`, `reject_ingest()`, `generate_presigned_url()`, `generate_presigned_upload_url()`
- `infra/lambda/` — Lambda functions for event-driven triggers (recording ingest, bubble sync, health canary). The former `validate_config_sync` Lambda was deleted on 2026-10-29 — the ChatKit backend's field registry now owns `chatkit_production_config` schema + `_column_registry` directly, so no dashboard-side validator is needed.

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/deploy.sh` | Build Docker, push ECR, terraform apply |
| `scripts/backfill_alerts.py` | Reprocess stored page changes through agents (re-runs both agents) |
| `scripts/backfill_document_extractions.py` | Backfill `document_extractions_table.jsonl` from stored `agent_output.json` (safe, never touches `alerts_table.jsonl`). Handles flat schema library items, list-format `agent_output.json`, and `alerts` array wrapper. |
| `scripts/rebuild_alerts_table.py` | Rebuild `alerts_table.jsonl` from stored `agent_output.json` files (no agent re-run, useful for dedup/schema fixes) |
| `scripts/backfill_bubble_action.py` | Backfill `bubble_action` on existing `alerts_table.jsonl` rows using the classifier (idempotent, `--force` to re-classify). Enriches with doc extraction keyed by `library_item_url` (not `agent_call_id`) — required for multi-document page changes where one call_id produces N library items. |
| `scripts/backfill_recordings.py` | Backfill `recording_s3_key` on alerts that have `event_title`+`event_start_date_time` but no recording match yet. Two-pass: standard find_recording() then filename-based fallback for rows where event date is N/A. |
| `scripts/backfill_transcripts.py` | Backfill `transcript_s3_key` on alerts that have a `recording_s3_key` but haven't been transcribed yet. Groups by `agent_call_id` to transcribe once per recording. `--local` flag uses local Whisper model. |
| `scripts/run_local_doc_qa.py` | One-off local doc QA runner. Loads `CHATKIT_INTERNAL_API_KEY` from Secrets Manager/SSM in-process, then invokes `eval.run_doc_eval.run()` on the acids in the supplied file. |
## QA Evaluation Pipeline

Ongoing automated evaluation of web tracking agent output. Triggered after each Newsreel publication. Evaluates rows where the associated event or library item exists in Bubble (i.e., `bubble_action` is set). One agent call per alert row.

**Entry point:** `python3 -m eval.run_eval`

| Flag | Description |
|------|-------------|
| `--limit N` | Evaluate last N eligible rows (default 50) |
| `--since TIMESTAMP` | Only rows at or after Unix timestamp |
| `--agent-call-ids a,b` | Evaluate specific rows by agent_call_id |
| `--dry-run` | Print selected rows without calling agent |

**Pipeline modules (`eval/`):**

| File | Purpose |
|------|---------|
| `eval/run_eval.py` | Entry point — calls `_load_secrets()` to load OpenAI + DB creds from SSM, then orchestrates full eval run. `main()` wraps the `run()` call in a top-level try/except that logs the full traceback before re-raising, so eval failures are visible in CloudWatch. |
| `eval/row_selector.py` | Loads eligible rows. No publication gate — every row in `alerts_table.jsonl` is a candidate. Transcript rows (`alert_type == "New Meeting Transcript Available"`) are excluded. Rows are sorted by `run_timestamp` descending, deduped by `agent_call_id`, limited at the call level, then expanded to include all sibling rows per selected call. `--agent-call-ids` bypasses filtering entirely and returns the requested rows directly. **Prior behavior** (removed 2026-11-05): the selector queried Bubble for existing calendar/library items and only graded rows whose Bubble record existed — this gated QA on publication state and caused bulk runs to keep grading the same Aug backlog once newer rows outran their Bubble sync. |
| `eval/html_fetcher.py` | Fetches before/after HTML snapshots from S3 for each row |
| `eval/eval_agent.py` | Calls `chat:web-extraction-qa-agent` (DynamoDB config) — one call per group (all rows sharing an `agent_call_id`), returns per-row per-field scores keyed by `eval_row_key`. All rows from the same HTML page diff are evaluated together. HTML snapshots are inlined directly in the prompt (same pattern as the WTA itself since the QA grader needs the exact diff). Resolves orgs via `search_knowledge_base` against `ba:organizations`; org tree injection is gated behind `ORG_TREE_INJECTION_ENABLED` (default false). The `search_knowledge_base` tool calls the backend `/knowledge/search` for reference namespaces only. Two-step pattern: Step 1 — Agents SDK free-text analysis with the HTTP-backed search tool; Step 2 — `chat_json()` formats into per-row per-field JSON keyed by `eval_row_key`. |
| `eval/result_store.py` | Upserts results into `alerts/eval_results_table.jsonl` keyed by `eval_row_key`. Single-row calls: key = `agent_call_id` (backward compatible). Sibling rows: key = `agent_call_id|library_item_url`. `delete_eval_result()` removes all entries for a given `agent_call_id`. |

**Agent config:** `chat:web-extraction-qa-agent` in DynamoDB `chatkit_production_config`. System instructions and rubric configured in Bubble admin, same pattern as `web-tracking-agent`.

**Storage:** `alerts/eval_results_table.jsonl` — upsert keyed by `eval_row_key`. Single-row agent calls use `agent_call_id` as the key (backward compatible with prior stored results). Sibling rows (multiple rows sharing one `agent_call_id`) use `agent_call_id|library_item_url` as the key so each gets its own entry. Re-running QA replaces prior entries. Dashboard DELETE removes all entries for a given `agent_call_id` via `eval/result_store.delete_eval_result()`.

**Output schema per row:** original alert fields + `eval_run_id`, `eval_timestamp`, `eval_scores` (dict of field → `{score, reasoning}`), `overall_summary`.

**ECS trigger:** dashboard fires `POST /api/eval { agent_call_id }` → ECS RunTask CMD override `["python", "-m", "eval.run_eval", "--agent-call-ids", "<id>"]`. Works because `entrypoint.sh` `exec "$@"` when `$1 == python`. Must use `-m eval.run_eval` (module form), not `eval/run_eval.py` (file form), to resolve internal imports correctly.

## Document Extraction QA Pipeline

Parallel QA pipeline for `document_extractions_table.jsonl` rows. Evaluates extraction output after inlining the complete source document in the prompt — same pattern as the extraction agent (upload via `ingest_document(..., ttl_hours=24*30)` → `document_text(doc_uuid)` → inline under `=== SOURCE DOCUMENT ===`). Reruns and QA reuse the same `doc_uuid` (deterministic from `library_item_url`), so the ingest is a cached no-op when the extraction agent already uploaded the document.

**Entry point:** `python3 -m eval.run_doc_eval`

| Flag | Description |
|------|-------------|
| `--limit N` | Evaluate last N eligible rows (default 20) |
| `--agent-call-ids a,b` | Evaluate specific rows by agent_call_id |
| `--dry-run` | Print selected rows without calling agent |

Eligible rows: excludes `extraction_source == "transcript"` rows and rows with no real `library_item_url`.

**Pipeline modules (`eval/`):**

| File | Purpose |
|------|---------|
| `eval/run_doc_eval.py` | Entry point — deduplicates by `(library_item_url, eval_row_key)` keeping most recent row, groups rows by `(doc_extraction_id, document_url_web_tracking_agent)`, evaluates each group in a **single agent call** (all agenda-item rows together — no batch splitting). Each group runs in a fresh subprocess. `_is_placeholder_scores()` guard handles the edge case where the agent doesn't reach all rows in a group — those rows receive empty scores rather than false results. |
| `eval/doc_eval_agent.py` | QA agent — fetches PDF text via `_fetch_pdf_text()`, ingests to the backend, pulls stitched source text via `document_text()`, and **inlines** it in the prompt under `=== SOURCE DOCUMENT ===`. Before/after HTML snapshots are ingested via `bubble/page_snapshot_ingest.py` (reusing the extraction's `agent_call_id` so the ingest is a cached no-op) and added to the agent's search scope — NOT inlined. Agent's `search_knowledge_base` tool POSTs to `/knowledge/search` for the reference namespaces + the page-snapshot namespaces. Uses `chat:document-extraction-qa-agent` DynamoDB config; gates on `CHATKIT_INTERNAL_API_KEY`. `_EXCLUDE_KEYS` controls which fields are omitted from the extraction JSON sent to the QA agent — excludes pipeline metadata (`run_id`, `run_timestamp`, `target_id`, `source_url`, `agent_call_id`, `config_hash`, `last_rerun_at`), library item identity fields (`library_item_title`, `library_item_url`, `library_item_file_name`), and eval bookkeeping (`eval_run_id`, `eval_timestamp`, `eval_scores`, `eval_row_key`, `extraction_source`). `_ALERT_INCLUDE_KEYS = {"organization", "alert_type", "event_title", "event_start_date_time"}` — matches what `document_agent.py` injects via `alert_context`. |
| `eval/doc_result_store.py` | Upserts results into `alerts/doc_eval_results_table.jsonl` keyed by `eval_row_key`. Before upserting, removes ALL existing entries for the same `(agent_call_id, library_item_url)` pair regardless of key format — prevents stale entries under old keys from persisting alongside new ones. Also stamps `eval_row_key` back onto source rows in `document_extractions_table.jsonl` so the dashboard can do a direct field lookup instead of recomputing the key. |
| `eval/doc_score_reporter.py` | Reads all of `doc_eval_results_table.jsonl`, computes overall and per-field accuracy (correct / total, partials tracked separately), writes `alerts/doc_eval_score_report.json` to S3. Called automatically at the end of every `run_doc_eval.run()`. Non-fatal: a failure logs a warning and does not propagate. Fields sorted ascending by accuracy (weakest first). |

**Agent config:** `chat:document-extraction-qa-agent` in DynamoDB `chatkit_production_config`.

**Storage:** `alerts/doc_eval_results_table.jsonl` — same upsert pattern as `eval_results_table.jsonl`.

**ECS trigger:** dashboard fires `POST /api/eval/documents { agent_call_id }` → ECS RunTask CMD override `["python", "-m", "eval.run_doc_eval", "--agent-call-ids", "<id>"]`.

## Agent configuration (DynamoDB)

### Write flow (owned by ChatKit backend)

1. Admin edits labels / instructions / schema fragments in **Bubble admin UI**
2. The ChatKit backend persists field metadata to **`chatkit_production_field_registry`** (keyed by `agent_id`, stores stable `key`, `label`, `prior_keys`, `kind`, etc. — the authoritative source of truth)
3. The ChatKit backend derives `output_json_schema` + `output_requested_values` from the field_registry and writes them atomically to **`chatkit_production_config`**
4. Our agents and the dashboard **read** `chatkit_production_config` only — nothing on our side mutates it (the former `validate_config_sync` Lambda was removed 2026-10-29)

**Stable IDs**: the `key` field in `chatkit_production_field_registry` is the stable storage ID that never changes across label renames. `prior_keys` carries every past storage key so rows written under old names normalize cleanly. Our side reads this registry in `storage/field_normalizer.py` to build the alert / doc-extraction normalization maps.

### Config table

Table: `chatkit_production_config` (env: `CHATKIT_CONFIG_TABLE`)
Key format: `chat:{chat_id}`

### `web-tracking-agent` (page_change_agent)

| Field | Type | Description |
|-------|------|-------------|
| `instructions` | String | System prompt for the agent |
| `model` | String | OpenAI model ID (e.g. `gpt-5.4`) |
| `reasoning_effort` | String | `low` / `medium` / `high` — passed via `ModelSettings(reasoning=Reasoning(effort=...))` |
| `pgvector_namespaces` | List | Namespaces for knowledge base search. Current value: `["bubble-data", "art-chronicles", "art-newsreels", "naic-guidelines", "naic-proceedings", "international-guidelines", "ratings-agencies"]` |
| `output_json_schema` | Map | **JSON Schema object** (draft-07) defining the exact output fields. Written by Bubble admin sync. Used for OpenAI Structured Outputs (`response_format: json_schema`). May have top-level `alerts` array wrapper for multi-alert support. |
| `output_json_schema_name` | String | Schema name for the API call (e.g. `"web_tracking_alert"`) |
| `output_json_schema_strict` | Bool | Whether to use strict mode (default: `true`) |
| `output_json_schema_hash` | String | SHA256 hash of the schema, used for change detection |
| `output_requested_values` | List | Ordered list of human-readable column labels (one per field in `output_json_schema.required`). Written by Bubble admin sync. Consumed by the dashboard `/api/schema` to render column headers. |

The `output_json_schema.required` array defines the **ordered** list of field names. The dashboard zips this with `output_requested_values` to produce human-readable column headers. Editing either in the Bubble admin Values tab automatically updates agent output AND dashboard columns — no code deploy.

**Schema with alerts wrapper:** When the schema has a top-level `alerts` array wrapper, `output_json_schema.required` is `["alerts"]` (just the wrapper). The dashboard's `/api/schema` drills into `alerts.items` to get the inner schema's `required` array (the actual 21 field names) for column derivation. The `output_requested_values` list corresponds to the inner fields, not the wrapper.

**Schema sanitization:** OpenAI Structured Outputs rejects certain JSON Schema keywords in strict mode. `_sanitize_schema_for_openai()` in `page_change_agent.py` removes them at runtime:
- `$schema` declaration → dropped
- `oneOf` at any property level → replaced with `{"type": "string"}`
- Unsupported `format` values (e.g. `"uri"`) → dropped (supported: `date-time`, `time`, `date`, `duration`, `email`, `hostname`, `ipv4`, `ipv6`, `uuid`)

### `document-data-extraction` (document_agent)

| Field | Description |
|-------|-------------|
| `instructions`, `model`, `reasoning_effort`, `pgvector_namespaces` | Same as above |
| `output_json_schema` | JSON Schema object defining the output fields. Fully dynamic — stored in DynamoDB, drives both agent output and `document_extractions_table.jsonl` columns. |
| `output_json_schema_name`, `output_json_schema_strict`, `output_requested_values` | Same semantics as `web-tracking-agent` fields |

## Current output schema fields

The authoritative live schema is in DynamoDB — `output_json_schema` + `output_requested_values` on each config row. Never trust a snapshot in this doc; it drifts. To inspect current state:

```bash
AWS_PROFILE=bridgeway aws dynamodb get-item \
  --table-name chatkit_production_config \
  --key '{"config_key":{"S":"chat:web-tracking-agent"}}' \
  --output json \
  | jq '.Item.output_json_schema' | head -200
```

High-level shape (as of 2026-10-30, subject to change via Bubble admin):

- **Flat top-level schema** — no nested `events[]` / `library_items[]` / `agenda_items[]` wrappers. Event / library / agenda data lives in prefixed top-level fields (`event_title`, `agenda_item_title`, `library_item_preliminary_title`, etc.).
- **Multi-alert wrapper**: `required: ["alerts"]` with `properties.alerts` as an array of inner objects. The dashboard's `/api/schema` drills into `alerts.items` to get the 21-22 inner field names for column derivation.
- **Compound array fields** (status + value): `agenda_item_title`, `agenda_item_title_official`, `agenda_item_standardized_id`, `agenda_item_official_id` are each arrays of `{status, <value>}` objects. Status enum is `["New", "Existing", "N/A"]`.
- **Chronicle Topic** is a standalone top-level string field (split from the old compound in 2026-10-29). Agents output chronicle data in `chronicle_topic` only; the inside of `agenda_item_title` no longer carries nested chronicle_topics.
- **Binary enums for relevance**: `is_the_alert_relevant_for_an_art_newsreel_article.status` and `is_the_document_relevant_for_a_future_newsreel_article.status` are both `["Yes", "No"]` (dropped "Additional review needed" on 2026-10-29).
- **`organization`**: top-level `string[]` — resolved by the agent via `search_knowledge_base` against the `ba:organizations` namespace. The legacy injected-tree path (`bubble/org_tree.py` → `=== ORGANIZATION REFERENCE ===` block) is gated behind `ORG_TREE_INJECTION_ENABLED` (default `false` across all 4 agents since 2026-11-03 commit `9efc69d`) and is retained only as a manual kill switch.

Pre-May-2026 rows in `alerts_table.jsonl` use the old nested-array format (`events[]`, `library_items[]`, `agenda_items[]`). The dashboard's `FIELD_ALIASES` in `AlertsTable.tsx` + `storage/field_normalizer.py` + the field_registry's `prior_keys` resolve old rows to current column names on display.

## Flat vs nested schema detection

`_build_rows_for_single_alert()` in `alert_s3.py` detects the schema format:
- Checks if `events`, `library_items`, or `agenda_items` are present as non-empty lists
- **If no nested arrays found** → flat schema path: all agent output fields stored verbatim as top-level keys, returns one row
- **If nested arrays found** → backward-compat path: explodes library items into separate rows, flattens first-item fields with `event_*`/`agenda_item_*` prefixes

## Common commands

```bash
make install                          # create venv, install deps
make run                              # run the pipeline
python3 spike.py                      # minimal local run
python3 spike.py --target-ids <id>    # run single target
python3 spike.py --simulate-change --target-ids <id>  # inject fake diff
python3 scripts/backfill_alerts.py    # reprocess stored page changes through agents
python3 scripts/backfill_alerts.py --limit 10 --dry-run  # preview without writing
python3 scripts/backfill_document_extractions.py --limit 5 --dry-run  # backfill doc extractions only
python3 scripts/rebuild_alerts_table.py  # rebuild from stored agent_output.json (no re-run)

./scripts/deploy.sh                   # build Docker, push ECR, terraform apply
./scripts/deploy.sh --run-task        # + trigger one ECS task immediately
```

## Key environment variables

**State & storage:**
- `STATE_BACKEND=local|dynamodb` — state backend
- `STATE_TABLE` — DynamoDB state table name
- `PAGE_CHANGE_SNAPSHOT_BUCKET` — S3 bucket for before/after HTML
- `CHANGELOG_BUCKET` — S3 bucket for alerts and changelogs
- `CHANGELOG_PREFIX` — S3 prefix (default `changelog/`)

**Agents:**
- `PAGE_CHANGE_AGENT_ENABLED=true` — enable RAG agents (required for alert pipeline)
- `CHATKIT_INTERNAL_API_KEY` — required for the backend `/knowledge/*` endpoints (ingest + source-text fetch + search). Gates the four agents' retrieval paths; absence triggers the direct-API fallback with no KB access.
- `CHATKIT_CONFIG_TABLE` — DynamoDB config table (default: `chatkit_production_config`)
- `OPENAI_API_KEY` — required for agents and embeddings
- `OPENAI_FETCH_FROM_SSM=true` — load OpenAI API key from AWS SSM

**Rerun mode:**
- `RERUN_RUN_ID` — run_id to re-evaluate (set by ECS RunTask override)
- `RERUN_TARGET_ID` — target_id to re-evaluate (set by ECS RunTask override)
- `RERUN_MODE` — controls which agents run during rerun: `"alerts"` (page_change_agent only), `"docs"` (document_agent only), `"both"` (default). Set by ECS RunTask override from the dashboard.

**Manual chunk mode (manually uploaded transcripts):**
- `MANUAL_CHUNK_AGENT_CALL_ID` — agent_call_id of the alert row to update
- `MANUAL_CHUNK_TRANSCRIPT_S3_KEY` — S3 key of the uploaded .txt transcript file

**Email:**
- `SEND_EMAIL=true`, `FROM_EMAIL`, `TO_EMAILS`, `SES_REGION`

**Hardening:**
- `MAX_RETRIES=3` — fetch retries per target
- `BACKOFF_SECONDS=2` — retry backoff
- `DELAY_BETWEEN_PAGES=1` — seconds between target fetches

**Bubble/Eidarix sync:**
- `BUBBLE_API_URL`, `BUBBLE_API_KEY`, `EIDARIX_VERSION`

## Bubble sync fields

### `bubble_action` (set by classifier, stored on alert dict)

```json
{
  "bubble_action": {
    "event": "create" | "update" | null,
    "library_item": "create" | "update" | null,
    "agenda_items": true | false,
    "event_preview": {
      "title", "start_datetime", "end_datetime", "group", "url", "call_in",
      "match_key",                        // "{org} | {date}" — kept for compat
      "what_changes",                     // list of strings — kept for compat
      "fields": {"Title": "...", ...},    // display names → values for modal FieldTable
      "field_ids": {"title_text": ...},   // Bubble API field IDs → values for executor (org names, not IDs)
      "match_search": {"org": "...", "date": "YYYY-MM-DD"}  // UPDATE lookup criteria
    },
    "library_item_preview": {
      "title", "url", "filename", "type", "group",
      "what_changes",                     // kept for compat
      "fields": {"Name": "...", ...},     // display names → values
      "field_ids": {"name_text": ...},    // Bubble API field IDs → values
      "match_search": {"url": "...", "title": "..."}  // UPDATE lookup criteria
    },
    "agenda_item_previews": [{ "title", "status", "chronicle_topics", "official_title", "reference_id" }],
    "notes": "<alert_type>"
  }
}
```

Only set when `applicable=True`. Absent on No Meaningful Change / carousel alerts.
The `match_key` (`"{primary_org} | {date}"`) is how existing Events are looked up in Bubble.

The `fields` dict in each preview contains display-name → value pairs shown in the modal FieldTable.
The `field_ids` dict contains Bubble API field IDs → values used by the executor. Org names in `orgs__list_custom_organization` / `organizations_list_custom_organization` are plain text; the executor resolves them to Bubble `_id` values at write time.
The `match_search` dict is used by the executor to find existing records before UPDATE: `{"org": ..., "date": "YYYY-MM-DD"}` for calendaritem; `{"url": ..., "title": ...}` for libraryitem.

### `bubble_sync_status`

| Value | Meaning |
|-------|---------|
| absent/null | Not yet synced (or not applicable) |
| `"syncing"` | ECS task started; Bubble API calls in progress |
| `"synced"` | Successful Bubble API call |
| `"error"` | Executor raised an exception (`bubble_sync_error` has details) |

Dashboard shows the **Actions** button on rows with `bubble_action` set, which opens the Content Gate modal (`ContentGateModal.tsx`). The modal publishes to Bubble by hitting `POST /api/bubble/sync` which runs the TypeScript executor `src/lib/bubble-sync-eidarix.ts` **in-process** inside the dashboard ECS task — no Lambda, no ECS RunTask, no polling. Full technical reference: `dashboard/src/components/docs/content-gate.md`.

**Pre-write snapshots (TS executor only):** every PATCH against an existing Bubble record is preceded by `snapshotBubbleRecord(type, id)`, which GETs the live body and logs it to `/ecs/naic-dashboard` under `[bubble-sync][SNAPSHOT]` before the overwrite. Hooked at the 3 UPDATE sites (`calendaritem`, `libraryitem`, `agendaitem`). Only recovery path for overwritten fields since Bubble's Data API exposes no record-history endpoint. Out-of-band CLI PATCHes (not through `syncAlert()`) bypass this — snapshot manually with a Data API GET before any ad-hoc PATCH/DELETE. The Python `bubble/bubble_sync.py` executor does **not** currently snapshot; add the same safeguard there if it's ever rewired to a live flow.

The Python `bubble/bubble_sync.py` module is the equivalent executor for the pipeline-side use case (not reached from the dashboard today). The TypeScript executor is the current source of truth for all dashboard-triggered writes.

**Executor sequence in `bubble_sync.py`:**
1. Resolve org display names → Bubble `_id` values (via `/obj/organization`, space-constrained); resolve chronicle topic names → `_id` values (via `/obj/chronicletopic`, space-constrained)
2. If `agenda_items == true`: `_resolve_agenda_items()` reads from `bubble_action.agenda_item_previews` (primary source — reflects any user edits/deletions from the modal); falls back to flat `agenda_item_title_chronicle_topics` field for old rows without previews. `_strip_status_prefix()` removes "New - "/"Existing - "/"Updated - " prefixes from all titles and reference_ids before any Bubble API call. Status-aware: `"New"` items POST to `wf/create-agenda-item/`; `"Existing"`/`"Updated"` items search `/obj/agendaitem` by title. Failures raise `RuntimeError` — the whole sync stops and the row is marked `bubble_sync_status: "error"`. IDs collected as `agenda_item_ids` for steps 3 and 4. When `action == "agenda_items"`, patches `eidarix_agenda_item_ids` on the JSONL row and returns immediately.
3. If `library_item == "create"`: POST to `wf/create-library-item/`. Payload: `{title, url, chronicle_topics: [ids], organizations: [ids], type: type_id, agenda_items: [ids_from_step2], date: "YYYY-MM-DD", date_display, summary, alert_id, space_id}`. `type` ID resolved at runtime from `/obj/libraryitemtype`. Chronicle topics = union of all agenda item topics + doc extraction topics.
4. If `library_item == "update"`: find existing record by URL/title via Bubble Data API, PATCH via Data API (TODO: migrate to workflow)
5. If `event == "create"`: POST to `wf/create-event`. Payload: `{chronicle_topics: [ids], alert_id, space_id, agenda_items: [ids_from_step2], agenda: [library_item_id_from_step3], start_datetime, end_datetime, location_url, phone_and_access_code, type: type_id, organizations: [ids]}`. `type` ID resolved at runtime from `/obj/calendaritemtype`. `agenda` = list of library item Bubble IDs. `agenda_items` = list of agenda item Bubble IDs.
6. If `event == "update"`: **trimmed payload only** — `{id, alert_id, space_id, chronicle_topics: [ids], agenda_items?, agenda?}`. Does NOT re-send datetime/type/orgs (already on the record; sending extra fields causes Eidarix "length isn't valid" errors).
7. Patch JSONL with `bubble_sync_status: "synced"`, `bubble_event_id`, `bubble_library_item_id`, `eidarix_agenda_item_ids`

**Eidarix API:**
- Workflow base URL (writes): `https://eidarix.bridgewayanalytics.com/version-{version}/api/1.1/wf/`
- Data API base URL (reads/lookups):
  - Reference data (orgs, topics): non-versioned `https://eidarix.bridgewayanalytics.com/api/1.1/obj/` with `space` constraint = content space ID
  - Type lookups (`libraryitemtype`, `calendaritemtype`): versioned `https://eidarix.bridgewayanalytics.com/version-{version}/api/1.1/obj/` with no space constraint (versioned URL is already space-scoped)
- Auth: `Authorization: Bearer <BUBBLE_API_KEY>` (from `bubble/bridgemind.py`)
- **Space model:** there is one real data space — the **content space** `1768998437948x865417918648382000` — where all organizations, chronicle topics, library item types, and calendar item types live. Both the TS and Python executors scope reference lookups to it. Workflow payloads also set `space_id` to this value (Eidarix workflows resolve linked topic IDs against the payload's `space_id`). The `EIDARIX_VERSION` env var only controls which Bubble database/version receives the writes (`version-test` vs `version-live`).
- Current production setting: `EIDARIX_VERSION=live` (writes land in the live Bubble database).
- **File upload** (transcripts): `POST https://eidarix.bridgewayanalytics.com/version-{version}/fileupload` — multipart/form-data with `private=false`, `name=filename`, `contents=file_binary`
- Full payload spec and examples: `docs/bubble_sync_payload_spec.json`
- Agenda item field mapping: `title` ← `agenda_item_title_chronicle_topics[i].agenda_item_title`, `official_title` ← `agenda_item_title_official[i].official_title`, `reference_id` ← `agenda_item_standardized_id[i].standardized_id` (skips N/A)
- **`organizations` in workflow payloads** = Bubble `_id` strings (executor resolves names→IDs before calling workflow). **`chronicle_topics`** = Bubble `_id` strings. **`type`** (library item and event) = Bubble `_id` from type lookup endpoint.

**Eidarix workflow endpoints:**

| Endpoint | Method | Purpose | Status |
|----------|--------|---------|--------|
| `wf/create-agenda-item/` | POST | Create agenda item | ✅ Working |
| `wf/create-library-item/` | POST | Create library item | ✅ Working |
| `wf/create-event` | POST | Create calendar event | ✅ Working |
| `wf/update-event` | POST | Update calendar event (trimmed payload) | ✅ Working |
| `obj/libraryitemtype` | GET | Resolve library item type name → ID | ✅ Working (versioned URL, no space constraint) |
| `obj/calendaritemtype` | GET | Resolve event type name → ID | ✅ Working (versioned URL, no space constraint) |
| `obj/chronicletopic` | GET | Resolve chronicle topic name → ID | ✅ Working (non-versioned URL + content-space constraint) |
| `obj/organization` | GET | Resolve org name → ID | ✅ Working (non-versioned URL + content-space constraint) |

**Verified end-to-end against the live database (October 2026)** — agenda item + library item creation with topic/org/type resolution confirmed. See CloudWatch log group `/ecs/naic-dashboard` filter `[bubble-sync]` for live traffic.

## Conventions

- Extractors are pluggable; defined in target config `extract` array with `{type, extractor, params}`
- State is per-target (keyed by `target.id`)
- Failures on one target don't stop the run; errors collected in final report
- Agent configs live in DynamoDB — change system prompts/models/schema without redeploying
- Retrieval is hosted: all four agents POST to the ChatKit backend's `/knowledge/search` (hybrid semantic + lexical + Decisions-API reranker, server-side) rather than running retrieval client-side. Extraction + QA additionally inline the complete source document in the prompt via `GET /knowledge/document-extraction/{uuid}/text`.
- Chronicle topics are a fixed taxonomy in Bubble.io (87 nodes in "Chronicles" tree)
- **Storage is verbatim** — `alert_s3.py` stores exactly what the agent outputs. Null stays null, empty string stays empty. Dashes in the dashboard = agent output null or field absent. Do NOT add coercion (null->"N/A") to `_flatten_val` or `_build_table_rows` — it masks agent deviations from instructions.
- New flat schema (May 2026+): `_build_table_rows` stores all top-level agent output fields verbatim. The `events`, `library_items`, `agenda_items` nested-array handling is still present for backward compatibility with old rows but won't trigger for new-schema responses.
- **Multi-alert pipeline:** `extract_page_change()` returns `list[dict]`. `__agent_output` on change events is a list. `_build_table_rows()` accepts `list[dict] | dict`. Each row includes `agent_call_id` (UUID per agent invocation). The dashboard "Call ID" sticky column shows the last 8 chars of `agent_call_id`.
- **Two-step enforcement (document_agent):** Step 1 = Agents SDK with the HTTP-backed `search_knowledge_base` tool (free-text output), Step 2 = `chat_json()` with Structured Outputs to enforce the exact schema. The source document is inlined in the prompt before Step 1 runs, so Step 1's `search_knowledge_base` tool only reaches the reference collections.
- **ModelSettings for reasoning models:** Use `ModelSettings(reasoning=Reasoning(effort="low"))` — NOT `ModelSettings(reasoning_effort="low")` which silently ignores the parameter.
- Bubble.io integration is currently legacy; Bubble admin UI syncs `output_json_schema` + `output_requested_values` to DynamoDB
- Debug artifacts go to `debug/` directory (gitignored)
- **Dynamic org tree (kill-switch only):** `bubble/org_tree.py` → `get_org_tree()` fetches the live Bubble org hierarchy (30-min in-process cache, DFS traversal sorted by `Order`, `"-" * level + Name` format) and falls back to `prompts/org_tree.txt` if Bubble is unavailable. **Not called in the normal path.** Gated by `ORG_TREE_INJECTION_ENABLED` (default `false`) across all 4 agents — orgs are resolved via `search_knowledge_base` against the `ba:organizations` namespace in production. Flip the env var to `true` only to rollback to injection for a specific run.
- **ECS entrypoint.sh:** Supports running arbitrary Python commands via CMD override (e.g., `python scripts/backfill_document_extractions.py`). If `$1` is `python`, the full command is exec'd directly. Default (no args) runs `python spike.py`. The S3 targets download block (when `TARGETS_SOURCE` is an `s3://` URI) is non-fatal — errors in the Python download script are caught internally and print a warning to stderr, and the outer shell command is guarded with `|| echo "WARNING..."` so eval/backfill ECS tasks don't crash when `TARGETS_SOURCE` is not set or the download fails.
- **Library item extraction (flat schema):** `spike.py` extracts library items from flat schema fields (`library_item_preliminary_title`, `library_item_url`, `library_items_file_name`) in the normal pipeline, not just from nested `library_items[]` arrays. The backfill script mirrors this logic.
- **Multi-alert granularity (DynamoDB instructions):** The `web-tracking-agent` instructions explicitly constrain granularity: one row per distinct document/PDF (by URL or filename), one row per distinct event/meeting. Multiple agenda items within the same document go in the `agenda_item_title_chronicle_topics` array of that one row — do NOT fan out by agenda item.
- **Backend-API fallback:** `page_change_agent`, `document_agent`, `eval/doc_eval_agent`, and `eval/eval_agent` all wrap the `search_knowledge_base` tool and the Agents SDK `Runner.run()` in a try/except that falls through to a direct `chat_json` call (no KB tools, Structured Outputs still enforced) if the backend API is unreachable. Logs a `WARNING ... backend search unavailable — falling back to direct API` line. Keeps reruns + QA + the 6h pipeline producing output when `/knowledge/*` is down.

## Critical warnings

- **Never change `output_json_schema.required` without updating the dashboard.** The dashboard's `/api/schema` reads `required` to determine column names. If you wrap the schema in an `alerts` array, `required` becomes `["alerts"]` — the dashboard must drill into `alerts.items` to find the inner schema. This is already handled in `/api/schema/route.ts` and `/api/doc-schema/route.ts`.
- **Never add `--no-cache` to Docker builds** — skips the npm install cache layer and makes builds ~10x slower.
- **`chatkit_production_config` writes are no longer intercepted by our Lambda** — the ChatKit backend's field registry owns `output_json_schema` + `_column_registry` end-to-end. The `validate_config_sync` Lambda was deleted on 2026-10-29 because it was mutating `_column_registry` after every sync and causing stable-ID drift against the schema's `required[]`. Bubble admin edits now flow straight through to DynamoDB via the ChatKit backend; no dashboard-side validation step.
- **Never re-introduce client-side reranking.** The backend's `/knowledge/search` runs its own Decisions-API reranker, calibrated and under our team's observability (`retrieval_traces`). The previous client-side fork (deleted 2026-11-09) used `gpt-5-nano` with `max_completion_tokens=200`, which silently returned empty bodies when reasoning consumed the budget — every chunk scored 0.0, top-N became arbitrary, and extraction output was non-deterministically truncated. If retrieval quality drops, file against the backend rather than reintroducing local scoring.
- **`_pgvector_enabled()` must gate on `CHATKIT_INTERNAL_API_KEY`, not legacy pgvector env vars.** All four agents have a function named `_pgvector_enabled()` as a historical holdover from the direct-DB era. The 2026-11-09 migration updated `bubble/document_agent.py`, `bubble/page_change_agent.py`, and `eval/doc_eval_agent.py` to gate on the new env var but missed `eval/eval_agent.py`, which kept returning `False` because it was checking `DATABASE_IP` / `DATABASE_NAME` / `DATABASE_PASSWORD_CHATKIT` — vars that were deleted from the task def at the same time. Effect: every WTA QA run silently fell through to the direct-API path with no `search_knowledge_base` tool, so the QA agent couldn't retrieve references and scored chronically Partially Correct on chronicle_topic / organization / is_relevant_for_newsreel. Fixed in `cc43a11`. **Rule for the future: any time you touch one agent's retrieval gate, grep for `_pgvector_enabled` across all four files and change them together.**
- **Never pass composite strings as `trace_id` to `bubble/doc_extraction_ingest.search()`.** The backend enforces `trace_id: 1-128 chars, [A-Za-z0-9._-]`. A plain UUID (`agent_call_id`) passes trivially; composite row keys like `eval_row_key` ("<uuid>|<url>|<id>") violate both the length and charset rules. The wrapper now sanitizes defensively (replaces disallowed chars with `_`, clamps to 128), so you can't crash the search even if you pass garbage — but the resulting trace_id loses structure. Pass the plain `agent_call_id` wherever possible. Full validation rules: `docs/knowledge-base-integration.md#backend-validation-rules`.
- **`library_item_preliminary_title` is a dict in new flat schema** (`{status, title}`), not a string. Code that calls `.strip()` on it will crash — always check type first.
- **Never auto-ingest to the newsreel knowledge base.** Both `ingest_for_newsreel()` (documents and transcripts) must only be called via the ingest gate (`storage/ingest_actions.py` approve functions), never directly from `spike.py`. The pipeline sets `ingest_status: "pending"`; the dashboard user approves. Transcripts are submitted via presigned S3 URL to `POST /internal/documents/ingest` — no chunking step.
- **`ingest_status` flows verbatim** — set on the alert dict or doc_result dict before `store_run_alerts()` is called; `_build_rows_for_single_alert()` and `_build_doc_extraction_rows()` store it automatically. Do not add special-case handling in the row builders.
- **`patch_jsonl_row()` is not atomic** — it downloads, patches, and re-uploads the full JSONL. Concurrent writes can clobber each other. Acceptable for dashboard use (one user at a time), but not for high-concurrency batch writes.
