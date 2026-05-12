# web-change-tracker

A website change-tracking and intelligence pipeline that monitors NAIC web pages on a 6-hour schedule, detects meaningful changes, and uses RAG-based LLM agents to extract structured alerts from before/after HTML snapshots. Alerts feed a downstream dashboard ([NAICDashboard-](../NAICDashboard-), live at `https://tracker.bridgewayanalytics.com`).

**Infrastructure:** AWS (ECS Fargate, EventBridge, DynamoDB, S3, SES, pgvector) · **Language:** Python

---

## Overview

The system monitors a configured list of target URLs, detects content changes via SHA256 fingerprinting, saves before/after HTML snapshots to S3, and passes them to LLM agents for structured extraction. Two agents run in sequence: one classifies the change and extracts events/documents/agenda items (page_change_agent), the second matches detected documents to Chronicle topics and agenda items in the knowledge base (document_agent). Results are stored as structured JSON + Excel in S3 and consumed by the alerts dashboard.

---

## Architecture

### Pipeline

```
EventBridge (6h cron)
        |
  ECS Fargate -- spike.py
        |
  Load targets.json
        |
  For each target URL:
  +----------------------------------------------------+
  | 1. Fetch page (Playwright / requests)              |
  | 2. SHA256 hash + pluggable extractors              |
  |    (docs, event_links, meetings, events)           |
  | 3. Diff against DynamoDB state                     |
  | 4. Save state to DynamoDB                          |
  | 5. Store before/after stripped HTML -> S3           |
  +----------------------------------------------------+
        |  (if PAGE_CHANGE_AGENT_ENABLED)
  page_change_agent
  -- compares before/after HTML
  -- output schema driven by output_json_schema in DynamoDB (OpenAI Structured Outputs)
  -- returns list[dict]: one or more alert dicts per page change
  -- each alert stamped with agent_call_id (UUID per invocation)
  -- if schema has top-level "alerts" array, unwraps to multiple rows; otherwise single row
        |  (if materials-type alert)
  document_agent (per library item)
  -- two-step pgvector enforcement: Agents SDK -> Structured Outputs
  -- outputs: topic_ids, agenda_item_ids, chronicle matches
        |
  store_run_alerts -> S3
  -- alerts_table.jsonl (append)
  -- alerts_table.xlsx (regenerated)
  -- document_extractions_table.jsonl (append, separate from alerts)
  -- per-run alerts.json
        |
  Email summary -> SES
```

### Rerun Mode

When `RERUN_RUN_ID` + `RERUN_TARGET_ID` env vars are set, `spike.py` skips the normal pipeline and instead:
1. Fetches stored before/after HTML from S3
2. Re-runs agents with current DynamoDB config
3. Writes result to `alerts/reruns/<run_id>/<target_id>/result.json`
4. Dashboard handles Accept (patches JSONL) or Discard (deletes result)

`RERUN_MODE` controls which agents run: `"alerts"` (page_change_agent only), `"docs"` (document_agent only), `"both"` (default).

Full spec: `docs/rerun-feature.md`

### AWS Resources

| Resource | Purpose |
|----------|---------|
| **EventBridge** | 6-hour cron schedule |
| **ECS Fargate** | Compute (Playwright requires container) |
| **DynamoDB** | Per-target page state; agent configs (`chatkit_production_config`) |
| **S3** | Before/after HTML snapshots, alerts JSONL/xlsx, changelogs, rerun results |
| **PostgreSQL + pgvector** | Knowledge base for RAG agents (Chronicles, NAIC proceedings, guidelines) |
| **SES** | Email delivery for change summaries |
| **CloudWatch** | Logs, metrics |

---

## RAG Agents

Both agents are configured via DynamoDB (`chatkit_production_config` table, key format `chat:{id}`) and can be updated without redeployment. When `PGVECTOR_ENABLED=true`, agents use the OpenAI Agents SDK with `search_knowledge_base` + `list_available_documents` tools; otherwise they fall back to direct Responses API calls.

### page_change_agent (`chat:web-tracking-agent`)

Receives before/after stripped HTML plus target context (label, URL, org_path, tags). Returns `list[dict]` — one or more alert dicts per page change. Each alert is stamped with an `agent_call_id` (UUID generated per invocation) and `config_hash` (MD5 of instructions + model).

**Multi-alert support:** If the DynamoDB `output_json_schema` wraps fields in a top-level `alerts` array, the agent can produce multiple alert rows from a single page change (e.g., a page that posts both a new meeting and new materials). `_unwrap_alerts()` extracts the list. If no `alerts` wrapper, the single dict is wrapped in a one-element list for backward compatibility.

