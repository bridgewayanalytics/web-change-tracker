# Dashboard Redesign — Web Change Tracker Alerts Table

## What You Are Being Asked To Do

The NAIC Dashboard currently shows an **event-centric view** of upcoming NAIC calendar items, pulling data from Bubble and an older S3 format (`bubble_reports/latest.json`). That system has been replaced.

The new system is a **web change tracker** that monitors NAIC pages (committee pages, newsroom, resource center) and uses an LLM agent to detect and classify changes. It writes structured alert rows to S3 as a growing JSONL file and regenerates an Excel file from it after every run.

**Your job is to replace the current dashboard view with a live display of the alerts table** — essentially rendering the Excel in the browser, with filtering and before/after HTML diff links per row.

The existing infrastructure (Next.js, ECS, Terraform, Bubble credentials, AWS credentials) stays in place. You are replacing the data source and the table UI, not the deployment stack.

---

## New Data Source

### Primary: S3 JSONL

**Bucket:** `web-change-tracker-prod-artifacts-815039343351`
**Key:** `alerts/alerts_table.jsonl`
**Format:** Newline-delimited JSON — one JSON object per line, one line per alert row.

This file grows over time. Every production run of the web change tracker appends new rows. The dashboard should read the full file and display all rows, newest first.

There is also a regenerated Excel at `alerts/alerts_table.xlsx` (same bucket) but the JSONL is the canonical source for the dashboard.

### Before/After HTML Snapshots

For every alert row, the HTML before and after the change was detected is stored in S3:

```
pages/<target_id>/YYYY/MM/DD/<run_id>/before.html
pages/<target_id>/YYYY/MM/DD/<run_id>/after.html
```

Where `YYYY/MM/DD` is derived from the `run_timestamp` field in the row.

Example for a row with `target_id: "naic.newsroom"`, `run_id: "run-1776219778"`, `run_timestamp: "2026-04-15T02:22:58+00:00"`:
```
pages/naic.newsroom/2026/04/15/run-1776219778/before.html
pages/naic.newsroom/2026/04/15/run-1776219778/after.html
```

These files are **not public** — you must serve them via a Next.js API route that generates a pre-signed S3 URL or proxies the content. The bucket is already accessible via the task's IAM role.

---

## Alert Row Schema

Every row in the JSONL has these fields. Many will be empty strings for a given row — that is expected and normal (e.g. event fields are blank for document-only alerts).

| Field | Type | Description |
|---|---|---|
| `run_id` | string | Unique ID for the tracker run, e.g. `"run-1776219778"` |
| `run_timestamp` | string | ISO 8601 UTC timestamp of when the run executed |
| `target_id` | string | Internal page identifier (see Page Labels below) |
| `source_url` | string | URL of the page that was monitored |
| `alert_type` | string | One of: `New Meeting`, `Updated Meeting`, `New or Updated Report or Other Resource`, `Other` |
| `alert_title` | string | Short human-readable title of the change detected |
| `alert_description` | string | 2–4 sentence description of what changed |
| `alert_url` | string | URL most relevant to this alert (usually same as source_url) |
| `alert_url_title` | string | Display text for the alert URL (usually same as alert_title) |
| `alert_date_time` | string | ISO date/datetime of the alert (often date-only, e.g. `"2026-04-15T00:00:00-04:00"`) |
| `organization` | string | Organization name, e.g. `"National Association of Insurance Commissioners"` |
| `event_title` | string | Title of the meeting/event (populated for meeting alert types) |
| `event_start_date_time` | string | ISO datetime of event start |
| `event_end_date_time` | string | ISO datetime of event end |
| `event_timezone` | string | IANA timezone string, e.g. `"America/New_York"` |
| `event_duration` | string | Human-readable duration, e.g. `"1 hour"` or `"Updated Duration: 1 hour -> 1 hour 30 minutes"` |
| `event_is_full_day` | boolean | Whether the event is all-day |
| `event_url` | string | Webex or other meeting join link |
| `event_call_in_access_code` | string | Phone dial-in info if present |
| `agenda_item_title` | string | Title of the relevant agenda item |
| `agenda_item_title_official` | string | Official/formal agenda item title if different |
| `agenda_item_standardized_id` | string | Internal standardized ID, e.g. `"RBCIREWG#CommentsOnExposures"` |
| `agenda_item_official_id` | string | Official agenda item reference number |
| `chronicle_topics` | string | Comma-separated list of related Chronicle topic names, e.g. `"CLOs and Asset-Backed Securities, RBC Covariance & Asset Concentration Risk"` |
| `library_item_preliminary_title` | string | Title of a document/library item detected in this change |
| `library_item_url` | string | URL of the document |
| `new_library_item_file_name` | string | File name if a new file was detected |
| `candidate_chronicles` | string | Comma-separated Bubble IDs of candidate Chronicle topics (from pgvector RAG matching) |
| `candidate_agenda_items` | string | Comma-separated Bubble IDs of candidate agenda items (from pgvector RAG matching) |

