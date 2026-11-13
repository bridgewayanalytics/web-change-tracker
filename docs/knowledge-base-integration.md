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

**Inline the source document. Search everything else.**

- The *authoritative source document* the agent is extracting from (PDF or
  transcript) goes **into the prompt** as one complete stitched block. The
  agent never has to search for its own source document.
- *Before/after HTML snapshots* are ingested per-pipeline-run into
  `page-snapshot:<uuid>` namespaces (deterministic uuid per
  `(agent_call_id, side)`) and the agent reaches them via
  `search_knowledge_base`. This lets the agent surface the `Updated:
  MM/DD/YYYY` label in a download link's `title=` attribute without having
  to prompt-slice ~20 KB of raw HTML — the regression we corrected on
  2026-11-10 after accidentally re-introducing HTML inlining during the
  pgvector-to-backend migration.
- The *reference collections* (`ba:chronicles`, `ba:newsreels`, `ba:NAIC Guidelines`,
  `naic-proceedings`, `ba:organizations`, `ba:International Guidelines`,
  `ba:Rating Agencies`, `bubble-data`) remain in a search index. The agent gets
  one tool — `search_knowledge_base` — that POSTs to `/knowledge/search` for
  those namespaces + the per-call page-snapshot namespaces.

Degraded path: when `CHATKIT_INTERNAL_API_KEY` is missing (backend unreachable),
`document_agent` falls through to a direct Responses API call with no search
tools. In that path only, the full before/after HTML is inlined under
`=== PAGE CHANGE CONTEXT ===` as the only way the agent can see link
metadata — accepting the prompt-size cost because search isn't available.

## Per-agent behavior

### `document_agent.py` (extraction)

1. `pdf_text = _fetch_pdf_text(document_url)` — scrape/render.
2. `doc_uuid = doc_uuid_for(document_url)` — deterministic sha256 prefix.
3. `ingest_document(doc_uuid, filename, pdf_text, "text/plain", ttl_hours=24*30)`.
4. If `status != "cached"`: `wait_until_ready(doc_uuid)`.
5. `src = document_text(doc_uuid)` → `src["text"]` inlined under
   `=== SOURCE DOCUMENT — {title} ===` in the user prompt.
6. `page_ns = ingest_page_snapshots(agent_call_id, before_html, after_html)`
   returns up to two `page-snapshot:<uuid>` namespaces (deterministic uuid per
   `(acid, side)`, 30-day TTL). **HTML is NOT inlined** — it goes into the
   knowledge base and the agent reaches it via search.
7. `reference_namespaces = (config_namespaces − {document-extraction:*, page-snapshot:*}) + page_ns`.
8. Agents SDK runs with one tool (`search_knowledge_base`) that calls
   `search(query, reference_namespaces, trace_id=agent_call_id, document_id=source_document_id)`.
9. Step 2: `chat_json()` reformats the free-text to match the DynamoDB
   `output_json_schema`.

### `eval/doc_eval_agent.py` (QA)

Same shape as extraction: `_prepare_eval_args` ingests the source (cached
no-op if the extraction agent already did it in this pipeline run), inlines
it in `user_message`, ingests the page snapshots (also a cached no-op —
reuses the extraction's `agent_call_id`), appends the returned namespaces to
the search scope, runs the Agents SDK with the HTTP search tool. `trace_id`
= `agent_call_id` of the extraction being graded.

### `bubble/page_change_agent.py` (WTA)

The WTA's whole purpose is to diff the before/after HTML, so the HTML is
inlined directly in `user_content` (no truncation, no vectorization). Only
swap applies from the migration: `search_knowledge_base` is now the
in-closure HTTP-backed function, scoped to reference namespaces only.

### `eval/eval_agent.py` (alerts QA)

Same as WTA. `trace_id` = first eval_row_key.

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
| Agent produced fewer rows than the document has | Check `document_agent: inlined source document doc_id=… chars=…` log — if the chars count is way lower than the source, the ingest/text round-trip is wrong. If it's full-size, check Step 1 output chunks — the agent should see all items. |
| `search_knowledge_base backend error: …` in logs | Backend API unreachable or returning non-2xx. Agent will fall through to direct-API path. Verify `CHATKIT_INTERNAL_API_KEY` is set and the backend is up. |
| Searches missing from the admin dashboard retrieval-traces view | Agent isn't passing a `trace_id`. Check the `trace_id` passed to `_run_with_pgvector` (document_agent / page_change_agent) or `_run_agent_async` (doc_eval_agent / eval_agent). |
| Cached doc-uuid returning stale text | `ttl_hours` expired; the content was purged. Next extraction will re-ingest. To force-overwrite before TTL, call `ingest_document(..., force=True)`. |

## Reference

- **Backend API contract:** admin-dashboard repo → `docs/integration/document-extraction-api.md`
- **Pre-migration retrieval pipeline (historical):** `docs/retrieval-pipeline.md`
- **Backend reply to our review request:** `retrieval-pipeline-reply.md` (from Biraj, dated 2026-11-09)
