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
from datetime import datetime, timezone

from storage.doc_schema import DOC_PIPELINE_FIELDS

log = logging.getLogger(__name__)

_CHAT_ID = "document-data-extraction"

# Alert types that indicate new/updated documents and should trigger extraction.
# Covers all alert types that result in a library item (create or update).
DOCUMENT_ALERT_TYPES = frozenset({
    "New Agenda",
    "New Materials",
    "New Agenda & Materials",
    "Updated Agenda",
    "Updated Materials",
    "Updated Agenda & Materials",
    "New Request for Comment",
    "Updated Request for Comment",
    "New Effective Date",
    "Updated Effective Date",
    "New or Updated Report or Other Resource",
    "Other",
})

_FALLBACK_SYSTEM_PROMPT = """\
You are a document data extraction assistant. Given a document name and URL,
extract structured data from the document and return it as a single JSON object.
Return ONLY valid JSON — no markdown fences, no commentary outside the JSON.
"""

# Appended only for the pgvector (Agents SDK) path where the model outputs free text.
# For the direct Responses API path, structured outputs enforce the format.
_JSON_OUTPUT_SUFFIX = """

## Output Format
For documents with multiple agenda items, cover each in sequential order (number = 1, 2, 3...).
All document-level fields (organization, dates, document type, URLs) repeat for every agenda item.
If the document contains no distinct agenda items, report a single entry with agenda_item_title = "N/A".
Return your analysis as structured JSON.
"""

# Lazily loaded from DynamoDB; None means not yet fetched
_dynamo_config: dict | None = None


def _load_dynamo_config() -> dict:
    global _dynamo_config
    if _dynamo_config is None:
        from config.chatkit_config import get_chat_config
        _dynamo_config = get_chat_config(_CHAT_ID)
    return _dynamo_config


def _get_base_system_prompt() -> str:
    """System prompt without JSON output suffix — for structured outputs path."""
    cfg = _load_dynamo_config()
    return cfg.get("instructions") or _FALLBACK_SYSTEM_PROMPT


def _get_system_prompt() -> str:
    """System prompt with JSON output suffix appended — for pgvector/text path."""
    return _get_base_system_prompt() + _JSON_OUTPUT_SUFFIX


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


# ---------------------------------------------------------------------------
# Structured Outputs schema helpers (mirrors page_change_agent pattern)
# ---------------------------------------------------------------------------

# Reuse the sanitizer from page_change_agent to keep logic in one place
def _get_output_json_schema() -> dict | None:
    """Return the structured output schema from DynamoDB, sanitized for OpenAI, or None if not set."""
    cfg = _load_dynamo_config()
    schema = cfg.get("output_json_schema")
    if not isinstance(schema, dict):
        return None
    from bubble.page_change_agent import _sanitize_schema_for_openai
    return _sanitize_schema_for_openai(schema)


def _get_output_json_schema_name() -> str:
    cfg = _load_dynamo_config()
    return cfg.get("output_json_schema_name") or "document_extraction"


def _get_output_json_schema_strict() -> bool:
    cfg = _load_dynamo_config()
    v = cfg.get("output_json_schema_strict")
    return bool(v) if v is not None else True


