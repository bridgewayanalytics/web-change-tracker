# Retrieval Pipeline — Hybrid Search + LLM Reranker

> **Status (2026-11-09): SUPERSEDED.**
>
> This doc describes the pre-migration state of our retrieval pipeline — the direct-DB
> pgvector fork living in `bubble/pgvector/*` as of early November 2026. It was shared
> with the ChatKit backend team to solicit review.
>
> Based on their response, that whole fork was deleted and the four agents now call
> the hosted backend API directly:
>
> - `GET /knowledge/document-extraction/{doc_uuid}/text` — inline the full source document
> - `POST /knowledge/search` — hybrid search + Decisions-API reranker for reference collections
>
> This doc is retained as historical context and for anyone diffing the before/after.
> For the current architecture see:
> - `CLAUDE.md` → Architecture + Key entry points (document_agent, doc_eval_agent, page_change_agent, eval_agent)
> - `docs/knowledge-base-integration.md` — single source of truth for the HTTP contract

---

## What this doc is

Written for the engineer(s) who own the ChatKit backend pgvector store. It
explains exactly what the web-change-tracker agents do between calling
`search_knowledge_base(query)` and getting a list of chunks back. Everything
below is **client-side code we wrote in this repo** — none of it lives in
pgvector or the ChatKit backend.

We want your feedback on whether this design is sensible and whether we should
be leaning more on backend-provided retrieval primitives instead.

**Code locations in this repo:**
- `bubble/pgvector/search_tool.py` — hybrid retrieval (semantic + lexical + RRF)
- `bubble/pgvector/reranker.py` — LLM-based re-scoring
- `bubble/pgvector/client.py` — asyncpg connection pool

**Backend-provided tables we read (read-only, via direct SQL):**
- `documents` — doc-level metadata
- `document_chunks` — chunk text + `embedding` (halfvec) + `search_tsv` (tsvector)

**Agent-facing tool** (registered with the Agents SDK):
```python
@function_tool
async def search_knowledge_base(query: str, limit: int = 10) -> list[dict]
```

---

## Stage 1 — Query embedding

The agent calls the tool with a natural-language `query`. We embed the query
client-side with OpenAI's `text-embedding-3-large` (constant in
`search_tool.py`):

```python
_EMBEDDING_MODEL = "text-embedding-3-large"
```

The embedding is sent to Postgres as a `halfvec` literal for similarity
comparison. One embedding call per `search_knowledge_base` invocation.

---

## Stage 2 — Hybrid retrieval (semantic + lexical + RRF)

We run a single SQL query that executes three things in one round trip:

```sql
WITH semantic AS (
    SELECT dc.id, dc.content, ..., 
           ROW_NUMBER() OVER (ORDER BY dc.embedding <=> $1::halfvec) AS rank_sem
    FROM document_chunks dc JOIN documents d ON dc.document_id = d.id
    WHERE d.namespace = ANY($2::text[])
      AND (d.expires_at IS NULL OR d.expires_at > NOW())
    ORDER BY dc.embedding <=> $1::halfvec LIMIT 30      -- _SEM_LIMIT
),
lexical AS (
    SELECT dc.id, dc.content, ...,
           ROW_NUMBER() OVER (ORDER BY ts_rank_cd(dc.search_tsv, q) DESC) AS rank_lex
    FROM document_chunks dc JOIN documents d ON dc.document_id = d.id,
         plainto_tsquery('english', $3) q
    WHERE dc.search_tsv @@ q
      AND d.namespace = ANY($2::text[])
      AND (d.expires_at IS NULL OR d.expires_at > NOW())
    ORDER BY ts_rank_cd(dc.search_tsv, q) DESC LIMIT 30 -- _LEX_LIMIT
)
SELECT COALESCE(s.id, l.id) AS id, COALESCE(s.content, l.content) AS content, ...,
       COALESCE(1.0 / (60 + s.rank_sem), 0) +
       COALESCE(1.0 / (60 + l.rank_lex), 0) AS rrf_score
FROM semantic s FULL OUTER JOIN lexical l ON s.id = l.id
ORDER BY rrf_score DESC LIMIT 25;                       -- _HYBRID_LIMIT
```

Three limits controlling the funnel (module constants in `search_tool.py`):

| Constant | Value | Purpose |
|---|---|---|
| `_SEM_LIMIT` | 30 | Top-30 by cosine distance (semantic) |
| `_LEX_LIMIT` | 30 | Top-30 by `ts_rank_cd` (lexical) |
| `_HYBRID_LIMIT` | 25 | Top-25 after RRF fusion |

**RRF formula:** `score(chunk) = 1 / (60 + rank_sem) + 1 / (60 + rank_lex)` — the
60-constant k is textbook Reciprocal Rank Fusion. Chunks that appear in both
lists get both bonus terms; chunks in only one get one.

