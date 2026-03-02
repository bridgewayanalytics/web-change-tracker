# web-change-tracker

A reusable website change-tracking system that monitors multiple web pages on a schedule, detects meaningful changes (new meetings, new PDFs/resources, updated content), persists "last seen" state, and produces human-readable change summaries via email.

**Infrastructure:** AWS · **Language:** Python

---

## Overview

The system takes a configured list of target URLs, fetches each page (including JavaScript-rendered sites), extracts normalized signals, computes fingerprints for change detection, and compares against stored state. When changes are found, it records change events, optionally downloads new/changed documents, and sends an email summary.

---

## Requirements

### Functional

| Requirement | Description |
|-------------|-------------|
| **Configure targets** | URLs, optional CSS/XPath selectors, labels, per-target schedule |
| **Detect changes robustly** | Minimal false positives via fingerprinting and diff logic |
| **Track assets** | New/changed downloadable assets (e.g., PDFs) |
| **Email summary** | Concise change report: who/what/when/link |
| **Persist state** | Last-seen fingerprints and change history |

### Non-Functional

| Requirement | Description |
|-------------|-------------|
| **Idempotency** | Do not re-notify for the same change |
| **Observability** | Logs, metrics, basic alerting |
| **Rate limiting** | Polite scraping; respect crawl delays |
| **Resilience** | Retries, timeouts, graceful degradation |
| **Extensibility** | Pluggable extractors for new website patterns |

---

## Architecture

### Components

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  Scheduler  │────▶│   Runner    │────▶│   Scraper   │────▶│  Normalizer │
│ EventBridge │     │ Lambda/ECS  │     │ Playwright  │     │  Extractors │
└─────────────┘     └─────────────┘     └─────────────┘     └──────┬──────┘
                                                                   │
┌─────────────┐     ┌─────────────┐     ┌─────────────┐            │
│  Notifier   │◀────│Persist State│◀────│ Diff Engine │◀───────────┘
│  SES/Email  │     │  DynamoDB   │     │  Fingerprint│
└─────────────┘     └─────────────┘     └─────────────┘
                          │
                          ▼
                   ┌─────────────┐
                   │     S3      │  (PDFs, versioned artifacts)
                   └─────────────┘
