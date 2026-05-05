# CLAUDE.md — Project Context

## What this project does

Website change-tracking system that monitors configured NAIC web pages on a 6-hour schedule, detects meaningful changes (new PDFs, meetings, agenda items), and runs RAG-based LLM agents on before/after HTML snapshots to produce structured alerts. Alerts feed a downstream dashboard (repo: `NAICDashboard-`). Bubble.io integration exists but is currently legacy.

## Tech stack

- **Language:** Python
- **Scraping:** Playwright (JS-rendered), requests + BeautifulSoup (simple pages)
- **Change detection:** SHA256 fingerprinting, difflib
- **RAG agents:** OpenAI Agents SDK + pgvector (hybrid semantic + lexical search via PostgreSQL)
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
   - At runtime, `_sanitize_schema_for_openai()` strips keywords the OpenAI Responses API rejects in strict mode (`$schema`, `oneOf` → replaced with `{"type":"string"}`, unsupported `format` values like `uri`)
   - Falls back to extracting a JSON schema from a ` ```json ``` ` fenced block in `instructions`, then to `_FALLBACK_OUTPUT_SCHEMA` hardcoded in the file
   - **New flat schema** (as of May 2026): all fields are top-level — no nested `events[]`, `library_items[]`, `agenda_items[]` arrays. Event/agenda/library-item fields are prefixed at the top level (`event_title`, `agenda_item_title_and_chronicle_topics`, `library_item_preliminary_title`, etc.)
8. **document_agent** — For materials-type alerts, extracts structured metadata via two paths:
   - Config key: `chat:document-data-extraction` in DynamoDB
   - **When `PGVECTOR_ENABLED=true` + DB creds available (two-step):** Step 1 — Agents SDK with pgvector tools gathers free-text analysis; Step 2 — `chat_json()` with Structured Outputs reformats to the output schema
   - **When pgvector unavailable:** direct Responses API call with Structured Outputs (does NOT skip/return `{}` — always produces output)
   - Output: fully dynamic — whatever fields the DynamoDB config `output_json_schema` instructs. Stored as `document_extractions_table.jsonl` in S3.
   - N/A guard: items where the library item name is `"N/A"`, `"N/A."`, or `"-"` are skipped (no extraction attempted)
9. **Alert storage** (`alert_s3.py`) — writes `alerts_table.jsonl`, per-run `alerts.json`, `alerts_table.xlsx` to S3. Excel export serializes list/dict cell values to JSON strings before writing to openpyxl cells.
10. **Notifier** (SES) — email summary of changes

## Key directories

- `storage/` — State store (DynamoDB prod / `state.json` dev), S3 for HTML snapshots, alerts, changelogs
- `bubble/` — RAG agents (`page_change_agent.py`, `document_agent.py`), legacy Bubble.io integration, pgvector client
- `bubble/pgvector/` — pgvector connection pool and search tool (mirrors ChatKit infrastructure)
- `scrape/` — HTML content extraction, PDF metadata, page chunking
- `config/` — RunSpec (CLI > env > defaults), chatkit DynamoDB config loader
- `scripts/` — Deploy, backfill, smoke tests
- `prompts/` — Prompt context files injected into agent user messages. `org_tree.txt` is the org hierarchy tree (dash-depth format, 140+ orgs) used to guide organization field assignment
- `infra/terraform/` — ECS Fargate, EventBridge, DynamoDB, S3, IAM
- `tests/` — Unit/integration tests
- `analysis/` — PDF agenda detection analysis and sample datasets
- `debug/` — E2E/AI debug artifacts (gitignored)

## Key entry points

- `spike.py` — Main pipeline orchestrator
- `targets.json` — Target URL config with extract rules per URL
- `config/run_spec.py` — RunSpec: single source of truth for runtime behavior
- `config/chatkit_config.py` — Loads agent config from DynamoDB `chatkit_production_config`
- `bubble/page_change_agent.py` — Page change RAG agent
- `bubble/document_agent.py` — Document matching RAG agent
- `bubble/openai_client.py` — OpenAI Responses API client; `chat_json()` supports both `json_object` and `json_schema` structured outputs
- `storage/alert_s3.py` — Alert storage and Excel export

## Agent configuration (DynamoDB)

Table: `chatkit_production_config` (env: `CHATKIT_CONFIG_TABLE`)
Key format: `chat:{chat_id}`

### `web-tracking-agent` (page_change_agent)

| Field | Type | Description |
|-------|------|-------------|
| `instructions` | String | System prompt for the agent |
| `model` | String | OpenAI model ID (e.g. `gpt-5.4`) |
| `reasoning_effort` | String | `low` / `medium` / `high` |
| `pgvector_namespaces` | List | Namespaces for knowledge base search. Current value: `["bubble-data", "art-chronicles", "art-newsreels", "naic-guidelines", "naic-proceedings", "international-guidelines", "ratings-agencies"]` |
| `output_json_schema` | Map | **JSON Schema object** (draft-07) defining the exact output fields. Written by Bubble admin sync. Used for OpenAI Structured Outputs (`response_format: json_schema`). |
| `output_json_schema_name` | String | Schema name for the API call (e.g. `"web_tracking_alert"`) |
| `output_json_schema_strict` | Bool | Whether to use strict mode (default: `true`) |
| `output_json_schema_hash` | String | SHA256 hash of the schema, used for change detection |
| `output_requested_values` | List | Ordered list of human-readable column labels (one per field in `output_json_schema.required`). Written by Bubble admin sync. Consumed by the dashboard `/api/schema` to render column headers. |

