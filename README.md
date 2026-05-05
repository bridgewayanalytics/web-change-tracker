# web-change-tracker

A website change-tracking and intelligence pipeline that monitors NAIC web pages on a 6-hour schedule, detects meaningful changes, and uses RAG-based LLM agents to extract structured alerts from before/after HTML snapshots. Alerts feed a downstream dashboard.

**Infrastructure:** AWS · **Language:** Python

---

## Overview

The system monitors a configured list of target URLs, detects content changes via SHA256 fingerprinting, saves before/after HTML snapshots to S3, and passes them to LLM agents for structured extraction. Two agents run in sequence: one classifies the change and extracts events/documents/agenda items, the second matches detected documents to Chronicle topics and agenda items in the knowledge base. Results are stored as structured JSON + Excel in S3 and consumed by the alerts dashboard.

---

## Architecture

### Pipeline

```
EventBridge (6h cron)
        ↓
  ECS Fargate — spike.py
        ↓
  Load targets.json
        ↓
  For each target URL:
  ┌──────────────────────────────────────────────────┐
  │ 1. Fetch page (Playwright / requests)            │
  │ 2. SHA256 hash + pluggable extractors            │
  │    (docs, event_links, meetings, events)         │
  │ 3. Diff against DynamoDB state                   │
  │ 4. Save state to DynamoDB                        │
  │ 5. Store before/after stripped HTML → S3         │
  └──────────────────────────────────────────────────┘
        ↓  (if PAGE_CHANGE_AGENT_ENABLED)
  page_change_agent
  — compares before/after HTML
  — output schema driven by output_json_schema in DynamoDB (OpenAI Structured Outputs)
  — outputs: 21 flat fields (alert_type, event_*, agenda_item_*, library_item_*, etc.)
        ↓  (if materials-type alert)
  document_agent (per library item)
  — pgvector search over knowledge base
  — outputs: topic_ids, agenda_item_ids
        ↓
  store_run_alerts → S3
  — alerts_table.jsonl (append)
  — alerts_table.xlsx (regenerated)
  — per-run alerts.json
        ↓
  Email summary → SES
```

### AWS Resources

| Resource | Purpose |
|----------|---------|
| **EventBridge** | 6-hour cron schedule |
| **ECS Fargate** | Compute (Playwright requires container) |
| **DynamoDB** | Per-target page state; agent configs (`chatkit_production_config`) |
| **S3** | Before/after HTML snapshots, alerts JSONL/xlsx, changelogs |
| **PostgreSQL + pgvector** | Knowledge base for RAG agents (Chronicles, NAIC proceedings, guidelines) |
| **SES** | Email delivery for change summaries |
| **CloudWatch** | Logs, metrics |

---

## RAG Agents

Both agents are configured via DynamoDB (`chatkit_production_config` table, key format `chat:{id}`) and can be updated without redeployment. When `PGVECTOR_ENABLED=true`, agents use the OpenAI Agents SDK with `search_knowledge_base` + `list_available_documents` tools; otherwise they fall back to direct Responses API calls.

### page_change_agent (`chat:web-tracking-agent`)

Receives before/after stripped HTML plus target context (label, URL, org_path, tags). The output schema is **fully dynamic** — driven by `output_json_schema` (a JSON Schema draft-07 object) stored in DynamoDB and written by the Bubble admin sync. Updating it there changes agent output, S3 storage, and dashboard columns with no code deploy.

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

**Schema compatibility:** `alerts_table.jsonl` contains rows from two schema generations. Pre-May 2026 rows use nested arrays (`events[]`, `library_items[]`, `agenda_items[]`). The dashboard handles both formats.

**Schema sanitization:** `_sanitize_schema_for_openai()` in `page_change_agent.py` strips unsupported JSON Schema keywords before passing to the OpenAI Responses API: removes `$schema`, replaces `oneOf` with `{"type":"string"}`, drops unsupported `format` values (e.g. `"uri"`). The Bubble-generated schema may include these; they are stripped at runtime without modifying the stored schema.