```

| Component | Responsibility |
|-----------|----------------|
| **Scheduler** | EventBridge cron triggers run at configured intervals (e.g., every 6 hours) |
| **Runner** | Lambda or ECS Fargate invokes the change-detection pipeline |
| **Scraper** | Fetches pages via Playwright (JS-rendered) or requests + BeautifulSoup (simple pages) |
| **Normalizer** | Extracts signals (meetings, docs, text) using pluggable extractors |
| **Diff Engine** | Computes fingerprints (SHA256), compares to stored state, detects changes |
| **Persist State** | `storage/` module: local (`state.json`) for dev; DynamoDB for production. Select via `STATE_BACKEND=local|dynamodb`. |
| **Notifier** | SES sends email summary of changes |

### AWS Resources

| Resource | Purpose |
|----------|---------|
| **EventBridge** | Cron schedule (e.g., `rate(6 hours)`) |
| **Lambda** or **ECS Fargate** | Compute for scraping + diff pipeline |
| **DynamoDB** | Last-seen state, change history |
| **S3** | Downloaded PDFs, versioned page snapshots |
| **SES** | Email delivery for change summaries |
| **CloudWatch** | Logs, metrics, alarms |

> **Note:** ECS Fargate is recommended if Playwright packaging size or Chromium dependencies make Lambda unwieldy.

### Data Flow

1. **EventBridge** fires on schedule → invokes **Lambda** or **ECS** task.
2. Runner loads target config (URLs, selectors, labels).
3. For each target: **Scraper** fetches page; **Normalizer** extracts signals; **Diff Engine** hashes content and compares to DynamoDB.
4. On change: write to DynamoDB, optionally download PDFs to S3, queue email via SES.
5. **Notifier** sends consolidated email with change summary.

---

## Target Configuration

Targets are defined in `targets.json` with a resource-type driven `extract` array:

```json
[
  {
    "id": "life_rbc_wg",
    "label": "Life RBC Working Group",
    "url": "https://example.com",
    "extract": [
      {
        "type": "docs",
        "extractor": "link_collector_v1",
        "params": { "extensions": [".pdf"] },
        "_purpose": "Collect PDF documents linked from the page."
      },
      {
        "type": "event_links",
        "extractor": "keyword_links_v1",
        "params": { "keywords": ["meeting", "agenda"] },
        "_purpose": "Links whose visible text mentions meetings or agendas."
      }
    ]
  }
]
```

| Field | Description |
|-------|-------------|
| `id` | Unique identifier for state persistence |
| `label` | Human-readable label for reports |
| `url` | Page URL to monitor |
| `extract` | Array of rules: `{ type, extractor, params }` |
| `org_id` | Optional. Organization ID for report grouping (e.g. `"naic"`) |
| `org_path` | Optional. Array of path segments for sub-grouping (e.g. `["committees","e"]`) |
| `group` | Optional. Group label (e.g. `"working-groups"`) |
| `tags` | Optional. Array of tags for filtering or categorization |

**URL filtering (all link-based extractors):**
- `allow_domains` — optional list; if set, only URLs from these domains are kept
- `deny_domains` — optional list; URLs from these domains are excluded
- Default deny list includes `translate.google.com`, `add-to-calendar-pro.com`, and common social domains

**keyword_links_v1** additionally supports:
- `deny_url_patterns` — regex list for path/URL; default excludes social and nav paths (`/facebook`, `/twitter`, `/linkedin`, `/connect`, etc.). Pass `[]` to disable.

**Extractors:**
- `link_collector_v1` — collects links matching `params.extensions` (e.g. `[".pdf"]`); returns `{title, url}` (title: anchor text, else filename, else host/path)
- `keyword_links_v1` — collects links whose text contains any `params.keywords`; returns `{title, url}`
- `naic_meetings_v1` — NAIC meeting blocks (Public Webex Meeting); returns `{title, date_text, time_text, expected_duration, webex_url, agenda_url, materials_url, notes}`
- `naic_events_v1` — NAIC-specific event extraction; returns `{title, datetime_text, url}`

---

## Change Event Model

When a change is detected, a **change event** is computed per target and resource type:

| Field | Description |
|-------|-------------|
| `first_run` | No previous state; baseline recorded |
| `page_changed` | Page text hash (SHA256) changed |
| `by_type` | Per resource type: `{added: [...], removed: [...]}` — stable keys (URL for links, triple for events) |
| `before_hash` / `after_hash` | Previous and current page fingerprints |

Reports group by target, then by resource type. Output is written to `last_report.txt`.

---

## Bubble Integration

Change events are mapped to **Bubble** Resource and Calendar Item payloads for import into a Bubble.io app. The pipeline: (1) build payloads from the diff, (2) optional AI enrichment (categorization, candidate IDs when snapshot present), (3) reference enrichment so fields pointing at existing Bubble data (trees, nodes, calendar items) are set to Bubble IDs.

### Mapping Rules

| Change Type | Bubble Output |
|-------------|---------------|
| **docs added** | Resource objects (Name, URL, parent, Organization, Type1, etc.) |
| **event_links / meeting links** | Resource objects; if link suggests agenda/materials/webex/call, also attached to Calendar Item Agenda |
| **meetings added** | Calendar Item objects (title, date, Agenda from associated links) |

Meeting links are associated with meetings by date match when possible; otherwise the first upcoming meeting. Schema fields are in `bubble/schemas.py`. **PDF meeting meta** (optional): for PDF resources, the pipeline can extract meeting metadata (date, group, times) from the PDF content; quality gates validate and reject invalid extractions; results are written to `debug/pdf_meeting_meta.json`.

### Reference Enrichment (existing Bubble data)

When `--bubble-enrich` is on (default on if `AI_ENRICHMENT_ENABLED=true`), **reference fields** are resolved to Bubble IDs using either a **Bubble snapshot** (E2E) or the **Bubble Data API** (read-only):

| Field | How it's set |
|-------|----------------|
| **Organization** (Resource) | NAIC node under the organization tree (default `Organization`). Uses **normalized name matching** (lowercase, strip `(E)`-style codes, hyphen→space); selects the "NAIC" node. |
| **NAIC Group (tree node)** (Calendar) | Tree node from target context (`org_path` + `label`). Uses **normalized label matching** (no path-walking): exact normalized, substring containment, or token-overlap (Jaccard ≥ 0.75). |
| **Type1** (Resource) | Deterministic keyword classification (News, Agenda/Materials, In the weeds) or optional AI override (confidence ≥ 0.7). Tree: `Resources Types`. |
| **topic suggestion** (Resource) | Optional AI-suggested topic path, resolved to node ID when confidence ≥ 0.7. Tree: `Chronicles`. |
| **Related calendar items** (Resource) | Existing Bubble calendar items matched by **NAIC Group (tree node)** + date window (±7 days) or no-date fallback (up to 3 upcoming). Constraints JSON recorded in evidence. |

No Bubble **write** endpoints are called; resolution uses `bubble/lookups.py` (search/list) or snapshot data. Env overrides: `BUBBLE_ORGANIZATION_TREE` (default `Organization`), `BUBBLE_TYPE1_TREE` (default `Resources Types`), `BUBBLE_TOPIC_TREE` (default `Chronicles`).

### Output Files

| File | Description |
|------|-------------|
| `last_bubble_resources.json` | Resource payload array (when `--emit-bubble-json`) |
| `last_bubble_calendar_items.json` | Calendar Item payload array |
| `last_bubble_report.json` | Combined: `counts`, `web_urls`, `resources`, `calendar_items` |
| `debug/bubble_snapshot.json` | Snapshot of trees, tree_nodes, calendar_items, resources (when `--e2e-bubble`) |
| `debug/ai_inputs_resources.jsonl` | AI enrichment inputs (when AI runs with snapshot) |
| `debug/ai_outputs_resources.jsonl` | AI enrichment outputs for resources |
| `debug/ai_inputs_calendar_items.jsonl` | AI enrichment inputs for calendar items |
| `debug/ai_outputs_calendar_items.jsonl` | AI enrichment outputs for calendar items |
| `debug/reference_resolution_report.json` | Resolution decisions (organization, type1, naic_group, calendar_linking) with evidence |
| `debug/pdf_meeting_meta.json` | Extracted meeting metadata from PDF resources (date, group, times); valid/invalid counts |

### CLI Flags

| Flag | Description |
|------|-------------|
| `--emit-bubble-json` | Write `last_bubble_resources.json`, `last_bubble_calendar_items.json`, and `last_bubble_report.json` |
| `--bubble-report` | Use Bubble JSON format for report and email (summary + Calendar Items + Resources) |
| `--bubble-enrich` | Run reference enrichment (resolve Organization, NAIC Group, Type1, topic suggestion, Related calendar items). Default on if `AI_ENRICHMENT_ENABLED=true`. |
| `--no-ai` | Disable AI in reference enrichment even when `AI_ENRICHMENT_ENABLED` is set |
| `--ai-enrich` | Force OpenAI payload enrichment (categorization, schema fill); requires `OPENAI_API_KEY` |
| `--e2e-bubble` | E2E Bubble: build snapshot, pass into payload + AI; write debug artifacts; no write endpoints |
| `--e2e-bubble-verify` | After enrichment, verify all reference IDs against snapshot; exit non-zero if invalid (use with `--e2e-bubble`) |
| `--bubble-snapshot-limit` | Max items per type in snapshot (default 200) |
| `--dry-run-bubble` | Do not call Bubble write endpoints (default True; app has no write calls) |
| `--no-dry-run-bubble` | Opt out of dry-run (for future write support) |
| `--print-bubble-schema` | Print Bubble Resource field list and exit |
| `--smoke-bubble-resolvers` | Run resolver smoke tests against LIVE Bubble (NAIC Group → Calendar); exit 0/1 |
| `--pdf-meeting-meta` | Extract meeting metadata from PDF resources (date, group, times); default ON when `PROD_OBSERVE_MODE=true` |

### Run spec (production)

Runtime behavior is controlled by a **RunSpec** (single source of truth) derived from CLI args and environment. Precedence: **CLI > env > defaults**. The RunSpec summary is logged at startup and included at the top of every email report.

| Env / behavior | Description |
|----------------|-------------|
| `PROD_OBSERVE_MODE` | When `true`: requires `bubble_enrich_enabled`, `ai_reference_fields_blocked`, and debug artifacts; intended for production observe (live Bubble reads, no silent degraded modes). |
| `AI_REFERENCE_FIELDS_BLOCKED` | Default `true`: AI must not overwrite reference fields (Organization, Type1, Related calendar items, etc.). If `false` while AI enrich is on, a HIGH severity warning is emitted (and included in email header). |
| `ARTIFACT_OUTPUT_DIR` | Directory for `reference_resolution_report.json` and `verify_report.json` (default `debug`). |
| `S3_ARTIFACT_UPLOAD_ENABLED` | When `true`, upload `reference_resolution_report.json`, `verify_report.json`, and `pdf_meeting_meta.json` to S3 (requires `ARTIFACT_BUCKET`; optional `ARTIFACT_PREFIX`). |
| `RUN_SPEC_VALIDATION_FAIL_FAST` | When `true`, validation failures (e.g. prod_observe without bubble_enrich) raise and exit instead of only logging warnings. |

**Recommended production (observe, live Bubble, safe):**

```bash
PROD_OBSERVE_MODE=true ARTIFACT_OUTPUT_DIR=debug EMAIL_ENABLED=true \
  python spike.py --bubble-enrich --emit-bubble-json --bubble-report
