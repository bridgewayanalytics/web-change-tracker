# Knowledge-base integration

How this repo talks to the ChatKit backend's hosted knowledge-base after the
2026-11-09 migration off the direct-DB pgvector fork.

## Architecture

All four agents (`document_agent`, `doc_eval_agent`, `page_change_agent`,
`eval_agent`) interact with the knowledge store via two HTTP endpoints. No
direct database access.

```
                ┌─────────────────────────────────────────────────┐
                │  ChatKit backend (chat-api.bridgewayanalytics…) │
                │                                                 │
                │  POST /knowledge/document-extraction/{uuid}     │  ← ingest text or PDF
                │   • ttl_hours (1–8784, optional)                │
                │                                                 │
                │  GET  /knowledge/document-extraction/{uuid}/text│  ← stitched source
                │   • overlaps_removed for text/plain             │
                │   • `--- Page N ---` markers for PDFs           │
                │                                                 │
                │  POST /knowledge/search                         │  ← hybrid + rerank
                │   • namespaces[]                                │
                │   • document_extraction_id / document_id        │
                │   • trace_id → retrieval_traces.thread_id       │
                │                                                 │
                │  GET  /knowledge/document-extraction/{uuid}/status
                │                                                 │
                └─────────────────────────────────────────────────┘
                                   ↑
                                   │ x-api-key = CHATKIT_INTERNAL_API_KEY
                                   │
┌──────────────────────────────────┴─────────────────────────────────┐
│  bubble/doc_extraction_ingest.py — thin requests-based client       │
│  (doc_uuid_for, ingest_document, wait_until_ready, get_status,      │
│   document_text, search, ingest_and_arm, arm_if_ready)              │
└─────────────────────────────────────────────────────────────────────┘
                                   ↑
                                   │
┌──────────────────────────────────┴─────────────────────────────────┐
│   bubble/document_agent.py                                          │
│   bubble/page_change_agent.py                                       │
│   eval/doc_eval_agent.py                                            │
│   eval/eval_agent.py                                                │
└─────────────────────────────────────────────────────────────────────┘
```

## Core principle

**Inline the source. Search the references.** (Backend team's retrieval
pipeline reply, 2026-11-09.)

- The *authoritative source document* (PDF, DOCX, or transcript) is ingested
  per-call under a deterministic `document-extraction:<uuid>` namespace so the
  QA agent and reruns re-derive the same `doc_uuid` and hit a cached document.
  The agent then fetches the stitched text via `GET /knowledge/document-extraction/
  {uuid}/text` and **inlines it directly in the user message** under
  `=== SOURCE DOCUMENT ===`. The source-document namespace is **not** added
  to the agent's search scope — the model would just waste turns re-retrieving
  content it already has in front of it.
- *Before/after HTML snapshots* are **inlined directly** in the user message
  under `=== BEFORE === / === AFTER ===` (WTA) or `=== PAGE CHANGE CONTEXT ===`
  (extraction / QA). The `Updated: MM/DD/YYYY` label in a download link's
  `title=` attribute sits right in that inlined HTML. HTML is NOT pushed into
  a vector store and NOT searched via `search_knowledge_base`.
- The *reference collections* (`ba:chronicles`, `ba:newsreels`, `ba:NAIC Guidelines`,
  `naic-proceedings`, `ba:organizations`, `ba:International Guidelines`,
  `ba:Rating Agencies`, `bubble-data`) are static search indexes.
- The agent gets one tool — `search_knowledge_base` — that POSTs to
  `/knowledge/search` for the **reference collections only**. No
  `document-extraction:*` and no `page-snapshot:*` ever ride on the namespace
  list; those contents are already in the prompt.

Degraded path: when `CHATKIT_INTERNAL_API_KEY` is missing (backend unreachable),
the agents fall through to a direct Responses API call with no search tool.
The user message already inlines source + HTML, so output still gets produced;
the agent just loses access to reference collections for cross-referencing.

## Per-agent behavior

### `document_agent.py` (extraction)

1. `pdf_text = _fetch_pdf_text(document_url)` — scrape/render.
2. `doc_uuid = doc_uuid_for(document_url)` — deterministic sha256 prefix.
3. `ingest_document(doc_uuid, filename, pdf_text, "text/plain", ttl_hours=24*30)`.
4. If `status != "cached"`: `wait_until_ready(doc_uuid)`.
5. `src = document_text(doc_uuid)` — fetches the stitched source text
   (sliding-window overlaps removed). **Inline `src["text"]` in user_content**
   under `=== SOURCE DOCUMENT ===`.