### Page Labels (target_id → display name)

Map `target_id` to a human-readable label for display and filtering:

```json
{
  "naic.newsroom": "NAIC Newsroom",
  "naic.resource_center": "NAIC Resource Center",
  "naic.capital_markets_bureau": "Capital Markets Bureau",
  "naic.e.index": "E Committee Index",
  "naic.e.statutory_accounting_principles_wg": "Statutory Accounting Principles Working Group",
  "naic.e.blanks_wg": "Blanks Working Group",
  "naic.e.capital_adequacy_tf": "Capital Adequacy Task Force",
  "naic.e.health_rbc_wg": "Health Risk-Based Capital Working Group",
  "naic.e.life_rbc_wg": "Life Risk-Based Capital Working Group",
  "naic.ae.generator_economic_scenarios_sg": "Generator Economic Scenarios Subgroup",
  "naic.e.property_casualty_rbc_wg": "Property & Casualty Risk-Based Capital Working Group",
  "naic.e.rbc_investment_risk_evaluation_wg": "Risk-Based Capital Investment Risk Evaluation Working Group",
  "naic.e.receivership_insolvency_tf": "Receivership & Insolvency Task Force",
  "naic.e.financial_stability_tf": "Financial Stability Task Force",
  "naic.e.macroprudential_wg": "Macroprudential Working Group",
  "naic.e.group_capital_calculation_wg": "Group Capital Calculation Working Group",
  "naic.e.invested_assets_tf": "Invested Assets Task Force",
  "naic.e.credit_rating_provider_wg": "Credit Rating Provider Working Group",
  "naic.e.investment_analysis_wg": "Investment Analysis Working Group",
  "naic.e.investment_designation_analysis_wg": "Investment Designation Analysis Working Group",
  "naic.e.reinsurance.index": "Reinsurance (E) Index",
  "naic.e.reinsurance_financial_analysis_wg": "Reinsurance Financial Analysis Working Group",
  "naic.e.valuation_analysis_wg": "Valuation Analysis Working Group",
  "naic.ex.index": "EX Committee Index",
  "naic.ex.climate_resiliency_tf": "Climate Resiliency Task Force",
  "naic.ex.rbc_model_governance_tf": "RBC Model Governance Task Force",
  "naic.a.index": "A Committee Index",
  "naic.a.latf": "Life Actuarial Task Force",
  "naic.a.latf.vm22sg": "VM-22 Subgroup (LATF)"
}
```

---

## What the New Dashboard Should Display

### Table Columns

Display these columns in this order. All are optional/nullable — render empty cells gracefully.

| Column Header | Source Field(s) | Notes |
|---|---|---|
| **Page** | `target_id` → label map | Use the label, not the raw target_id |
| **Alert Type** | `alert_type` | Consider a colored badge: blue=Meeting, green=Document, gray=Other |
| **Alert Title** | `alert_title` | Link to `alert_url` if present |
| **Description** | `alert_description` | Truncate to ~2 lines with expand; this field is verbose |
| **Alert Date** | `alert_date_time` | Display as `Apr 15, 2026`; strip time component |
| **Organization** | `organization` | |
| **Event** | `event_title`, `event_start_date_time`, `event_end_date_time`, `event_timezone`, `event_duration`, `event_url` | Collapse into a single cell; only populated for meeting alert types. Show start date/time and link to `event_url` |
| **Agenda Item** | `agenda_item_title` | |
| **Chronicle Topics** | `chronicle_topics` | Comma-separated; may be empty |
| **Document** | `library_item_preliminary_title`, `library_item_url` | Link title to URL if present |
| **Before / After** | Derived from `run_id`, `target_id`, `run_timestamp` | Two small links per row (see below) |
| **Run** | `run_timestamp` | Display as `Apr 15, 2026 2:22 AM UTC`; newest first |

The columns `candidate_chronicles`, `candidate_agenda_items`, `source_url`, `run_id` are internal — either hide them or put them in an expandable detail panel.

### Filters

- **Page** — multi-select or dropdown of all `target_id` labels
- **Alert Type** — checkboxes or dropdown: All / New Meeting / Updated Meeting / New or Updated Report or Other Resource / Other
- **Date range** — filter by `alert_date_time`
- **Search** — free text across `alert_title`, `alert_description`, `organization`

### Sort

Default: `run_timestamp` descending (newest alerts first).

### Before / After HTML Links

Per row, derive the S3 key like this (pseudocode):

```typescript
const date = run_timestamp.slice(0, 10).replace(/-/g, '/') // "2026/04/15"
const beforeKey = `pages/${target_id}/${date}/${run_id}/before.html`
const afterKey  = `pages/${target_id}/${date}/${run_id}/after.html`
```