```

**E2E verify (snapshot, strict):**

```bash
python spike.py --e2e-bubble --e2e-bubble-verify --bubble-enrich --emit-bubble-json
```

**Minimal scrape-only (explicitly low quality, no Bubble refs):**

```bash
# No --bubble-enrich, no AI; refs unresolved (suitable for local/dev only)
python spike.py
```

### Bubble Doctor CLI

Read-only diagnostics (no secrets in output):

```bash
python -m bubble.doctor list-trees
python -m bubble.doctor dump-tree --tree-name "Organization"
python -m bubble.doctor find-node --tree-name "Organization" --query "NAIC"
python -m bubble.doctor find-calendar --title "Life Risk-Based Capital" --date "2026-02-25"
```

### Resolver Smoke Test

Smoke-test the NAIC Group → Calendar Item resolver against LIVE Bubble:

```bash
python scripts/bubble_resolver_smoke.py "Risk-Based Capital Investment Risk Evaluation Working Group"
python scripts/bubble_resolver_smoke.py "Life Actuarial Task Force" --date 2026-03-02
```

Requires `BUBBLE_API_URL` and `BUBBLE_API_KEY`. Or run the full resolver smoke suite via `--smoke-bubble-resolvers`.

### AI Enrichment

Optional OpenAI enrichment fills NAIC categorization (Type, Type1, topic suggestion, NAIC Group, subtopic, etc.). When a **Bubble snapshot** is available (e.g. `--e2e-bubble`), the model receives compact **candidate lists** (organization tree nodes, NAIC group nodes, resource type nodes, recent calendar items) and is instructed to output **Bubble IDs** for reference fields; output is validated (all schema keys present, no extras). Uses the Responses API with `gpt-5` and reasoning effort `medium` by default. On API failure or invalid output, enrichment is skipped and original payloads are used.

**SSM Parameter Store:** When `STATE_BACKEND=aws|dynamodb` or `ENVIRONMENT=prod`, OpenAI settings are loaded from SSM if not set in env. Locally, set `OPENAI_FETCH_FROM_SSM=true` to fetch. On SSM failure, logs a warning and continues without AI. Never logs the API key.

| Var | Default | Description |
|-----|---------|-------------|
| `OPENAI_API_KEY_SSM_PARAM` | `/web-change-tracker/prod/openai_api_key` | SSM param for API key (SecureString) |
| `OPENAI_MODEL_SSM_PARAM` | `/web-change-tracker/prod/openai_model` | SSM param for model |
| `OPENAI_REASONING_EFFORT_SSM_PARAM` | `/web-change-tracker/prod/openai_reasoning_effort` | SSM param for effort |
| `OPENAI_FETCH_FROM_SSM` | `false` local | If true, fetch from SSM even when not prod |
| `OPENAI_ENABLED` | `true` in prod, `false` local | Enable when `ENVIRONMENT=production` |
| `OPENAI_ENRICH_ONLY_IF_CHANGED` | `true` | Skip when no changes detected |
| `OPENAI_ENRICH_MIN_ITEMS` | `1` | Min resources+events to run |
| `OPENAI_ENRICH_MAX_RESOURCES` | `25` | Max resources to enrich (first N) |
| `OPENAI_ENRICH_MAX_EVENTS` | `10` | Max calendar items to enrich (first N) |
| `OPENAI_MODEL` | `gpt-5` | Model name |
| `OPENAI_REASONING_EFFORT` | `medium` | Reasoning effort for gpt-5/o-series |
| `AI_ENRICHMENT_ENABLED` | — | When set, enables `--bubble-enrich` by default and allows AI in reference enrichment |

```bash
# Force AI payload enrichment:
OPENAI_API_KEY=sk-... python spike.py --emit-bubble-json --ai-enrich