6. Inline `before_html` / `after_html` in user_content under
   `=== PAGE CHANGE CONTEXT ===` so link metadata (`Updated:` labels) sits
   in the prompt.
7. `reference_namespaces = config_namespaces − {document-extraction:*, page-snapshot:*}`.
   Nothing added back — source + HTML are in the prompt.
8. Agents SDK runs with one tool (`search_knowledge_base`) that calls
   `search(query, reference_namespaces, trace_id=agent_call_id)`. No
   `document_id` passed (would scope every search to the source PDF and
   silently block reference namespaces).
9. Step 2: `chat_json()` reformats the free-text to match the DynamoDB
   `output_json_schema`.

### `eval/doc_eval_agent.py` (QA)

Same shape as extraction: fetches the document via `_fetch_document_text`
(or transcript S3 for transcript rows), fetches the before/after HTML from
S3, and `_build_user_message` inlines both into the user message. Reference
namespaces only in the search scope — no document-extraction, no
page-snapshot. `trace_id` = `agent_call_id` of the extraction being graded.

### `bubble/page_change_agent.py` (WTA)

The WTA inlines `before_html` / `after_html` **directly** in the user
message under `=== BEFORE === / === AFTER ===`. The HTML is the WTA's
source material — the model needs to see the exact diff between versions
to reason about what changed, and chunked retrieval of its own source is a
known anti-pattern (backend-team reply §2, Nov 9). `search_knowledge_base`
reaches reference namespaces only (chronicles, newsreels, orgs). `trace_id`
= `agent_call_id`.

### `eval/eval_agent.py` (alerts QA)

Same shape as the WTA: before/after HTML inlined directly in the prompt
so the grader sees the exact diff. `search_knowledge_base` reaches
reference namespaces only. `trace_id` = first eval_row_key (sanitized —
see "Backend validation rules" below).

## Deterministic doc_uuid

```python
def doc_uuid_for(library_item_url: str) -> str:
    return hashlib.sha256(library_item_url.strip().encode()).hexdigest()[:32]
```

- The extraction agent and the QA agent independently compute the same
  `doc_uuid` for the same URL, so when QA runs it finds the document already
  indexed.
- Reruns of the same alert hit the same `doc_uuid`; `ingest_document` is a
  content-hash no-op (status=`"cached"`) and `document_text` returns the
  previously-stitched content.

## TTL policy

- **Extraction ingests** use `ttl_hours=24*30` (30 days). Re-uploading the same
  `doc_uuid` under the same content_hash is a no-op that doesn't extend the
  TTL. QA re-derives the same `doc_uuid` and finds the document for the TTL's
  lifetime.
- **Reference namespaces** (chronicles, newsreels, etc.) are managed by the
  backend team and have no TTL set from our side.

## Telemetry

Every call to `/knowledge/search` writes a `retrieval_traces` row keyed on
the `thread_id` = the `trace_id` we pass. Our convention:

| Agent | `trace_id` |
|---|---|
| `document_agent` | parent alert's `agent_call_id`, falling back to a per-extraction `doc_call_id` |
| `doc_eval_agent` | extraction's `agent_call_id` |
| `page_change_agent` | the WTA `agent_call_id` generated at the top of the call |
| `eval_agent` | first `eval_row_key` in the group (sanitized — see "Backend validation rules" below) |

So one pipeline run's searches across all agents group together in the admin
dashboard's conversation drawer.

## Backend validation rules (enforced on `/knowledge/search`)

The backend rejects requests with `422 Unprocessable Entity` when any of
these constraints are violated. All four are handled centrally in
`bubble/doc_extraction_ingest.search()` so individual agents never need to
worry about them, but you should know about them before you change the
wrapper:

| Field | Rule | How the wrapper handles it |
|---|---|---|
| `query` | 1–2000 chars | Strips whitespace, clamps to 1800 (headroom); empty queries fall back to the literal `"query"` |
| `trace_id` | 1–128 chars, `[A-Za-z0-9._-]` only | Replaces any other character with `_`, then clamps to 128 |
| `namespaces` | must be non-empty OR include a `document_extraction_id` | Caller's responsibility; the wrapper does not synthesize namespaces |
| `limit` | 1–25 | Clamped to `max(1, min(limit, 25))` |

