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

Change events can be mapped to **Bubble** Resource and Calendar Item payloads for import into a Bubble.io app.

### Mapping Rules

| Change Type | Bubble Output |
|-------------|---------------|
| **docs added** | Resource objects (Name, URL, parent, Organization, etc.) |
| **event_links / meeting links** | Resource objects; if link suggests agenda/materials/webex/call, also attached to Calendar Item Agenda |
| **meetings added** | Calendar Item objects (title, date, Agenda from associated links) |

Meeting links (agenda, materials, webex, call) are associated with meetings by date match when possible; otherwise the first upcoming meeting. Schema fields are defined in `bubble/schemas.py`; full Resource and Calendar Item schemas are supported.

### CLI Flags

| Flag | Description |
|------|-------------|
| `--emit-bubble-json` | Write Bubble payloads to `last_bubble_resources.json` and `last_bubble_calendar_items.json` |
| `--bubble-report` | Use Bubble JSON format for report and email (summary + Calendar Items + Resources sections) |
| `--ai-enrich` | Force OpenAI enrichment (bypass auto conditions; requires `OPENAI_API_KEY`) |
| `--print-bubble-schema` | Print Bubble Resource field list and exit |

### AI Enrichment

Optional OpenAI enrichment fills NAIC categorization fields (Type, topic suggestion, NAIC Group, subtopic, etc.). In production, runs automatically when conditions are met; use `--ai-enrich` to force. Uses the Responses API with `gpt-5` and reasoning effort `medium` by default. On API failure or invalid output, enrichment is skipped and original payloads are used.

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

```bash
# Force enrichment:
OPENAI_API_KEY=sk-... python spike.py --emit-bubble-json --ai-enrich

# Prod: auto-runs when ENVIRONMENT=production
ENVIRONMENT=production OPENAI_API_KEY=sk-... python spike.py --emit-bubble-json
```

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
│   ├── payload.py           # Bubble payload building, link association, context helpers
│   ├── schemas.py           # Resource & Calendar Item schema field definitions
│   ├── ai_enrichment.py     # OpenAI enrichment for Bubble payloads
│   ├── openai_client.py     # OpenAI Responses API client
│   ├── ssm_loader.py        # Load OpenAI settings from SSM in prod
│   └── schema_exports/      # CSV exports for validation & examples
├── schema_loader.py         # Bubble schema loading from CSV exports
├── emailer.py               # Optional SES email when changes detected
├── targets.json             # Target config with extract rules
├── Dockerfile
├── requirements.txt
├── state.json               # Local state (gitignored)
├── last_report.txt          # Latest report output (gitignored)
├── last_bubble_resources.json
├── last_bubble_calendar_items.json
├── snapshots/               # Test mode: saved snapshots per target (gitignored)
├── infra/terraform/         # Terraform: ECS Fargate, EventBridge, DynamoDB, S3, IAM
├── prompts/                 # Prompt templates for AI enrichment
└── tests/
    ├── test_bubble_payload.py
    ├── test_ai_enrichment_contract.py
    ├── test_report.py
    └── test_ai_review.py
```

---

## Testing

End-to-end testing depends on real site changes, so we rely on layered testing:

| Strategy | Description |
|----------|-------------|
| **Snapshot test mode** | `--snapshot-dir` saves content per target; `--compare-snapshot` compares against snapshots. Edit snapshot files to simulate changes without waiting for site updates. Works with `USE_PLAYWRIGHT=0` (requests fallback). |
| **Unit tests** | `test_bubble_payload.py` (payload building, link association), `test_ai_enrichment_contract.py` (schema/output checks, mocked), `test_report.py`, `test_ai_review.py` |
| **Integration tests** | Against static snapshots; fixtures in `tests/fixtures/` |
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
- [x] Email summary via SES (SEND_EMAIL, DRY_RUN)
- [x] Bubble payload generation: Resources, Calendar Items, meeting-link association
- [x] `--emit-bubble-json`, `--bubble-report`, `--ai-enrich`, `--print-bubble-schema` CLI flags
- [x] Optional OpenAI enrichment for Bubble payloads (Type, NAIC Group, topic, etc.)

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
- **Bubble / AI enrichment:** `OPENAI_API_KEY` (required when enrichment runs); `OPENAI_MODEL`, `OPENAI_REASONING_EFFORT` (see AI Enrichment section).
- **AWS:** `AWS_REGION`, credentials when using DynamoDB/S3/SES.

Or use the script:

```bash
./scripts/run-local.sh
```

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

# Report and email in Bubble format
python spike.py --bubble-report

# With AI enrichment (requires OPENAI_API_KEY)
python spike.py --emit-bubble-json --ai-enrich
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
