# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## What this repo is

**NAIC Alerts Dashboard** — a Next.js 14 app deployed at `https://tracker.bridgewayanalytics.com` with two main table pages:

1. **Alerts** (`/alerts`) — displays structured alerts from `alerts/alerts_table.jsonl` in S3
2. **Document Extractions** (`/document-extractions`) — displays document extraction results from `alerts/document_extractions_table.jsonl` in S3

Both tables support dynamic columns (from DynamoDB), filtering, re-evaluation via ECS rerun tasks, amber inline result rows with Accept/Discard, and an **ingest gate** for approving/rejecting rows for the newsreel knowledge base.

Writes to S3 JSONL files happen on: Accept/Discard (rerun), and Approve/Reject (ingest gate).

Auth0 protects all pages — unauthenticated users are redirected to login.

## Commands

```bash
npm run dev          # Start Next.js dev server (http://localhost:3000)
npm run build        # Production build
npm run lint         # ESLint
npm run test         # Vitest (single run)
npm run test:watch   # Vitest (watch mode)
```

Tests live alongside source files as `*.test.ts` in `src/`. Vitest uses the `@` path alias mapped to `src/`.

## Deploy

```bash
AWS_PROFILE=bridgeway ./scripts/deploy.sh
```

Builds a **linux/amd64** Docker image (the build script sets `--platform linux/amd64` — do not add it manually), pushes to ECR, runs `terraform apply`, and force-deploys the ECS service.

**Do NOT add `--no-cache` to the docker build command** — it makes builds ~10x slower by skipping the npm install cache layer.

AWS SSO profile: `bridgeway`. Run `aws sso login --profile bridgeway` if credentials expire.

Deploy script flags:
- `--tag <tag>` — Custom image tag (default: latest)
- `--skip-build` — Skip Docker build, just push existing image
- `--skip-terraform` — Skip terraform, just redeploy ECS

## Architecture

### Data flow

**Alerts:**
1. **S3** (`alerts/alerts_table.jsonl`) → read by `/api/alerts` (60s in-memory cache)
2. **S3** (`alerts/eval_results_table.jsonl`) → read by `/api/eval` (no cache); map of `eval_row_key → eval result` (key = `agent_call_id` for single-row runs; `agent_call_id|library_item_url` for sibling rows)
3. **DynamoDB** (`chatkit_production_config`, key `chat:web-tracking-agent`) → `output_json_schema` + `output_requested_values` drive dynamic column order + labels via `/api/schema`
4. **`AlertsTable` component** derives columns from the schema API response (falls back to deriving columns from data keys if unavailable)
5. **Re-evaluate flow** → ECS RunTask → poll → inline amber result row → Accept patches S3 / Discard deletes result
6. **QA eval flow** → `POST /api/eval` → ECS RunTask → poll `/api/eval/[taskId]` → on complete, refetch `/api/eval` → score badge appears in Actions cell; click to expand field-by-field scores

**Document Extractions:**
1. **S3** (`alerts/document_extractions_table.jsonl`) → read by `/api/document-extractions` (60s in-memory cache via `lib/doc-extractions-cache.ts`)
2. **S3** (`alerts/doc_eval_results_table.jsonl`) → read by `/api/eval/documents`; map of `agent_call_id → doc eval result`
3. **DynamoDB** (`chatkit_production_config`, key `chat:document-data-extraction`) → `output_json_schema` + `output_requested_values` drive dynamic column order + labels via `/api/doc-schema`
4. **`DocExtractionsTable` component** — mirrors AlertsTable; `DOC_IDENTITY_COLS` (`library_item_title`, `library_item_url`, `library_item_file_name`, `source_url`) always shown first; no sticky data columns
5. **Re-evaluate flow** → `POST /api/doc-rerun` → ECS RunTask → poll `/api/doc-rerun/[taskId]` → inline amber result row → Accept patches `document_extractions_table.jsonl` / Discard deletes result
6. **Doc QA eval flow** → `POST /api/eval/documents` → ECS RunTask → poll `/api/eval/documents/[taskId]` → on complete, refetch `/api/eval/documents` → violet QA score row appears below each data row

