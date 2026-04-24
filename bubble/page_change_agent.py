"""
LLM agent that compares before/after HTML snapshots and extracts structured
change data for Bubble Alert, Resource, and Calendar Item payloads.

Enabled via PAGE_CHANGE_AGENT_ENABLED=true.
Model overridable via PAGE_CHANGE_AGENT_MODEL (default: inherits from DynamoDB config).

When PGVECTOR_ENABLED=true and DB credentials are present, the agent runs via
the OpenAI Agents SDK with pgvector search tools (search_knowledge_base,
list_available_documents), giving it access to the full knowledge base of
calendar items, agenda items, Chronicle topics, and NAIC proceedings.

When pgvector is unavailable, falls back to a direct OpenAI Responses API call
with the same system prompt.

Instructions, model, and reasoning_effort are loaded from DynamoDB
chatkit_production_config (chat:web-tracking-agent) at first use.
"""

import asyncio
import json
import logging
import os
import re

log = logging.getLogger(__name__)

PAGE_CHANGE_AGENT_ENABLED = os.environ.get("PAGE_CHANGE_AGENT_ENABLED", "false").strip().lower() in (
    "1", "true", "yes",
)

_CHAT_ID = "web-tracking-agent"

# Fallback system prompt when DynamoDB config is unavailable
_FALLBACK_SYSTEM_PROMPT = """\
You are a structured data extraction agent. You will receive two versions of a
web page's main content (before and after an update) and a short context object
describing the page.

Your task: identify what is NEW or CHANGED in the after version relative to the
before version, then return a single JSON object with the following schema:

{
  "alert_type": string,
  "alert_title": string,
  "alert_description": string,
  "alert_url": string | null,
  "organization": string | null,
  "alert_date_time": string | null,
  "is_relevant_for_art_newsreel": boolean,
  "events": [
    {
      "title": string,
      "start_datetime": string | null,
      "end_datetime": string | null,
      "timezone": string | null,
      "is_full_day": boolean,
      "url": string | null,
      "call_in_access_code": string | null,
      "duration": string | null
    }
  ],
  "library_items": [
    {
      "preliminary_title": string,
      "url": string | null,
      "file_name": string | null
    }
  ],
  "agenda_items": [
    {
      "title": string,
      "official_title": string | null,
      "standardized_id": string | null,
      "official_id": string | null,
      "is_existing": boolean,
      "chronicle_topics": [string]
    }
  ]
}

alert_type must be one of:
- "New Agenda"
- "New Materials"
- "New Agenda & Materials"
- "Updated Agenda"
- "Updated Materials"
- "Updated Agenda & Materials"
- "New Meeting"
- "Updated Meeting"
- "New Request for Comment"
- "Updated Request for Comment"
- "New Effective Date"
- "Updated Effective Date"
- "New or Updated Report or Other Resource"
- "Alert not relevant - the change was limited to carrousel or reordering of content"
- "No Meaningful Change"
- "Other"

Rules:
- Only include events and library_items that are NEW or CHANGED vs the before version.
- If nothing meaningful changed, set alert_type to "No Meaningful Change".
- If the change is only a carousel rotation or reordering with no new content, set alert_type to
  "Alert not relevant - the change was limited to carrousel or reordering of content".
- If the before version is empty (first run), extract all relevant items from after.
- Set is_relevant_for_art_newsreel to true if the alert contains new substantive content
  (documents, agenda items, meeting materials) that would be relevant for an ART Newsreel article.
  Set to false otherwise.
- Return ONLY valid JSON. No markdown fences, no commentary outside the JSON.
"""

# Fallback output schema when DynamoDB output_schema_json is absent.
# This dict is serialised to JSON and injected into the user message.
_FALLBACK_OUTPUT_SCHEMA: dict = {
    "alert_type": "string",
    "alert_title": "string",
    "alert_description": "string",
    "alert_url": "string | null",
    "organization": "string | null",
    "alert_date_time": "string | null (ISO 8601 Eastern Time)",
    "is_relevant_for_art_newsreel": "boolean",
    "events": [
        {
            "title": "string",
            "start_datetime": "string | null",
            "end_datetime": "string | null",
            "timezone": "string | null",
            "is_full_day": "boolean",
            "url": "string | null",
            "call_in_access_code": "string | null",
            "duration": "string | null",
        }
    ],
    "library_items": [
        {
            "preliminary_title": "string",
            "url": "string | null",
            "file_name": "string | null",
        }
    ],
    "agenda_items": [
        {
            "title": "string",
            "official_title": "string | null",
            "standardized_id": "string | null",
            "official_id": "string | null",
            "is_existing": "boolean",
            "chronicle_topics": ["string"],
        }
    ],
}

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


def _get_output_schema_str() -> str:
    """
    Return the JSON output schema string to embed in the user message.

    Source priority:
      1. output_schema_json attribute in DynamoDB (content team controls field names)
      2. _FALLBACK_OUTPUT_SCHEMA (hardcoded dict, serialised to JSON)

    Storing output_schema_json in DynamoDB lets the content team rename or add
    fields without any code changes — the agent automatically uses the new names.
    """
    cfg = _load_dynamo_config()
    schema_json = cfg.get("output_schema_json")
    if schema_json and isinstance(schema_json, str):
        return schema_json
    return json.dumps(_FALLBACK_OUTPUT_SCHEMA, indent=2)


def _get_model() -> str | None:
    cfg = _load_dynamo_config()
    return cfg.get("model") or None


def _get_reasoning_effort() -> str:
    cfg = _load_dynamo_config()
    return cfg.get("reasoning_effort") or "medium"