# With reference enrichment (resolve Bubble IDs):
python spike.py --emit-bubble-json --bubble-enrich

# E2E: snapshot + mapping context for AI + ref resolution (no Bubble writes):
python spike.py --emit-bubble-json --e2e-bubble
```

### Email Report Body

When Bubble output is used, the email body includes:

- **New Library Items (Resources):** *N*
- **New Calendar Items (Events):** *M*
- **Source links:** deduplicated list of source URLs that triggered changes
- **Bubble Resource payload:** JSON block
- **Bubble Calendar Item payload:** JSON block

Email is sent only when there are meaningful changes (`targets_changed > 0`).

---

## Recommended Libraries & Tools

| Purpose | Tool |
|---------|------|
| Scraping | **Playwright** (Python) for JS-rendered pages; **requests** + **BeautifulSoup** for simple pages |
| Parsing | **BeautifulSoup4** / **lxml** |
| Change detection | **hashlib** (SHA256), **difflib** for human-readable diffs; optionally **simhash** for fuzzy matching |
| Data modeling | **pydantic** |
| Storage | DynamoDB (state), S3 (artifacts) |
| Scheduling | AWS EventBridge (cron) |
| Compute | Lambda or ECS Fargate |
| Notifications | Amazon SES (or SNS → email) |
| Logging/Monitoring | CloudWatch Logs, metrics, alarms |
| IaC | Terraform or AWS CDK |

---

## Repository Structure

```
web-change-tracker/
├── spike.py                 # Main change-detection pipeline (fetch → extract → diff → report)
├── storage/
│   ├── state_store_dynamodb.py  # DynamoDB per-target state
│   ├── state_store_local.py     # Local state.json (dev)
│   └── changelog_s3.py          # S3 append-only changelog
├── bubble/
│   ├── client.py            # Bubble Data API client (read/write endpoints)
│   ├── lookups.py           # Read-only lookups: trees, tree nodes, calendar items, resources (cached)
│   ├── payload.py           # Bubble payload building, link association, context helpers, PDF meeting meta
│   ├── schemas.py           # Resource & Calendar Item schema field definitions
│   ├── enrich_refs.py       # Reference enrichment: resolve Organization, NAIC Group, Type1, topic, calendar links
│   ├── reference_resolution.py  # Report resolution decisions and evidence for debugging
│   ├── snapshot.py          # Build Bubble snapshot (trees, nodes, calendar, resources) for E2E
│   ├── mapping_context.py   # Extract candidate tree nodes / calendar items from snapshot for AI
│   ├── ai_enrichment.py     # OpenAI enrichment for Bubble payloads (with snapshot context)
│   ├── openai_client.py     # OpenAI Responses API client
│   ├── ssm_loader.py        # Load OpenAI settings from SSM in prod
│   ├── doctor.py            # CLI: list-trees, dump-tree, find-node, find-calendar (read-only)
│   ├── healthcheck.py       # Read-only Bubble API healthcheck (BUBBLE_API_URL/BUBBLE_API_KEY)
│   └── schema_exports/      # CSV exports for validation & examples
├── scripts/
│   ├── bubble_resolver_smoke.py  # Smoke-test NAIC Group → Calendar resolver against LIVE Bubble
│   ├── deploy.sh                 # Build Docker, push ECR, terraform apply
│   ├── validate_bubble_api.sh    # Validate Bubble API endpoints (trees, nodes, calendar, resources)
│   └── run-local.sh
├── config/
│   └── run_spec.py         # RunSpec: compute_run_spec, validate_run_spec, render_run_spec_summary
├── schema_loader.py         # Bubble schema loading from CSV exports
├── emailer.py               # Optional SES email when changes detected
├── scrape/
│   └── pdf_meeting_meta.py  # Extract meeting metadata from PDFs (date, group, times)
├── targets.json             # Target config with extract rules
├── Dockerfile
├── requirements.txt
├── state.json               # Local state (gitignored)
├── last_report.txt          # Latest report output (gitignored)
├── last_bubble_resources.json
├── last_bubble_calendar_items.json
├── last_bubble_report.json  # Combined counts, web_urls, payloads
├── snapshots/               # Test mode: saved snapshots per target (gitignored)
├── debug/                   # E2E/AI debug: bubble_snapshot.json, reference_resolution_report.json, pdf_meeting_meta.json, ai_inputs_*.jsonl, ai_outputs_*.jsonl
├── infra/terraform/         # Terraform: ECS Fargate, EventBridge, DynamoDB, S3, IAM
├── prompts/                 # Prompt templates for AI enrichment
└── tests/
    ├── test_bubble_payload.py
    ├── test_ai_enrichment_contract.py
    ├── test_enrich_refs.py
    ├── test_report.py
    └── test_ai_review.py
