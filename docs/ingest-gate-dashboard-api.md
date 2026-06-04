# Ingest Gate — Dashboard API Contract

**For:** NAICDashboard- developer  
**Last updated:** 2026-06-04

---

## Overview

Transcripts and relevant documents are no longer auto-ingested into the newsreel knowledge base. Instead, each qualifying row gets `ingest_status: "pending"` and waits for a user action in the dashboard. The dashboard must implement:

1. New columns on both tables (recording link, transcript link, ingest gate)
2. Approve / edit fields / reject actions per row
3. Manual transcript upload on the alerts table
4. Manual document URL paste on the document extractions table

---

## New fields on alert rows (`alerts_table.jsonl`)

| Field | Type | Description |
|-------|------|-------------|
| `recording_s3_key` | string \| null | S3 key of the mp3 in `recordings-bucket-1`. Generate presigned URL at render time. |
| `transcript_s3_key` | string \| null | S3 key of the plain-text transcript in the artifacts bucket. Generate presigned URL at render time. |
| `transcript_chunks_s3_key` | string \| null | S3 key of the JSONL chunk file. Present when chunking is done. |
| `manual_transcript_s3_key` | string \| null | Set when a transcript was manually uploaded (vs. auto-transcribed). Same value as `transcript_s3_key` for those rows. |
| `ingest_status` | `null` \| `"pending"` \| `"approved"` \| `"rejected"` | Gate state. `null` = not eligible. Show the gate UI only when non-null. |

## New fields on doc extraction rows (`document_extractions_table.jsonl`)

| Field | Type | Description |
|-------|------|-------------|
| `ingest_status` | `null` \| `"pending"` \| `"approved"` \| `"rejected"` | Same lifecycle. Set to `"pending"` when `newsreel_relevance.status == "Yes"`. |

---

## New UI columns

### Alerts table

| Column | Source | Display |
|--------|--------|---------|
| Recording | `recording_s3_key` | Link icon → call `GET /api/presigned-url?key=<recording_s3_key>` → open in new tab |
| Transcript | `transcript_s3_key` (or `manual_transcript_s3_key`) | Link icon → presigned URL → open in new tab. If null, show upload button (see Manual Upload). |
| Knowledge Base | `ingest_status` | Hide if null. Show badge + action button if pending/approved/rejected (see Gate UI). |

### Document extractions table

Same "Knowledge Base" column (no recording/transcript columns needed).

---

## Gate UI (per row)

Show only when `ingest_status` is non-null.

| Status | Badge | Actions available |
|--------|-------|-------------------|
| `"pending"` | Yellow "Pending" | **Approve**, **Edit fields**, **Reject** |
| `"approved"` | Green "Approved" | (none — already ingested) |
| `"rejected"` | Grey "Rejected" | **Approve** (allow re-approval after review) |

**Edit fields:** Open a modal pre-filled with the row's fields. User edits, then clicks Approve — the edited fields are patched to the JSONL and the row is ingested.

---

## API Routes to implement

### `GET /api/presigned-url`

Query param: `key` (S3 key)  
Returns: `{ url: string }` — presigned GET URL (1-hour expiry)

```ts
// Uses aws-sdk s3.getSignedUrlPromise("getObject", { Bucket, Key, Expires: 3600 })
// Bucket = process.env.CHANGELOG_BUCKET
```

---

### `POST /api/ingest/approve-transcript`

Body: `{ agent_call_id: string, field_overrides?: Record<string, unknown> }`

1. If `field_overrides` provided, call `PATCH /api/ingest/patch-row` first (alerts table, match on `agent_call_id`)
2. Read the row from `alerts_table.jsonl` to get `transcript_chunks_s3_key`
3. Call `POST https://api.bridgewayanalytics.com/internal/documents/ingest-transcript-chunks` with `{ s3_bucket, s3_key: transcript_chunks_s3_key, namespace: "newsreel-generation:ART" }` and `x-api-key: <CHATKIT_INTERNAL_API_KEY>`
4. Patch `alerts_table.jsonl`: set `ingest_status: "approved"` on all rows with matching `agent_call_id`

Returns: `{ ok: true, status: string, chunk_count?: number }`

---

### `POST /api/ingest/approve-document`

Body: `{ agent_call_id: string, library_item_url: string, field_overrides?: Record<string, unknown> }`