**Namespace filtering:** The query's `$2::text[]` is a Postgres text array of
namespaces the parent agent is allowed to search. For the document extraction
agent it's typically ~9 namespaces, e.g.:

```
['bubble-data', 'ba:chronicles', 'ba:newsreels', 'ba:NAIC Guidelines',
 'naic-proceedings', 'ba:International Guidelines', 'ba:Rating Agencies',
 'ba:organizations', 'document-extraction:<deterministic-uuid>']
```

The `document-extraction:*` namespace holds the specific source document this
agent is being asked to extract from (uploaded via
`POST /knowledge/document-extraction/<uuid>` immediately before the agent
runs). The rest are long-lived reference stores managed by the backend.

**Important:** all namespaces compete equally for the top-25 slots. No
per-namespace quota. See "Known problem" below.

---

## Stage 3 — LLM reranker (currently broken)

The 25 candidates from Stage 2 go to `rerank_chunks(query, candidates, top_n=10)`
in `reranker.py`. For each candidate it makes an OpenAI chat completion call:

```python
system_prompt = """You are a relevance scoring engine for an insurance regulation
knowledge base. Given a user query and a candidate text chunk, score the chunk's
relevance to the query on a scale of 0-10.

Scoring criteria:
- 10: Directly answers the query with specific data, figures, or regulatory text
- 7-9: Highly relevant, contains key information but may not directly answer
- 4-6: Topically related but does not specifically address the query
- 1-3: Tangentially related at best
- 0: Not relevant

Respond with ONLY a JSON object: {"score": <number>, "reason": "<one sentence>"}"""

user_msg = f"Query: {query}\n\nChunk (content_type={...}, title={...}, date={...}):\n{chunk['content'][:1500]}"

resp = await client.chat.completions.create(
    model="gpt-5-nano",
    messages=[{"role":"system","content":system_prompt},
              {"role":"user","content":user_msg}],
    max_completion_tokens=200,
)
score = json.loads(resp.choices[0].message.content)["score"]
chunk["rerank_score"] = float(score)
```

### Concurrency control

Calls are batched with `_RERANK_CONCURRENCY = 4` per `asyncio.gather` wave:

```python
for i in range(0, len(candidates), _RERANK_CONCURRENCY):
    batch = candidates[i : i + _RERANK_CONCURRENCY]
    batch_results = await asyncio.gather(*[_score_one(...) for c in batch])
```

The batching exists because unbounded `asyncio.gather` over many candidates
caused glibc heap corruption (double-free → SIGABRT) via shared httpx/OpenSSL
state. Documented in the file docstring.

### Final selection

```python
scored.sort(key=lambda x: x.get("rerank_score", 0.0), reverse=True)
return scored[:top_n]
```

Return top-10 by `rerank_score` back to the agent as the `search_knowledge_base`
tool result.

### The current failure mode

`RERANK_MODEL = "gpt-5-nano"` is hardcoded. gpt-5-nano is a reasoning model, so
the `max_completion_tokens=200` budget is consumed almost entirely by hidden
reasoning tokens — the visible `content` is empty, `finish_reason="length"`,
`_score_one` catches the parse failure, sets `rerank_score = 0.0`, logs a
warning.

In practice every rerank call for every chunk returns `0.0`. The "top-10 by
rerank_score" becomes "whichever 10 chunks `asyncio.gather` happened to resolve
first" — effectively arbitrary ordering within the Stage-2 top-25 pool.

(We know this is a bug. The file has swappable model resolution via contextvars
so parent agents can override — but no parent agent currently does, so
everyone hits the nano path.)

---

## Stage 4 — Optional: org-upload boost

When the request has an `org_namespace` set (per-user uploads), we run a
second query against that namespace alone (semantic only, LIMIT 50), rerank
all of those together with the global pool, then apply a `_ORG_SCORE_BOOST =
1.5` multiplier to org_upload chunks' rerank scores before sorting. Not
relevant to the document extraction use case we're debugging below, but
documenting here for completeness.

---

## What the agent actually sees

`search_knowledge_base` returns a `list[dict]` where each dict has:

```python
{
    "id": int,                 # chunk id
    "content": str,            # chunk text (NOT truncated)
    "content_type": str,       # e.g. "document_extraction", "newsreel"
    "external_id": str,        # document's external_id
    "title": str,              # from documents.metadata->>'title'
    "date": str | None,        # from documents.metadata->>'date'
    "source_uri": str,         # from documents.source_uri
    "chunk_meta": jsonb,       # from document_chunks.metadata
    "chunk_index": int,        # chunk ordering within the document
    "rrf_score": float,        # from Stage 2
    "rerank_score": float,     # from Stage 3 (currently always 0.0)
}
```

The agent decides what to do with these 10 chunks — run more queries,
summarize, extract specific values, etc. We give it `list_available_documents`
as a second tool it can use to enumerate documents in a namespace, but our
system prompts don't instruct it to call that tool proactively.

---