### document_agent (`chat:document-data-extraction`)

Runs per library item detected by the page_change_agent. Fetches the actual PDF from the document URL, extracts plain text (pypdf + pdfminer.six fallback), and passes up to 12,000 chars to the agent along with the document title and URL. The agent also has access to pgvector search tools for cross-referencing the knowledge base (Chronicles, NAIC proceedings, guidelines, etc.).

Output schema is **fully dynamic** — whatever fields the DynamoDB `instructions` config tells the model to return are stored verbatim. No hardcoded output schema in code. As of April 2026, the instructions ask the agent to extract agenda items, organization, document type, Chronicle topics, standardized IDs, etc.

Results are stored in a **separate S3 table:** `alerts/document_extractions_table.jsonl` (never mixed into `alerts_table.jsonl`).

Triggers for:
- Alert types: `New Materials`, `New Agenda & Materials`, `Updated Materials`, `Updated Agenda & Materials`, `New or Updated Report or Other Resource`
- Any alert that has `library_items` in the agent output

**Important:** Returns `{}` (skip) if pgvector is unavailable. Without search tools, the model hallucinates IDs. Only run with `PGVECTOR_ENABLED=true` and valid DB credentials.

**Important:** A `## Output Format` JSON suffix is automatically appended to the DynamoDB `instructions` at runtime — the content team's instructions describe what to extract but don't specify JSON format. This suffix tells the model to return a single JSON object. Do not add a JSON format requirement to the DynamoDB config itself.

**Output schema source of truth:** The JSON output schema for `page_change_agent` is stored in DynamoDB as `output_json_schema` (a JSON Schema object written by the Bubble admin sync). `output_json_schema.required` is the ordered field list. `output_requested_values` is the parallel list of human-readable labels consumed by the dashboard. The DynamoDB `instructions` guide extraction behaviour and can be updated freely. Adding, renaming, or reordering output fields is done entirely in the Bubble admin UI — no code deploy needed.

### pgvector search

Hybrid RRF fusion of semantic (cosine via `halfvec`) + lexical (`ts_rank_cd`) search over the `document_chunks` / `documents` tables, followed by reranking. Scoped per agent by `pgvector_namespaces` from DynamoDB config (e.g., `bubble-data`, `art-chronicles`, `naic-proceedings`).

---

## Alert Tables

### `alerts/alerts_table.jsonl` — page_change_agent output

Append-only, one row per alert. Consumed by the alerts dashboard.

**Schema is fully dynamic** — columns are derived directly from the agent output. Adding a field to the `web-tracking-agent` DynamoDB config (via the Bubble admin UI) automatically adds a column on the next run. No code changes required.

Storage rules:
- All agent output fields stored verbatim — no coercion, no N/A substitution
- Pipeline metadata added to every row: `run_id`, `run_timestamp`, `target_id`, `source_url`, `config_hash`
- `config_hash` → MD5 of `system_prompt + model` at time of run (used by re-evaluate feature)
- For backward compatibility: if the agent output contains `events` / `agenda_items` / `library_items` arrays (old schema), they are stored as full JSON arrays AND first-item fields are flattened with `event_*` / `agenda_item_*` prefixes

**Schema generations:**

| Generation | Period | Structure |
|------------|--------|-----------|
| Old schema | pre-May 2026 | Nested arrays: `events[]`, `library_items[]`, `agenda_items[]`; `organization` string; `is_relevant_for_art_newsreel` boolean |
| New schema | May 2026+ | All flat top-level fields; `organization` string array; complex object fields (`library_item_preliminary_title: {status, title}`, etc.); one row per alert |

The dashboard handles both generations gracefully (blank cells for fields the other generation lacks).

### `alerts/document_extractions_table.jsonl` — document_agent output

Separate table written only when document_agent produces results. One row per library item processed.