```

---

## Testing

End-to-end testing depends on real site changes, so we rely on layered testing:

| Strategy | Description |
|----------|-------------|
| **Snapshot test mode** | `--snapshot-dir` saves content per target; `--compare-snapshot` compares against snapshots. Edit snapshot files to simulate changes without waiting for site updates. Works with `USE_PLAYWRIGHT=0` (requests fallback). Use `--simulate-change` for deterministic diffs. |
| **Unit tests** | `test_bubble_payload.py` (payload building, link association), `test_ai_enrichment_contract.py` (schema/output checks, mocked), `test_enrich_refs.py` (reference resolution, NAIC group, calendar linking), `test_report.py`, `test_ai_review.py` |
| **E2E Bubble** | `--e2e-bubble` builds a Bubble snapshot and passes it into payload mapping and AI enrichment; no Bubble write endpoints are called. Debug artifacts written to `debug/`. |
| **Integration tests** | Against static snapshots; fixtures in `tests/fixtures/` |
| **Resolver smoke** | `scripts/bubble_resolver_smoke.py` or `--smoke-bubble-resolvers` runs NAIC Group → Calendar resolver against LIVE Bubble |
| **Manual test plan** | Deploy to dev, add test target, trigger run, verify email and state |

---

## Security & Legal

- **Respect `robots.txt`** where applicable; honor crawl-delay hints if present
- **Throttle** requests per host; avoid aggressive parallel scraping
- **Public pages only**; no authentication or private data
- Store minimal PII; only URLs, hashes, and change metadata in DynamoDB
- SES: use verified identities; follow AWS abuse-prevention guidelines

---

## Phase 1 Scope (MVP)

**Goal:** Change detection, email summary, and Bubble integration for import into Bubble.io.

**Done (local spike):**

- [x] Target config loaded from `targets.json` (extract array, resource-type driven)
- [x] Scraper fetches URLs (Playwright + requests fallback)
- [x] Extractors: link_collector_v1, keyword_links_v1, naic_meetings_v1, naic_events_v1
- [x] Diff engine compares to stored state; detects changes per resource type
- [x] Report written to last_report.txt (grouped by target, then resource type)
- [x] State store abstraction (LocalStateStore, DynamoDB)
- [x] Email summary via SES (SEND_EMAIL, DRY_RUN); body includes new items counts, source links, Bubble payload JSON
- [x] Bubble payload generation: Resources, Calendar Items, meeting-link association
- [x] Bubble reference enrichment: Organization, NAIC Group, Type1, topic suggestion, Related calendar (deterministic + optional AI)
- [x] Bubble snapshot + E2E mode: `--e2e-bubble`, snapshot passed into mapping/AI; debug artifacts under `debug/`
- [x] Bubble Doctor CLI: `python -m bubble.doctor` (list-trees, dump-tree, find-node, find-calendar)
- [x] Resolver smoke script: `scripts/bubble_resolver_smoke.py`; `--smoke-bubble-resolvers` flag
- [x] `--emit-bubble-json`, `--bubble-report`, `--bubble-enrich`, `--no-ai`, `--ai-enrich`, `--e2e-bubble`, `--print-bubble-schema` CLI flags
- [x] Optional OpenAI enrichment for Bubble payloads (Type, NAIC Group, topic, etc.); mapping context from snapshot when available
- [x] PDF meeting meta extraction (date, group, times) with quality gates; `debug/pdf_meeting_meta.json`
- [x] Reference resolution report (`debug/reference_resolution_report.json`) with evidence
- [x] Deployment script: `scripts/deploy.sh` (Docker build, ECR push, Terraform apply)

**Remaining (AWS):**

- [ ] EventBridge triggers runs on schedule
- [ ] CloudWatch logs and metrics

---

## Getting Started

**Local run:**

```bash
make install              # create venv, install deps
make install-playwright   # optional; falls back to requests if unavailable
make run                  # run the pipeline
```

**Docker (Playwright included):**

```bash
# Build and run with docker-compose (env from .env)
cp .env.example .env      # optional; edit as needed
docker compose up --build