### API routes (`src/app/api/`)

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/alerts` | GET | Reads `alerts_table.jsonl` from S3, applies filters (targetId, alertType, startDate, endDate, q), returns rows. 60s in-memory cache. |
| `/api/schema` | GET | Reads `output_json_schema` + `output_requested_values` from DynamoDB (`chat:web-tracking-agent`). Returns `{ columns, labels }`. Drills into `alerts.items` if schema has alerts array wrapper. Falls back to instructions fenced block. |
| `/api/config` | GET | Returns current agent config hash + model name from DynamoDB (used by rerun modal). |
| `/api/rerun` | POST | Starts ECS Fargate task with `RERUN_RUN_ID` + `RERUN_TARGET_ID` env overrides. Returns `{ taskArn }`. |
| `/api/rerun/[taskId]` | GET | Polls ECS task status. When stopped + exit 0, fetches result from S3 `alerts/reruns/<run_id>/<target_id>/result.json`. |
| `/api/rerun/accept` | POST | Patches `alerts_table.jsonl` in S3 (replaces rows for run_id+target_id with rerun rows). Always preserves `run_timestamp`, `agent_call_id`, `alert_date_time`, and `run_id` from the original row (`run_id` is in `alwaysPreserve` so `html_fetcher.py` can reconstruct the correct S3 path for QA eval). Conditionally preserves `target_id` and `source_url` when missing from the rerun row. Busts cache. |
| `/api/rerun/discard` | POST | Deletes rerun result from S3. |
| `/api/snapshot` | GET | Proxies before/after HTML snapshots from S3 for download. Safety check: path must start with `pages/` and end with `.html`. |
| `/api/pages` | GET | Returns list of tracked pages (id + name) for the filter dropdown. |
| `/api/document-extractions` | GET | Reads `document_extractions_table.jsonl` from S3, applies filters, returns rows. 60s in-memory cache. |
| `/api/doc-schema` | GET | Reads `output_json_schema` + `output_requested_values` from DynamoDB (`chat:document-data-extraction`). Unwraps single-property array wrapper if present. Filters out labels containing `, report`. |
| `/api/doc-rerun` | POST | Starts ECS rerun task for document extractions. Returns `{ taskArn }`. |
| `/api/doc-rerun/[taskId]` | GET | Polls ECS task. When complete, returns `{ run_id, target_id, rerun_timestamp, config_hash, original_rows, rerun_rows }` from S3. |
| `/api/doc-rerun/accept` | POST | Patches `document_extractions_table.jsonl` (replaces rows for run_id+target_id). Busts doc cache. |
| `/api/doc-rerun/discard` | POST | Deletes doc rerun result from S3. |
| `/api/presigned-url` | GET | Generates presigned S3 GET URL for a recording or transcript file. Query param: `key`. Redirects (302) to the presigned URL (1-hour expiry). |
| `/api/ingest/approve-transcript` | POST | Generates presigned URL for `transcript_s3_key`, calls `POST /internal/documents/ingest`, patches alert row to `ingest_status: "approved"`. Body: `{ agent_call_id }`. |
| `/api/ingest/approve-document` | POST | Calls doc ingest API, patches doc extraction row to `ingest_status: "approved"`. Body: `{ agent_call_id, library_item_url }`. |
| `/api/ingest/reject` | POST | Patches row to `ingest_status: "rejected"`. Body: `{ table: "alerts"|"docs", agent_call_id, library_item_url? }`. |
| `/api/ingest/manual-document-url` | POST | Direct one-off URL ingest — no row created. Body: `{ url, filename }`. |
| `/api/ingest/upload-transcript-url` | POST | Returns presigned S3 PUT URL for manual transcript file upload. Body: `{ agent_call_id, filename }`. Returns `{ upload_url, s3_key }`. |
| `/api/ingest/trigger-manual-chunk` | POST | Legacy — starts ECS task in `MANUAL_CHUNK` mode. No longer called from the dashboard (chunking removed). Body: `{ agent_call_id, transcript_s3_key }`. |
| `/api/bubble/sync` | GET | Returns `bubble_action` preview for a given `agent_call_id` (query param). Used by modal before confirm. |
| `/api/bubble/sync` | POST | Invokes the `web-change-tracker-prod-bubble-sync` Lambda **synchronously** (`InvocationType: RequestResponse`) — waits for completion and returns the final result directly. No polling needed. Stamps `bubble_sync_status: "syncing"` optimistically before invoke; Lambda patches `"synced"` or `"error"` on the row. `action` values: `"all"` (full sequence), `"agenda_items"`, `"library_item"`, `"event"`. Body: `{ agent_call_id, action? }`. Returns `{ ok, bubble_library_item_id?, bubble_event_id?, eidarix_agenda_item_ids? }`. |
| `/api/bubble/record` | GET | Fetches current field values from Bubble for a given `agent_call_id` and `object_type` (`calendaritem` or `libraryitem`). Searches Bubble using `match_search` from `bubble_action`. Returns `{ found, id, fields }` — `fields` is a display-name → value dict. Used by `ContentGateModal` to populate the unified UPDATE view showing both current Bubble values and proposed agent values side by side. |
| `/api/alerts/patch-row` | PATCH | Patches arbitrary fields on alert rows by `agent_call_id`. Body: `{ agent_call_id, fields: Record<string, unknown> }`. Used by `ContentGateModal` to persist field edits. |
| `/api/eval` | GET | Returns `{ results: Record<eval_row_key, EvalResult> }` from `alerts/eval_results_table.jsonl`. Key is `agent_call_id` for single-row runs; `agent_call_id|library_item_url` for sibling rows. Each entry has `eval_scores`, `eval_timestamp`, `eval_run_id`. |
| `/api/eval` | POST | Fires ECS RunTask with CMD override `["python", "-m", "eval.run_eval", "--agent-call-ids", "<id>"]`. Body: `{ agent_call_id }`. Returns `{ taskId }`. |
| `/api/eval` | DELETE | Removes all eval results for a given `agent_call_id` (exact match + `agent_call_id|*` sibling entries) from `eval_results_table.jsonl`. Body: `{ agent_call_id }`. |
| `/api/eval/[taskId]` | GET | Polls ECS task status. Returns `{ status: "running" | "complete" | "failed" | "error" }`. No S3 result fetch — eval results are written directly to JSONL by the eval script. |
| `/api/eval/documents` | GET | Returns `{ results: Record<agent_call_id, DocEvalResult> }` from `alerts/doc_eval_results_table.jsonl`. |
| `/api/eval/documents` | POST | Fires ECS RunTask with CMD override `["python", "-m", "eval.run_doc_eval", "--agent-call-ids", "<id>"]`. Body: `{ agent_call_id }`. Returns `{ taskId }`. |
| `/api/eval/documents` | DELETE | Removes eval results for a given `agent_call_id` from `doc_eval_results_table.jsonl`. Body: `{ agent_call_id }`. |
| `/api/eval/documents/[taskId]` | GET | Polls ECS task status for document eval runs. Same response shape as `/api/eval/[taskId]`. |

### Auth0 integration

- **Middleware** (`src/middleware.ts`): Checks `__session` / `__session.0` (chunked) cookies. Protects all routes except `/auth/*`, `/_next/*`, `/api/*`, static assets. Auth failure redirects to `/auth/login?returnTo=<pathname>`.
- **Auth0 client** (`src/lib/auth0.ts`): `Auth0Client` from `@auth0/nextjs-auth0/server`.
- **Auth routes**: `/auth/login`, `/auth/logout`, `/auth/callback` — handled by Auth0 SDK via `src/app/auth/[auth0]/route.ts`.
- **Env vars**: `AUTH0_SECRET`, `AUTH0_DOMAIN`, `AUTH0_CLIENT_ID`, `AUTH0_CLIENT_SECRET`, `APP_BASE_URL` (or `AUTH0_BASE_URL`). Auth0 is disabled if any var is missing.
- **Auth0 tenant**: `dev-dwy0013t.us.auth0.com`
- **Callback URL**: `https://tracker.bridgewayanalytics.com/auth/callback` must be in Auth0 allowed callback URLs.

### Key components

**`src/app/alerts/`:**
- **`AlertsTable.tsx`** — main table component. Dynamic columns + human-readable labels. Sticky "Call ID" column shows last 8 chars of `agent_call_id`. Re-evaluate button → confirmation modal → ECS task → group-based amber result rows (`RerunResultGroup`). Fixed columns at the right: **Recording** (presigned mp3 link), **Transcript** (presigned txt link + manual upload button), **Chunks** (chunked transcript view). Actions cell: **Re-evaluate** button + **Actions** button (opens `ContentGateModal` for rows with `bubble_action` or `transcript_s3_key`) + **✓ Complete** badge (when all Bubble IDs stamped and transcript approved) + **QA section** (see below). `recording_s3_key`, `transcript_s3_key`, `transcript_chunks_s3_key`, `manual_transcript_s3_key`, `ingest_status`, `bubble_action`, `bubble_sync_status` are in `METADATA_COLS` (not shown as data columns). Props: `rows`, `onAccepted`, `schemaVersion`, `hasQaScoreFilter`. **Scroll:** table container uses `overflow-auto hide-bottom-scrollbar` (trackpad horizontal swipe works); top scrollbar div (`topRef`) is synced via `onScroll` events — do NOT change the table container to `overflowX: hidden` or trackpad scrolling breaks. **Row numbers:** stable descending numbers based on full unfiltered `rows` array (`alertRowIndexMap = useMemo(() => new Map(rows.map((r,i) => [r,i])), [rows])`); do not reset when filters are applied. **URL columns:** all `_url` fields display the full hyperlink text (not "Link"); semicolon-separated URLs split into individual `<a>` tags.
- **`page.tsx`** — alerts page container. Filter bar with: target page dropdown, alert type dropdown (includes **"Has QA Score"** as a sentinel option `__qa__` — filters client-side via `hasQaScoreFilter` prop, not sent to server), date range, free-text search, **Apply Filters** button, **Clear Filters** button, **"Show not relevant" checkbox** (default off — hides rows where `alert_type` contains "not relevant"; Clear Filters resets it to off).

**`src/app/document-extractions/`:**
- **`DocExtractionsTable.tsx`** — mirrors AlertsTable. Dynamic columns from `/api/doc-schema`. First sticky column is a **row number** (`#`, width 44px), then **Actions** (Re-evaluate + Run QA), then **Extraction ID** (last 8 chars of `agent_call_id`). `DOC_IDENTITY_COLS = ["library_item_title", "library_item_url", "library_item_file_name", "source_url"]` always shown first. No sticky data columns. Group-based rerun UI — **accepting a re-evaluated row automatically triggers QA** (`startQa(callId)` called in `onAccept`, same as AlertsTable). New fixed **Knowledge Base** column at the right (`IngestGateCell` — approve/reject for document ingest). `ingest_status` is in `METADATA_COLS` (not shown as a data column). **QA infrastructure** (mirrors alerts QA): `evalResults` fetched from `GET /api/eval/documents` on mount; `qaRunning` Map tracks in-flight ECS tasks polled every 5s via `/api/eval/documents/[taskId]`; `localStorage` key `doc_qaRunning` persists in-flight tasks across reloads. QA button always visible: "Run QA" when no eval result exists, "Re-run QA" when one does — never hidden. `DocQaScoreRow` (violet background) rendered below each data row when eval result exists, showing score (`correct/total`), `Evaluated: [timestamp]` label, and "Re-run QA" link. Props `hasQaScoreFilter` and `hasImperfectQaFilter` control client-side QA filtering applied before render (`displayedRows`). **Row numbers:** stable descending, based on full unfiltered `rows` array (`rowIndexMap`); do not reset on filter. **Pagination:** 50 rows/page; controls rendered **above** the table (not below — table uses internal scroll so below-table content is never visible). **URL columns:** all `_url` fields display full hyperlink text; semicolon-separated URLs split into individual `<a>` tags. **Actions cell:** shows `Re-eval: [time]` when `last_rerun_at` is present (stamped on accept), otherwise `run_timestamp`. **`data_extraction_datetime` is immutable:** treated as unchanged in the rerun preview (`IMMUTABLE_COLS` set) and always copied from the original row in `doc-rerun/accept/route.ts` — never reflects the re-evaluation time. `/api/doc-rerun/accept` also stamps `last_rerun_at` on accepted rows.
- **`page.tsx`** — document extractions page container. Filter bar includes: Page dropdown, **Organization** dropdown (client-side, derived from all API rows), **Document Type** dropdown (client-side), date range, search. QA toggle buttons: **"Has QA Score"** (violet) and **"Imperfect QA Only"** (amber) — mutually exclusive, passed as props to `DocExtractionsTable`. Clear button shows active filter count. Org and doc type filtering applied client-side after API fetch so dropdown options always reflect the full result set.

**`src/lib/`:**
- **`alerts-cache.ts`** — in-memory cache for alerts (60s TTL)
- **`doc-extractions-cache.ts`** — shared in-memory cache module for document extractions (`getDocExtractionsCache`, `setDocExtractionsCache`, `bustDocExtractionsCache`, `CACHE_TTL = 60_000`). Used by both the GET route and accept route to bust the same in-process cache.
- **`chatkit-api-key.ts`** — SSM helper for `CHATKIT_INTERNAL_API_KEY`. Reads from env first, then SSM param `/web-change-tracker/prod/chatkit_internal_api_key`. Used by ingest approval routes.
- **`patch-jsonl.ts`** — `patchJsonlRows()`: downloads a JSONL file from S3, patches all matching rows in-place, re-uploads. Used by ingest approve/reject routes.
- **`auth0.ts`** — Auth0 client instance

**`src/components/`:**
- **`IngestGateCell.tsx`** — Shared ingest gate UI cell. Shows status badge (Pending/Approved/Rejected) with Approve | Reject action buttons and optimistic local state updates. Used by `DocExtractionsTable`.
- **`ContentGateModal.tsx`** — Unified content gate modal (June 2026). Replaces `BubbleSyncModal` + separate ingest buttons. Shows sections in creation-dependency order: (1) **Create Agenda Items** card — only when `agenda_item_previews` contains items with `status == "New"`; each item shows an editable title input and chronicle topic chips; a × delete button removes an item (deletion is local until confirm); button calls `/api/bubble/sync { action: "agenda_items" }`; must complete before Library Item step can proceed. If any items were deleted or titles edited, `bubble_action.agenda_item_previews` is patched to S3 via `/api/alerts/patch-row` before the Lambda call so the Lambda uses the user's version. (2) **Bubble Library Item** card — if `bubble_action.library_item` is set; shows existing Bubble values alongside proposed values for UPDATE, or proposed-only for CREATE; "Linked Agenda Items" field in the FieldTable shows all items (new + existing) to be linked. (3) **Bubble Event** card — if `bubble_action.event` is set; blocked until Library Item card is done (when lib action present). (4) **Transcript → Newsreel** — only shown when `alert_type === "New Meeting Transcript Available"` (NOT just any row with `transcript_s3_key` — recording matcher stamps that key on all sibling rows for the same meeting). (5) **Document → Newsreel** (if `library_item_url` valid). Dependency blocking: agenda card must be done before lib button is enabled; lib card must be done before event button is enabled. `status` prefixes ("New - ", "Existing - ", "Updated - ") stripped from display titles via `stripStatusPrefix()`. Field edits patch `bubble_action` via `PATCH /api/alerts/patch-row` before syncing. Bubble sections fire `/api/bubble/sync` with `action` param; newsreel sections call `/api/ingest/approve-transcript` or `/api/ingest/approve-document`. Document ingest status fetched async from `/api/document-extractions?agent_call_id=...`. **Org pre-validation:** before invoking Lambda for library item or event steps, calls `GET /api/bubble/record` to verify org names resolve in Bubble; shows a validation popup listing any unresolved orgs before allowing confirm. **Sync polling:** uses wall-clock deadline (not retry count) to bound polling duration — prevents hangs when ECS tasks exceed expected runtime. Patched Bubble IDs (`bubble_library_item_id`, `bubble_event_id`, `eidarix_agenda_item_ids`) are written back into the parent row state after each successful step via `/api/alerts/patch-row`.
- **`BubbleSyncModal.tsx`** — Legacy. Superseded by `ContentGateModal`. Not used by `AlertsTable`.

### Column system

Columns are **fully dynamic** — derived from DynamoDB:

1. **`/api/schema`** reads `output_json_schema` (a full JSON Schema object written by the Bubble admin sync) and `output_requested_values` (an ordered list of human-readable label strings, also written by Bubble)
2. **Alerts array unwrapping:** if `output_json_schema.properties` has an `alerts` key with `type: "array"` and `items`, the schema route drills into `items` to get the inner schema. This is necessary because the multi-alert wrapper changes `required` from 21 field names to just `["alerts"]`.
3. `output_json_schema.required` (inner schema after unwrapping) provides the ordered list of field names (column identifiers)
4. `output_requested_values` provides the display labels (zipped 1:1 with `required`)
5. `AlertsTable` fetches `{columns, labels}` on mount
6. Column headers display the Bubble label (e.g. "Is the Alert Relevant for an ART Newsreel article?") not the raw field name
7. Any data fields not listed in the schema are appended alphabetically as extra columns
8. Pipeline metadata (`run_id`, `run_timestamp`, `target_id`, `source_url`, `config_hash`, `events`, `agenda_items`, `agent_call_id`) is always excluded via `METADATA_COLS`

**To add/rename/reorder columns:** edit in the Bubble admin Values tab → sync to DynamoDB. The next page load picks up the new schema automatically. No code deploy needed.

### Field aliases and legacy column suppression

`alerts_table.jsonl` contains rows from two schema generations. Several field names changed between generations. Rather than migrating data, `AlertsTable.tsx` handles this transparently:

- **`FIELD_ALIASES`** — maps current field name → old field name it replaced:
  ```typescript
  {
    event_start_date_time: "event_start_datetime",
    event_end_date_time: "event_end_datetime",
    event_call_in_number_access_code: "event_call_in_access_code",
    is_alert_relevant_for_art_newsreel: "is_relevant_for_art_newsreel",
    library_items_file_name: "library_item_file_name",
    agenda_item_title_official: "agenda_item_official_title",
    agenda_item_title_and_chronicle_topics: "agenda_item_title",
  }
  ```
- **`resolveCell(row, col)`** — first checks `row[col]`; if absent, falls back to `row[FIELD_ALIASES[col]]`. Old rows display correctly under new column headers with no data migration.
- **`SUPERSEDED_COLS`** — set of old field names covered by an alias or merged into another field. These are excluded from `deriveColumns()` so they never appear as duplicate columns. Also includes legacy-only fields (`candidate_agenda_items`, `candidate_chronicles`, `event_timezone`).

**When a field is renamed in Bubble admin:** add `{ newName: oldName }` to `FIELD_ALIASES` and add `oldName` to `SUPERSEDED_COLS`. This is a one-line code change + deploy; no data migration required.

### Cell rendering

`CellValue` in `AlertsTable.tsx` handles the new flat schema's complex types:
- **Strings** → plain text; long strings (>150 chars) get expand/collapse
- **URLs** (columns ending in `_url` where value starts with `http`) → clickable link; `"N/A"` is NOT rendered as a link
- **Arrays of strings** → comma-separated; topic/chronicle columns render as tag badges (purple pills)
- **Arrays of objects** (e.g. `agenda_item_title_and_chronicle_topics`) → collapsible JSON
- **Objects with `{status, ...}`** (e.g. `library_item_preliminary_title`, `is_alert_relevant_for_art_newsreel`) → renders the main value + status in parentheses; N/A/No statuses are greyed out
- **Alert types** → color-coded badges (blue for meetings, green for reports, orange for agendas)
- **Booleans** → "Yes" / ""
- **Empty / null / undefined** → dash (`---`)

### Ingest gate

The pipeline sets `ingest_status: "pending"` on rows that are ready for the newsreel knowledge base (transcript available, or document flagged `newsreel_relevance.status == "Yes"`) instead of auto-ingesting. The dashboard user reviews and approves or rejects each row.

`ingest_status` lifecycle: `null` (not eligible) → `"pending"` (ready for review) → `"approved"` (sent to knowledge base) or `"rejected"` (dismissed, can be re-approved).

**Approve transcript:** generates a presigned S3 GET URL for `transcript_s3_key` (or `manual_transcript_s3_key`) → calls `POST https://chat-api.bridgewayanalytics.com/internal/documents/ingest` with `{namespace, filename, url: presignedUrl}` → patches row to `"approved"`.

**Approve document:** reads `library_item_url` + `library_item_title` from row → calls `POST https://chat-api.bridgewayanalytics.com/internal/documents/ingest` (multipart, URL-based) → patches row to `"approved"`.

**Manual transcript upload (alerts table):** for rows without `transcript_s3_key` (manually recorded meetings): get presigned PUT URL → browser uploads .txt file to `transcripts/manual/<agent_call_id>/<filename>` → row immediately marked ready with `ingest_status: "pending"` (no chunking step).

Auth for ingest API calls: `CHATKIT_INTERNAL_API_KEY` from env or SSM (`/web-change-tracker/prod/chatkit_internal_api_key`), fetched lazily via `lib/chatkit-api-key.ts`.

### QA Evaluation feature

Each alert row can be QA-evaluated by the `chat:eval-agent` (rubric-based field-by-field scoring). Evaluation is triggered per-row via ECS, results are stored in `alerts/eval_results_table.jsonl` (upsert keyed by `eval_row_key`: `agent_call_id` for single-row runs, `agent_call_id|library_item_url` for sibling rows from the same run). `lookupEvalResult(row, evalResults)` in `AlertsTable.tsx` resolves the correct key: when both a plain `callId` key and a composite `callId|libUrl` key exist in `evalResults`, the function returns whichever has the newer `eval_timestamp` (so a fresh solo re-evaluation overwrites a stale sibling-group result rather than being shadowed by it). If only one key exists, it returns that one directly.

**Actions cell — QA section:**
- **QA** button (purple) — shown when no eval result exists for that row; fires `POST /api/eval`
- **Score badge** (e.g. `18/21 ▼`, color green/amber/red) — shown when eval result exists; click to toggle expanded score panel below the row
- **Re-run QA** link — shown alongside the badge; re-runs eval and overwrites prior result
- **QA running…** spinner — shown while ECS task is in-flight (polled every 5s via `/api/eval/[taskId]`)

**QA score row:** a `<tr>` always shown below the data row when an eval result exists (not collapsible). The Actions cell shows: "QA N/total", pattern observation excerpt, and evaluation date. Each data column cell shows a score badge (✓ Correct / ~ Partially Correct / ✗ Incorrect) with the reasoning text inline below it.

**Has QA Score filter:** "Has QA Score" option inside the Alert Type dropdown (sentinel value `__qa__`). When selected, `AlertsTable` filters rendered rows client-side to only those with an entry in `evalResults`. The `__qa__` value is not sent to the server `/api/alerts` endpoint.

**State in `AlertsTable`:** `evalResults: Record<string, EvalResult>` fetched from `GET /api/eval` on mount and after each QA task completes; `qaRunning: Map<string, taskId>` for in-flight tasks.

**Score color thresholds:** green ≥ 90% correct, amber ≥ 70%, red < 70%.

### Re-evaluate feature

1. User clicks **Re-evaluate** on any row → `RerunModal` opens (shows config hash comparison)
2. User confirms → `POST /api/rerun` starts ECS task → button shows "Running..."
3. Dashboard polls every 5s → when complete, an **amber result group** appears after the last original row in the group
4. Changed cells are highlighted in deeper amber; hovering shows original value as tooltip
5. **Accept** → patches `alerts_table.jsonl` (always preserving `run_timestamp`, `agent_call_id`, `alert_date_time`, and `run_id` from the original so HTML links and QA eval S3 path lookups remain valid) + refreshes table; **Discard** → deletes rerun result
6. Multiple reruns can be pending simultaneously — each gets its own result group

State: `completedReruns: Map<string, RerunResult>` (keyed by `run_id::target_id`).

**Group-based rerun display:** `RerunResultGroup` (replaces the old `RerunResultRow`) renders all rerun rows for a `(run_id, target_id)` group together. For single-row reruns (most common), it looks identical to the old behavior. For multi-row reruns:
- Shows all rerun rows with amber background after the last original row in the group
- First rerun row shows Accept/Discard buttons + row count summary (e.g. "2 → 3 rows")
- Each rerun row is diff-highlighted against the **best-matching** original row (greedy match minimizing field differences via `bestMatchOriginal()`) — handles reordering and count changes gracefully
- Accept/Discard applies to the entire group (matches backend behavior)
- Re-evaluate button only shown on the first row of a multi-row group

### Agent Call ID column

Both tables have a sticky "Call ID" column (after the Actions column) that shows the last 8 characters of `agent_call_id` — a UUID generated per `extract_page_change()` invocation. This is more granular than `run_id` (which is shared across all targets in a 6-hour pipeline run). All rows produced by the same agent call share the same `agent_call_id`. Old rows without `agent_call_id` display as "---".

### Data transparency principle

The dashboard shows **exactly what the agent output** — no coercion, no N/A substitution. A dash cell means the agent output null or the field was absent. This is intentional: dashes indicate agent deviation from instructions, not a dashboard bug.

### Schema compatibility (old vs new rows)

`alerts_table.jsonl` contains rows from two schema generations:
- **Old schema (pre-May 2026):** nested `events[]`, `library_items[]`, `agenda_items[]` arrays; `is_relevant_for_art_newsreel` boolean; `organization` string; different field names (e.g. `event_start_datetime`, `event_call_in_access_code`); no `agent_call_id`
- **New schema (May 2026+):** flat top-level fields; `organization` string array; complex object fields (`library_item_preliminary_title: {status, title}`, `is_alert_relevant_for_art_newsreel: {status, reference}`, etc.); uses OpenAI Structured Outputs so all 21 fields are always present; includes `agent_call_id` (UUID per agent invocation); supports multi-alert output (one page change can produce multiple rows via `alerts` array wrapper in schema)

`deriveColumns()` shows all schema columns plus any extra data fields from rows. `FIELD_ALIASES` + `resolveCell()` ensure old rows display under new column headers. Both generations display correctly side-by-side with blank cells where fields differ.

**Known extra column from old rows:** `event_timezone` (present in ~180 pre-May 2026 rows, no equivalent in new schema) — appears as an unstyled extra column after the schema columns.

## Environment

Requires `.env.local` (or ECS task env vars) with:

| Var | Description |
|-----|-------------|
| `BUBBLE_ARTIFACT_BUCKET` | S3 bucket containing `alerts/alerts_table.jsonl` (e.g. `web-change-tracker-prod-artifacts-815039343351`) |
| `AWS_REGION` | AWS region (default `us-east-1`) |
| `CHATKIT_CONFIG_TABLE` | DynamoDB table for agent configs (default `chatkit_production_config`) |
| `AUTH0_SECRET` | Random 32+ char string for session cookie encryption |
| `AUTH0_BASE_URL` / `APP_BASE_URL` | Public URL (e.g. `https://tracker.bridgewayanalytics.com`) |
| `AUTH0_DOMAIN` | Auth0 tenant domain (e.g. `dev-dwy0013t.us.auth0.com`) |
| `AUTH0_CLIENT_ID` | Auth0 Application Client ID |
| `AUTH0_CLIENT_SECRET` | Auth0 Application Client Secret |
| `CHATKIT_INTERNAL_API_KEY` | API key for newsreel ingest API. If not set, loaded from SSM param `/web-change-tracker/prod/chatkit_internal_api_key`. |
| `CHATKIT_INTERNAL_API_KEY_SSM_PARAM` | Override SSM param name for `CHATKIT_INTERNAL_API_KEY`. |
| `INGEST_API_URL` | Base URL for transcript chunk ingest (default: `https://api.bridgewayanalytics.com`). |
| `CHATKIT_API_URL` | Base URL for document ingest (default: `https://chat-api.bridgewayanalytics.com`). |

ECS cluster: `naic-dashboard-cluster` · Service: `naic-dashboard-service` · ALB: `naic-dashboard-alb`

## Infrastructure

Terraform in `infra/terraform/`.

| Resource | Details |
|----------|---------|
| **ECS Fargate** | ARM64, 0.5 vCPU, 1024 MB, port 3000 |
| **ALB** | HTTPS (443) with ACM cert + HTTP (80) → HTTPS redirect |
| **ACM** | SSL cert for `tracker.bridgewayanalytics.com` |
| **Security Groups** | ALB: ports 80 + 443 from anywhere; ECS: port 3000 from ALB only |
| **CloudWatch** | Log group `/ecs/naic-dashboard` |
| **ECR** | Docker image repository |
| **IAM** | Execution role (DynamoDB + S3 read, ECS Describe); Task role (DynamoDB GetItem, S3 Get/Put, ECS RunTask) |

Domain: `tracker.bridgewayanalytics.com` (DNS managed by Microsoft, CNAME to ALB).

Health check: `/api/pages` (200 matcher).

## Critical warnings

- **Never change `output_json_schema` in DynamoDB without verifying the dashboard handles the new structure.** If the schema has a top-level `alerts` array wrapper, `required` is `["alerts"]` — the dashboard must drill into `alerts.items` for column derivation. Both `/api/schema` and `/api/doc-schema` already handle this.
- **Never add `--no-cache` to Docker builds** — skips the npm install cache layer and makes builds ~10x slower.
- **Never add `--platform` to Docker builds** — the deploy script already sets `--platform linux/amd64`.
- **`library_item_preliminary_title` is a dict in new flat schema** (`{status, title}`), not a string. Cell rendering handles this via the `{status, ...}` object renderer.
- **Ingest gate — never call ingest APIs directly from the pipeline.** `ingest_for_newsreel()` and `ingest_transcript_chunks()` in `web-change-tracker` are only called via the dashboard approve routes. The pipeline sets `ingest_status: "pending"` and stops. Calling these functions directly would bypass the gate.
- **`patch-jsonl.ts` is not atomic** — download/patch/reupload can lose concurrent writes. Fine for single-user dashboard use; do not use in batch or parallel contexts.
- **`ingest_status` is a pipeline field, not an agent output field.** It will not appear in the dynamic schema columns because it is in `METADATA_COLS`. Do not remove it from `METADATA_COLS` — it would appear as a spurious data column.
