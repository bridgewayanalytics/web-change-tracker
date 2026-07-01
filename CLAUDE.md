# CLAUDE.md — Project Context

## What this project does

Website change-tracking system that monitors configured NAIC web pages on a 6-hour schedule, detects meaningful changes (new PDFs, meetings, agenda items), and runs RAG-based LLM agents on before/after HTML snapshots to produce structured alerts. Alerts feed a downstream dashboard (repo: `NAICDashboard-`, deployed at `https://tracker.bridgewayanalytics.com`). Bubble.io integration exists but is currently legacy.

## Tech stack

- **Language:** Python
- **Scraping:** Playwright (JS-rendered), requests + BeautifulSoup (simple pages)
- **Change detection:** SHA256 fingerprinting, difflib
- **RAG agents:** OpenAI Agents SDK + pgvector (hybrid semantic + lexical search via PostgreSQL)
- **Structured outputs:** OpenAI Responses API with JSON Schema (`response_format: json_schema`) — schema loaded from DynamoDB, sanitized for API compatibility at runtime
- **Reranking:** OpenAI chat completions (gpt-5.4 with `reasoning_effort=low`) for pgvector search result reranking
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
8. **document_agent** — For all alert types that produce a library item (12 types, see `DOCUMENT_ALERT_TYPES` in `document_agent.py`), extracts structured metadata via two paths:
   - Config key: `chat:document-data-extraction` in DynamoDB
   - **When `PGVECTOR_ENABLED=true` + DB creds available (two-step enforcement):** Step 1 — Agents SDK with pgvector tools (`search_knowledge_base`, `list_available_documents`) gathers free-text analysis; Step 2 — `chat_json()` with Structured Outputs reformats the free-text into the exact DynamoDB output schema. This two-step approach ensures pgvector search is actually used while also enforcing strict schema compliance.
   - **When pgvector unavailable:** direct Responses API call with Structured Outputs (does NOT skip/return `{}` — always produces output, just without knowledge base context)
   - Output: fully dynamic — whatever fields the DynamoDB config `output_json_schema` instructs. Stored as `document_extractions_table.jsonl` in S3.
   - N/A guard: items where the library item name is `"N/A"`, `"N/A."`, `"-"`, or `""` are skipped via `_item_has_real_name()` (no extraction attempted)
   - A `## Output Format` JSON suffix is automatically appended to the DynamoDB `instructions` at runtime — do not add JSON format requirements to the DynamoDB config itself
9. **Alert storage** (`alert_s3.py`) — writes `alerts_table.jsonl`, per-run `alerts.json`, `alerts_table.xlsx` to S3. Excel export serializes list/dict cell values to JSON strings before writing to openpyxl cells.
10. **Recording matcher** (`bubble/recording_matcher.py`) — after the document agent loop, matches meeting alerts to mp3 recordings in `recordings-bucket-1` S3 bucket for recordings already present at pipeline time. Stamps `recording_s3_key` on matching alerts. Recordings uploaded after the pipeline run are handled by the event-driven path (step 10b).
10b. **Event-driven recording ingest** — when a new mp3 lands in `recordings-bucket-1`, an S3 event triggers `infra/lambda/recording_ingest_trigger/handler.py`, which fires an ECS RunTask with `RECORDING_S3_KEY` set. `spike.py` enters `_run_recording_ingest()` mode: (1) transcribes the mp3 (idempotent), (2) stamps `recording_s3_key`/`transcript_s3_key`/`ingest_status="pending"` on matching alert rows, (3) runs transcript doc extraction and appends to `document_extractions_table.jsonl`, (4) creates one synthetic "New Meeting Transcript Available" alert row per unique event title with `bubble_action` stamped and appends to `alerts_table.jsonl`. Idempotency guard: if a synthetic row with the same `transcript_s3_key` already exists, step 4 is skipped entirely. Nothing is auto-ingested — all publishing goes through the content gate.
11. **Transcriber** (`bubble/transcriber.py`) — for alerts that got a `recording_s3_key`, converts the mp3 to a timestamped transcript via OpenAI Whisper (`verbose_json`, `[HH:MM:SS] text` per segment) and stores it in the artifacts bucket under `transcripts/`. The transcriber returns the S3 key; `spike.py` stamps `transcript_s3_key` and `ingest_status: "pending"` on the alert after the call returns. Idempotent.
11b. **Transcript document extraction** (`spike.py`) — immediately after transcription, downloads the transcript from S3 and runs it through the `document-data-extraction` agent (same config as library items, `text_limit=40_000` chars). Stamps `extraction_source: "transcript"` and `transcript_s3_key` on the result. Appended to `ev["__doc_extraction"]` so it lands in `document_extractions_table.jsonl` alongside library item extractions.
11c. **Synthetic transcript alert** (`spike.py`) — after transcript doc extraction, creates a new alert row deterministically for every alert that received a `transcript_s3_key`. Sets `alert_type = "New Meeting Transcript Available"`, copies all event fields from the parent alert, and assigns a fresh `agent_call_id`. Appended to `ev["__agent_output"]` before the bubble_sync_classifier so it receives `bubble_action` (`event: update`, `library_item: create` with type "Meeting Transcript"). `ingest_status = "pending"` so the content gate shows the Transcript → Newsreel section.
12. **Ingest gate** — no auto-ingest into the newsreel knowledge base. `ingest_status` field controls lifecycle: `null` (not eligible), `"pending"` (awaiting dashboard approval), `"approved"` (ingested), `"rejected"` (dismissed). Dashboard shows approve/edit/reject buttons per row. See `storage/ingest_actions.py`.
13. **Bubble sync classifier** (`bubble/bubble_sync_classifier.py`) — after the recording/transcript pipeline, runs `classify_alert()` on every alert and stamps `bubble_action` on applicable rows. Pure function, no API calls. Maps `alert_type` → `{event, library_item, agenda_items}` with full field previews (event_preview, library_item_preview, agenda_item_previews). `enrich_with_doc_extraction()` merges doc extraction metadata into both previews: description/date/type on library item CREATE; chronicle topics (`topics___dt_list_custom_newsreel_update`) on both event and library item previews. Irrelevant alerts (No Meaningful Change, carousel) get no field set.
15. **Notifier** (SES) — email summary of changes

