# web-change-tracker

Website change-tracking pipeline that monitors NAIC web pages on a 6-hour schedule, detects meaningful changes, and uses LLM agents to extract structured alerts from before/after HTML snapshots.

Alerts feed the [NAIC Dashboard](../NAICDashboard-) at `https://tracker.bridgewayanalytics.com`.

---

## How it works

```
 EventBridge (every 6 hours)
          │
          ▼
 ┌─────────────────────────────────────────────────────────────┐
 │  ECS Fargate — spike.py                                     │
 │                                                             │
 │  1. Load targets.json (50+ NAIC page URLs)                  │
 │  2. For each target:                                        │
 │     ├─ Fetch page (Playwright or requests)                  │
 │     ├─ Run pluggable extractors (PDFs, meetings, events)    │
 │     ├─ SHA256 fingerprint → diff against DynamoDB state     │
 │     └─ If changed: save before/after HTML to S3             │
 │                                                             │
 │  3. page_change_agent (OpenAI + pgvector RAG)               │
 │     ├─ Compares before/after HTML                           │
 │     ├─ Outputs structured alert(s) via Structured Outputs   │
 │     └─ Schema fully dynamic — driven by DynamoDB config     │
 │                                                             │
 │  4. document_agent (per detected document)                  │
 │     ├─ Fetches PDF, extracts text                           │
 │     ├─ Two-step: Agents SDK + pgvector → Structured Outputs │
 │     └─ Matches to agenda items, chronicle topics            │
 │                                                             │
 │  5. Store results to S3                                     │
 │     ├─ alerts_table.jsonl          (alert rows, append)     │
 │     ├─ alerts_table.xlsx           (regenerated each run)   │
 │     ├─ doc_extractions_table.jsonl (doc rows, append)       │
 │     └─ per-run snapshots + agent_output.json                │
 │                                                             │
 │  6. Email summary via SES                                   │
 └─────────────────────────────────────────────────────────────┘
          │
          ▼
 Dashboard reads JSONL from S3, renders dynamic tables
```

---

## Key concepts

**Dynamic schema** — Agent output fields, column order, and column labels are all driven by `output_json_schema` + `output_requested_values` in DynamoDB. Change them in the Bubble admin UI → agent output + dashboard columns update automatically. No code deploy.

**Multi-alert** — One page change can produce multiple alert rows (e.g. a new meeting + new materials). The schema wraps fields in a top-level `alerts` array; `_unwrap_alerts()` extracts the list.

**Per-document extraction** — The document agent processes each library item independently (never batched). Re-evaluation can be scoped to a single document via `RERUN_LIBRARY_ITEM_URL`.

**Two schema generations** — Pre-May 2026 rows use nested arrays (`events[]`, `library_items[]`). May 2026+ rows are flat with prefixed fields. Both the pipeline and dashboard handle both transparently.

---

## Rerun mode

When the dashboard triggers a re-evaluation, it launches an ECS task with env overrides:

| Env var | Purpose |
|---------|---------|
| `RERUN_RUN_ID` | Which run to re-evaluate |
| `RERUN_TARGET_ID` | Which target page |
| `RERUN_MODE` | `"alerts"` (page agent only), `"docs"` (doc agent only), `"both"` |
| `RERUN_LIBRARY_ITEM_URL` | Scope doc re-eval to a single document (optional) |

The result is written to `alerts/reruns/<run_id>/<target_id>/result.json`. The dashboard shows an amber diff row; the user accepts (patches JSONL) or discards (deletes result).

---

## AWS resources

| Resource | Purpose |
|----------|---------|
| **ECS Fargate** | Runs pipeline (Playwright needs a container) |
| **EventBridge** | 6-hour cron trigger |
| **DynamoDB** | Per-target page state + agent configs (`chatkit_production_config`) |
| **S3** | HTML snapshots, alert/doc JSONL, Excel, rerun results |
| **PostgreSQL + pgvector** | Knowledge base for RAG (Chronicles, proceedings, guidelines) |
| **SES** | Email delivery |
| **CloudWatch** | Logs |

---

## Getting started

```bash
make install                # create venv, install deps
make install-playwright     # optional (falls back to requests)

# Minimal run (no agents)
python spike.py

# With agents
PAGE_CHANGE_AGENT_ENABLED=true PGVECTOR_ENABLED=true python spike.py

# Single target
python spike.py --target-ids naic.e.life_rbc_wg

# Simulate a change (fake diff, no scraping)
python spike.py --simulate-change --target-ids naic.e.life_rbc_wg
```

### Deploy

```bash
AWS_PROFILE=bridgeway ./scripts/deploy.sh              # build + push + terraform
AWS_PROFILE=bridgeway ./scripts/deploy.sh --run-task   # + trigger one ECS task now
```

The Docker entrypoint supports arbitrary Python commands via CMD override:
```bash
# Run backfill on ECS (command override in ECS RunTask)
python scripts/backfill_document_extractions.py --limit 10
```

---

## Scripts

