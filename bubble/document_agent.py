"""
LLM agent that extracts structured document data (Chronicle topic IDs, agenda item IDs)
for a detected document, using the DynamoDB `document-data-extraction` chat config.

Enabled via PAGE_CHANGE_AGENT_ENABLED=true (shares the same feature flag as page_change_agent).

When PGVECTOR_ENABLED=true and DB credentials are present, the agent runs via
the OpenAI Agents SDK with pgvector search tools, giving it access to the full
knowledge base. Otherwise falls back to a direct OpenAI Responses API call.

Replaces chatkit_client.extract_document_data() for in-process document enrichment.
"""

import asyncio
import json
import logging
import os
import re

log = logging.getLogger(__name__)

_CHAT_ID = "web-tracking-document-matching"

# Alert types that indicate new/updated documents and should trigger extraction
DOCUMENT_ALERT_TYPES = frozenset({
    "New Materials",
    "New Agenda & Materials",
    "Updated Materials",
    "Updated Agenda & Materials",
    "New or Updated Report or Other Resource",
})

_FALLBACK_SYSTEM_PROMPT = """\
You are a document matching assistant. Given a document name and URL, search the
knowledge base to find related Chronicle topics and agenda items, then return their
Bubble IDs.

Steps:
1. Call list_available_documents() to understand available content types.
2. Search for matching Chronicle topics using search_knowledge_base(). Each result
   includes an "external_id" field — that is the Bubble ID. For chronicles,
   content_type will be "chronicle".
3. Search for matching agenda items. For agenda items, content_type will be
   "bubble_data" with the Bubble ID in the "external_id" field.
4. Return ONLY a single JSON object:

{
  "topic_ids": [string],
  "agenda_item_ids": [string],
  "summary": string
}

Rules:
- topic_ids: Use the "external_id" values from matching chronicle search results.
- agenda_item_ids: Use the "external_id" values from matching agenda item results.
- Only include IDs where the match is clearly relevant (score > 0.01).
- summary: 1-2 sentence description of the document content.
- Return ONLY valid JSON. No markdown fences, no commentary outside the JSON.
"""

# Lazily loaded from DynamoDB; None means not yet fetched
_dynamo_config: dict | None = None


def _load_dynamo_config() -> dict:
    global _dynamo_config
    if _dynamo_config is None:
        from config.chatkit_config import get_chat_config
        _dynamo_config = get_chat_config(_CHAT_ID)
    return _dynamo_config


def _get_system_prompt() -> str:
    cfg = _load_dynamo_config()
    return cfg.get("instructions") or _FALLBACK_SYSTEM_PROMPT


def _get_model() -> str:
    cfg = _load_dynamo_config()
    return cfg.get("model") or "gpt-5.4"


def _get_reasoning_effort() -> str:
    cfg = _load_dynamo_config()
    return cfg.get("reasoning_effort") or "low"


def _get_pgvector_namespaces() -> list[str]:
    cfg = _load_dynamo_config()
    ns = cfg.get("pgvector_namespaces")
    if isinstance(ns, list):
        return ns
    return [
        "bubble-data", "art-chronicles", "art-newsreels",
        "naic-guidelines", "naic-proceedings",
        "international-guidelines", "ratings-agencies",
    ]


def _pgvector_enabled() -> bool:
    if os.environ.get("PGVECTOR_ENABLED", "").strip().lower() not in ("1", "true", "yes"):
        return False
    required = ("DATABASE_IP", "DATABASE_NAME")
    if not all(os.environ.get(k, "").strip() for k in required):
        return False
    return bool(
        os.environ.get("DATABASE_PASSWORD_CHATKIT", "").strip()
        or os.environ.get("DATABASE_PASSWORD", "").strip()
    )


# ---------------------------------------------------------------------------
# Agents SDK path (pgvector enabled)
# ---------------------------------------------------------------------------

async def _run_with_pgvector(
    system_prompt: str,
    user_content: str,
    model: str,
    reasoning_effort: str,
    pgvector_namespaces: list[str],
) -> str:
    from agents import Agent, Runner, ModelSettings
    from bubble.pgvector.client import init_pg_pool, close_pg_pool
    from bubble.pgvector.search_tool import set_pgvector_namespaces, search_knowledge_base, list_available_documents

    await init_pg_pool()
    try:
        set_pgvector_namespaces(pgvector_namespaces)
        agent = Agent(
            name=_CHAT_ID,
            instructions=system_prompt,
            tools=[search_knowledge_base, list_available_documents],
            model=model,
            model_settings=ModelSettings(reasoning_effort=reasoning_effort),
        )
        result = await Runner.run(agent, input=user_content)
        return result.final_output or ""
    finally:
        await close_pg_pool()


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------

def _parse_output(raw: str) -> dict:
    if not raw:
        return {}

    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    candidate = fenced.group(1).strip() if fenced else raw.strip()

    try:
        data = json.loads(candidate)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError):
        pass

    match = re.search(r"\{[\s\S]*\}", candidate)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            pass

    log.debug("document_agent: could not parse JSON from output")
    return {}


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def should_run_for_alert(agent_output: dict) -> bool:
    """Return True if the agent output indicates document extraction is warranted."""
    alert_type = (agent_output.get("alert_type") or "").strip()
    if alert_type in DOCUMENT_ALERT_TYPES:
        return True
    library_items = agent_output.get("library_items") or []
    return bool(library_items)


def extract_document_data(
    document_name: str,
    document_url: str,
    pdf_text: str | None = None,
) -> dict:
    """
    Extract Chronicle topic IDs, agenda item IDs, and a summary for a document.

    Args:
        document_name: Title or file name of the document.
        document_url:  URL where the document can be accessed.
        pdf_text:      Optional extracted text signals (first ~3 000 chars).

    Returns:
        Dict with keys: topic_ids (list), agenda_item_ids (list), summary (str).
        Returns {} on any failure (never raises).
    """
    from bubble.page_change_agent import PAGE_CHANGE_AGENT_ENABLED
    if not PAGE_CHANGE_AGENT_ENABLED:
        return {}

    try:
        model = os.environ.get("DOCUMENT_AGENT_MODEL", "").strip() or _get_model()
        reasoning_effort = _get_reasoning_effort()
        system_prompt = _get_system_prompt()
        pgvector_namespaces = _get_pgvector_namespaces()

        lines = [
            f"Document title: {document_name}",
            f"URL: {document_url}",
        ]
        if pdf_text:
            lines.append(f"\nKey content signals:\n{pdf_text[:3000]}")
        lines.append(
            "\nIdentify the most relevant Chronicle topic(s) and related agenda items. "
            "Return a single JSON object with keys: topic_ids, agenda_item_ids, summary. "
            "Return ONLY valid JSON — no markdown fences, no commentary."
        )
        user_content = "\n".join(lines)

        if not _pgvector_enabled():
            log.info("document_agent: pgvector not available, skipping (no knowledge base to search)")
            return {}

        log.info("document_agent: running with pgvector tools (model=%s)", model)
        raw = asyncio.run(_run_with_pgvector(
            system_prompt, user_content, model, reasoning_effort, pgvector_namespaces,
        ))
        result = _parse_output(raw)

        return result if isinstance(result, dict) else {}

    except Exception as e:
        log.warning("document_agent failed (non-fatal): %r", e, exc_info=True)
        return {}