## Concrete failure case (why we're writing this doc)

### Setup
- PDF with 14 numbered agenda items
- Playwright-extracted text ingested to `document-extraction:<uuid>` via
  `POST /knowledge/document-extraction/<uuid>` (`text/plain`, ~3365 chars)
- Backend chunked it into **2 chunks**:
  - Chunk `idx=0` (1957 chars): heading + roll call + agenda items 1-6
  - Chunk `idx=1` (1618 chars): agenda items 5-15 (overlapping)
- Agent config `pgvector_namespaces` = 9 namespaces including
  `document-extraction:<uuid>`

### What happened
1. Agent calls `search_knowledge_base("agenda items ...")`
2. Stage 2 hybrid retrieval returned 25 candidates across all 9 namespaces.
   Chunk 0 of our document ranked into the top 25 (it has "AGENDA" as a
   literal header string, strong lexical match). Chunk 1 ranked lower — probably
   outside the top 25, or inside but late.
3. Stage 3 reranker was called with 25 candidates. All got `rerank_score =
   0.0` (nano failure mode).
4. `scored.sort(... reverse=True)` is a stable sort when all scores tie, so
   the top-10 is the first 10 chunks in whatever order `asyncio.gather`
   resolved them.
5. Chunk 1 didn't win that lottery. The agent only saw chunk 0.
6. Chunk 0 ends at item 6. Agent correctly reported in its extraction:
   *"The retrieved agenda text ends during item 6, so the remainder of that
   item and any subsequent items could not be verified."*
7. Final output: 6 extracted agenda items. Should have been 14.

### Our interpretation

The agent made the right call on the data it had. The retrieval pipeline
silently dropped chunk 1 before the agent could see it. Two compounding
causes:

1. **Reranker broken** → any chunk's position in the top-10 is effectively
   random within the Stage-2 pool
2. **No per-namespace quota** → the source document (2 chunks) competes
   equally with reference stores (thousands of chunks). A low-hit-rate query
   can push the source document out of the top-25 entirely, before the reranker
   even runs.

---

## Questions for your review

1. **Does the backend expose a retrieval primitive we should be using instead?**
   We rolled our own hybrid + rerank client-side. If the backend has a
   `GET /search?query=...&namespaces=...` endpoint with better ranking, we
   should probably use that and delete most of `search_tool.py` +
   `reranker.py`.

2. **Should `document-extraction:<uuid>` namespaces be special-cased?**
   Our proposed fix: when a `document-extraction:*` namespace is in the agent's
   namespace list, always return ALL its chunks (up to some cap like 20)
   unconditionally, separate from the general top-K pool. Rationale: that
   namespace is the authoritative source document; everything else is
   reference. Does the backend have a notion of "primary source namespace"
   we could lean on?

3. **Backend reranker?** Is there a plan to move reranking server-side (e.g.
   via Cohere Rerank, a cross-encoder, or the backend's own LLM)? If yes we'd
   stop rolling our own; if no we'll replace `gpt-5-nano` with a working model.

4. **Chunk size strategy.** For a 3365-char PDF the backend produced 2 chunks
   (one of 1957 chars, one of 1618 chars) with ~360 chars of overlap. Is that
   the intended chunking? For agenda-item extraction the item-boundary
   semantics matter — splitting agenda items across chunks causes the
   failure above. We'd love a doc on current chunking behavior and whether we
   can pass hints at ingest time (e.g. "chunk on `\n\d+\.` boundaries").

5. **Telemetry on chunk-level retrieval.** We log OpenAI token usage per
   query but have no backend-side visibility into which chunks were returned
   from which namespaces, what their rank positions were, or how many
   chunks exist per document. Any dashboard or structured logs we could
   subscribe to for debugging?

6. **Expires_at convention.** Our ingests set no `expires_at`. The query
   predicate `(d.expires_at IS NULL OR d.expires_at > NOW())` handles both
   cases — but is there a recommended TTL for per-document extraction
   namespaces (they're re-ingested on every pipeline run)?

---

## Appendix — exact module-level constants

From `bubble/pgvector/search_tool.py`:

```python
_EMBEDDING_MODEL = "text-embedding-3-large"
_SEM_LIMIT = 30         # top-N from semantic CTE
_LEX_LIMIT = 30         # top-N from lexical CTE
_HYBRID_LIMIT = 25      # top-N after RRF fusion
_ORG_SCORE_BOOST = 1.5  # multiplier on org_upload rerank scores
```

From `bubble/pgvector/reranker.py`:

```python
RERANK_MODEL = "gpt-5-nano"      # hardcoded, overridable via contextvar
_RERANK_CONCURRENCY = 4          # concurrent httpx calls per batch
# max_completion_tokens=200 per call
# System prompt scores 0-10 and asks for JSON output
```

Agent-facing tool default: `search_knowledge_base(query, limit=10)` → returns
top-10 chunks after reranking.