The output schema is **fully dynamic** — driven by `output_json_schema` (a JSON Schema draft-07 object) stored in DynamoDB and written by the Bubble admin sync. Updating it there changes agent output, S3 storage, and dashboard columns with no code deploy.

Current output schema (as of May 2026) — all fields flat, no nested arrays:

```json
{
  "alert_type": "New Agenda & Materials",
  "alert_title": "...",
  "alert_description": "...",
  "alert_url": "https://...",
  "organization": ["NAIC"],
  "alert_date_time": "2026-04-21T00:00:00-04:00",
  "event_title": "N/A",
  "event_start_date_time": "N/A",
  "event_end_date_time": "N/A",
  "event_duration": "N/A",
  "event_is_full_day": "N/A",
  "event_url": "N/A",
  "event_call_in_number_access_code": "N/A",
  "agenda_item_title_and_chronicle_topics": [{"status": "New", "agenda_item_title": "...", "chronicle_topics": ["..."]}],
  "agenda_item_title_official": {"status": "N/A", "title_official": "N/A"},
  "agenda_item_standardized_id": {"status": "New", "standardized_id": "..."},
  "agenda_item_official_id": {"status": "N/A", "official_id": "N/A"},
  "library_item_preliminary_title": {"status": "New", "title": "..."},
  "library_item_url": "https://...",
  "library_items_file_name": "filename.pdf",
  "is_alert_relevant_for_art_newsreel": {"status": "Yes", "reference": "..."}
}
```

**Schema compatibility:** `alerts_table.jsonl` contains rows from two schema generations. Pre-May 2026 rows use nested arrays (`events[]`, `library_items[]`, `agenda_items[]`). The dashboard handles both formats via `FIELD_ALIASES` + `resolveCell()`.

**Schema sanitization:** `_sanitize_schema_for_openai()` strips unsupported JSON Schema keywords before passing to the OpenAI Responses API: removes `$schema`, replaces `oneOf` with `{"type":"string"}`, drops unsupported `format` values (e.g. `"uri"`).

### document_agent (`chat:document-data-extraction`)

Runs per library item detected by the page_change_agent. Fetches the actual PDF from the document URL, extracts plain text (pypdf + pdfminer.six fallback), and passes up to 12,000 chars to the agent along with the document title and URL.

**Two-step pgvector enforcement (when `PGVECTOR_ENABLED=true`):**
1. Agents SDK with pgvector search tools → gathers free-text analysis with knowledge base context
2. `chat_json()` with Structured Outputs → reformats free-text into the exact DynamoDB output schema

This ensures the knowledge base is actually searched (via Agents SDK tool calls) while producing strict schema-compliant output.

**When pgvector unavailable:** direct Responses API call with Structured Outputs. Does NOT skip — always produces output, just without knowledge base context.

Output schema is **fully dynamic** — whatever fields the DynamoDB `output_json_schema` instructs. Stored in `alerts/document_extractions_table.jsonl` (never mixed into `alerts_table.jsonl`).

Triggers for alert types: `New Materials`, `New Agenda & Materials`, `Updated Materials`, `Updated Agenda & Materials`, `New or Updated Report or Other Resource`, or any alert with `library_items` in output.

N/A guard: `_item_has_real_name()` skips items where the library item name is `"N/A"`, `"N/A."`, `"-"`, or empty.

### pgvector search

Hybrid RRF fusion of semantic (cosine via `halfvec`) + lexical (`ts_rank_cd`) search over the `document_chunks` / `documents` tables, followed by reranking (gpt-5.4, `reasoning_effort=low`). Scoped per agent by `pgvector_namespaces` from DynamoDB config (e.g., `bubble-data`, `art-chronicles`, `naic-proceedings`).

---

## Alert Tables

### `alerts/alerts_table.jsonl` — page_change_agent output

Append-only, one or more rows per page change. Consumed by the alerts dashboard.

**Schema is fully dynamic** — columns are derived directly from the agent output. Adding a field to the `web-tracking-agent` DynamoDB config (via the Bubble admin UI) automatically adds a column on the next run. No code changes required.

Storage rules:
- All agent output fields stored verbatim — no coercion, no N/A substitution
- Pipeline metadata added to every row: `run_id`, `run_timestamp`, `target_id`, `source_url`, `config_hash`, `agent_call_id`
- `agent_call_id` → UUID generated per `extract_page_change()` invocation. All rows from the same agent call share the same `agent_call_id`.
- `config_hash` → MD5 of `system_prompt + model` at time of run (used by re-evaluate feature)
- Flat schema detection: if agent output has no `events`/`library_items`/`agenda_items` arrays, all fields stored verbatim as top-level keys
- Backward compat: if nested arrays present, library items exploded into separate rows + first-item fields flattened

