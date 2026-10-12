# NAIC Alerts Dashboard

A Next.js 14 dashboard that displays structured alerts produced by the [web-change-tracker](../web-change-tracker) pipeline. Reads `alerts/alerts_table.jsonl` from S3 and renders a dynamic table with filtering, re-evaluation, and HTML snapshot download.

Live at: `https://tracker.bridgewayanalytics.com` (Auth0 protected)

## Tech Stack

- Next.js 14 (TypeScript, App Router)
- Tailwind CSS
- Auth0 (`@auth0/nextjs-auth0`)
- AWS SDK v3 (S3, DynamoDB, ECS)
- Deploy: ECS Fargate (ARM64), ALB with HTTPS, Terraform

## Local Development

### 1. Install dependencies

```bash
npm install
```

### 2. Environment variables

Create `.env.local`:

```env
BUBBLE_ARTIFACT_BUCKET=web-change-tracker-prod-artifacts-815039343351
AWS_REGION=us-east-1
CHATKIT_CONFIG_TABLE=chatkit_production_config

# Auth0 (optional for local dev — omit all to disable auth)
AUTH0_SECRET=<random-32-char-string>
AUTH0_BASE_URL=http://localhost:3000
AUTH0_DOMAIN=dev-dwy0013t.us.auth0.com
AUTH0_CLIENT_ID=<client-id>
AUTH0_CLIENT_SECRET=<client-secret>
```

### 3. Run dev server

```bash
npm run dev
```

