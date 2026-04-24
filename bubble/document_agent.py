"""
LLM agent that extracts structured document data for a detected document,
using the DynamoDB `chat:document-data-extraction` config.

Enabled via PAGE_CHANGE_AGENT_ENABLED=true (shares the same feature flag as page_change_agent).

When PGVECTOR_ENABLED=true and DB credentials are present, the agent runs via
the OpenAI Agents SDK with pgvector search tools, giving it access to the full
knowledge base.

Output is fully dynamic — whatever fields the DynamoDB config instructs the model
to return are stored verbatim. No hardcoded output schema.
Results are stored in a separate S3 table: alerts/document_extractions_table.jsonl
"""

import asyncio
import json
import logging
import os
import re

# Max characters of PDF text to include in the agent prompt
_PDF_TEXT_LIMIT = 12000

log = logging.getLogger(__name__)

_CHAT_ID = "document-data-extraction"

# Alert types that indicate new/updated documents and should trigger extraction
DOCUMENT_ALERT_TYPES = frozenset({
    "New Materials",
    "New Agenda & Materials",
    "Updated Materials",
    "Updated Agenda & Materials",
    "New or Updated Report or Other Resource",
})

_FALLBACK_SYSTEM_PROMPT = """\
You are a document data extraction assistant. Given a document name and URL,
extract structured data from the document and return it as a single JSON object.
Return ONLY valid JSON — no markdown fences, no commentary outside the JSON.
"""

_JSON_OUTPUT_SUFFIX = """

## Output Format
Return your response as a single JSON object containing all extracted values.
Use snake_case keys (e.g., agenda_item_title, organization, document_type).
Return ONLY the JSON object — no markdown fences, no commentary outside the JSON.
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
    base = cfg.get("instructions") or _FALLBACK_SYSTEM_PROMPT
    # Always append JSON output requirement — the DynamoDB instructions describe
    # what to extract but don't specify output format.
    return base + _JSON_OUTPUT_SUFFIX


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
# PDF fetch helper
# ---------------------------------------------------------------------------

def _fetch_pdf_text(url: str) -> str | None:
    """
    Fetch a PDF from `url` and extract plain text.
    Returns None on any failure (network error, not a PDF, parse error, etc.).
    Only attempts fetch for URLs that look like PDFs.
    """
    if not url or not url.lower().endswith(".pdf"):
        return None
    try:
        import requests
        resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        if "pdf" not in content_type.lower() and not url.lower().endswith(".pdf"):
            return None
        from scrape.pdf_meeting_meta import _extract_plain_text
        text = _extract_plain_text(resp.content)
        if text and text.strip():
            log.info("document_agent: fetched PDF text (%d chars) from %s", len(text), url[:80])
            return text.strip()
    except Exception as e:
        log.debug("document_agent: could not fetch PDF text from %s: %s", url[:80], e)
    return None


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
    Extract structured data from a document using the document-data-extraction agent.

    Output is fully dynamic — fields are whatever the DynamoDB config instructs
    the model to return. No hardcoded output schema.

    Returns a dict of extracted fields, or {} on any failure (never raises).
    """
    from bubble.page_change_agent import PAGE_CHANGE_AGENT_ENABLED
    if not PAGE_CHANGE_AGENT_ENABLED:
        return {}

    try:
        model = os.environ.get("DOCUMENT_AGENT_MODEL", "").strip() or _get_model()
        reasoning_effort = _get_reasoning_effort()
        system_prompt = _get_system_prompt()
        pgvector_namespaces = _get_pgvector_namespaces()

        # Auto-fetch PDF text if not provided by caller
        if not pdf_text and document_url:
            pdf_text = _fetch_pdf_text(document_url)

        lines = [
            f"Document title: {document_name}",
            f"URL: {document_url}",
        ]
        if pdf_text:
            lines.append(f"\nDocument content:\n{pdf_text[:_PDF_TEXT_LIMIT]}")
        user_content = "\n".join(lines)

        if not _pgvector_enabled():
            log.info("document_agent: pgvector not available, skipping (no knowledge base to search)")
            return {}

        log.info("document_agent: running (model=%s) for: %s", model, document_name[:80])
        raw = asyncio.run(_run_with_pgvector(
            system_prompt, user_content, model, reasoning_effort, pgvector_namespaces,
        ))
        result = _parse_output(raw)

        if result:
            log.info("document_agent: extracted %d field(s) for: %s", len(result), document_name[:60])
        else:
            log.info("document_agent: no output for: %s", document_name[:60])

        return result if isinstance(result, dict) else {}

    except Exception as e:
        log.warning("document_agent failed (non-fatal): %r", e, exc_info=True)
        return {}