**Schema generations:**

| Generation | Period | Structure |
|------------|--------|-----------|
| Old schema | pre-May 2026 | Nested arrays: `events[]`, `library_items[]`, `agenda_items[]`; `organization` string; `is_relevant_for_art_newsreel` boolean |
| New schema | May 2026+ | All flat top-level fields; `organization` string array; complex object fields (`library_item_preliminary_title: {status, title}`, etc.); one or more rows per page change; `agent_call_id` per row |

### `alerts/document_extractions_table.jsonl` — document_agent output

Separate table written only when document_agent produces results. One row per library item processed.

Columns: pipeline metadata (`run_id`, `run_timestamp`, `target_id`, `source_url`, `agent_call_id`) + library item identity (`library_item_title`, `library_item_url`, `library_item_file_name`) + all fields returned by the document extraction agent verbatim (fully dynamic).

---

## Target Configuration

Targets are defined in `targets.json`:

```json
[
  {
    "id": "naic.e.life_rbc_wg",
    "label": "Life Risk-Based Capital Working Group",
    "org_id": "naic",
    "org_path": ["NAIC", "E", "Working Groups"],
    "group": "working_group",
    "tags": ["committee:E", "wg", "rbc"],
    "url": "https://content.naic.org/committees/e/life-risk-based-capital-wg",
    "extract": [
      {"type": "docs", "extractor": "link_collector_v1", "params": {"extensions": [".pdf"]}},
      {"type": "event_links", "extractor": "keyword_links_v1", "params": {"keywords": ["meeting", "agenda", "materials"]}},
      {"type": "meetings", "extractor": "naic_meetings_v1", "params": {}}
    ]
  }
]
```

| Field | Description |
|-------|-------------|
| `id` | Unique identifier for state persistence |
| `label` | Human-readable label passed to agents as context |
| `url` | Page to monitor |
| `extract` | Array of `{type, extractor, params}` rules |
| `org_path` | Path segments for organizational context (passed to agents) |
| `group` | Group type (e.g. `working_group`, `task_force`) |
| `tags` | Tags for filtering/categorization |

### Extractors

| Extractor | Output |
|-----------|--------|
| `link_collector_v1` | Links by file extension — `{title, url}` |
| `keyword_links_v1` | Links whose text matches keywords — `{title, url}` |
| `naic_meetings_v1` | NAIC Webex meeting blocks — `{title, date_text, time_text, webex_url, agenda_url, materials_url}` |
| `naic_events_v1` | NAIC event listings — `{title, datetime_text, url}` |

---

## Storage Layout (S3)

| Path | Contents |
|------|----------|
| `pages/<target_id>/YYYY/MM/DD/<run_id>/before.html` | Stripped HTML before change |
| `pages/<target_id>/YYYY/MM/DD/<run_id>/after.html` | Stripped HTML after change |
| `pages/<target_id>/YYYY/MM/DD/<run_id>/meta.json` | Run metadata (label, url, run_timestamp, first_run) |
| `pages/<target_id>/YYYY/MM/DD/<run_id>/agent_output.json` | Full page_change_agent output |
| `pages/<target_id>/YYYY/MM/DD/<run_id>/doc_extractions.json` | document_agent results (per-page) |
| `runs/YYYY/MM/DD/<run_id>/alerts.json` | Structured alerts for the run |
| `alerts/alerts_table.jsonl` | Append-only page_change_agent alert table (all runs) |
| `alerts/alerts_table.xlsx` | Excel version of alerts_table.jsonl, regenerated each run |
| `alerts/document_extractions_table.jsonl` | Append-only document_agent extraction table (separate from alerts) |
| `alerts/reruns/<run_id>/<target_id>/result.json` | Re-evaluate result (see Rerun Mode) |

---

## Dashboard

The alerts dashboard lives in a separate repo: **`NAICDashboard-`** (Next.js 14, deployed to ECS Fargate behind an ALB at `https://tracker.bridgewayanalytics.com`).

It reads `alerts/alerts_table.jsonl` from S3 and renders a dynamic table whose columns are derived entirely from `output_json_schema` + `output_requested_values` in DynamoDB. Column headers show human-readable Bubble labels. Field renames between schema generations are handled transparently via `FIELD_ALIASES` in `AlertsTable.tsx`.

