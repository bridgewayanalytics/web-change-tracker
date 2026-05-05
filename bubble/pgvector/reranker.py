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

RERANK_MODEL = "gpt-5-nano"

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


async def _score_one(client: AsyncOpenAI, query: str, chunk: dict[str, Any]) -> dict[str, Any]:
    try:
        resp = await client.chat.completions.create(
            model=RERANK_MODEL,
            messages=[
                {"role": "system", "content": RERANK_SYSTEM_PROMPT},
                {"role": "user", "content": (
                    f"Query: {query}\n\n"
                    f"Chunk (content_type={chunk['content_type']}, "
                    f"title={chunk['title']}, date={chunk['date']}):\n"
                    f"{chunk['content'][:1500]}"
                )},
            ],
            max_tokens=150,
        )
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
        log_msg = f"[reranker] WARNING: scoring failed ({type(e).__name__}) — falling back to score=0"
        import logging
        logging.getLogger(__name__).debug(log_msg)
        chunk["rerank_score"] = 0.0
    return chunk


async def rerank_chunks(
    query: str,
    candidates: list[dict[str, Any]],
    top_n: int = 8,
) -> list[dict[str, Any]]:
    """Score candidates in parallel and return the top_n by relevance."""
    if not candidates:
        return candidates
    client = _get_rerank_client()
    if client is None:
        return candidates[:top_n]
    scored = await asyncio.gather(*[_score_one(client, query, c) for c in candidates])
    scored_list = list(scored)
    scored_list.sort(key=lambda x: x.get("rerank_score", 0.0), reverse=True)
    return scored_list[:top_n]
