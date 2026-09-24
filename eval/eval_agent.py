"""
QA evaluation agent — one call per HTML page diff (agent_call_id group).

All alert rows produced from the same page change (same agent_call_id) are
evaluated together in a single agent call, matching the doc extraction QA
pattern. The agent receives all rows and returns per-row scores keyed by
eval_row_key.

When PGVECTOR_ENABLED=true and DB credentials are present, runs via the
OpenAI Agents SDK with pgvector tools (two-step pattern):
  Step 1 — agent searches chronicles/newsreels and produces a free-text analysis
  Step 2 — chat_json() formats the analysis into per-row, per-field JSON scores

Falls back to direct chat_json() when pgvector is unavailable.
"""

import asyncio
import json
import logging
import os

from storage.alert_schema import ALERT_PIPELINE_FIELDS

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


def _build_group_user_message(
    rows: list[dict],
    eval_row_keys: list[str],
    before_html: str,
    after_html: str,
) -> str:
    """
    Build the QA agent prompt for all alert rows from one HTML page diff.
    All rows in the group are evaluated together so the agent has full context
    and does not penalize any row for content that belongs to a sibling row.
    """
    parts = []

    from bubble.org_tree import get_org_tree
    org_tree = get_org_tree()
    if org_tree:
        parts += [
            "\n## Organization Reference (valid org names — use this to evaluate the organization field)",
            org_tree,
        ]

    source_url = (rows[0].get("source_url") or "") if rows else ""
    if source_url:
        parts += [
            f"\n## Monitored Page URL\n{source_url}",
            "This is the URL of the page that was being monitored. "
            "The alert_url field should exactly match this URL — evaluate it against this value, "
            "not against links found inside the HTML.",
        ]

    if before_html:
        parts += ["\n## Before HTML (what the page looked like before the change)", before_html]

    if after_html:
        parts += ["\n## After HTML (what the page looked like after the change)", after_html]

    parts.append(f"\n## Agent Output ({len(rows)} alert row(s) to evaluate)")
    for key, row in zip(eval_row_keys, rows):
        alert_json = json.dumps(
            {k: v for k, v in row.items() if k not in ALERT_PIPELINE_FIELDS and not k.startswith("bubble_action")},
            indent=2,
            default=str,
        )
        parts += [f"\n### Row: {key}\n```json", alert_json, "```"]

    parts.append(
        "\nEvaluate every field in each row above against the HTML snapshots and org reference. "
        "Return a JSON object with one key per eval_row_key (use the exact keys shown in the ### Row: headings above). "
        "Each value is a JSON object where each field name maps to:\n"
        '{"score": "Correct" | "Partially Correct" | "Incorrect", "reasoning": "<evidence-based explanation>"}\n\n'
        "Reasoning MUST be auditable — cite specific evidence:\n"
        "- Quote or reference the HTML or context that supports your score\n"
        "- For agenda_item_title_chronicle_topics: state what the correct chronicle topics ARE based on the HTML and any chronicles context provided\n"
        "- For is_the_alert_relevant_for_an_art_newsreel_article: cite the newsreel backend presence check result and any newsreel/chronicle mentions found\n"
        "- If the agent output is wrong, state what the correct answer should be\n\n"
        "Field-specific scoring rules:\n"
        "- library_item_preliminary_title: This is a DESCRIPTIVE preliminary title — it does not need to exactly match the HTML link text "
        "(which is often just 'Agenda', 'Materials', 'Recording', etc.). Score as Correct if the title accurately identifies the document "
        "(type, date, meeting/group) based on all available context including the filename, URL, and surrounding HTML. "
        "Only mark Partially Correct if the title is inaccurate or misleading, not merely because it elaborates beyond the link text.\n"
        "- library_items_file_name: Filenames are extracted from URLs and must be URL-decoded (spaces instead of %20 or %2520, etc.). "
        "Score decoded filenames as Correct — do NOT penalize for decoding. Only mark Incorrect/Partially Correct if the wrong file is identified.\n\n"
        'Each per-row object must include an "overall_summary" key: '
        '{"correct": N, "partially_correct": N, "incorrect": N, "total": N, "pattern": "<any systematic patterns>"}\n\n'
        "Top-level output structure:\n"
        '{"<eval_row_key>": {"<field>": {"score": ..., "reasoning": ...}, ..., "overall_summary": {...}}, ...}'
    )

    return "\n".join(parts)


def _flatten_scores(raw: dict, rows: list[dict], eval_row_keys: list[str]) -> list[dict]:
    """Convert {eval_row_key: {field_scores}} response into per-row result dicts."""
    results = []
    for key, row in zip(eval_row_keys, rows):
        scores = raw.get(key, {})
        if isinstance(scores, dict):
            overall_summary = scores.pop("overall_summary", None)
        else:
            scores = {}
            overall_summary = None
        results.append({
            "eval_row_key": key,
            "eval_scores": scores,
            "overall_summary": overall_summary,
        })
    return results