Columns: pipeline metadata (`run_id`, `run_timestamp`, `target_id`, `source_url`) + library item identity (`library_item_title`, `library_item_url`, `library_item_file_name`) + all fields returned by the document extraction agent verbatim (fully dynamic).

**Never written to during alerts_table.jsonl updates and vice versa. These two tables are always written separately.**

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
| `alerts/reruns/<run_id>/<target_id>/result.json` | Re-evaluate result (see Re-evaluate Feature) |

---

## Re-evaluate Feature

Allows a stored alert to be re-run through the agents after updating DynamoDB instructions, without re-scraping the page. The before/after HTML is already stored in S3.

**Full spec:** `docs/rerun-feature.md` — read this before working on any part of this feature.

### How it works

1. The dashboard sends `{ run_id, target_id }` to `/api/rerun`
2. The dashboard API calls ECS `RunTask` with environment overrides `RERUN_RUN_ID` and `RERUN_TARGET_ID`
3. `spike.py` detects these vars at startup and enters rerun mode (skips normal pipeline)
4. Rerun mode: fetches stored HTML from S3, re-runs `page_change_agent` + `document_agent`, writes result to `alerts/reruns/<run_id>/<target_id>/result.json`
5. Dashboard polls for task completion, then fetches the result and shows a before/after diff
6. User clicks Accept (patches `alerts_table.jsonl`) or Discard (deletes rerun result)

### Config change detection

Every alert row stores a `config_hash` field (MD5 of `system_prompt + model`). Before triggering a rerun, the dashboard fetches the current config hash and compares it to the stored one. If they match, the user is warned that the config hasn't changed since the original run.

### Rerun result schema (`alerts/reruns/<run_id>/<target_id>/result.json`)

```json
{
  "run_id": "run-1776781376",
  "target_id": "naic.e.life_rbc_wg",
  "rerun_timestamp": "2026-04-22T...",
  "config_hash": "<md5>",
  "original_rows": [...],
  "rerun_rows": [...]
}
```

`original_rows` and `rerun_rows` use the same schema as `alerts_table.jsonl` rows.

---

## Dashboard

The alerts dashboard lives in a separate repo: **`NAICDashboard-`** (Next.js, deployed to ECS Fargate behind an ALB).

It reads `alerts/alerts_table.jsonl` from S3 and renders a dynamic table whose columns are derived entirely from `output_json_schema` + `output_requested_values` in DynamoDB (no hardcoded schema). Column headers show human-readable Bubble labels. Field renames between schema generations are handled transparently via `FIELD_ALIASES` in `AlertsTable.tsx` — old rows display under new column headers without data migration.

The dashboard also implements the Re-evaluate UI (button → confirmation modal → ECS trigger → inline amber result row below the original row → Accept/Discard). Multiple reruns can be pending simultaneously. See `docs/rerun-feature.md` for the full API contract between the two systems.

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
| `DATABASE_IP` | pgvector DB host |
| `DATABASE_NAME` | pgvector DB name |
| `DATABASE_USERNAME_CHATKIT` | pgvector DB user |
| `DATABASE_PASSWORD_CHATKIT` | pgvector DB password |
| `DATABASE_PORT` | pgvector DB port (default `6432`) |
| `RERUN_RUN_ID` | (Rerun mode) run_id to re-evaluate — set by ECS RunTask override |
| `RERUN_TARGET_ID` | (Rerun mode) target_id to re-evaluate — set by ECS RunTask override |

### Email

| Var | Description |
|-----|-------------|
| `SEND_EMAIL` | `true` to send via SES |
| `FROM_EMAIL` | Sender address (SES verified) |
| `TO_EMAILS` | Comma-separated recipient list |
| `SES_REGION` | AWS region for SES |

### Bubble (legacy)

| Var | Description |
|-----|-------------|
| `BUBBLE_API_URL` | Bubble Data API root |
| `BUBBLE_API_KEY` | Bubble API key |
| `AI_ENRICHMENT_ENABLED` | Enable Bubble reference enrichment |

### Hardening

