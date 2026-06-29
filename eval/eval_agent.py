"""
QA evaluation agent — one call per alert row.

Loads system instructions from DynamoDB (chat:eval-agent), same pattern
as document_agent.py. Passes the alert output, before/after HTML, and
reference context. Returns a dict of per-field scores and an overall summary.
"""

import json
import logging

log = logging.getLogger(__name__)

_CHAT_ID = "eval-agent"

_FALLBACK_SYSTEM_PROMPT = """\
You are a QA evaluation agent. Evaluate the accuracy of each field in the
provided alert output against the source HTML and reference content.
For each field return: score (Correct / Partially Correct / Incorrect) and
a one-sentence reasoning. Return a JSON object with a key per field.
"""

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


def _build_user_message(
    row: dict,
    before_html: str,
    after_html: str,
    reference_context: str,
) -> str:
    alert_json = json.dumps(
        {k: v for k, v in row.items() if not k.startswith("bubble_action")},
        indent=2,
        default=str,
    )

    parts = [
        "## Agent Output (the alert to evaluate)\n```json",
        alert_json,
        "```",
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
        "- For agenda_item_title_chronicle_topics: state what the correct chronicle topics ARE based on the Bubble ground truth and chronicles context, not just whether the agent got them right\n"
        "- For is_the_alert_relevant_for_an_art_newsreel_article: cite the newsreel backend presence check result and any newsreel/chronicle mentions found — explain the reasoning behind relevance or non-relevance\n"
        "- If the agent output is wrong, state what the correct answer should be\n\n"
        'Include an "overall_summary" key: {"correct": N, "partially_correct": N, "incorrect": N, "total": N, "pattern": "<any systematic patterns>"}'
    )

    return "\n".join(parts)


def evaluate_row(
    row: dict,
    before_html: str,
    after_html: str,
    reference_context: str,
) -> dict:
    """
    Run the eval agent on one alert row.
    Returns a dict with per-field scores and overall_summary.
    On failure returns {"error": "<message>"}.
    """
    from bubble.openai_client import chat_json

    system_prompt = _get_system_prompt()
    model = _get_model()
    reasoning_effort = _get_reasoning_effort()
    user_message = _build_user_message(row, before_html, after_html, reference_context)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    try:
        result = chat_json(
            messages,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        if not isinstance(result, dict):
            return {"error": "Agent returned non-dict response"}
        return result
    except Exception as e:
        log.error("Eval agent failed for agent_call_id=%s: %s", row.get("agent_call_id"), e)
        return {"error": str(e)}