def get_config_hash() -> str:
    """MD5 of the current system prompt + model — used to detect config changes."""
    import hashlib
    cfg = _load_dynamo_config()
    key = (cfg.get("instructions") or _FALLBACK_SYSTEM_PROMPT) + "|" + (cfg.get("model") or "")
    return hashlib.md5(key.encode("utf-8")).hexdigest()


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
    json_schema: dict | None = None,
    json_schema_name: str = "document_extraction",
    json_schema_strict: bool = True,
) -> dict:
    """
    Two-step extraction:
      1. Run the Agents SDK with pgvector tools — lets the model search the knowledge
         base and gather information as free text.
      2. Make one Structured Outputs call to reformat the gathered information into
         the exact DynamoDB schema, enforcing correct field names and types.
    """
    from agents import Agent, Runner, ModelSettings
    from agents.model_settings import Reasoning
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
            model_settings=ModelSettings(
                reasoning=Reasoning(effort=reasoning_effort),
                verbosity=reasoning_effort,
            ),
        )
        result = await Runner.run(agent, input=user_content)
        gathered = result.final_output or ""
    finally:
        await close_pg_pool()

    if not gathered:
        return {}

    log.info("document_agent [STEP1_OUTPUT]: %s", gathered[:2000])

    # Step 2: enforce schema via Structured Outputs so output always matches DynamoDB schema
    if json_schema:
        from bubble.openai_client import chat_json
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a JSON formatter. Format the document extraction analysis below "
                    "into the required schema. The schema expects an 'agenda_items' array with "
                    "exactly one element per agenda item covered in the analysis — do NOT collapse "
                    "multiple agenda items into a single element. Repeat all document-level fields "
                    "(organization, document title, type, dates, URLs) on every element. "
                    "Use only data from the analysis — do not invent values. "
                    "For required string fields with no explicit value, output 'N/A'."
                ),
            },
            {"role": "user", "content": gathered},
        ]
        return chat_json(
            messages,
            model=model,
            reasoning_effort=reasoning_effort,
            json_schema=json_schema,
            json_schema_name=json_schema_name,
            json_schema_strict=json_schema_strict,
        )

    # Fallback if no schema: parse free text
    return _parse_output(gathered)


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

def _fetch_single_pdf(url: str) -> str | None:
    """Fetch and extract text from a single PDF URL. Returns None on any failure."""
    url = url.strip()
    if not url or not url.lower().split("?")[0].endswith(".pdf"):
        return None
    try:
        import requests
        resp = requests.get(url, timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://content.naic.org/",
            "Accept": "application/pdf,*/*",
        })
        resp.raise_for_status()
        from scrape.pdf_meeting_meta import _extract_plain_text
        text = _extract_plain_text(resp.content)
        if text and text.strip():
            log.info("document_agent: fetched PDF text (%d chars) from %s", len(text), url[:80])
            return text.strip()
        log.warning("document_agent: PDF fetched but extracted no text from %s", url[:80])
    except Exception as e:
        log.warning("document_agent: could not fetch PDF text from %s: %s", url[:80], e)
    return None


def _fetch_pdf_text(url: str) -> str | None:
    """
    Fetch a PDF from `url` and extract plain text.
    If `url` is semicolon-separated, tries each candidate in order and returns
    the first successful result. Does NOT concatenate — each extraction call
    must cover exactly one document.
    Returns None if no PDF content could be extracted.
    """
    if not url:
        return None
    candidates = [u.strip() for u in url.split(";") if u.strip()]
    for candidate in candidates:
        text = _fetch_single_pdf(candidate)
        if text:
            return text
    return None


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

_NA_VALUES = frozenset({"N/A", "N/A.", "-", ""})

_ET_OFFSET = -4  # Eastern Daylight Time (UTC-4); close enough for a timestamp field


def _unwrap_agenda_items(result: dict) -> list[dict]:
    """Extract per-row list from agenda_items wrapper, or wrap flat result in a list."""
    items = result.get("agenda_items")
    if isinstance(items, list) and items:
        return [item for item in items if isinstance(item, dict)]
    return [result]


def _stamp_extraction_datetime(out: dict, original_datetime: str | None = None) -> None:
    """Stamp data_extraction_date_time on the output dict (always overwrites agent output).
    Uses original_datetime if provided (rerun path — preserves the first-run value).
    Falls back to current wall-clock time (first run — LLM cannot reliably know the time).
    """
    if not out:
        return
    if original_datetime and original_datetime.strip().upper() not in _NA_VALUES:
        out["data_extraction_date_time"] = original_datetime
        return
    from datetime import timedelta
    et_now = datetime.now(timezone.utc).astimezone(
        timezone(timedelta(hours=_ET_OFFSET))
    )
    out["data_extraction_date_time"] = et_now.strftime("%Y-%m-%dT%H:%M:%S%z")