async def _run_with_pgvector(
    system_prompt: str,
    user_content: str,
    model: str,
    reasoning_effort: str,
    namespaces: list[str],
    eval_row_keys: list[str],
    field_names: list[str] | None = None,
) -> dict:
    """
    Two-step evaluation with pgvector tool access.

    Pool lifecycle is owned here — opened at entry, closed in finally. Called via
    asyncio.run() so every eval group gets a fresh event loop. This ensures asyncpg's
    native SSL cleanup runs on the same loop that created the pool, preventing heap
    corruption (double free / SIGSEGV) that occurs when background async tasks from
    the Agents SDK linger on a persistent loop and race with pool teardown.
    """
    from bubble.pgvector.client import init_pg_pool, close_pg_pool
    await init_pg_pool()
    try:
        from agents import Agent, Runner, ModelSettings
        from agents.model_settings import Reasoning
        from bubble.pgvector.search_tool import (
            set_pgvector_namespaces,
            search_knowledge_base,
            list_available_documents,
        )

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

    keys_str = ", ".join(f'"{k}"' for k in eval_row_keys)
    field_names_str = ", ".join(f'"{k}"' for k in (field_names or []))
    from bubble.openai_client import chat_json
    messages = [
        {
            "role": "system",
            "content": (
                "You are a JSON formatter. Given the QA evaluation analysis below, produce a JSON object "
                f"with exactly these top-level keys (one per evaluated row): {keys_str}. "
                "Each value is a JSON object where each field maps to: "
                '{"score": "Correct" | "Partially Correct" | "Incorrect", "reasoning": "<evidence-based explanation>"}. '
                + (
                    f"Use EXACTLY these field names as the inner score keys: {field_names_str}. "
                    "Score ALL listed fields — if the analysis did not explicitly discuss a field, "
                    "infer its score from any available evidence in the analysis. "
                    "Do NOT use display labels or human-readable names — use only the exact field names listed above. "
                    if field_names_str else ""
                ) +
                'Each per-row object must include an "overall_summary" key: '
                '{"correct": N, "partially_correct": N, "incorrect": N, "total": N, "pattern": "<systematic patterns>"}.'
            ),
        },
        {"role": "user", "content": gathered},
    ]
    return chat_json(messages, model=model, reasoning_effort=reasoning_effort)


def evaluate_group(
    rows: list[dict],
    before_html: str,
    after_html: str,
    eval_row_keys: list[str],
) -> list[dict]:
    """
    Run the eval agent on all alert rows from one HTML page diff (same agent_call_id).
    Returns a list of {eval_row_key, eval_scores, overall_summary} dicts — one per row.
    On failure, returns dicts with eval_scores={} and an error key.
    """
    if not rows:
        return []

    system_prompt = _get_system_prompt()
    model = _get_model()
    reasoning_effort = _get_reasoning_effort()
    user_message = _build_group_user_message(rows, eval_row_keys, before_html, after_html)

    # Collect all scored field names across all rows (deduped, ordered)
    seen: set[str] = set()
    field_names: list[str] = []
    for row in rows:
        for k in row:
            if k not in ALERT_PIPELINE_FIELDS and not k.startswith("bubble_action") and k not in seen:
                field_names.append(k)
                seen.add(k)

    agent_call_id = rows[0].get("agent_call_id", "unknown")

    if _pgvector_enabled():
        namespaces = _get_pgvector_namespaces()
        log.info(
            "eval_agent: running group with pgvector (model=%s namespaces=%s rows=%d) agent_call_id=%s",
            model, namespaces, len(rows), agent_call_id,
        )
        try:
            raw = asyncio.run(
                _run_with_pgvector(system_prompt, user_message, model, reasoning_effort, namespaces, eval_row_keys, field_names)
            )
            if not isinstance(raw, dict):
                return [{"eval_row_key": k, "eval_scores": {}, "error": "Agent returned non-dict response"} for k in eval_row_keys]
            return _flatten_scores(raw, rows, eval_row_keys)
        except Exception as e:
            log.error("Eval agent (pgvector) failed for agent_call_id=%s: %s", agent_call_id, e)
            return [{"eval_row_key": k, "eval_scores": {}, "error": str(e)} for k in eval_row_keys]

    # Fallback: direct chat_json
    from bubble.openai_client import chat_json
    log.info(
        "eval_agent: running group via direct API (model=%s rows=%d) agent_call_id=%s",
        model, len(rows), agent_call_id,
    )
    keys_str = ", ".join(f'"{k}"' for k in eval_row_keys)
    field_names_str = ", ".join(f'"{k}"' for k in field_names)
    direct_system = (
        system_prompt
        + f"\n\nReturn a JSON object with exactly these top-level keys: {keys_str}. "
        f"Each value is an object whose score keys are EXACTLY these field names: {field_names_str}. "
        'Each field maps to: {"score": "Correct" | "Partially Correct" | "Incorrect", "reasoning": "..."}. '
        'Also include an overall_summary key per row: {"correct": N, "partially_correct": N, "incorrect": N, "total": N, "pattern": "..."}.'
    )
    messages = [
        {"role": "system", "content": direct_system},
        {"role": "user", "content": user_message},
    ]
    try:
        raw = chat_json(messages, model=model, reasoning_effort=reasoning_effort)
        if not isinstance(raw, dict):
            return [{"eval_row_key": k, "eval_scores": {}, "error": "Agent returned non-dict response"} for k in eval_row_keys]
        return _flatten_scores(raw, rows, eval_row_keys)
    except Exception as e:
        log.error("Eval agent failed for agent_call_id=%s: %s", agent_call_id, e)
        return [{"eval_row_key": k, "eval_scores": {}, "error": str(e)} for k in eval_row_keys]
