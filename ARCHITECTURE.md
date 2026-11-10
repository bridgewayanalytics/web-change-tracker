# Architecture

Production system architecture for the NAIC web change-tracking pipeline.

---

## System overview

```
 EventBridge (every 6 hours)
          │
          ▼
 ┌─────────────────────────────────────────────────────────────────┐
 │  ECS Fargate — spike.py                                         │
 │                                                                 │
 │  1. Load targets.json (50+ NAIC page URLs)                      │
 │  2. Scrape each target (Playwright or requests fallback)        │
 │  3. Run pluggable extractors (PDFs, meetings, events)           │
 │  4. SHA256 fingerprint → diff against DynamoDB state            │
 │  5. If changed → save before/after HTML to S3                   │
 │  6. page_change_agent (OpenAI Agents SDK + hosted KB)           │
 │     └─ Structured alert(s) via Structured Outputs               │
 │  7. document_agent (per detected document, independently)       │
 │     └─ Two-step: Agents SDK + KB search → Structured Outputs    │
 │  8. Store results to S3 (JSONL, Excel, per-run snapshots)       │
 │  9. Email summary via SES                                       │
 └─────────────────────────────────────────────────────────────────┘
          │
          ▼
 Dashboard (NAICDashboard-) reads JSONL from S3, renders tables
```

---

## AWS resources

| Resource | Purpose |
|----------|---------|
| **ECS Fargate** | Runs pipeline container (Playwright requires Docker) |
| **EventBridge** | 6-hour cron schedule + dashboard-triggered reruns |
| **DynamoDB** | Per-target page state (`web-change-tracker-state`) + agent configs (`chatkit_production_config`) |
| **S3** | HTML snapshots, alert/doc JSONL, Excel exports, rerun results |
| **ChatKit backend `/knowledge/*` API** | Hosted knowledge base for RAG (Chronicles, Newsreels, Guidelines, orgs, proceedings). Hybrid search + Decisions-API reranker server-side. Agents call it via HTTP; no direct DB access from this repo. |
| **SES** | Email delivery |
| **ECR** | Docker image registry |
| **CloudWatch** | Logs |
| **SSM Parameter Store** | OpenAI API key (fetched at runtime when `OPENAI_FETCH_FROM_SSM=true`) |

---

## Pipeline stages

### 1. Target loading

Targets are defined in `targets.json`. Each target specifies a URL and an array of pluggable extractors:

- `link_collector_v1` — Collect PDF/document links
- `keyword_links_v1` — Collect links matching keywords (e.g. "meeting", "agenda")
- `naic_meetings_v1` — Extract meeting metadata from NAIC meeting pages
- `naic_events_v1` — Extract event metadata from NAIC event listings

### 2. Scraping

Playwright (headless Chromium) for JS-rendered pages, with automatic fallback to `requests + BeautifulSoup` for simple pages or when Playwright is unavailable. HTML is stripped to content-relevant sections before diffing.

### 3. Change detection

SHA256 fingerprint of extracted content is compared against the stored state in DynamoDB. If the fingerprint differs, the page is marked as changed and before/after HTML snapshots are saved to S3.

### 4. page_change_agent

Compares before/after HTML to produce structured alert JSON.

- **Config:** DynamoDB key `chat:web-tracking-agent` in `chatkit_production_config`
- **Model:** OpenAI (currently gpt-6-luna)
- **RAG:** HTTP to the ChatKit backend's `/knowledge/search` (hybrid semantic + lexical + Decisions-API reranker, server-side)
- **HTML:** Before/after snapshots inlined in the user prompt — the WTA's whole job is to diff them, so inlining is the point
- **Output:** Dynamic JSON Schema from DynamoDB, enforced via OpenAI Structured Outputs
- **Multi-alert:** One page change can produce multiple alert rows (schema wraps fields in `alerts` array; pipeline unwraps)

### 5. document_agent

Processes each detected document independently (never batched).

- **Config:** DynamoDB key `chat:document-data-extraction`
- **Source document:** Ingested to the backend via `POST /knowledge/document-extraction/{uuid}`, then the stitched text is pulled via `GET .../text` and **inlined** in the user prompt under `=== SOURCE DOCUMENT ===`
- **Page HTML:** Before/after snapshots ingested via `bubble/page_snapshot_ingest.py` into `page-snapshot:<uuid>` namespaces; the agent reaches them via `search_knowledge_base` (not inlined)
- **Two-step:** Step 1 — Agents SDK with `search_knowledge_base` tool for free-text analysis; Step 2 — Structured Outputs reformats to output schema
- **Degraded path:** When `CHATKIT_INTERNAL_API_KEY` is absent, a direct Responses API call with no search tools (HTML is inlined as the only way to see page metadata)
- **Skips:** Items where the library item name is "N/A", "N/A.", or "-"

### 6. Storage

All results written to S3 via `alert_s3.py`:

- `alerts_table.jsonl` — Append-only alert rows
- `alerts_table.xlsx` — Excel export (regenerated each run)
- `document_extractions_table.jsonl` — Append-only doc extraction rows
- Per-run snapshots: `before.html`, `after.html`, `meta.json`, `agent_output.json`, `doc_extractions.json`

Storage is verbatim — exactly what the agent outputs, no coercion.

### 7. Email notification

SES sends a summary of detected changes to configured recipients.

---

## Dynamic schema system

Agent output fields, column order, and column labels are driven entirely by DynamoDB config. No code deploy needed to change what agents output or what the dashboard displays.