# Or run once with docker
docker build -t web-change-tracker .
docker run --rm -v $(pwd):/app -e USE_PLAYWRIGHT=1 web-change-tracker
```

Required env vars for Docker (see `.env.example`):
- **Targets:** `TARGETS_FILE` (default `targets.json`); `TARGET_IDS` (comma-separated) to restrict to a subset.
- **State backend:** `STATE_BACKEND=local` (default) for `state.json`; `STATE_BACKEND=dynamodb` + `STATE_TABLE` for production.
- **Changelog:** `CHANGELOG_BUCKET`, `CHANGELOG_PREFIX` (default `changelog/`) to append events to S3.
- **Email:** `SEND_EMAIL`, `FROM_EMAIL`, `TO_EMAILS`, `SES_REGION`; `DRY_RUN=true` to test without sending.
- **Bubble / AI enrichment:** `OPENAI_API_KEY` (required when enrichment runs); `OPENAI_MODEL`, `OPENAI_REASONING_EFFORT` (see AI Enrichment section). For Bubble read lookups and E2E snapshot: `BUBBLE_API_URL`, `BUBBLE_API_KEY` (see `bubble/client.py`). **BUBBLE_API_URL** must be the exact root from Bubble's API settings (Settings → API). Custom domains often use `https://your-domain.com/api/1.1/obj` (no `/live`); bubbleapps.io often uses `https://your-app.bubbleapps.io/live/api/1.1/obj`. No spaces after `=` in `.env`.
- **AWS:** `AWS_REGION`, credentials when using DynamoDB/S3/SES.

