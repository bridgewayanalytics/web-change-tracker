"""
QA evaluation agent — one call per alert row.

When PGVECTOR_ENABLED=true and DB credentials are present, runs via the
OpenAI Agents SDK with pgvector tools (same two-step pattern as
document_agent.py): Step 1 searches the knowledge base and gathers
evidence; Step 2 formats the analysis into per-field JSON scores.

Falls back to direct chat_json (with pre-fetched reference_context) when
pgvector is unavailable.
"""

import asyncio
import json
import logging
import os

log = logging.getLogger(__name__)

_CHAT_ID = "web-extraction-qa-agent"

_FALLBACK_SYSTEM_PROMPT = """\
You are a QA evaluation agent. Evaluate the accuracy of each field in the
provided alert output against the source HTML and reference content.
For each field return: score (Correct / Partially Correct / Incorrect) and
a one-sentence reasoning. Return a JSON object with a key per field.
"""

_FALLBACK_PGVECTOR_NAMESPACES = ["ba:chronicles", "ba:newsreels", "newsreel-generation:ART"]

_dynamo_config: dict | None = None


def _load_config() -> dict:
    global _dynamo_config
    if _dynamo_config is None:
        from config.chatkit_config import get_chat_config
        _dynamo_config = get_chat_config(_CHAT_ID)
    return _dynamo_config


def _get_system_prompt() -> str:
    cfg = _load_config()
    return cfg.get("instructions") or _FALLBACK_SYSTEM_PROMPT


def _get_model() -> str:
    cfg = _load_config()
    return cfg.get("model") or "gpt-5.4"


def _get_reasoning_effort() -> str:
    cfg = _load_config()
    return cfg.get("reasoning_effort") or "low"


def _get_pgvector_namespaces() -> list[str]:
    cfg = _load_config()
    ns = cfg.get("pgvector_namespaces")
    if isinstance(ns, list):
        return ns
    return _FALLBACK_PGVECTOR_NAMESPACES


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


def _build_sibling_summary(sibling_rows: list[dict]) -> str:
    lines = []
    for i, r in enumerate(sibling_rows, 1):
        lib_title = r.get("library_item_preliminary_title") or {}
        if isinstance(lib_title, dict):
            lib_title = lib_title.get("title") or ""
        lines.append(
            f"  Row {i}: alert_type={r.get('alert_type')} | "
            f"library_item={lib_title or r.get('library_items_file_name') or 'N/A'} | "
            f"library_item_url={r.get('library_item_url') or 'N/A'}"
        )
    return "\n".join(lines)


def _build_user_message(
    row: dict,
    before_html: str,
    after_html: str,
    reference_context: str,
    sibling_rows: list[dict] | None = None,
) -> str:
    _EXCLUDE_KEYS = {"ingest_status", "bubble_sync_status", "bubble_sync_error"}
    alert_json = json.dumps(
        {k: v for k, v in row.items() if k not in _EXCLUDE_KEYS and not k.startswith("bubble_action")},
        indent=2,
        default=str,
    )

    parts = [
        "## Agent Output (the alert row to evaluate)\n```json",
        alert_json,
        "```",
    ]

    if sibling_rows:
        parts += [
            f"\n## Sibling Rows from the Same Run ({len(sibling_rows)} other row(s))",
            "This alert is one of multiple rows produced from the same HTML change. "
            "The following rows cover the other documents or items detected in the same page update. "
            "Score ONLY the primary row above. Do NOT penalize it for content that appears on a sibling row.",
            _build_sibling_summary(sibling_rows),
        ]

    if before_html:
        parts += ["\n## Before HTML (what the page looked like before the change)", before_html]

    if after_html:
        parts += ["\n## After HTML (what the page looked like after the change)", after_html]

    if reference_context:
        parts += ["\n" + reference_context]

    parts.append(
        "\nEvaluate every field in the Agent Output above against the HTML snapshots and reference context provided. "
        "Return a JSON object where each key is a field name and each value is:\n"
        '{"score": "Correct" | "Partially Correct" | "Incorrect", "reasoning": "<evidence-based explanation>"}\n\n'
        "Reasoning MUST be auditable — cite specific evidence:\n"
        "- Quote or reference the HTML or context that supports your score\n"
        "- For agenda_item_title_chronicle_topics: state what the correct chronicle topics ARE based on the HTML and any chronicles context provided, not just whether the agent got them right\n"
        "- For is_the_alert_relevant_for_an_art_newsreel_article: cite the newsreel backend presence check result and any newsreel/chronicle mentions found — explain the reasoning behind relevance or non-relevance\n"
        "- If the agent output is wrong, state what the correct answer should be\n\n"
        'Include an "overall_summary" key: {"correct": N, "partially_correct": N, "incorrect": N, "total": N, "pattern": "<any systematic patterns>"}'
    )

    return "\n".join(parts)


