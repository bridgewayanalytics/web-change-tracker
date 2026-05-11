# Re-evaluate Alert Feature

## Overview
Allow a user to re-run the page_change_agent on a stored alert after updating DynamoDB instructions, and see a before/after diff of what changed.

## Session Ownership
| Session | Repo | Responsibilities |
|---------|------|-----------------|
| web-change-tracker | `web-change-tracker` | Pipeline changes: config_hash, rerun mode in spike.py, ECS task params |
| naic-dashboard | `NAICDashboard-` | UI: button, confirmation modal, ECS trigger API, drawer diff view, Accept/Discard |

---

## Backend (web-change-tracker session)

### 1. `config_hash` in alert rows
- When `alert_s3.py` builds rows, compute `MD5(system_prompt + model)` from the agent config
- Store as `config_hash` field on every alert row
- Old rows without `config_hash` skip the check silently

### 2. Rerun mode in `spike.py`
- Detect env vars `RERUN_RUN_ID` and `RERUN_TARGET_ID`
- If set: fetch stored before/after HTML from S3 (`pages/<target_id>/YYYY/MM/DD/<run_id>/`)
- Re-run `extract_page_change()` + `extract_document_data()` with current DynamoDB config
- Write result to S3 at a separate key: `alerts/reruns/<run_id>/<target_id>/result.json`
- Do NOT overwrite `alerts_table.jsonl` — dashboard handles accept/discard

### 3. ECS task params
- Rerun tasks use the same ECS task definition
- Pass `RERUN_RUN_ID` and `RERUN_TARGET_ID` as environment variable overrides on `RunTask`
- Task exits after single rerun (no full pipeline run)

---

## Frontend (naic-dashboard session)

### API Routes
- `GET /api/config` — fetch current DynamoDB agent config (system prompt, model, last_modified hash)
- `POST /api/rerun` — body: `{ run_id, target_id, stored_config_hash }` → triggers ECS RunTask, returns task ARN
- `GET /api/rerun/[taskArn]` — poll ECS task status + fetch result from S3 when complete
- `POST /api/rerun/accept` — body: `{ run_id, target_id }` → reads rerun result from S3, patches alerts_table.jsonl
- `POST /api/rerun/discard` — body: `{ run_id, target_id }` → deletes rerun result from S3

### UI Components
- **Re-evaluate button** on each alert row in `AlertsTable.tsx`
- **Confirmation modal**: fetches `/api/config`, compares hash against row's `config_hash`
  - If hashes match: warn "Config unchanged since this alert was generated"
  - If differs (or no stored hash): show confirm button → triggers rerun
- **Spinner** on the row while ECS task runs (poll `/api/rerun/[taskArn]` every 5s)
- **Right-side drawer** when task completes:
  - Shows only changed fields by default (toggle to show all)
  - Two columns: Before | After, changed fields highlighted
  - Accept / Discard buttons at bottom

### S3 rerun result schema
```json
{
  "run_id": "...",
  "target_id": "...",
  "rerun_timestamp": "...",
  "config_hash": "...",
  "original": { ...original alert row fields... },
  "rerun": { ...new agent output fields... }
}
```

---

## Status
- [ ] `config_hash` added to `alert_s3.py`
- [ ] `page_change_agent.py` exposes config hash
- [ ] Rerun mode in `spike.py`
- [ ] S3 rerun result write
- [ ] `/api/config` route
- [ ] `/api/rerun` route (ECS trigger)
- [ ] `/api/rerun/[taskArn]` poll route
- [ ] Accept/Discard routes
- [ ] Re-evaluate button + modal
- [ ] Drawer diff view