Serve these via a Next.js API route — do NOT try to make the S3 objects public. The route should either:

**Option A — Pre-signed URL redirect (recommended):**
```
GET /api/snapshot?key=pages/naic.newsroom/2026/04/15/run-123/before.html
→ 302 redirect to a pre-signed S3 URL (expires in 60s)
```

**Option B — Proxy:**
```
GET /api/snapshot?key=...
→ stream the S3 object directly
```

The S3 bucket is `web-change-tracker-prod-artifacts-815039343351`. The ECS task role already has `s3:GetObject` on `*` in this bucket (confirmed in IAM policy).

---

## What to Keep from the Current Codebase

- **Next.js / Tailwind / TypeScript setup** — keep as-is
- **`src/lib/ssm.ts`** — still needed for loading credentials from SSM
- **`src/lib/s3.ts`** — extend/replace with new read logic (currently reads `bubble_reports/latest.json`; change to read `alerts/alerts_table.jsonl`)
- **Deployment stack** — Dockerfile, Terraform in `infra/terraform/`, `scripts/deploy.sh` — all unchanged
- **`/api/groups` route** — can be replaced or repurposed as `/api/pages` returning the page labels list
- **General filter bar pattern** — the `FilterBar.tsx` component is a good pattern to reuse

## What to Replace

| Current | Replace With |
|---|---|
| `src/lib/bubble.ts` | Not needed — new system does not use Bubble for display data |
| `src/lib/merge.ts` | Not needed — no merging required; JSONL is already flat |
| `src/lib/calendar-resources.ts` | Not needed |
| `/api/upcoming` route | New `/api/alerts` route that reads the JSONL from S3 |
| `UpcomingItemsTable.tsx` | New `AlertsTable.tsx` with the columns defined above |
| `upcoming/page.tsx` | Rewrite to use the new data shape |
| S3 reads of `bubble_reports/` | Read `alerts/alerts_table.jsonl` instead |

The Bubble API (`BUBBLE_API_URL`, `BUBBLE_API_KEY`) credentials are still present in the environment but the new dashboard does not need to call Bubble. Leave the env vars in place — they may be used later.

---

## New API Route Spec

### `GET /api/alerts`

Reads `alerts/alerts_table.jsonl` from S3, parses all rows, applies optional filters, returns JSON.

**Query params:**
- `targetId` — filter by target_id (e.g. `naic.newsroom`)
- `alertType` — filter by alert_type
- `startDate` / `endDate` — filter by alert_date_time (YYYY-MM-DD)
- `q` — free text search across title + description

**Response:**
```json
{
  "rows": [ ...alert row objects... ],
  "total": 42,
  "error": null
}
```

Rows should be returned newest-first (sort by `run_timestamp` descending).

Cache: 60 seconds (the JSONL is updated at most once per tracker run, which runs on a schedule).

### `GET /api/snapshot`

**Query param:** `key` — the full S3 key (e.g. `pages/naic.newsroom/2026/04/15/run-123/before.html`)

**Behavior:** Validate the key starts with `pages/` and ends with `.html`, then redirect to a pre-signed S3 URL (60s expiry) or stream the content.

---

## Current State of the Data

As of April 2026 there are ~10 rows in the JSONL covering detections from April 15, 2026. Alert types currently present:

- `Updated Meeting` — e.g. RBC Investment Risk WG extended meeting duration
- `New Meeting` — e.g. Capital Adequacy Task Force new Webex posted
- `New or Updated Report or Other Resource` — new documents on NAIC Resource Center / Newsroom
- `Other` — minor page changes (reordering, article swaps)

The file will grow with each production run (runs on a scheduled basis via AWS EventBridge Scheduler → ECS Fargate).

---

## Data Quality Notes for the UI

1. Many fields will be empty strings `""` — treat them as null for display purposes.
2. `alert_date_time` is often a date-only ISO string with a time component of `T00:00:00-04:00` — just display the date portion.
3. `chronicle_topics` is a comma-separated string, not an array.
4. `candidate_chronicles` and `candidate_agenda_items` contain Bubble IDs — these are internal and shouldn't be displayed prominently (fine in a detail panel).
5. `event_title` may be `"N/A"` — treat as empty.
6. The `run_id` format is `run-<unix_timestamp>`, e.g. `run-1776219778`.

---

## Environment Variables

The ECS task and local dev use these (via `.env.local` or SSM):

```
# Already present — keep, but new dashboard doesn't call Bubble directly
BUBBLE_API_URL
BUBBLE_API_KEY

# Already present — used for S3 reads
BUBBLE_ARTIFACT_BUCKET=web-change-tracker-prod-artifacts-815039343351
AWS_REGION=us-east-1
```

No new environment variables are needed.
