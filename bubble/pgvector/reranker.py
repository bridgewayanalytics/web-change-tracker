"""LLM-based reranker for pgvector search candidates.

Uses parallel gpt-5-nano calls for relevance scoring.
The function signature is the stable interface; swap the implementation to
Cohere Rerank or a cross-encoder without changing any other code.
"""
import asyncio
import json
import os
from typing import Any, Optional

import httpx
from openai import AsyncOpenAI

RERANK_MODEL = "gpt-5.4"

RERANK_SYSTEM_PROMPT = """You are a relevance scoring engine for an insurance regulation
knowledge base. Given a user query and a candidate text chunk, score the chunk's
relevance to the query on a scale of 0-10.

Scoring criteria:
- 10: Directly answers the query with specific data, figures, or regulatory text
- 7-9: Highly relevant, contains key information but may not directly answer
- 4-6: Topically related but does not specifically address the query
- 1-3: Tangentially related at best
- 0: Not relevant

Respond with ONLY a JSON object: {"score": <number>, "reason": "<one sentence>"}"""

# Module-level singleton — avoids creating a new connection pool per call.
_rerank_client: Optional[AsyncOpenAI] = None


def _get_rerank_client() -> Optional[AsyncOpenAI]:
    global _rerank_client
    if _rerank_client is not None:
        return _rerank_client
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    _rerank_client = AsyncOpenAI(
        api_key=api_key,
        timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
    )
    return _rerank_client


def _resolve_rerank_model() -> str:
    """Resolve the rerank model from the parent agent's contextvar; fall back to RERANK_MODEL."""
    try:
        from bubble.pgvector.search_tool import get_rerank_model
        m = (get_rerank_model() or "").strip()
        if m:
            return m
    except Exception:
        pass
    return RERANK_MODEL


def _resolve_rerank_surface() -> str:
    """Resolve the telemetry surface — parent agent's surface if set, else 'reranker'."""
    try:
        from bubble.pgvector.search_tool import get_rerank_surface
        s = (get_rerank_surface() or "").strip()
        if s:
            return s
    except Exception:
        pass
    return "reranker"


async def _score_one(
    client: AsyncOpenAI, query: str, chunk: dict[str, Any], model: str,
) -> tuple[dict[str, Any], dict[str, int]]:
    """Score one chunk; also return token usage so caller can aggregate telemetry."""
    usage = {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0, "reasoning_tokens": 0}
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": RERANK_SYSTEM_PROMPT},
                {"role": "user", "content": (
                    f"Query: {query}\n\n"
                    f"Chunk (content_type={chunk['content_type']}, "
                    f"title={chunk['title']}, date={chunk['date']}):\n"
                    f"{chunk['content'][:1500]}"
                )},
            ],
            max_completion_tokens=200,
        )
        # Collect usage before parsing content (so even on parse failure we still bill).
        try:
            u = getattr(resp, "usage", None)
            if u is not None:
                usage["input_tokens"] = int(getattr(u, "prompt_tokens", 0) or 0)
                usage["output_tokens"] = int(getattr(u, "completion_tokens", 0) or 0)
                pt_det = getattr(u, "prompt_tokens_details", None)
                if pt_det is not None:
                    usage["cached_input_tokens"] = int(getattr(pt_det, "cached_tokens", 0) or 0)
                ct_det = getattr(u, "completion_tokens_details", None)
                if ct_det is not None:
                    usage["reasoning_tokens"] = int(getattr(ct_det, "reasoning_tokens", 0) or 0)
        except Exception:
            pass

        choice = resp.choices[0]
        if choice.finish_reason == "length":
            chunk["rerank_score"] = 0.0
        else:
            content = (choice.message.content or "").strip()
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.lower().startswith("json"):
                    content = content[4:]
            result = json.loads(content)
            chunk["rerank_score"] = float(result["score"])
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            "[reranker] scoring failed (%s: %s) — falling back to score=0",
            type(e).__name__, e,
        )
        chunk["rerank_score"] = 0.0
    return chunk, usage


_RERANK_CONCURRENCY = 4  # concurrent httpx connections before heap corruption risk


async def rerank_chunks(
    query: str,
    candidates: list[dict[str, Any]],
    top_n: int = 8,
) -> list[dict[str, Any]]:
    """Score candidates in parallel (batched) and return the top_n by relevance.

    Model and telemetry surface are inherited from the parent agent via contextvars
    set by `search_tool.set_rerank_model()` / `set_rerank_surface()`. Falls back to
    the hardcoded RERANK_MODEL and surface="reranker" if neither is set.

    Batching limits concurrent httpx connections to _RERANK_CONCURRENCY.
    Unbounded asyncio.gather() over many candidates causes shared OpenSSL/httpx
    state to corrupt glibc's heap (double free), killing the process with SIGABRT.
    """
    if not candidates:
        return candidates
    client = _get_rerank_client()
    if client is None:
        return candidates[:top_n]

    model = _resolve_rerank_model()
    surface = _resolve_rerank_surface()

    scored: list[dict[str, Any]] = []
    agg = {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0, "reasoning_tokens": 0, "requests": 0}
    for i in range(0, len(candidates), _RERANK_CONCURRENCY):
        batch = candidates[i : i + _RERANK_CONCURRENCY]
        batch_results = await asyncio.gather(*[_score_one(client, query, c, model) for c in batch])
        for chunk, usage in batch_results:
            scored.append(chunk)
            agg["input_tokens"] += usage["input_tokens"]
            agg["output_tokens"] += usage["output_tokens"]
            agg["cached_input_tokens"] += usage["cached_input_tokens"]
            agg["reasoning_tokens"] += usage["reasoning_tokens"]
            agg["requests"] += 1

    # Post aggregated usage under the parent agent's surface so cost rolls up to
    # whoever invoked the search. Falls back to the generic "reranker" surface.
    if agg["requests"] and (agg["input_tokens"] or agg["output_tokens"]):
        try:
            from bubble.usage_telemetry import post_usage
            post_usage(
                surface, model,
                agg["input_tokens"], agg["output_tokens"],
                cached_input_tokens=agg["cached_input_tokens"],
                reasoning_tokens=agg["reasoning_tokens"],
                requests_count=agg["requests"],
            )
        except Exception:
            pass  # telemetry must never break the agent

    scored.sort(key=lambda x: x.get("rerank_score", 0.0), reverse=True)
    return scored[:top_n]