1. If `field_overrides` provided, patch first
2. Read the row from `document_extractions_table.jsonl` to get `library_item_url` and `library_item_title`
3. Call `POST https://chat-api.bridgewayanalytics.com/internal/documents/ingest` (multipart) with `{ namespace, filename, url }`
4. Patch `document_extractions_table.jsonl`: set `ingest_status: "approved"` on matching `(agent_call_id, library_item_url)`

Returns: `{ ok: true, status: string, document_id?: string }`

---

### `POST /api/ingest/reject`

Body: `{ table: "alerts" | "docs", agent_call_id: string, library_item_url?: string }`

Patch the JSONL: set `ingest_status: "rejected"` on matching rows.

Returns: `{ ok: true, patched: number }`

---

### `POST /api/ingest/manual-document-url`

Body: `{ url: string, filename: string }`

Direct one-off ingest of a URL into the knowledge base. Does not create a row.

Call `POST https://chat-api.bridgewayanalytics.com/internal/documents/ingest` with `{ namespace: "newsreel-generation:ART", filename, url }`.

Returns: `{ ok: true, status: string, document_id?: string }`

---

### `POST /api/ingest/upload-transcript-url`

Body: `{ agent_call_id: string, filename: string }`

Generates a presigned S3 PUT URL so the browser can upload directly to S3.

```ts
const key = `transcripts/manual/${agent_call_id}/${filename}`
const uploadUrl = await s3.getSignedUrlPromise("putObject", {
  Bucket: process.env.CHANGELOG_BUCKET,
  Key: key,
  ContentType: "text/plain",
  Expires: 900,
})
return { upload_url: uploadUrl, s3_key: key }
```

Returns: `{ upload_url: string, s3_key: string }`

---

### `POST /api/ingest/trigger-manual-chunk`

Body: `{ agent_call_id: string, transcript_s3_key: string }`

Trigger an ECS RunTask in manual_chunk mode. Same pattern as re-eval.

Environment override:
```json
{
  "MANUAL_CHUNK_AGENT_CALL_ID": "<agent_call_id>",
  "MANUAL_CHUNK_TRANSCRIPT_S3_KEY": "<transcript_s3_key>"
}
```

The task reads the alert row by `agent_call_id`, runs `chunk_transcript()`, and patches the row with `transcript_chunks_s3_key` and `ingest_status: "pending"`.

Returns: `{ task_arn: string }`

---

## Manual transcript upload flow (alerts table)

When `transcript_s3_key` is null on a row (meeting recorded manually):

1. User clicks **Upload Transcript** button on the row
2. `POST /api/ingest/upload-transcript-url` → returns `{ upload_url, s3_key }`
3. Browser PUT to `upload_url` with the .txt file contents
4. `POST /api/ingest/trigger-manual-chunk` with `{ agent_call_id, s3_key }`
5. Show spinner while ECS task runs (poll ECS task status every 5s, same as re-eval pattern)
6. When task completes, row now has `ingest_status: "pending"` — show the gate UI

---

## Auth

`CHATKIT_INTERNAL_API_KEY` is in AWS SSM at `/web-change-tracker/prod/chatkit_internal_api_key`. The Next.js API routes should fetch it from SSM (or cache it as a process-level env var loaded at startup).

`CHANGELOG_BUCKET` — set as env var on the Next.js server (`process.env.CHANGELOG_BUCKET`).

---

## JSONL patch helper (TypeScript)

Same pattern as Accept/Discard in the rerun feature:

```ts
async function patchJsonlRows(
  s3: AWS.S3,
  bucket: string,
  key: string,
  matchFields: Record<string, unknown>,
  updateFields: Record<string, unknown>,
): Promise<number> {
  const body = await s3.getObject({ Bucket: bucket, Key: key }).promise()
  const lines = body.Body!.toString("utf-8").split("\n").filter(Boolean)
  let patched = 0
  const updated = lines.map(line => {
    const row = JSON.parse(line)
    if (Object.entries(matchFields).every(([k, v]) => row[k] === v)) {
      Object.assign(row, updateFields)
      patched++
    }
    return JSON.stringify(row)
  })
  await s3.putObject({
    Bucket: bucket,
    Key: key,
    Body: updated.join("\n"),
    ContentType: "application/x-ndjson",
  }).promise()
  return patched
}
```
