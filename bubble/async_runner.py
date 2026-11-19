"""
Shared event loop for all agent invocations in a single process.

Problem this solves:
  Each `asyncio.run(coro)` creates a new event loop and closes it on exit.
  The OpenAI Agents SDK (and httpx underneath it) caches an AsyncClient
  tied to the first event loop it sees. On the second asyncio.run() in the
  same process, the cached client references a closed loop and every call
  raises `RuntimeError: Event loop is closed`.

  Four agents (page_change_agent, document_agent, eval_agent, doc_eval_agent)
  call asyncio.run() once per invocation. In every 6h pipeline run, the FIRST
  document_agent invocation succeeds; every subsequent one crashes with
  "Event loop is closed", our broad `except Exception` catches it, and the
  agent falls through to the degraded direct-API path with no
  search_knowledge_base tool. Observed in CloudWatch for every pipeline
  run since the Nov 9 pgvector-to-backend migration — 11 of 19 doc
  extractions in Nov 19 02:22 UTC run took the degraded path, meaning
  no chronicle / org / newsreel / prior-doc lookups for ~60% of
  extractions per run.

Fix: hand every `asyncio.run()` call through run_sync() instead. This keeps
the SAME event loop alive across all agent invocations in the process, so
the Agents SDK's cached HTTP clients stay valid.
"""
import asyncio
import logging
from typing import Awaitable, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

_shared_loop: asyncio.AbstractEventLoop | None = None


def _get_shared_loop() -> asyncio.AbstractEventLoop:
    """Return a process-wide event loop, creating a fresh one if the previous
    was closed (e.g. a test or subprocess teardown closed it)."""
    global _shared_loop
    if _shared_loop is None or _shared_loop.is_closed():
        _shared_loop = asyncio.new_event_loop()
        log.debug("async_runner: created shared event loop id=%s", id(_shared_loop))
    return _shared_loop


def run_sync(coro: Awaitable[T]) -> T:
    """Drop-in replacement for `asyncio.run(coro)` that reuses one process-wide
    event loop instead of creating and closing one per call.

    Safe to call from any thread the loop is not already running in.
    Not safe to call from inside a running coroutine — use `await coro` there.
    """
    return _get_shared_loop().run_until_complete(coro)