| Script | What it does |
|--------|-------------|
| `deploy.sh` | Build Docker, push ECR, terraform apply |
| `backfill_alerts.py` | Re-run both agents on stored HTML snapshots |
| `backfill_document_extractions.py` | Re-run doc agent only from stored `agent_output.json` (safe — never touches `alerts_table.jsonl`) |
| `rebuild_alerts_table.py` | Rebuild JSONL from stored `agent_output.json` (no agent re-run) |
| `wrap_schema_alerts.py` | Wrap DynamoDB schema in `alerts` array for multi-alert support |
| `backfill_call_id.py` | One-time: backfill `agent_call_id` on existing JSONL rows |

All backfill scripts support `--dry-run` and `--limit N`.

---

## Target configuration

Targets are defined in `targets.json`:

```json
{
  "id": "naic.e.life_rbc_wg",
  "label": "Life Risk-Based Capital Working Group",
  "org_path": ["NAIC", "E", "Working Groups"],
  "url": "https://content.naic.org/committees/e/life-risk-based-capital-wg",
  "extract": [
    {"type": "docs", "extractor": "link_collector_v1", "params": {"extensions": [".pdf"]}},
    {"type": "event_links", "extractor": "keyword_links_v1", "params": {"keywords": ["meeting", "agenda"]}},
    {"type": "meetings", "extractor": "naic_meetings_v1", "params": {}}
  ]
}
```

Extractors are pluggable: `link_collector_v1`, `keyword_links_v1`, `naic_meetings_v1`, `naic_events_v1`.

---

## S3 layout

```
changelog/
├── pages/<target_id>/YYYY/MM/DD/<run_id>/
│   ├── before.html              # Stripped HTML before change
│   ├── after.html               # Stripped HTML after change
│   ├── meta.json                # Run metadata
│   ├── agent_output.json        # page_change_agent output
│   └── doc_extractions.json     # document_agent results
├── alerts/
│   ├── alerts_table.jsonl       # All alert rows (append-only)
│   ├── alerts_table.xlsx        # Excel export (regenerated each run)
│   ├── document_extractions_table.jsonl  # All doc extraction rows
│   └── reruns/<run_id>/<target_id>/result.json  # Rerun diff results
└── runs/YYYY/MM/DD/<run_id>/
    └── alerts.json              # Per-run structured alerts
```

---

## Environment variables

### Core

| Var | Description |
|-----|-------------|
| `STATE_BACKEND` | `local` or `dynamodb` (default: local) |
| `STATE_TABLE` | DynamoDB state table name |
| `PAGE_CHANGE_SNAPSHOT_BUCKET` | S3 bucket for HTML snapshots |
| `CHANGELOG_BUCKET` | S3 bucket for alerts and changelogs |

### Agents

| Var | Description |
|-----|-------------|
| `PAGE_CHANGE_AGENT_ENABLED` | `true` to enable LLM agents |
| `PGVECTOR_ENABLED` | `true` to enable knowledge base search |
| `CHATKIT_CONFIG_TABLE` | DynamoDB config table (default: `chatkit_production_config`) |
| `OPENAI_API_KEY` | Required for agents |
| `OPENAI_FETCH_FROM_SSM` | `true` to load key from AWS SSM |
| `DATABASE_IP`, `DATABASE_NAME`, `DATABASE_USERNAME_CHATKIT`, `DATABASE_PASSWORD_CHATKIT`, `DATABASE_PORT` | pgvector connection |

### Email

| Var | Description |
|-----|-------------|
| `SEND_EMAIL` | `true` to send via SES |
| `FROM_EMAIL` | Sender address |
| `TO_EMAILS` | Comma-separated recipients |
| `SES_REGION` | AWS region for SES |

---

## Repository structure

```
web-change-tracker/
├── spike.py                     # Pipeline orchestrator (normal + rerun mode)
├── targets.json                 # Target URL configuration
├── config/
│   ├── run_spec.py              # Runtime config (CLI > env > defaults)
│   └── chatkit_config.py        # DynamoDB agent config loader
├── storage/
│   ├── alert_s3.py              # Alert storage, schema detection, Excel export
│   ├── page_change_s3.py        # Before/after HTML snapshots
│   ├── state_store_dynamodb.py  # DynamoDB state backend
│   └── state_store_local.py     # Local state (dev)
├── bubble/
│   ├── page_change_agent.py     # Page change agent (extract_page_change)
│   ├── document_agent.py        # Document extraction agent (extract_document_data)
│   ├── openai_client.py         # OpenAI Responses API client
│   └── pgvector/                # pgvector search (hybrid RRF + reranking)
├── scrape/
│   ├── html_content_extractor.py
│   ├── pdf_meeting_meta.py
│   └── pdf_agenda_signals.py
├── scripts/                     # Deploy, backfill, schema tools
├── prompts/org_tree.txt         # Organization hierarchy (140+ orgs)
├── infra/terraform/             # ECS, EventBridge, DynamoDB, S3, IAM
├── docs/                        # Feature specs
└── tests/                       # Unit/integration tests
```

---

## Testing

```bash
pytest tests/                                                    # Unit tests
python spike.py --simulate-change --target-ids <id>              # Inject fake diff
python scripts/backfill_alerts.py --dry-run --limit 5            # Test agent pipeline
```

---

## Further reading

- `CLAUDE.md` — Detailed implementation reference (schema fields, agent config, conventions)
- `docs/rerun-feature.md` — Re-evaluate feature spec
- `docs/bubble_object_schemas.md` — Bubble.io data model reference
