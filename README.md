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
| **Persist State** | `state_store.py` abstraction: `LocalStateStore` (state.json) for local runs; `S3StateStore` stub for AWS; DynamoDB/S3 in production |
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

**Extractors:**
- `link_collector_v1` — collects links matching `params.extensions` (e.g. `[".pdf"]`); returns `{label, url}`
- `keyword_links_v1` — collects links whose text contains any `params.keywords`; returns `{label, url}`
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
├── spike.py              # Main change-detection pipeline (fetch → extract → diff → report)
├── state_store.py        # Storage abstraction: LocalStateStore (state.json), S3StateStore (stub)
├── emailer.py            # Optional SES email when changes detected (SEND_EMAIL, DRY_RUN)
├── targets.json          # Target config with extract rules
├── Makefile              # Local and CI commands (make run, make ci)
├── scripts/
│   ├── run-local.sh      # Run locally (creates venv if needed)
│   └── run-ci.sh         # CI: install, lint, run
├── ARCHITECTURE.md       # AWS deployment: EventBridge → Lambda or ECS Fargate
├── requirements.txt
├── state.json            # Local state (gitignored)
├── last_report.txt       # Latest report output (gitignored)
├── docs/                 # Design docs, ADRs (future)
├── infra/                # IaC (Terraform or CDK) (future)
└── tests/                # Unit / integration tests (future)
```

---

## Testing

End-to-end testing depends on real site changes, so we rely on layered testing:

| Strategy | Description |
|----------|-------------|
| **Unit tests** | Deterministic tests using saved HTML fixtures; mock DynamoDB/S3 |
| **Integration tests** | Against static snapshots; fixtures in `tests/fixtures/` |
| **Simulation mode** | Swap live fetcher for fixture loader to simulate "before/after" without hitting live sites |
| **Manual test plan** | Documented steps: deploy to dev, add test target, trigger run, verify email and state |

---

## Security & Legal

- **Respect `robots.txt`** where applicable; honor crawl-delay hints if present
- **Throttle** requests per host; avoid aggressive parallel scraping
- **Public pages only**; no authentication or private data
- Store minimal PII; only URLs, hashes, and change metadata in DynamoDB
- SES: use verified identities; follow AWS abuse-prevention guidelines

---

## Phase 1 Scope (MVP)

**Goal:** Change detection + email summary only. No external integrations (e.g., Bubble) yet.

**Done (local spike):**

- [x] Target config loaded from `targets.json` (extract array, resource-type driven)
- [x] Scraper fetches URLs (Playwright + requests fallback)
- [x] Extractors: link_collector_v1, keyword_links_v1, naic_events_v1
- [x] Diff engine compares to stored state; detects changes per resource type
- [x] Report written to last_report.txt (grouped by target, then resource type)
- [x] State store abstraction (LocalStateStore, S3StateStore stub)

**Remaining (AWS):**

- [ ] Email summary via SES
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

- Edit `targets.json` to add or modify targets and extract rules.
- State is persisted in `state.json` (LocalStateStore).
- Report output is in `last_report.txt`.

**Optional email (SES):** Set `SEND_EMAIL=true`, `FROM_EMAIL`, `TO_EMAILS` (comma-separated), `SES_REGION`. Email is sent only when changes are detected. Set `DRY_RUN=true` to print the email without sending.

- See `ARCHITECTURE.md` for AWS deployment (EventBridge → Lambda or ECS Fargate).

---

## License

TBD