| Var | Default | Description |
|-----|---------|-------------|
| `MAX_RETRIES` | `3` | Fetch retries per target |
| `BACKOFF_SECONDS` | `2` | Retry backoff |
| `DELAY_BETWEEN_PAGES` | `1` | Seconds between target fetches |

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

### Backfill alerts from stored snapshots

Re-run agents on previously stored before/after HTML without re-scraping:
```bash
python scripts/backfill_alerts.py
python scripts/backfill_alerts.py --limit 5 --dry-run
```

Backfill **only** `document_extractions_table.jsonl` from already-stored `agent_output.json` files — **safe, never touches `alerts_table.jsonl`**:
```bash
python scripts/backfill_document_extractions.py --limit 5 --dry-run
python scripts/backfill_document_extractions.py --limit 10
```

Rebuild `alerts_table.jsonl` from already-stored `agent_output.json` files (no agent re-run, useful for deduplication or schema fixes):
```bash
python scripts/rebuild_alerts_table.py
python scripts/rebuild_alerts_table.py --dry-run
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
├── spike.py                        # Main pipeline orchestrator
├── targets.json                    # Target URL config
├── config/
│   ├── run_spec.py                 # RunSpec: CLI > env > defaults
│   └── chatkit_config.py           # DynamoDB agent config loader
├── storage/
│   ├── state_store_dynamodb.py     # DynamoDB per-target state
│   ├── state_store_local.py        # Local state.json (dev)
│   ├── alert_s3.py                 # Alert storage + Excel export
│   ├── page_change_s3.py           # Before/after HTML to S3
│   ├── html_snapshot_s3.py         # Raw HTML snapshots
│   ├── changelog_s3.py             # Append-only change event log
│   └── chunk_s3.py                 # Page chunks for vectorization
├── bubble/
│   ├── page_change_agent.py        # RAG agent: before/after HTML → alert JSON
│   ├── document_agent.py           # RAG agent: library item → topic/agenda IDs
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
│   ├── backfill_alerts.py                # Reprocess stored page changes through agents
│   ├── backfill_document_extractions.py  # Backfill document_extractions_table.jsonl only (safe, never touches alerts_table.jsonl)
│   ├── rebuild_alerts_table.py           # Rebuild alerts_table.jsonl from stored agent_output.json (no re-run)
│   ├── deploy.sh                         # Build Docker, push ECR, terraform apply
│   └── infer_pdf.py                      # PDF inference/analysis
├── docs/
│   └── rerun-feature.md            # Re-evaluate alert feature spec (shared between this repo + dashboard)
├── analysis/
│   └── pdf_agenda_detection/       # PDF structure analysis + sample dataset
├── infra/terraform/                # ECS, EventBridge, DynamoDB, S3, IAM
├── tests/                          # Unit/integration tests
├── prompts/
│   └── org_tree.txt                # Organization hierarchy (dash-depth, 140+ orgs) sourced from Bubble API
│                                   # Injected into agent context to guide organization field assignment
└── debug/                          # Debug artifacts (gitignored)
```

---

## Testing

| Strategy | Description |
|----------|-------------|
| **Unit tests** | `pytest tests/` — payload building, enrichment, PDF extraction, calendar alerts |
| **Simulate change** | `python spike.py --simulate-change --target-ids <id>` — inject fake diff without scraping |
| **Snapshot mode** | `--snapshot-dir snapshots/` saves state; `--compare-snapshot` compares without updating state |
| **Backfill** | `scripts/backfill_alerts.py --dry-run` — test agent pipeline on stored HTML without writing |
| **Bubble diagnostics** | `python -m bubble.doctor list-trees` — read-only Bubble API checks |

---

## Security & Legal

- Public pages only; no authentication or private data scraped
- Throttle requests (`DELAY_BETWEEN_PAGES`); respect crawl delays
- Minimal state stored: URLs, hashes, change metadata only
- No Bubble write endpoints called; all Bubble integration is read-only

---

## License

TBD
