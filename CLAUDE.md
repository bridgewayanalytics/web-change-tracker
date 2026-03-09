# CLAUDE.md — Project Context

## What this project does

Website change-tracking system that monitors configured web pages on a schedule, detects meaningful changes (new meetings, PDFs, updated content), and produces change summaries via email. Detected changes are mapped into Bubble.io payloads (Resources and Calendar Items) with optional AI enrichment.

## Tech stack

- **Language:** Python
- **Scraping:** Playwright (JS-rendered), requests + BeautifulSoup (simple pages)
- **Change detection:** SHA256 fingerprinting, difflib
- **AI enrichment:** OpenAI (GPT-5) via Responses API
- **Infrastructure:** AWS — ECS Fargate, EventBridge, DynamoDB, S3, SES, CloudWatch
- **IaC:** Terraform (in `infra/terraform/`)
- **External integration:** Bubble.io Data API (read-only lookups)
- **Data modeling:** pydantic

## Key entry points

- `spike.py` — Main pipeline: fetch → extract → diff → report → Bubble payloads → email
- `targets.json` — Target config with extract rules per URL
- `config/run_spec.py` — RunSpec: single source of truth for runtime behavior (CLI > env > defaults)

## Architecture (pipeline order)

1. **Scheduler** (EventBridge cron) → triggers ECS task
2. **Runner** (`spike.py`) loads targets, orchestrates pipeline
3. **Scraper** fetches pages (Playwright or requests fallback)
4. **Extractors** (pluggable): `link_collector_v1`, `keyword_links_v1`, `naic_meetings_v1`, `naic_events_v1`
5. **Diff Engine** — SHA256 fingerprint comparison against stored state
6. **Bubble payloads** — Resources + Calendar Items built from changes
7. **Reference enrichment** — resolves Bubble IDs (Organization, NAIC Group, Type1, topic, calendar links)
8. **Notifier** (SES) — email summary; report uploaded to S3

## Key directories

- `storage/` — State store abstraction (DynamoDB prod, local `state.json` dev), S3 changelog
- `bubble/` — Bubble.io integration: client, lookups, payload building, enrichment, AI, schemas, doctor CLI
- `scrape/` — PDF meeting metadata extraction
- `config/` — RunSpec computation and validation
- `scripts/` — Deploy, smoke tests, local run
- `infra/terraform/` — ECS Fargate, EventBridge, DynamoDB, S3, IAM
- `tests/` — Unit/integration tests
- `debug/` — E2E/AI debug artifacts (gitignored)

## Common commands

```bash
make install              # create venv, install deps
make run                  # run the pipeline
python spike.py           # minimal local run
python spike.py --emit-bubble-json --bubble-enrich  # with Bubble payloads + ref enrichment
python spike.py --e2e-bubble --e2e-bubble-verify    # E2E with snapshot verification
python -m bubble.doctor list-trees                   # read-only Bubble diagnostics
./scripts/deploy.sh                                  # build Docker, push ECR, terraform apply
```

## Environment

- `STATE_BACKEND=local|dynamodb` — state storage backend
- `PROD_OBSERVE_MODE=true` — production observe mode (live Bubble reads, strict validation)
- `AI_ENRICHMENT_ENABLED` — enables `--bubble-enrich` by default + AI in ref enrichment
- `AI_REFERENCE_FIELDS_BLOCKED=true` (default) — AI cannot overwrite reference fields
- `SEND_EMAIL=true` + `FROM_EMAIL`, `TO_EMAILS`, `SES_REGION` — email notifications
- `BUBBLE_API_URL`, `BUBBLE_API_KEY` — Bubble.io API access
- `OPENAI_API_KEY` — required for AI enrichment

## Conventions

- Extractors are pluggable; defined in target config `extract` array with `{type, extractor, params}`
- State is per-target (keyed by `target.id`)
- No Bubble write endpoints are called; all resolution is read-only
- Failures on one target don't stop the run; errors collected in final report
- Debug artifacts go to `debug/` directory