## Rerun mode

When `RERUN_RUN_ID` and `RERUN_TARGET_ID` environment variables are set, `spike.py` enters rerun mode instead of the normal pipeline:

1. Fetches stored before/after HTML from S3: `pages/<target_id>/YYYY/MM/DD/<run_id>/`
2. Re-runs `extract_page_change()` + `extract_document_data()` with current DynamoDB config
3. Writes result to `alerts/reruns/<run_id>/<target_id>/result.json` (never overwrites `alerts_table.jsonl`)
4. The dashboard handles Accept (patches JSONL) or Discard (deletes result)

Result schema includes: `run_id`, `target_id`, `rerun_timestamp`, `config_hash`, `original_rows`, `rerun_rows`, `doc_original_rows`, `doc_rerun_rows`.

`RERUN_MODE` env var controls which agents run: `"alerts"` (page_change_agent only), `"docs"` (document_agent only), `"both"` (default).

Full spec: `docs/rerun-feature.md`

## Key directories

- `storage/` — State store (DynamoDB prod / `state.json` dev), S3 for HTML snapshots, alerts, changelogs
- `bubble/` — RAG agents (`page_change_agent.py`, `document_agent.py`), recording matcher, transcriber, newsreel ingest, legacy Bubble.io integration, pgvector client
- `bubble/pgvector/` — pgvector connection pool and search tool (mirrors ChatKit infrastructure)
- `scrape/` — HTML content extraction, PDF metadata, page chunking
- `config/` — RunSpec (CLI > env > defaults), chatkit DynamoDB config loader
- `scripts/` — Deploy, backfill, schema management, smoke tests
- `prompts/` — Prompt context files injected into agent user messages. `org_tree.txt` is a static fallback org hierarchy tree (dash-depth format, 140+ orgs); live data is fetched from Bubble API via `bubble/org_tree.py`
- `infra/terraform/` — ECS Fargate, EventBridge, DynamoDB, S3, IAM
- `tests/` — Unit/integration tests
- `docs/` — Feature specs (rerun feature)
- `analysis/` — PDF agenda detection analysis and sample datasets
- `debug/` — E2E/AI debug artifacts (gitignored)

## Key entry points