def _stamp_web_page_url(out: dict, alert_context: dict | None) -> None:
    """Stamp web_page_url = source_url (always overrides agent output).
    The web_page_url must be the exact monitored page URL. The agent sometimes
    normalizes or guesses a different URL — source_url is the ground truth.
    """
    if not out or not alert_context:
        return
    if "web_page_url" not in out:
        return
    source_url = str(alert_context.get("source_url") or "").strip()
    if source_url:
        out["web_page_url"] = source_url


def _item_has_real_name(item: dict) -> bool:
    name = (
        item.get("preliminary_title")
        or item.get("title")
        or item.get("file_name")
        or ""
    ).strip().upper()
    return name not in _NA_VALUES


def should_run_for_alert(agent_output: dict) -> bool:
    """Return True if the agent output indicates document extraction is warranted."""
    alert_type = (agent_output.get("alert_type") or "").strip()
    if alert_type in DOCUMENT_ALERT_TYPES:
        return True
    # Old-schema fallback: only trigger if at least one library item has a real name
    library_items = agent_output.get("library_items") or []
    return any(_item_has_real_name(item) for item in library_items)


_HTML_CONTEXT_LIMIT = 8000  # chars per before/after HTML snippet injected into agent context


def extract_document_data(
    document_name: str,
    document_url: str,
    pdf_text: str | None = None,
    text_limit: int | None = None,  # retained for call-site compatibility; unused
    alert_context: dict | None = None,
    before_html: str | None = None,
    after_html: str | None = None,
    original_datetime: str | None = None,
) -> list[dict]:
    """
    Extract structured data from a document using the document-data-extraction agent.

    Output is fully dynamic — fields are whatever the DynamoDB config instructs
    the model to return. Returns one dict per agenda item (may be multiple for
    documents with multiple agenda items). Returns [] on any failure (never raises).
    """
    from bubble.page_change_agent import PAGE_CHANGE_AGENT_ENABLED, _store_agent_context
    if not PAGE_CHANGE_AGENT_ENABLED:
        return []

    import uuid
    doc_call_id = str(uuid.uuid4())

    try:
        model = os.environ.get("DOCUMENT_AGENT_MODEL", "").strip() or _get_model()
        reasoning_effort = _get_reasoning_effort()
        system_prompt = _get_system_prompt()
        pgvector_namespaces = _get_pgvector_namespaces()

        # Auto-fetch PDF text if not provided by caller
        if not pdf_text and document_url:
            pdf_text = _fetch_pdf_text(document_url)

        from bubble.org_tree import get_org_tree
        org_tree = get_org_tree()

        lines = [
            f"Document link label: {document_name}",
            f"URL: {document_url}",
        ]
        if _pgvector_enabled():
            lines.append("(Full document content is available via your knowledge base search tools — search for specific sections as needed to extract each field accurately.)")
        if org_tree:
            lines.append(f"\n=== ORGANIZATION REFERENCE ===\n{org_tree}")
        if alert_context:
            ctx_parts = []
            org = alert_context.get("organization")
            if org:
                org_str = ", ".join(org) if isinstance(org, list) else str(org)
                ctx_parts.append(f"Authoring Organization: {org_str}")
            alert_type = str(alert_context.get("alert_type") or "").strip()
            if alert_type and alert_type.upper() not in ("N/A", ""):
                ctx_parts.append(f"Alert type: {alert_type}")
            event_title = str(alert_context.get("event_title") or "").strip()
            if event_title and event_title.upper() not in ("N/A", ""):
                ctx_parts.append(f"Event: {event_title}")
            event_dt = str(alert_context.get("event_start_date_time") or "").strip()
            if event_dt and event_dt.upper() not in ("N/A", ""):
                ctx_parts.append(f"Event start date/time: {event_dt}")
            source_url = str(alert_context.get("source_url") or "").strip()
            if source_url:
                ctx_parts.append(f"Source page URL: {source_url}")
            if ctx_parts:
                lines.append("\n=== ALERT CONTEXT (page change alert that triggered this extraction) ===\n" + "\n".join(ctx_parts))
        if before_html or after_html:
            lines.append("\n=== PAGE CHANGE CONTEXT (HTML snapshots of the monitored page) ===")
            if before_html:
                lines.append(f"Before HTML (prior state):\n{before_html[:_HTML_CONTEXT_LIMIT]}")
            if after_html:
                lines.append(f"After HTML (new state with the document now present):\n{after_html[:_HTML_CONTEXT_LIMIT]}")
        user_content = "\n".join(lines)

        _store_agent_context(doc_call_id, "document", user_content, system_prompt)

        json_schema = _get_output_json_schema()
        json_schema_name = _get_output_json_schema_name()
        json_schema_strict = _get_output_json_schema_strict()

        if not _pgvector_enabled():
            # Direct Responses API path with Structured Outputs
            log.info("document_agent: pgvector not available, running via direct API (model=%s) for: %s", model, document_name[:80])
            from bubble.openai_client import chat_json
            messages = [
                {"role": "system", "content": _get_base_system_prompt()},
                {"role": "user", "content": user_content},
            ]
            result = chat_json(
                messages,
                model=model,
                reasoning_effort=reasoning_effort,
                json_schema=json_schema,
                json_schema_name=json_schema_name,
                json_schema_strict=json_schema_strict,
            )
            out = result if isinstance(result, dict) else {}
            if not out:
                log.info("document_agent: no output for: %s", document_name[:60])
                return []
            for _f in DOC_PIPELINE_FIELDS:
                out.pop(_f, None)
            out["doc_agent_context_key"] = f"alerts/contexts/document/{doc_call_id}.txt"
            rows = _unwrap_agenda_items(out)
            for row in rows:
                _stamp_extraction_datetime(row, original_datetime=original_datetime)
                _stamp_web_page_url(row, alert_context)
            log.info("document_agent: extracted %d row(s) for: %s", len(rows), document_name[:60])
            return rows

        # Ingest the document into pgvector so the agent can search the full text,
        # not just the 12k-char excerpt in the prompt. Deterministic doc_uuid means
        # reruns and QA agent runs hit the same already-indexed namespace (no-op upload).
        if document_url and document_url.strip():
            from bubble.doc_extraction_ingest import ingest_and_arm
            doc_namespace = ingest_and_arm(document_url, pdf_text, document_name)
            if doc_namespace:
                pgvector_namespaces = list(pgvector_namespaces) + [doc_namespace]

        log.info("document_agent: running with pgvector (model=%s) for: %s", model, document_name[:80])
        result = asyncio.run(_run_with_pgvector(
            system_prompt, user_content, model, reasoning_effort, pgvector_namespaces,
            json_schema=json_schema,
            json_schema_name=json_schema_name,
            json_schema_strict=json_schema_strict,
        ))

        out = result if isinstance(result, dict) else {}
        if not out:
            log.info("document_agent: no output for: %s", document_name[:60])
            return []
        for _f in DOC_PIPELINE_FIELDS:
            out.pop(_f, None)
        out["doc_agent_context_key"] = f"alerts/contexts/document/{doc_call_id}.txt"
        rows = _unwrap_agenda_items(out)
        for row in rows:
            _stamp_extraction_datetime(row)
            _stamp_web_page_url(row, alert_context)
        log.info("document_agent: extracted %d row(s) for: %s", len(rows), document_name[:60])
        return rows

    except Exception as e:
        log.warning("document_agent failed (non-fatal): %r", e, exc_info=True)
        return []