Or use the script:

```bash
./scripts/run-local.sh
```

**Deploy (ECS Fargate):**

```bash
./scripts/deploy.sh              # Build, push ECR, terraform apply
./scripts/deploy.sh --run-task   # Same + run one ECS task after apply
./scripts/deploy.sh --tag v1.2   # Use custom image tag
```

Requires Terraform outputs (region, ECR URL) from `infra/terraform`. See `infra/terraform/README.md` for SSM parameters (Bubble API, OpenAI).

**CI (e.g. GitHub Actions):**

```bash
make ci                   # install, lint, run
# or
./scripts/run-ci.sh
```

**Bubble output:**

```bash
# Write Bubble JSON payloads
python spike.py --emit-bubble-json

# With reference enrichment (resolve Organization, NAIC Group, Type1, etc.)
python spike.py --emit-bubble-json --bubble-enrich

# E2E: snapshot + mapping context for AI (no Bubble API writes)
python spike.py --emit-bubble-json --e2e-bubble

# Report and email in Bubble format
python spike.py --bubble-report

# With AI enrichment (requires OPENAI_API_KEY)
python spike.py --emit-bubble-json --ai-enrich
```

**Bubble diagnostics (read-only):**

```bash
python -m bubble.doctor list-trees
python -m bubble.doctor dump-tree --tree-name "Organization/Publisher"
python -m bubble.doctor find-node --tree-name "Organization/Publisher" --query "NAIC"
python -m bubble.doctor find-calendar --title "Life Risk-Based Capital" --date "2026-02-25"
```