The dashboard also implements the Re-evaluate UI (button -> confirmation modal -> ECS trigger -> inline amber result row below the original row -> Accept/Discard). Multiple reruns can be pending simultaneously. See `docs/rerun-feature.md` for the full API contract between the two systems.

Auth0 authentication protects all pages. The dashboard is accessible only to authorized users.

---

## Environment Variables

### State & Storage

| Var | Description |
|-----|-------------|
| `STATE_BACKEND` | `local` (default) or `dynamodb` |
| `STATE_TABLE` | DynamoDB table for per-target state |
| `PAGE_CHANGE_SNAPSHOT_BUCKET` | S3 bucket for before/after HTML |
| `CHANGELOG_BUCKET` | S3 bucket for alerts and changelogs |
| `CHANGELOG_PREFIX` | S3 prefix (default `changelog/`) |

### Agents & AI

| Var | Description |
|-----|-------------|
| `PAGE_CHANGE_AGENT_ENABLED` | `true` to enable RAG agents |
| `PGVECTOR_ENABLED` | `true` to enable pgvector knowledge base |
| `CHATKIT_CONFIG_TABLE` | DynamoDB table for agent configs (default: `chatkit_production_config`) |
| `OPENAI_API_KEY` | Required for agents and embeddings |
| `OPENAI_FETCH_FROM_SSM` | `true` to load OpenAI key from AWS SSM |
| `DATABASE_IP` | pgvector DB host |
| `DATABASE_NAME` | pgvector DB name |
| `DATABASE_USERNAME_CHATKIT` | pgvector DB user |
| `DATABASE_PASSWORD_CHATKIT` | pgvector DB password |
| `DATABASE_PORT` | pgvector DB port (default `6432`) |
| `RERUN_RUN_ID` | (Rerun mode) run_id to re-evaluate — set by ECS RunTask override |
| `RERUN_TARGET_ID` | (Rerun mode) target_id to re-evaluate — set by ECS RunTask override |
| `RERUN_MODE` | (Rerun mode) `"alerts"` / `"docs"` / `"both"` (default) — controls which agents run |

### Email

| Var | Description |
|-----|-------------|
| `SEND_EMAIL` | `true` to send via SES |
| `FROM_EMAIL` | Sender address (SES verified) |
| `TO_EMAILS` | Comma-separated recipient list |
| `SES_REGION` | AWS region for SES |

### Hardening

| Var | Default | Description |
|-----|---------|-------------|
| `MAX_RETRIES` | `3` | Fetch retries per target |
| `BACKOFF_SECONDS` | `2` | Retry backoff |
| `DELAY_BETWEEN_PAGES` | `1` | Seconds between target fetches |

### Bubble (legacy)

| Var | Description |
|-----|-------------|
| `BUBBLE_API_URL` | Bubble Data API root |
| `BUBBLE_API_KEY` | Bubble API key |
| `AI_ENRICHMENT_ENABLED` | Enable Bubble reference enrichment |

---

## Getting Started

### Local run

```bash
make install              # create venv, install deps
make install-playwright   # optional; falls back to requests
make run                  # run the pipeline
```

Minimal run without agents (dev/test):
```bash
python spike.py
```

Run with agents (requires OpenAI + DB creds):
```bash
PAGE_CHANGE_AGENT_ENABLED=true PGVECTOR_ENABLED=true python spike.py
```

Single target:
```bash
python spike.py --target-ids naic.e.life_rbc_wg
```

### Scripts

```bash
# Reprocess stored page changes through both agents
python scripts/backfill_alerts.py
python scripts/backfill_alerts.py --limit 5 --dry-run

# Backfill document_extractions_table.jsonl only (safe, never touches alerts_table.jsonl)
python scripts/backfill_document_extractions.py --limit 5 --dry-run
python scripts/backfill_document_extractions.py --limit 10

# Rebuild alerts_table.jsonl from stored agent_output.json (no agent re-run)
python scripts/rebuild_alerts_table.py
python scripts/rebuild_alerts_table.py --dry-run

# Wrap DynamoDB schema in alerts array for multi-alert support
python scripts/wrap_schema_alerts.py --dry-run

# Backfill agent_call_id on existing rows
python scripts/backfill_call_id.py --dry-run
```

### Deploy (ECS Fargate)

```bash
./scripts/deploy.sh              # build Docker, push ECR, terraform apply
./scripts/deploy.sh --run-task   # + trigger one ECS task immediately
./scripts/deploy.sh --tag v1.2   # use custom image tag
```