- `spike.py` — Main pipeline orchestrator (normal mode + rerun mode + manual_chunk mode)
- `targets.json` — Target URL config with extract rules per URL
- `config/run_spec.py` — RunSpec: single source of truth for runtime behavior
- `config/chatkit_config.py` — Loads agent config from DynamoDB `chatkit_production_config`
- `bubble/page_change_agent.py` — Page change RAG agent (`extract_page_change()`, `_unwrap_alerts()`, `_sanitize_schema_for_openai()`, `get_config_hash()`). Calls `get_org_tree()` on every invocation to inject the live Bubble org hierarchy into the agent's user message.
- `bubble/document_agent.py` — Document matching RAG agent (`extract_document_data(document_name, document_url, pdf_text=None, text_limit=None)`, two-step pgvector enforcement). Also used for transcript extraction with `text_limit=40_000` — `extraction_source: "transcript"` distinguishes those rows in `document_extractions_table.jsonl`.
- `bubble/openai_client.py` — OpenAI Responses API client; `chat_json()` supports both `json_object` and `json_schema` structured outputs
- `bubble/recording_matcher.py` — `find_recording(event_title, event_start_date_time)` matches alerts to mp3s in `recordings-bucket-1` by date + acronym scoring
- `bubble/transcriber.py` — `transcribe_recording(recording_s3_key)` converts mp3 → timestamped text via Whisper (`verbose_json` format, `[HH:MM:SS] text` per segment), stores under `transcripts/` in artifacts bucket. `format_with_timestamps(segments)` shared with backfill script.
- `bubble/newsreel_ingest.py` — `ingest_for_newsreel(document_url, filename)` pushes documents and transcripts (via presigned URL) to ChatKit newsreel-generation knowledge base
- `bubble/org_tree.py` — `get_org_tree()` fetches the live Bubble org hierarchy (143 orgs) and formats it as dash-depth text for injection into agent context. 30-min in-process cache; falls back to `prompts/org_tree.txt`.
- `bubble/bubble_sync_classifier.py` — `classify_alert(alert) → BubbleSyncPlan` pure classifier; stamps `bubble_action` on applicable alerts. `enrich_with_doc_extraction(bubble_action, extraction)` merges doc extraction fields (description, date, type, chronicle topics) into previews. No I/O. `_build_agenda_previews()` reads flat-schema `agenda_item_title_chronicle_topics` first, falls back to old nested-schema `agenda_item_title_and_chronicle_topics` for backward compatibility. See `bubble_action` field docs below.
- `bubble/bubble_sync.py` — `sync_alert(agent_call_id)` real Bubble sync executor: resolves org names → IDs, CREATE/UPDATE libraryitem + calendaritem using `field_ids` from preview, links them via `relevant_resources_list_custom_resource` (the write field ID for the `Agenda` display-name field on calendaritem — list of libraryitem IDs). Triggered via ECS RunTask from dashboard route.
- `storage/alert_s3.py` — Alert storage, flat/nested schema detection, Excel export, `patch_jsonl_row()` utility
- `storage/ingest_actions.py` — Ingest gate: `approve_transcript_ingest()`, `approve_document_ingest()`, `ingest_manual_document_url()`, `reject_ingest()`, `generate_presigned_url()`, `generate_presigned_upload_url()`
- `infra/lambda/validate_config_sync/handler.py` — DynamoDB Streams Lambda; auto-corrects `chatkit_production_config` on every Bubble sync (label count, garbage keys, schema normalization, column registry)

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/deploy.sh` | Build Docker, push ECR, terraform apply |
| `scripts/backfill_alerts.py` | Reprocess stored page changes through agents (re-runs both agents) |
| `scripts/backfill_document_extractions.py` | Backfill `document_extractions_table.jsonl` from stored `agent_output.json` (safe, never touches `alerts_table.jsonl`). Handles flat schema library items, list-format `agent_output.json`, and `alerts` array wrapper. |
| `scripts/rebuild_alerts_table.py` | Rebuild `alerts_table.jsonl` from stored `agent_output.json` files (no agent re-run, useful for dedup/schema fixes) |
| `scripts/wrap_schema_alerts.py` | Wrap flat DynamoDB `output_json_schema` in `alerts` array wrapper for multi-alert support |
| `scripts/backfill_call_id.py` | One-time backfill of `agent_call_id` on existing JSONL rows (groups by `run_id` + `target_id`) |
| `scripts/backfill_bubble_action.py` | Backfill `bubble_action` on existing `alerts_table.jsonl` rows using the classifier (idempotent, `--force` to re-classify). Enriches with doc extraction keyed by `library_item_url` (not `agent_call_id`) — required for multi-document page changes where one call_id produces N library items. |
| `scripts/backfill_recordings.py` | Backfill `recording_s3_key` on alerts that have `event_title`+`event_start_date_time` but no recording match yet. Two-pass: standard find_recording() then filename-based fallback for rows where event date is N/A. |
| `scripts/backfill_transcripts.py` | Backfill `transcript_s3_key` on alerts that have a `recording_s3_key` but haven't been transcribed yet. Groups by `agent_call_id` to transcribe once per recording. `--local` flag uses local Whisper model. |
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
| `eval/run_eval.py` | Entry point — calls `_load_secrets()` to load OpenAI + DB creds from SSM, then orchestrates full eval run |
| `eval/row_selector.py` | Loads eligible rows from `alerts_table.jsonl` (`ingest_status == "approved"`). Applies limit at the agent-call level but returns ALL rows per selected call (siblings included). Sibling rows share `agent_call_id` and are grouped in `run_eval.py`. |
| `eval/html_fetcher.py` | Fetches before/after HTML snapshots from S3 for each row |
| `eval/context_builder.py` | Builds multi-section context: (1) live org tree for org field validation, (2) Bubble ground truth (agenda item chronicle topics from `bubble_action`), (3) filename presence check in `newsreel-generation:ART`, (4) semantic search in `ba:chronicles` + `ba:newsreels`, (5) semantic search in `newsreel-generation:ART`. Strips `"NEW ORGANIZATION: "` prefix from org values before building pgvector queries. |
| `eval/eval_agent.py` | Calls `chat:eval-agent` (DynamoDB config) — one call per row, returns per-field scores. Accepts optional `sibling_rows` list; when present, injects a sibling summary block so the agent knows which other documents came from the same run and doesn't penalize a row for content that belongs to a sibling. |
| `eval/result_store.py` | Upserts results into `alerts/eval_results_table.jsonl` keyed by `eval_row_key`. Single-row calls: key = `agent_call_id` (backward compatible). Sibling rows: key = `agent_call_id|library_item_url`. `delete_eval_result()` removes all entries for a given `agent_call_id`. |

**Agent config:** `chat:eval-agent` in DynamoDB `chatkit_production_config`. System instructions and rubric configured in Bubble admin, same pattern as `web-tracking-agent`.

**Storage:** `alerts/eval_results_table.jsonl` — upsert keyed by `eval_row_key`. Single-row agent calls use `agent_call_id` as the key (backward compatible with prior stored results). Sibling rows (multiple rows sharing one `agent_call_id`) use `agent_call_id|library_item_url` as the key so each gets its own entry. Re-running QA replaces prior entries. Dashboard DELETE removes all entries for a given `agent_call_id` via `eval/result_store.delete_eval_result()`.

**Output schema per row:** original alert fields + `eval_run_id`, `eval_timestamp`, `eval_scores` (dict of field → `{score, reasoning}`), `overall_summary`.

**ECS trigger:** dashboard fires `POST /api/eval { agent_call_id }` → ECS RunTask CMD override `["python", "-m", "eval.run_eval", "--agent-call-ids", "<id>"]`. Works because `entrypoint.sh` `exec "$@"` when `$1 == python`. Must use `-m eval.run_eval` (module form), not `eval/run_eval.py` (file form), to resolve internal imports correctly.

## Agent configuration (DynamoDB)

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

## Current output schema fields (as of May 2026)

New flat schema — all top-level, no nested arrays for event/library/agenda items:

| Field | Type | Notes |
|-------|------|-------|
| `alert_type` | string (enum) | 15 valid values |
| `alert_title` | string | |
| `alert_description` | string | |
| `alert_url` | string | URL where change was detected |
| `organization` | `string[]` | Array of org name(s) — guided by live Bubble org tree (`bubble/org_tree.py`; falls back to `prompts/org_tree.txt`) |
| `alert_date_time` | string | ISO 8601 Eastern Time |
| `event_title` | string | "N/A" if no event |
| `event_start_date_time` | string | ISO 8601 or "N/A" |
| `event_end_date_time` | string | ISO 8601 or "N/A" |
| `event_duration` | string | e.g. "2h 30m" or "N/A" |
| `event_is_full_day` | string | "Full Day" or "N/A" |
| `event_url` | string | "N/A" if none |
| `event_call_in_number_access_code` | string | "N/A" if none |
| `agenda_item_title_chronicle_topics` | `[{status, agenda_item_title, chronicle_topics[]}]` | Array (minItems 1) |
| `agenda_item_title_official` | `[{status, official_title}]` | Array (minItems 1) |
| `agenda_item_standardized_id` | `[{status, standardized_id}]` | Array (minItems 1) |
| `agenda_item_official_id` | `[{status, official_id}]` | Array (minItems 1) |
| `library_item_preliminary_title` | `{status, title}` | Object; status in New/Updated/Existing/N/A |
| `library_item_url` | string | URL or "N/A" |
| `library_items_file_name` | string | Filename or "N/A" |
| `is_the_alert_relevant_for_an_art_newsreel_article` | `{status, details}` | status in Yes/No/Additional review needed |

Previous schema (before May 2026) used nested arrays: `events[]`, `library_items[]`, `agenda_items[]`. Old rows in `alerts_table.jsonl` use that format. The dashboard handles both gracefully via `FIELD_ALIASES` in `AlertsTable.tsx`.

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
python3 scripts/wrap_schema_alerts.py --dry-run  # preview schema wrapping
python3 scripts/backfill_call_id.py --dry-run  # preview agent_call_id backfill
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
- `PGVECTOR_ENABLED=true` — enable pgvector knowledge base search
- `CHATKIT_CONFIG_TABLE` — DynamoDB config table (default: `chatkit_production_config`)
- `OPENAI_API_KEY` — required for agents and embeddings
- `OPENAI_FETCH_FROM_SSM=true` — load OpenAI API key from AWS SSM
- `DATABASE_IP`, `DATABASE_NAME`, `DATABASE_USERNAME_CHATKIT`, `DATABASE_PASSWORD_CHATKIT`, `DATABASE_PORT` — pgvector DB

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

**Bubble (legacy):**
- `BUBBLE_API_URL`, `BUBBLE_API_KEY`, `AI_ENRICHMENT_ENABLED`

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
    "agenda_item_previews": [{ "title", "chronicle_topics" }],
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

Dashboard shows "Sync to Bubble" button on rows with `bubble_action` set and no `bubble_sync_status`. Clicking opens a preview modal showing exact field values (CREATE/UPDATE cards) before confirming. After confirm, `/api/bubble/sync` fires an ECS RunTask (same cluster as reruns) that runs `bubble_sync.sync_alert(agent_call_id)`.

**Executor sequence in `bubble_sync.py`:**
1. Resolve org display names → Bubble `_id` values (list all organizations)
2. If `library_item == "create"`: POST to `libraryitem` with `field_ids`; capture returned ID
3. If `library_item == "update"`: find by `match_search` (URL then title), PATCH with `field_ids`
4. If `event == "create"`: POST to `calendaritem` with `field_ids`; set `relevant_resources_list_custom_resource` to `[library_item_id]` if library item present
5. If `event == "update"`: find by `match_search` (org + date), PATCH with `field_ids` + library item link
6. Patch JSONL with `bubble_sync_status: "synced"`, `bubble_event_id`, `bubble_library_item_id`

**Note:** The current executor does NOT sync `agenda_items` (agendaitem records) — it only syncs calendaritem and libraryitem. Agenda item creation/update is deferred to the forthcoming Eidarix sync API (see `docs/bubble_sync_payload_spec.json`). The `agenda_items` list in `bubble_action` is shown in the dashboard preview modal but not executed by `bubble_sync.py`.

## Conventions

- Extractors are pluggable; defined in target config `extract` array with `{type, extractor, params}`
- State is per-target (keyed by `target.id`)
- Failures on one target don't stop the run; errors collected in final report
- Agent configs live in DynamoDB — change system prompts/models/schema without redeploying
- pgvector search uses RRF fusion of semantic (halfvec cosine) + lexical (tsquery) results, then reranked via gpt-5.4
- Chronicle topics are a fixed taxonomy in Bubble.io (87 nodes in "Chronicles" tree)
- **Storage is verbatim** — `alert_s3.py` stores exactly what the agent outputs. Null stays null, empty string stays empty. Dashes in the dashboard = agent output null or field absent. Do NOT add coercion (null->"N/A") to `_flatten_val` or `_build_table_rows` — it masks agent deviations from instructions.
- New flat schema (May 2026+): `_build_table_rows` stores all top-level agent output fields verbatim. The `events`, `library_items`, `agenda_items` nested-array handling is still present for backward compatibility with old rows but won't trigger for new-schema responses.
- **Multi-alert pipeline:** `extract_page_change()` returns `list[dict]`. `__agent_output` on change events is a list. `_build_table_rows()` accepts `list[dict] | dict`. Each row includes `agent_call_id` (UUID per agent invocation). The dashboard "Call ID" sticky column shows the last 8 chars of `agent_call_id`.
- **Two-step pgvector enforcement (document_agent):** Step 1 = Agents SDK with pgvector tools (free-text output), Step 2 = `chat_json()` with Structured Outputs to enforce the exact schema. This ensures the knowledge base is actually searched (Agents SDK tool calls) while producing schema-compliant output.
- **ModelSettings for reasoning models:** Use `ModelSettings(reasoning=Reasoning(effort="low"))` — NOT `ModelSettings(reasoning_effort="low")` which silently ignores the parameter.
- Bubble.io integration is currently legacy; Bubble admin UI syncs `output_json_schema` + `output_requested_values` to DynamoDB
- Debug artifacts go to `debug/` directory (gitignored)
- **Dynamic org tree:** `bubble/org_tree.py` fetches the live Bubble org hierarchy at runtime via `get_org_tree()` (30-min in-process cache, DFS traversal sorted by `Order`, `"-" * level + Name` format). Falls back to `prompts/org_tree.txt` if Bubble is unavailable. Called from `page_change_agent.py` for every agent invocation — org structure changes in Bubble propagate automatically within 30 minutes.
- **ECS entrypoint.sh:** Supports running arbitrary Python commands via CMD override (e.g., `python scripts/backfill_document_extractions.py`). If `$1` is `python`, the full command is exec'd directly. Default (no args) runs `python spike.py`.
- **Library item extraction (flat schema):** `spike.py` extracts library items from flat schema fields (`library_item_preliminary_title`, `library_item_url`, `library_items_file_name`) in the normal pipeline, not just from nested `library_items[]` arrays. The backfill script mirrors this logic.
- **Multi-alert granularity (DynamoDB instructions):** The `web-tracking-agent` instructions explicitly constrain granularity: one row per distinct document/PDF (by URL or filename), one row per distinct event/meeting. Multiple agenda items within the same document go in the `agenda_item_title_chronicle_topics` array of that one row — do NOT fan out by agenda item.

## Critical warnings

- **Never change `output_json_schema.required` without updating the dashboard.** The dashboard's `/api/schema` reads `required` to determine column names. If you wrap the schema in an `alerts` array, `required` becomes `["alerts"]` — the dashboard must drill into `alerts.items` to find the inner schema. This is already handled in `/api/schema/route.ts` and `/api/doc-schema/route.ts`.
- **Never add `--no-cache` to Docker builds** — skips the npm install cache layer and makes builds ~10x slower.
- **Any write to `chatkit_production_config` triggers the `validate_config_sync` Lambda.** The Lambda rewrites `output_json_schema`, `output_requested_values`, `_column_registry`, and `_field_aliases`. This is intentional (corrects Bubble sync garbage), but be aware: adding/editing columns via the AWS console or Bubble admin will trigger it.
- **Never use `gpt-5-nano` for reranking** — it causes 400 errors with reasoning parameters. Use `gpt-5.4` with `reasoning_effort=low`.
- **`library_item_preliminary_title` is a dict in new flat schema** (`{status, title}`), not a string. Code that calls `.strip()` on it will crash — always check type first.
- **Never auto-ingest to the newsreel knowledge base.** Both `ingest_for_newsreel()` (documents and transcripts) must only be called via the ingest gate (`storage/ingest_actions.py` approve functions), never directly from `spike.py`. The pipeline sets `ingest_status: "pending"`; the dashboard user approves. Transcripts are submitted via presigned S3 URL to `POST /internal/documents/ingest` — no chunking step.
- **`ingest_status` flows verbatim** — set on the alert dict or doc_result dict before `store_run_alerts()` is called; `_build_rows_for_single_alert()` and `_build_doc_extraction_rows()` store it automatically. Do not add special-case handling in the row builders.
- **`patch_jsonl_row()` is not atomic** — it downloads, patches, and re-uploads the full JSONL. Concurrent writes can clobber each other. Acceptable for dashboard use (one user at a time), but not for high-concurrency batch writes.