async def _run_with_pgvector(
    system_prompt: str,
    user_content: str,
    model: str,
    reasoning_effort: str,
    namespaces: list[str],
) -> dict:
    """
    Two-step evaluation with pgvector tool access:
      1. Agents SDK run — agent searches chronicles, newsreels, and ART documents
         as needed, then writes a free-text evaluation analysis.
      2. chat_json() call — formats the free-text into structured per-field scores.
    """
    from agents import Agent, Runner, ModelSettings
    from agents.model_settings import Reasoning
    from bubble.pgvector.client import init_pg_pool, close_pg_pool
    from bubble.pgvector.search_tool import (
        set_pgvector_namespaces,
        search_knowledge_base,
        list_available_documents,
    )

    await init_pg_pool()
    try:
        set_pgvector_namespaces(namespaces)
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
        return {"error": "Agent returned empty output"}

    # Step 2: format gathered analysis into structured per-field JSON scores
    from bubble.openai_client import chat_json
    messages = [
        {
            "role": "system",
            "content": (
                "You are a JSON formatter. Given the QA evaluation analysis below, produce a JSON object "
                "where each key is a field name from the evaluated alert and each value is: "
                '{"score": "Correct" | "Partially Correct" | "Incorrect", "reasoning": "<evidence-based explanation>"}. '
                'Also include an "overall_summary" key: '
                '{"correct": N, "partially_correct": N, "incorrect": N, "total": N, "pattern": "<systematic patterns>"}. '
                "Use only the analysis provided — do not invent or omit scores."
            ),
        },
        {"role": "user", "content": gathered},
    ]
    return chat_json(messages, model=model, reasoning_effort=reasoning_effort)


def evaluate_row(
    row: dict,
    before_html: str,
    after_html: str,
    reference_context: str,
    sibling_rows: list[dict] | None = None,
) -> dict:
    """
    Run the eval agent on one alert row.
    Returns a dict with per-field scores and overall_summary.
    On failure returns {"error": "<message>"}.
    """
    system_prompt = _get_system_prompt()
    model = _get_model()
    reasoning_effort = _get_reasoning_effort()
    user_message = _build_user_message(row, before_html, after_html, reference_context, sibling_rows)

    if _pgvector_enabled():
        namespaces = _get_pgvector_namespaces()
        log.info(
            "eval_agent: running with pgvector tools (model=%s namespaces=%s) for agent_call_id=%s",
            model, namespaces, row.get("agent_call_id"),
        )
        try:
            result = asyncio.run(
                _run_with_pgvector(system_prompt, user_message, model, reasoning_effort, namespaces)
            )
            if not isinstance(result, dict):
                return {"error": "Agent returned non-dict response"}
            return result
        except Exception as e:
            log.error("Eval agent (pgvector path) failed for agent_call_id=%s: %s", row.get("agent_call_id"), e)
            return {"error": str(e)}

    # Fallback: direct chat_json with pre-fetched reference_context
    from bubble.openai_client import chat_json
    log.info(
        "eval_agent: running via direct API (model=%s) for agent_call_id=%s",
        model, row.get("agent_call_id"),
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    try:
        result = chat_json(messages, model=model, reasoning_effort=reasoning_effort)
        if not isinstance(result, dict):
            return {"error": "Agent returned non-dict response"}
        return result
    except Exception as e:
        log.error("Eval agent failed for agent_call_id=%s: %s", row.get("agent_call_id"), e)
        return {"error": str(e)}