| DynamoDB field | Purpose |
|----------------|---------|
| `output_json_schema` | JSON Schema object defining agent output fields |
| `output_requested_values` | Ordered human-readable column labels |
| `instructions` | System prompt |
| `model` | OpenAI model ID |
| `pgvector_namespaces` | Knowledge base namespaces for RAG (legacy field name — now passed to the backend `/knowledge/search` API) |

The Bubble admin UI writes `output_json_schema` + `output_requested_values` to DynamoDB. The dashboard's `/api/schema` endpoint reads these to render column headers.

**Schema sanitization:** `_sanitize_schema_for_openai()` strips keywords OpenAI rejects in strict mode (`$schema`, `oneOf` → `{"type":"string"}`, unsupported `format` values).

**Schema generations:** Pre-May 2026 rows use nested arrays (`events[]`, `library_items[]`, `agenda_items[]`). May 2026+ rows are flat with prefixed fields. Both the pipeline and dashboard handle both transparently via field aliases.

---

## Rerun mode

The dashboard can trigger re-evaluation of agent output for a specific run+target. An ECS task is launched with env overrides:

| Env var | Purpose |
|---------|---------|
| `RERUN_RUN_ID` | Which run to re-evaluate |
| `RERUN_TARGET_ID` | Which target page |
| `RERUN_MODE` | `"alerts"` (page agent), `"docs"` (doc agent), `"both"` |
| `RERUN_LIBRARY_ITEM_URL` | Scope doc re-eval to a single document (optional) |

### Flow

1. Dashboard triggers ECS task with rerun env vars
2. `spike.py` loads stored HTML snapshots (no re-scraping)
3. Runs the specified agent(s) on stored data
4. Writes result to `alerts/reruns/<run_id>/<target_id>/result.json`
5. Dashboard shows amber diff row with original vs. rerun comparison
6. User accepts (patches JSONL) or discards (deletes result)

### Per-document re-evaluation

Document re-eval can be scoped to a single document via `RERUN_LIBRARY_ITEM_URL`. The library items loop in `_run_rerun()` skips URLs that don't match. The accept endpoint replaces only the matching document's row in the JSONL.

---

## S3 layout

```
changelog/
├── pages/<target_id>/YYYY/MM/DD/<run_id>/
│   ├── before.html
│   ├── after.html
│   ├── meta.json
│   ├── agent_output.json
│   └── doc_extractions.json
├── alerts/
│   ├── alerts_table.jsonl
│   ├── alerts_table.xlsx
│   ├── document_extractions_table.jsonl
│   └── reruns/<run_id>/<target_id>/result.json
└── runs/YYYY/MM/DD/<run_id>/
    └── alerts.json
```

---

## Knowledge base (ChatKit backend API)

Agent context comes from the ChatKit backend's hosted knowledge base, reached via HTTP. This repo has no direct database access — the backend owns chunking, embedding, hybrid retrieval, and reranking.

**Endpoints used:**
- `POST /knowledge/document-extraction/{uuid}` — ingest a document (text/plain or raw PDF/HTML)
- `GET  /knowledge/document-extraction/{uuid}/text` — fetch stitched source text (for inlining)
- `GET  /knowledge/document-extraction/{uuid}/status` — poll until indexed
- `POST /knowledge/search` — hybrid semantic + lexical search with Decisions-API reranker

**Reference namespaces** (searchable, populated by the backend team):
`bubble-data`, `ba:chronicles`, `ba:newsreels`, `ba:NAIC Guidelines`, `naic-proceedings`, `ba:International Guidelines`, `ba:Rating Agencies`, `ba:organizations`

**Per-call namespaces** (populated by this repo at invocation time):
- `document-extraction:<uuid>` — one per source document (PDF or transcript); content inlined directly in the prompt, so the agent's search tool excludes these.
- `page-snapshot:<uuid>` — one per `(agent_call_id, side)`; ingested via `bubble/page_snapshot_ingest.py` and appended to the agent's search scope so `Updated:` labels in link `title=` attributes are reachable without prompt-size cost.

Auth: `CHATKIT_INTERNAL_API_KEY` (loaded from SSM in prod). Full HTTP contract: `docs/knowledge-base-integration.md`.

---

## Dashboard integration

The downstream dashboard (`NAICDashboard-` repo) at `tracker.bridgewayanalytics.com`:

- Reads `alerts_table.jsonl` and `document_extractions_table.jsonl` from S3
- Reads column schema from `/api/schema` (backed by DynamoDB `output_requested_values`)
- Handles both old (nested) and new (flat) schema rows via field aliases
- Triggers reruns via `/api/doc-rerun` and `/api/rerun` API routes → ECS tasks
- Shows rerun results as amber diff rows with accept/discard actions

---

## Infrastructure as code

All AWS resources are defined in `infra/terraform/`:

- ECS cluster, task definition, ECR repository
- EventBridge schedule rule
- DynamoDB tables (state + config)
- S3 buckets (snapshots + changelog)
- IAM roles and policies
- Security groups and VPC networking
- CloudWatch log groups

### Deploy

```bash
AWS_PROFILE=bridgeway ./scripts/deploy.sh              # build + push + terraform
AWS_PROFILE=bridgeway ./scripts/deploy.sh --run-task   # + trigger one ECS task
```

The Docker entrypoint (`entrypoint.sh`) supports arbitrary Python commands via CMD override, enabling backfill scripts and one-off tasks to run on the same ECS infrastructure.

---

## Key identifiers

| ID | Scope | Format |
|----|-------|--------|
| `run_id` | Entire pipeline run | `run-<epoch>` |
| `target_id` | Single target page | `naic.e.life_rbc_wg` etc. |
| `agent_call_id` | Single agent invocation | UUID v4 |

`agent_call_id` is more granular than `run_id` — a single run processes many targets, and each target may produce multiple agent calls.