The wrapper also surfaces the full response body in any raised `HTTPError`,
so a failure mode like "trace_id must be 1-128 chars: letters, digits, '.', '_',
'-'" lands directly in CloudWatch (`WARNING eval.eval_agent search_knowledge_base
backend error: 422 ...`) rather than a bare `422 Client Error` with no detail.

**Historical note (2026-11-13):** the WTA QA agent (`eval/eval_agent.py`) was
the only caller passing a composite `eval_row_key` as the trace_id — all three
other agents pass a plain `agent_call_id` UUID which trivially satisfies both
the length and charset rules. The `eval_row_key` contains `|`, `:`, and `/`
and is often 150-250 chars, so every QA search silently 422'd. For weeks the
visible symptom was "WTA QA scores unusually many fields Partially Correct"
because the agent couldn't retrieve reference material to validate against.
See commits `966fa14`, `2e5b7d7`, `e089f7b`.

## Fallback when the backend is unreachable

All four agents wrap `Runner.run(agent, ...)` in a try/except. On any exception
from the backend path, they fall through to a direct Responses API call via
`chat_json()` with the same schema enforcement. The agent loses knowledge-base
context but still produces a schema-compliant output (no empty rows). A
`WARNING` log identifies which agent fell back and why.

## Shared event loop (`bubble.async_runner.run_sync`)

All four agents dispatch the Agents SDK runner through
`bubble.async_runner.run_sync(coro)` instead of the stdlib `asyncio.run(coro)`.

**Why:** `asyncio.run()` creates a fresh event loop per call and closes it on
exit. The OpenAI Agents SDK (via `httpx`) caches an `AsyncClient` tied to the
first event loop it sees. The second `asyncio.run()` in the same process gets
a new loop, but the cached client still references the closed one, so every
tool call raises `RuntimeError: Event loop is closed`. Our broad
`except Exception` catches that error and silently falls through to the
direct-API path (no `search_knowledge_base` tool).

Scheduled 6-hour pipeline runs do many sequential document extractions in one
Python process, so this bug silently degraded every extraction after the first.
Subprocess-per-group tasks (dashboard reruns, `eval.run_bulk_reeval`) weren't
affected because each one starts a fresh process.

**Diagnostic signal:** filter a pipeline log for `"Event loop is closed"` —
should be zero hits per run. Any hit means `asyncio.run()` crept back in
somewhere. Fixed in commit `cde3e0b`.

## What was removed on 2026-11-09

| Path | Why gone |
|---|---|
| `bubble/pgvector/search_tool.py` | hybrid SQL + reranker-call wiring — backend owns it now |
| `bubble/pgvector/reranker.py` | the gpt-5-nano reranker whose `max_completion_tokens=200` budget was eaten by reasoning tokens → silent score=0.0 for every candidate |
| `bubble/pgvector/client.py` + `tool_cache.py` | asyncpg pool + in-process cache — no direct DB access anymore |
| `scripts/check_doc_chunks.py`, `check_bubble_data_ns.py`, `check_ghost_files.py` | asyncpg-based probes replaced by the backend's chunk inspector on the admin dashboard |
| `asyncpg>=0.29.0` (requirements.txt) | no direct DB client needed |
| `PGVECTOR_ENABLED`, `DATABASE_IP`, `DATABASE_NAME`, `DATABASE_USERNAME`, `DATABASE_PASSWORD`, `DATABASE_PORT` (task-def env) | credentials no longer needed; `CHATKIT_INTERNAL_API_KEY` (already provisioned) is the only auth token |

## Debugging

| Symptom | Where to look |
|---|---|
| Agent produced fewer rows than the document has | Check `document_agent: ingest status=… doc_uuid=…` log — status should be `queued` → `ready` (or `cached` on reruns). Verify the source namespace was added to the search scope via the `source_ns=True` field in the `document_agent: running (model=…, ref_namespaces=…, source_ns=…, snapshots=…)` line. If Step 1 output references only a subset of the document, the agent may not be searching enough — check turn counts and prompt guidance. |
| `search_knowledge_base backend error: …` in logs | Backend API unreachable or returning non-2xx. Agent will fall through to direct-API path. Verify `CHATKIT_INTERNAL_API_KEY` is set and the backend is up. |
| Searches missing from the admin dashboard retrieval-traces view | Agent isn't passing a `trace_id`. Check the `trace_id` passed to `_run_with_pgvector` (document_agent / page_change_agent) or `_run_agent_async` (doc_eval_agent / eval_agent). |
| Cached doc-uuid returning stale text | `ttl_hours` expired; the content was purged. Next extraction will re-ingest. To force-overwrite before TTL, call `ingest_document(..., force=True)`. |

## Reference

- **Backend API contract:** admin-dashboard repo → `docs/integration/document-extraction-api.md`
- **Pre-migration retrieval pipeline (historical):** `docs/retrieval-pipeline.md`
- **Backend reply to our review request:** `retrieval-pipeline-reply.md` (from Biraj, dated 2026-11-09)