**Test mode (validate change detection without waiting for site updates):**

```bash
# 1. Save snapshots (normalized content + extracted lists per target)
python spike.py --snapshot-dir snapshots/

# 2. Simulate change: edit snapshots/<target_id>.json (e.g. remove a doc URL)
# 3. Compare current scrape against snapshot (no state.json updated; snapshots not overwritten)
python spike.py --compare-snapshot

# Or with explicit dir: --snapshot-dir snapshots/ --compare-snapshot
# Works with requests fallback (no Playwright needed):
USE_PLAYWRIGHT=0 python spike.py --snapshot-dir snapshots/
USE_PLAYWRIGHT=0 python spike.py --compare-snapshot
```

- Edit `targets.json` to add or modify targets and extract rules.

**Target selection:**

```bash
# Full run (all targets from targets.json)
python spike.py

# Custom targets file
python spike.py --targets-file config/my-targets.json

# Subset run (only specified target IDs)
python spike.py --target-ids life_rbc_wg,naic_events_example

# Via env vars
TARGETS_FILE=config/targets.json TARGET_IDS=life_rbc_wg python spike.py
```

- State is persisted per target_id (state.json or DynamoDB); subset runs only read/write state for processed targets.
- Report output is in `last_report.txt`.

**Production hardening env vars:** `MAX_RETRIES` (default 3), `BACKOFF_SECONDS` (default 2), `DELAY_BETWEEN_PAGES` (default 1). Failures on one target don’t stop the run; errors are collected and included in the final report.

**Production storage:** Set `STATE_BACKEND=dynamodb`, `STATE_TABLE`, `CHANGELOG_BUCKET`, `CHANGELOG_PREFIX` (default `changelog/`), and `AWS_REGION`. Run flow: load each target's state from DynamoDB → scrape → diff → save state to DynamoDB → append change events to S3. See `ARCHITECTURE.md` for schema and IAM policies.

**Optional email (SES):** Set `SEND_EMAIL=true`, `FROM_EMAIL`, `TO_EMAILS` (comma-separated), `SES_REGION`. Email is sent only when changes or errors are detected. Set `DRY_RUN=true` to print the email without sending.

- See `ARCHITECTURE.md` for AWS deployment. Use `infra/` Terraform for a scheduled ECS Fargate MVP.

---

## License

TBD
