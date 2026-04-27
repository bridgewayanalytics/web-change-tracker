# CLAUDE.md — Project Context

## What this project does

Website change-tracking system that monitors configured NAIC web pages on a 6-hour schedule, detects meaningful changes (new PDFs, meetings, agenda items), and runs RAG-based LLM agents on before/after HTML snapshots to produce structured alerts. Alerts feed a downstream dashboard. Bubble.io integration exists but is currently legacy — a new Bubble pipeline is planned but not yet built.

## Tech stack

- **Language:** Python
- **Scraping:** Playwright (JS-rendered), requests + BeautifulSoup (simple pages)
- **Change detection:** SHA256 fingerprinting, difflib
- **RAG agents:** OpenAI Agents SDK + pgvector (hybrid semantic + lexical search via PostgreSQL)
- **Agent config:** DynamoDB `chatkit_production_config` table (system prompts, model, namespaces)
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
   - Output schema is **fully dynamic** — field names defined by `output_schema_json` attribute in DynamoDB. The schema string is injected verbatim into the agent's user message; editing it in DynamoDB changes agent output without a code deploy.
   - Current core fields: `{alert_type, alert_title, alert_description, alert_url, organization, alert_date_time, is_relevant_for_art_newsreel, events, library_items, agenda_items}`
8. **document_agent** — For materials-type alerts, finds Chronicle topic IDs + agenda item IDs via pgvector search
   - Config key: `chat:web-tracking-document-matching` in DynamoDB
   - Output: `{topic_ids, agenda_item_ids, summary}`
9. **Alert storage** (`alert_s3.py`) — writes `alerts_table.jsonl`, per-run `alerts.json`, `alerts_table.xlsx` to S3
10. **Notifier** (SES) — email summary of changes

## Key directories

- `storage/` — State store (DynamoDB prod / `state.json` dev), S3 for HTML snapshots, alerts, changelogs
- `bubble/` — RAG agents (`page_change_agent.py`, `document_agent.py`), legacy Bubble.io integration, pgvector client
- `bubble/pgvector/` — pgvector connection pool and search tool (mirrors ChatKit infrastructure)
- `scrape/` — HTML content extraction, PDF metadata, page chunking
- `config/` — RunSpec (CLI > env > defaults), chatkit DynamoDB config loader
- `scripts/` — Deploy, backfill, smoke tests
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
- `storage/alert_s3.py` — Alert storage and Excel export

## Agent configuration (DynamoDB)

Table: `chatkit_production_config` (env: `CHATKIT_CONFIG_TABLE`)
Key format: `chat:{chat_id}`

| Chat ID | Agent | Fields |
|---------|-------|--------|
| `web-tracking-agent` | page_change_agent | `instructions`, `model`, `reasoning_effort`, `pgvector_namespaces`, `output_schema_json` |
| `web-tracking-document-matching` | document_agent | `instructions`, `model`, `reasoning_effort`, `pgvector_namespaces` |

`output_schema_json` on `web-tracking-agent` is a JSON string injected into the agent's user message as the output schema. Editing it in DynamoDB renames/adds fields in agent output and dashboard columns simultaneously — no code deploy needed. The dashboard's `/api/schema` route reads this same attribute to derive column order.

## Common commands

```bash
make install                          # create venv, install deps
make run                              # run the pipeline
python spike.py                       # minimal local run
python spike.py --target-ids <id>     # run single target
python scripts/backfill_alerts.py     # reprocess stored page changes through agents
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
- Agent configs live in DynamoDB — change system prompts/models without redeploying
- pgvector search uses RRF fusion of semantic (halfvec cosine) + lexical (tsquery) results, then reranked
- Chronicle topics are a fixed taxonomy in Bubble.io (87 nodes in "Chronicles" tree) — resources/events get mapped to them via agenda item analysis
- Alert table schema (`alerts_table.jsonl` / `.xlsx`) is dynamic — columns mirror exact agent output field names. Top-level agent fields pass through **verbatim** (no coercion, no N/A substitution). Nested arrays (`events`, `agenda_items`) stored as full JSON arrays. `library_items` flattened one row per item with `library_item_*` prefix. New fields added to agent output appear automatically.
- **Storage is verbatim** — `alert_s3.py` stores exactly what the agent outputs. Null stays null, empty string stays empty. Dashes in the dashboard = agent output null or field absent. Do NOT add coercion (null→"N/A") to `_flatten_val` or `_build_table_rows` — it masks agent deviations from instructions.
- Bubble.io integration is currently legacy; new Bubble pipeline is planned
- Debug artifacts go to `debug/` directory (gitignored)
