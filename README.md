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
| **Persist State** | DynamoDB stores last-seen fingerprints and metadata; S3 stores downloaded PDFs |
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

Targets are defined in YAML or JSON. Example:

```yaml
targets:
  - name: "Example Commission Meetings"
    url: "https://example.gov/meetings"
    type: "generic"           # or "naic-like" for specialized extractors
    enabled: true
    schedule: "0 */6 * * *"   # every 6 hours (cron)
    selectors:
      meeting_list: ".meeting-list li"
      main_content: "#main-content"
    asset_link_patterns:
      - ".*\\.pdf$"
      - ".*/documents/.*"
```

| Field | Description |
|-------|-------------|
| `name` | Human-readable label for reports |
| `url` | Page URL to monitor |
| `type` | `generic` or domain-specific (e.g. `naic-like`) for extractor selection |
| `selectors` | Optional CSS/XPath for targeted extraction |
| `asset_link_patterns` | Regex patterns for downloadable links (PDFs, etc.) |
| `schedule` | Cron expression; overrides default if set |
| `enabled` | Toggle target on/off |

---

## Change Event Model

When a change is detected, a **change event** is recorded:

| Field | Description |
|-------|-------------|
| `timestamp` | When the change was detected (ISO 8601) |
| `target_name` | From target config `name` |
| `change_type` | `new_meeting` \| `new_pdf` \| `page_text_change` \| `asset_updated` |
| `before_hash` | Previous fingerprint (SHA256) |
| `after_hash` | Current fingerprint |
| `extracted_links` | URLs of new/changed assets |
| `notes` | Optional human-readable description |

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

## Repository Structure (Proposed)

```
web-change-tracker/
├── docs/                 # Design docs, ADRs
├── src/                  # Python source
│   ├── scrapers/
│   ├── normalizers/
│   ├── diff_engine/
│   ├── persist/
│   └── notifiers/
├── infra/                # IaC (Terraform or CDK)
├── examples/             # Sample configs
│   └── targets.example.yaml
├── tests/
│   ├── unit/
│   └── integration/
├── config.yaml           # Default config
└── README.md
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

**Done means:**

- [ ] Target config loaded from YAML/JSON
- [ ] Scraper fetches configured URLs (Playwright + requests fallback)
- [ ] Normalizer extracts text and asset links; computes page + asset fingerprints
- [ ] Diff engine compares to DynamoDB; records change events on delta
- [ ] Email summary sent via SES with change list (target, change type, links)
- [ ] EventBridge triggers runs on schedule (e.g., every 6 hours)
- [ ] Basic CloudWatch logs and a “run succeeded/failed” metric

---

## Getting Started (Future)

Once implemented:

1. Copy `examples/targets.example.yaml` to `config/targets.yaml`
2. Configure AWS credentials and deploy infra (`infra/`)
3. Run locally with `make run` or deploy to Lambda/ECS
4. Verify first run in CloudWatch; check email for change summary

---

## License

TBD
