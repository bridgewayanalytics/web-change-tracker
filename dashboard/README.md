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
│   ├── auth/
│   │   └── [auth0]/route.ts           # Auth0 login/logout/callback handler
│   ├── api/
│   │   ├── alerts/route.ts             # Alerts data from S3
│   │   ├── schema/route.ts             # Dynamic column order from DynamoDB (alerts)
│   │   │                               #   + alerts array unwrapping
│   │   ├── config/route.ts             # Agent config hash
│   │   ├── rerun/route.ts              # Start ECS rerun task (alerts)
│   │   ├── rerun/[taskId]/             # Poll task + fetch alert result
│   │   ├── rerun/accept/               # Patch alerts_table.jsonl
│   │   ├── rerun/discard/              # Delete alert rerun result
│   │   ├── snapshot/                   # Proxy S3 HTML snapshots
│   │   ├── pages/route.ts              # Tracked pages list
│   │   ├── document-extractions/       # Document extractions data from S3
│   │   ├── doc-schema/route.ts         # Dynamic column order (document extractions)
│   │   │                               #   + array wrapper unwrapping
│   │   ├── doc-rerun/route.ts          # Start ECS rerun task (doc extractions)
│   │   ├── doc-rerun/[taskId]/         # Poll task + fetch doc result
│   │   ├── doc-rerun/accept/           # Patch document_extractions_table.jsonl
│   │   └── doc-rerun/discard/          # Delete doc rerun result
│   ├── layout.tsx                      # Root layout
│   └── page.tsx                        # Root page (redirects to /alerts)
├── lib/
│   ├── alerts-cache.ts                 # In-memory cache for alerts (60s TTL)
│   ├── doc-extractions-cache.ts        # In-memory cache for doc extractions (60s TTL)
│   └── auth0.ts                        # Auth0 client instance
├── middleware.ts                        # Auth0 session check, route protection
infra/terraform/                        # ECS, ALB, IAM, CloudWatch, ECR, security groups
scripts/
└── deploy.sh                           # Build + push + terraform + ECS redeploy
Dockerfile                              # Multi-stage Node.js 20 production build
```