Open [http://localhost:3000/alerts](http://localhost:3000/alerts).

## Features

### Alerts Table (`/alerts`)

Displays every row in `alerts/alerts_table.jsonl`. Columns are **fully dynamic** — derived from two DynamoDB fields on `chatkit_production_config` key `chat:web-tracking-agent`:
- `output_json_schema.required` → ordered list of field names (column identifiers)
- `output_requested_values` → ordered list of human-readable column labels (e.g. "Event Call-In Number & Access Code")

Both fields are written by the Bubble admin sync. Adding, renaming, or reordering fields in the Bubble admin Values tab automatically updates table columns and headers — no code deploy needed.

Column headers show the human-readable Bubble labels (e.g. "Is the Alert Relevant for an ART Newsreel article?"), not the raw JSON field names (e.g. `is_alert_relevant_for_art_newsreel`).

A sticky **Call ID** column (after Actions) shows the last 8 chars of `agent_call_id` — a UUID per agent invocation. This is more granular than `run_id` and helps identify which specific agent call produced each row. One page change can produce multiple rows (with the same `agent_call_id`) when the schema uses a top-level `alerts` array wrapper.

**Field renames are handled transparently:** `FIELD_ALIASES` in `AlertsTable.tsx` maps new field names → old field names. Old rows in S3 display correctly under new column headers without any data migration. To handle a new rename: add `{ newName: oldName }` to `FIELD_ALIASES` and `oldName` to `SUPERSEDED_COLS`.

Filters: target page, alert type, date range, free-text search. Buttons: **Apply Filters**, **Clear Filters**.

### Document Extractions Table (`/document-extractions`)

Displays every row in `alerts/document_extractions_table.jsonl`. Columns are **fully dynamic** — derived from DynamoDB key `chat:document-data-extraction` (`output_json_schema.required` + `output_requested_values`). Identity columns (`library_item_title`, `library_item_url`, `library_item_file_name`, `source_url`) are always shown first. Sticky **Call ID** column shows the last 8 chars of `agent_call_id`. Supports the same filter bar as Alerts (target page, date range, search). Re-evaluate flow mirrors the Alerts page.

### Re-evaluate

Re-runs the relevant agent on stored snapshots using the current DynamoDB config (without re-scraping the live page). Available on both the Alerts and Document Extractions pages.

1. Click **Re-evaluate** on any row → confirmation modal (shows whether config has changed since original run)
2. Confirm → ECS task starts, button shows "Running..."
3. When complete → **amber result group** appears after the last original row in the group
4. Changed cells highlighted via best-match diff; hover to see original value
5. **Accept** replaces all original rows for the group in S3 · **Discard** deletes the rerun result
6. Multiple groups can be re-evaluated simultaneously

For multi-row groups (same `run_id` + `target_id`), the rerun is group-based: all rerun rows appear together with a single Accept/Discard, and each rerun row is diff-highlighted against the closest-matching original row. Row count changes (e.g. 2 → 3 rows) are shown in the summary.

### QA Evaluation

Each alert row and document extraction row can be independently QA-evaluated via a rubric-based LLM agent.

**Alerts QA (`/alerts`):**
- Click **QA** button (purple) on any row → fires `POST /api/eval` → ECS task starts
- Dashboard polls `/api/eval/[taskId]` every 5s
- When complete, a **score badge** appears (e.g. `18/21`, color-coded green/amber/red) in the Actions cell
- Click the badge to expand a per-field score panel below the row (score + reasoning for every field)
- **Re-run QA** link replaces the prior result. **"Has QA Score"** option in the Alert Type dropdown filters to rows with results (client-side only).
- Score thresholds: green ≥ 90%, amber ≥ 70%, red < 70%

**Document Extractions QA (`/document-extractions`):**
- "Run QA" / "Re-run QA" button always visible in Actions cell
- Results appear as a violet `DocQaScoreRow` below each data row when an eval result exists
- `getEvalForRow(row, evalResults)` resolves the key using multiple fallbacks: direct `eval_row_key`, composite keys, prefix scan for `callId|*`, then bare `callId`
- **"Has QA Score"** (violet) and **"Imperfect QA Only"** (amber) toggle buttons in the filter bar

### Ingest Gate

The pipeline sets `ingest_status: "pending"` on rows eligible for the newsreel knowledge base (transcripts, documents flagged as newsreel-relevant). Dashboard users review and approve or reject each row — nothing is auto-ingested.

`ingest_status` lifecycle: `null` (not eligible) → `"pending"` (ready for review) → `"approved"` (sent to knowledge base) or `"rejected"` (dismissed).

Approval routes: `/api/ingest/approve-transcript` (transcripts), `/api/ingest/approve-document` (PDF/doc library items), `/api/ingest/reject`. Manual upload: `/api/ingest/upload-transcript-url` returns a presigned PUT URL for browser upload of `.txt` transcript files.

### Content Gate Modal

`ContentGateModal.tsx` is the unified UI for publishing content to Bubble (Eidarix) and the Newsreel Knowledge Base. Opens from the **Actions** button on rows with `bubble_action` or `transcript_s3_key`. Full technical reference: `src/components/docs/content-gate.md`.

Sections appear in creation-dependency order:

1. **Create Agenda Items** — editable titles + searchable chronicle-topic multi-select; × to remove. Must complete before Library Item.
2. **Library Item (Bubble)** — 3-column field table showing agent field → Bubble field → value. Reference fields (Type, Organizations, Chronicle Topics) use `SearchableCombobox` with options loaded from Bubble via `/api/bubble/options`. Footer has two buttons:
    - **Create / Update in Bubble** (primary)
    - **Add to Library Vector Store ▼** — dropdown with target KBs (currently Newsreel; room for future types). Fires the Newsreel ingest route for the row's transcript or document.
3. **Calendar Event (Bubble)** — same 3-column table + comboboxes. Blocked until Library Item done (when lib action present).
4. **Transcript / Document → Newsreel (standalone)** — only renders when there is no Library Item card to attach to.

Writes execute in-process from the dashboard ECS task via `src/lib/bubble-sync-eidarix.ts` (no Lambda intermediary). The Content Gate hits five endpoints on open: `/api/schema`, `/api/bubble/options` × 4, `/api/bubble/validate`, `/api/document-extractions`, and `/api/bubble/record` (UPDATE only).

**Date derivation (library item):** the Date value shown and sent is derived from the Bubble library item type — meeting materials and RFC use `event_start_date_time`; publications use `date_published` from doc extraction. User can override by typing.

**Bubble space model:** reference data (organizations, chronicle topics, library/calendar item types) always lives in the content space (`1768998437948x...`), regardless of `EIDARIX_VERSION`. Lookups and workflow payloads scope by this space.

### HTML Snapshots

Every alerts row has Before/After HTML download links — the exact stripped HTML the agent saw.

### Authentication

Auth0 protects all pages. Unauthenticated requests redirect to `/auth/login`. The middleware checks for `__session` / `__session.0` (chunked) cookies. API routes (`/api/*`) are not auth-protected (they're called server-side).

## API Routes

| Route | Method | Description |
|-------|--------|-------------|
| `/api/alerts` | GET | Returns filtered alert rows from S3. 60s cache. Filters: targetId, alertType, startDate, endDate, q. |
| `/api/schema` | GET | Returns `{ columns, labels }` from DynamoDB. Drills into `alerts.items` if schema has alerts array wrapper. Falls back to instructions fenced block. |
| `/api/config` | GET | Returns current agent config hash + model. |
| `/api/rerun` | POST | Starts ECS rerun task for alerts. |
| `/api/rerun/[taskId]` | GET | Polls ECS task status + fetches S3 alert rerun result. |
| `/api/rerun/accept` | POST | Patches `alerts_table.jsonl` with rerun rows. Always preserves `run_timestamp`, `agent_call_id`, `alert_date_time`, and `run_id` from the original (needed so QA eval can reconstruct the S3 snapshot path). |
| `/api/rerun/discard` | POST | Deletes alert rerun result from S3. |
| `/api/snapshot` | GET | Proxies S3 HTML snapshots for download. |
| `/api/pages` | GET | Returns list of tracked pages (id + name) for filter dropdown. |
| `/api/document-extractions` | GET | Returns filtered document extraction rows from S3. 60s cache. |
| `/api/doc-schema` | GET | Returns `{ columns, labels }` from DynamoDB for doc extractions. Filters out labels containing `, report`. |
| `/api/doc-rerun` | POST | Starts ECS rerun task for document extractions. |
| `/api/doc-rerun/[taskId]` | GET | Polls doc rerun task + returns `{ run_id, target_id, rerun_timestamp, config_hash, original_rows, rerun_rows }`. |
| `/api/doc-rerun/accept` | POST | Patches `document_extractions_table.jsonl` with rerun rows. Busts doc cache. |
| `/api/doc-rerun/discard` | POST | Deletes doc rerun result from S3. |
| `/api/alerts/patch-row` | PATCH | Patches arbitrary fields on alert rows by `agent_call_id`. Body: `{ agent_call_id, fields }`. Used by `ContentGateModal` to persist field edits and write back Bubble IDs after sync. |
| `/api/presigned-url` | GET | Generates presigned S3 GET URL for a recording or transcript file. Query param: `key`. Redirects (302) to presigned URL (1-hour expiry). |
| `/api/eval` | GET | Returns `{ results: Record<eval_row_key, EvalResult> }` from `alerts/eval_results_table.jsonl`. Key is `agent_call_id` for single-row runs; `agent_call_id\|library_item_url` for sibling rows. |
| `/api/eval` | POST | Fires ECS RunTask with CMD override `["python", "-m", "eval.run_eval", "--agent-call-ids", "<id>"]`. Body: `{ agent_call_id }`. Returns `{ taskId }`. |
| `/api/eval` | DELETE | Removes all eval results for a given `agent_call_id` from `eval_results_table.jsonl`. Body: `{ agent_call_id }`. |
| `/api/eval/[taskId]` | GET | Polls ECS task status. Returns `{ status: "running" \| "complete" \| "failed" \| "error" }`. |
| `/api/eval/documents` | GET | Returns `{ results: Record<eval_row_key, DocEvalResult> }` from `alerts/doc_eval_results_table.jsonl`. Key is `eval_row_key` when present, otherwise `agent_call_id`. |
| `/api/eval/documents` | POST | Fires ECS RunTask with CMD override `["python", "-m", "eval.run_doc_eval", "--agent-call-ids", "<id>"]`. Body: `{ agent_call_id }`. Returns `{ taskId }`. |
| `/api/eval/documents` | DELETE | Removes eval results for a given `agent_call_id` from `doc_eval_results_table.jsonl`. Body: `{ agent_call_id }`. |
| `/api/eval/documents/[taskId]` | GET | Polls ECS task status for document eval runs. Same response shape as `/api/eval/[taskId]`. |
| `/api/bubble/sync` | GET | Returns `bubble_action` preview for a given `agent_call_id` (query param). Used by modal before confirm. |
| `/api/bubble/sync` | POST | Runs `syncAlert()` in-process via `src/lib/bubble-sync-eidarix.ts` (no Lambda intermediary). Body: `{ agent_call_id, action?, skip_validation? }`. `action` values: `"all"`, `"agenda_items"`, `"library_item"`, `"event"`. Returns `{ ok, bubble_library_item_id?, bubble_event_id?, eidarix_agenda_item_ids? }`. |
| `/api/bubble/validate` | GET | Pre-flight check for the Content Gate: returns `{ valid, issues }` where issues cover unresolved orgs/topics/types. Query: `agent_call_id`. |
| `/api/bubble/record` | GET | Fetches current field values from Bubble for a given `agent_call_id` and `object_type`. Returns `{ found, id, fields }`. Used by `ContentGateModal` for UPDATE previews. |
| `/api/bubble/options` | GET | Returns `{ options: [{ id, name }] }` from Bubble for `type=organization\|chronicletopic\|libraryitemtype\|calendaritemtype`. 10-minute in-memory cache. Powers the `SearchableCombobox` dropdowns in the Content Gate. |
| `/api/ingest/approve-transcript` | POST | Generates presigned S3 GET URL for `transcript_s3_key`, calls the newsreel ingest API, patches alert row to `ingest_status: "approved"`. Body: `{ agent_call_id }`. |
| `/api/ingest/approve-document` | POST | Calls doc ingest API, patches doc extraction row to `ingest_status: "approved"`. Body: `{ agent_call_id, library_item_url }`. |
| `/api/ingest/reject` | POST | Patches row to `ingest_status: "rejected"`. Body: `{ table: "alerts"\|"docs", agent_call_id, library_item_url? }`. |
| `/api/ingest/manual-document-url` | POST | Direct one-off URL ingest — no row created. Body: `{ url, filename }`. |
| `/api/ingest/upload-transcript-url` | POST | Returns presigned S3 PUT URL for manual transcript upload. Body: `{ agent_call_id, filename }`. Returns `{ upload_url, s3_key }`. |
| `/api/ingest/trigger-manual-chunk` | POST | Legacy — starts ECS task in `MANUAL_CHUNK` mode. No longer called from the dashboard. Body: `{ agent_call_id, transcript_s3_key }`. |

## Deployment

```bash
AWS_PROFILE=bridgeway ./scripts/deploy.sh
```

Builds a linux/amd64 Docker image (the script sets the platform automatically), pushes to ECR, runs `terraform apply`, and force-deploys the ECS service.

**Notes:**
- Do not add `--no-cache` to the Docker build — it skips the npm install cache layer and makes builds ~10x slower.
- Do not add `--platform` manually — the script handles it.

Deploy script flags:
- `--tag <tag>` — Custom image tag (default: latest)
- `--skip-build` — Skip Docker build, just push existing image
- `--skip-terraform` — Skip terraform, just redeploy ECS

### Infrastructure

```bash
cd infra/terraform
terraform init
terraform apply
```

| Resource | Details |
|----------|---------|
| **ECS Fargate** | ARM64, 0.5 vCPU, 1024 MB, port 3000 |
| **ALB** | HTTPS listener (443) with ACM cert (`tracker.bridgewayanalytics.com`) + HTTP (80) → HTTPS 301 redirect |
| **Security Groups** | ALB: ports 80 + 443 from anywhere; ECS: port 3000 from ALB only |
| **CloudWatch** | Log group `/ecs/naic-dashboard` |
| **ECR** | Docker image repository |
| **IAM** | DynamoDB read, S3 read/write, ECS RunTask |

Domain: `tracker.bridgewayanalytics.com` (DNS managed by Microsoft, CNAME to ALB)

Health check: `/api/pages` (200 matcher)

## Project Structure

```
src/
├── app/
│   ├── alerts/
│   │   ├── page.tsx                    # Alerts page (filter bar, data fetching)
│   │   └── AlertsTable.tsx             # Dynamic table, re-evaluate UI, column logic,
│   │                                   #   FIELD_ALIASES, SUPERSEDED_COLS, resolveCell(),
│   │                                   #   CellValue renderer
│   ├── document-extractions/
│   │   ├── page.tsx                    # Document Extractions page (filter bar)
│   │   └── DocExtractionsTable.tsx     # Mirrors AlertsTable; DOC_IDENTITY_COLS, re-evaluate UI
│   ├── auth/[auth0]/route.ts           # Auth0 login/logout/callback handler
│   ├── api/
│   │   ├── alerts/                     # Alerts data from S3 + patch-row
│   │   ├── schema/                     # Dynamic column order (alerts) + alerts-array unwrap
│   │   ├── config/                     # Agent config hash
│   │   ├── rerun{,/[taskId],/accept,/discard}/
│   │   │                               # Alert rerun lifecycle
│   │   ├── snapshot/                   # Proxy S3 HTML snapshots
│   │   ├── pages/                      # Tracked pages list
│   │   ├── document-extractions/       # Doc extraction data from S3
│   │   ├── doc-schema/                 # Dynamic column order (doc extractions)
│   │   ├── doc-rerun{,/[taskId],/accept,/discard}/
│   │   │                               # Doc rerun lifecycle
│   │   ├── eval{,/[taskId],/documents,...}/
│   │   │                               # QA eval ECS triggers + results
│   │   ├── bubble/
│   │   │   ├── sync/                   # Content Gate sync entry point
│   │   │   ├── validate/               # Pre-flight resolution check
│   │   │   ├── record/                 # Fetches current Bubble values for UPDATE
│   │   │   └── options/                # Reference-data dropdown options (orgs, topics, types)
│   │   ├── ingest/                     # Newsreel KB approve/reject routes
│   │   └── presigned-url/              # Recording / transcript S3 presign
│   ├── layout.tsx                      # Root layout
│   └── page.tsx                        # Redirects to /alerts
├── components/
│   ├── ContentGateModal.tsx            # Unified publish modal (Bubble + Newsreel)
│   ├── SearchableCombobox.tsx          # Reusable single/multi dropdown with search
│   ├── IngestGateCell.tsx              # Approve/reject cell for doc extraction KB ingest
│   └── docs/
│       └── content-gate.md             # Content Gate technical reference
├── lib/
│   ├── alerts-cache.ts                 # 60s in-memory cache for alerts
│   ├── doc-extractions-cache.ts        # 60s in-memory cache for doc extractions
│   ├── bubble-sync-eidarix.ts          # In-process Bubble sync executor
│   ├── chatkit-api-key.ts              # SSM helper for the newsreel ingest API key
│   ├── patch-jsonl.ts                  # Download/patch/upload a JSONL row
│   ├── ssm.ts                          # Shared SSM parameter helper
│   └── auth0.ts                        # Auth0 client instance
├── middleware.ts                       # Auth0 session check, route protection
infra/terraform/                        # ECS, ALB, IAM, CloudWatch, ECR, security groups
scripts/
└── deploy.sh                           # Build + push + terraform + ECS redeploy
Dockerfile                              # Multi-stage Node.js 20 production build
```