The `output_json_schema.required` array defines the **ordered** list of field names. The dashboard zips this with `output_requested_values` to produce human-readable column headers. Editing either in the Bubble admin Values tab automatically updates agent output AND dashboard columns — no code deploy.

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
| `organization` | `string[]` | Array of org name(s) |
| `alert_date_time` | string | ISO 8601 Eastern Time |
| `event_title` | string | "N/A" if no event |
| `event_start_date_time` | string | ISO 8601 or "N/A" |
| `event_end_date_time` | string | ISO 8601 or "N/A" |
| `event_duration` | string | e.g. "2h 30m" or "N/A" |
| `event_is_full_day` | string | "Full Day" or "N/A" |
| `event_url` | string | "N/A" if none |
| `event_call_in_number_access_code` | string | "N/A" if none |
| `agenda_item_title_and_chronicle_topics` | `[{status, agenda_item_title, chronicle_topics[]}]` | Array (minItems 1) |
| `agenda_item_title_official` | `{status, title_official}` | Object |
| `agenda_item_standardized_id` | `{status, standardized_id}` | Object |
| `agenda_item_official_id` | `{status, official_id}` | Object |
| `library_item_preliminary_title` | `{status, title}` | Object; status ∈ New/Updated/Existing/N/A |
| `library_item_url` | string | URL or "N/A" |
| `library_items_file_name` | string | Filename or "N/A" |
| `is_alert_relevant_for_art_newsreel` | `{status, reference}` | status ∈ Yes/No/Additional review needed |

Previous schema (before May 2026) used nested arrays: `events[]`, `library_items[]`, `agenda_items[]`. Old rows in `alerts_table.jsonl` use that format. The dashboard handles both gracefully.

## Common commands

```bash
make install                          # create venv, install deps
make run                              # run the pipeline
python3 spike.py                      # minimal local run
python3 spike.py --target-ids <id>    # run single target
python3 scripts/backfill_alerts.py    # reprocess stored page changes through agents
python3 scripts/backfill_alerts.py --limit 10 --dry-run  # preview without writing
./scripts/deploy.sh                   # build Docker, push ECR, terraform apply
```

## Key environment variables

**State & storage:**
- `STATE_BACKEND=local|dynamodb` — state backend
- `STATE_TABLE` — DynamoDB state table name
- `PAGE_CHANGE_SNAPSHOT_BUCKET` — S3 bucket for before/after HTML
- `CHANGELOG_BUCKET` — S3 bucket for alerts and changelogs

**Agents:**
- `PAGE_CHANGE_AGENT_ENABLED=true` — enable RAG agents (required for alert pipeline)
- `PGVECTOR_ENABLED=true` — enable pgvector knowledge base search
- `CHATKIT_CONFIG_TABLE` — DynamoDB config table (default: `chatkit_production_config`)
- `OPENAI_API_KEY` — required for agents and embeddings
- `DATABASE_IP`, `DATABASE_NAME`, `DATABASE_USERNAME_CHATKIT`, `DATABASE_PASSWORD_CHATKIT`, `DATABASE_PORT` — pgvector DB

**Email:**
- `SEND_EMAIL=true`, `FROM_EMAIL`, `TO_EMAILS`, `SES_REGION`

**Bubble (legacy):**
- `BUBBLE_API_URL`, `BUBBLE_API_KEY`

## Conventions

- Extractors are pluggable; defined in target config `extract` array with `{type, extractor, params}`
- State is per-target (keyed by `target.id`)
- Failures on one target don't stop the run; errors collected in final report
- Agent configs live in DynamoDB — change system prompts/models/schema without redeploying
- pgvector search uses RRF fusion of semantic (halfvec cosine) + lexical (tsquery) results, then reranked
- Chronicle topics are a fixed taxonomy in Bubble.io (87 nodes in "Chronicles" tree)
- **Storage is verbatim** — `alert_s3.py` stores exactly what the agent outputs. Null stays null, empty string stays empty. Dashes in the dashboard = agent output null or field absent. Do NOT add coercion (null→"N/A") to `_flatten_val` or `_build_table_rows` — it masks agent deviations from instructions.
- New flat schema (May 2026+): `_build_table_rows` stores all top-level agent output fields verbatim. The `events`, `library_items`, `agenda_items` nested-array handling is still present for backward compatibility with old rows but won't trigger for new-schema responses.
- Bubble.io integration is currently legacy; Bubble admin UI syncs `output_json_schema` + `output_requested_values` to DynamoDB
- Debug artifacts go to `debug/` directory (gitignored)
- `prompts/org_tree.txt` — dash-depth hierarchy of 140+ organizations, sourced from Bubble API. Injected into agent context to guide `organization` field assignment. Regenerate from Bubble API if org structure changes.