def get_config_hash() -> str:
    """MD5 of the current system prompt + model — used to detect config changes."""
    import hashlib
    cfg = _load_dynamo_config()
    key = (cfg.get("instructions") or _FALLBACK_SYSTEM_PROMPT) + "|" + (cfg.get("model") or "")
    return hashlib.md5(key.encode("utf-8")).hexdigest()


def _pgvector_enabled() -> bool:
    """True when PGVECTOR_ENABLED=true and DB credentials are present."""
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
    """Run the agent with pgvector search tools via the OpenAI Agents SDK."""
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
    """
    Extract a JSON object from the agent's raw output.
    Handles both clean JSON and JSON embedded in prose/markdown.
    """
    if not raw:
        return {}

    # Strip markdown fences if present
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    candidate = fenced.group(1).strip() if fenced else raw.strip()

    # Try direct parse first
    try:
        data = json.loads(candidate)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError):
        pass

    # Fall back: find the outermost JSON object in the text
    match = re.search(r"\{[\s\S]*\}", candidate)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            pass

    log.debug("page_change_agent: could not parse JSON from output")
    return {}


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def extract_page_change(
    before_html: str,
    after_html: str,
    target_context: dict,
) -> dict:
    """
    Compare before/after HTML and return structured change extraction.

    Args:
        before_html: Stripped content HTML from previous state (empty string for first run).
        after_html:  Stripped content HTML from current fetch.
        target_context: {label, url, org_path, group, tags} from target config.

    Returns:
        Dict with keys: alert_type, alert_title, alert_description, alert_url,
        events, library_items, agenda_items.
        Returns {} on any failure (never raises).
    """
    if not PAGE_CHANGE_AGENT_ENABLED:
        return {}

    try:
        model = os.environ.get("PAGE_CHANGE_AGENT_MODEL", "").strip() or _get_model() or "gpt-5.4"
        reasoning_effort = _get_reasoning_effort()
        system_prompt = _get_system_prompt()
        pgvector_namespaces = _load_dynamo_config().get("pgvector_namespaces") or []

        label = target_context.get("label", "")
        url = target_context.get("url", "")
        org_path = target_context.get("org_path", [])
        group = target_context.get("group", "")
        tags = target_context.get("tags", [])

        context_block = (
            f"Page: {label}\n"
            f"URL: {url}\n"
            f"Org path: {' > '.join(org_path) if isinstance(org_path, list) else org_path}\n"
            f"Group: {group}\n"
            f"Tags: {', '.join(tags) if isinstance(tags, list) else tags}"
        )

        output_schema = _get_output_schema_str()
        user_content = (
            f"=== TARGET CONTEXT ===\n{context_block}\n\n"
            f"=== BEFORE (previous version) ===\n{before_html or '(empty — first run)'}\n\n"
            f"=== AFTER (current version) ===\n{after_html}\n\n"
            f"Return your analysis as a single JSON object with exactly this schema:\n"
            f"{output_schema}\n"
            "alert_type must be one of: 'New Agenda', 'New Materials', 'New Agenda & Materials', "
            "'Updated Agenda', 'Updated Materials', 'Updated Agenda & Materials', 'New Meeting', "
            "'Updated Meeting', 'New Request for Comment', 'Updated Request for Comment', "
            "'New Effective Date', 'Updated Effective Date', "
            "'New or Updated Report or Other Resource', "
            "'Alert not relevant - the change was limited to carrousel or reordering of content', "
            "'No Meaningful Change', 'Other'.\n"
            "Return ONLY valid JSON — no markdown fences, no commentary outside the JSON."
        )

        if _pgvector_enabled():
            log.info("page_change_agent: running with pgvector tools (model=%s)", model)
            raw = asyncio.run(_run_with_pgvector(
                system_prompt, user_content, model, reasoning_effort,
                pgvector_namespaces if isinstance(pgvector_namespaces, list) else [],
            ))
            result = _parse_output(raw)
        else:
            log.info("page_change_agent: running without pgvector (model=%s)", model)
            from bubble.openai_client import chat_json
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ]
            result = chat_json(messages, model=model, reasoning_effort=reasoning_effort)

        return result if isinstance(result, dict) else {}

    except Exception as e:
        log.warning("page_change_agent failed (non-fatal): %r", e, exc_info=True)
        return {}


def agent_output_to_by_type(agent_output: dict) -> dict:
    """
    Convert page_change_agent output into the by_type diff format consumed by
    build_resource_payload() and build_calendar_item_payload().

    Maps:
      library_items → by_type["docs"]["added"]
      events        → by_type["events"]["added"]
    """
    by_type: dict = {}

    library_items = agent_output.get("library_items") or []
    if library_items:
        by_type["docs"] = {
            "added": [
                {
                    "title": item.get("preliminary_title") or item.get("title") or item.get("file_name") or "",
                    "url": item.get("url") or "",
                }
                for item in library_items
            ],
            "removed": [],
        }

    events = agent_output.get("events") or []
    if events:
        by_type["events"] = {
            "added": [
                {
                    "title": ev.get("title") or "",
                    "url": ev.get("url") or "",
                    "start_datetime": ev.get("start_datetime"),
                    "end_datetime": ev.get("end_datetime"),
                    "timezone": ev.get("timezone"),
                    "is_full_day": ev.get("is_full_day", False),
                    "call_in_access_code": ev.get("call_in_access_code"),
                    "duration": ev.get("duration"),
                }
                for ev in events
            ],
            "removed": [],
        }

    return by_type