### Docker (local)

```bash
cp .env.example .env
docker compose up --build
```

---

## Repository Structure

```
web-change-tracker/
├── spike.py                        # Main pipeline orchestrator (normal + rerun mode)
├── targets.json                    # Target URL config
├── config/
│   ├── run_spec.py                 # RunSpec: CLI > env > defaults
│   └── chatkit_config.py           # DynamoDB agent config loader
├── storage/
│   ├── state_store_dynamodb.py     # DynamoDB per-target state
│   ├── state_store_local.py        # Local state.json (dev)
│   ├── alert_s3.py                 # Alert storage + flat/nested detection + Excel export
│   ├── page_change_s3.py           # Before/after HTML to S3
│   ├── html_snapshot_s3.py         # Raw HTML snapshots
│   ├── changelog_s3.py             # Append-only change event log
│   └── chunk_s3.py                 # Page chunks for vectorization
├── bubble/
│   ├── page_change_agent.py        # RAG agent: before/after HTML -> alert JSON
│   │                               #   extract_page_change(), _unwrap_alerts(),
│   │                               #   _sanitize_schema_for_openai(), get_config_hash()
│   ├── document_agent.py           # RAG agent: library item -> topic/agenda IDs
│   │                               #   extract_document_data(), two-step pgvector enforcement
│   ├── openai_client.py            # OpenAI Responses API: chat_json() (json_object + json_schema)
│   ├── pgvector/                   # pgvector connection pool + search tool
│   ├── calendar_alerts.py          # Calendar-based alert logic
│   ├── client.py                   # Bubble Data API client (legacy)
│   ├── lookups.py                  # Bubble read-only lookups (legacy)
│   ├── payload.py                  # Bubble payload building (legacy)
│   ├── enrich_refs.py              # Reference enrichment (legacy)
│   ├── ai_enrichment.py            # OpenAI payload enrichment (legacy)
│   └── doctor.py                   # CLI: Bubble diagnostics
├── scrape/
│   ├── html_content_extractor.py   # Strip HTML to content-only
│   ├── pdf_meeting_meta.py         # Extract meeting metadata from PDFs
│   ├── pdf_agenda_signals.py       # Extract agenda signals from PDFs
│   └── page_chunker.py             # Chunk pages for RAG
├── scripts/
│   ├── deploy.sh                         # Build Docker, push ECR, terraform apply
│   ├── backfill_alerts.py                # Reprocess stored page changes through agents
│   ├── backfill_document_extractions.py  # Backfill doc extractions only (safe, handles flat schema + list format)
│   ├── rebuild_alerts_table.py           # Rebuild from stored agent_output.json (no re-run)
│   ├── wrap_schema_alerts.py             # Wrap DynamoDB schema in alerts array
│   ├── backfill_call_id.py               # Backfill agent_call_id on existing rows
│   ├── entrypoint.sh                     # Docker entrypoint (supports arbitrary python commands via CMD override)
│   └── infer_pdf.py                      # PDF inference/analysis
├── docs/
│   └── rerun-feature.md            # Re-evaluate alert feature spec (shared with dashboard)
├── prompts/
│   └── org_tree.txt                # Organization hierarchy (dash-depth, 140+ orgs)
├── analysis/
│   └── pdf_agenda_detection/       # PDF structure analysis + sample dataset
├── infra/terraform/                # ECS, EventBridge, DynamoDB, S3, IAM
├── tests/                          # Unit/integration tests
└── debug/                          # Debug artifacts (gitignored)
```

---

## Testing

| Strategy | Description |
|----------|-------------|
| **Unit tests** | `pytest tests/` — payload building, enrichment, PDF extraction, calendar alerts |
| **Simulate change** | `python spike.py --simulate-change --target-ids <id>` — inject fake diff without scraping |
| **Snapshot mode** | `--snapshot-dir snapshots/` saves state; `--compare-snapshot` compares without updating state |
| **Backfill dry-run** | `scripts/backfill_alerts.py --dry-run` — test agent pipeline on stored HTML without writing |
| **Bubble diagnostics** | `python -m bubble.doctor list-trees` — read-only Bubble API checks |

---

## Security & Legal

- Public pages only; no authentication or private data scraped
- Throttle requests (`DELAY_BETWEEN_PAGES`); respect crawl delays
- Minimal state stored: URLs, hashes, change metadata only
- No Bubble write endpoints called; all Bubble integration is read-only
- Dashboard protected by Auth0 authentication

---

## License

TBD
