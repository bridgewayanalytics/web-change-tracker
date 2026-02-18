"""
OpenAI client for Bubble AI enrichment.
Reads OPENAI_API_KEY from env. Uses Responses API with ChatGPT 5 and reasoning (moderate).
Model and effort configurable via env (e.g. from SSM in prod).
"""

import json
import os

# Model and effort: env overrides for SSM-injected values in prod
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5")
REASONING_EFFORT = os.environ.get("OPENAI_REASONING_EFFORT", "medium")

# Models that support reasoning.effort (gpt-5, o-series)
_REASONING_MODELS = ("gpt-5.2", "gpt-5.1", "gpt-5", "o1", "o1-mini", "o3", "o4-mini")


def _get_client():
    """Lazy import to avoid failing when openai not installed or key missing."""
    try:
        from openai import OpenAI
    except ImportError:
        raise ImportError("openai package required for AI enrichment. pip install openai")
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise ValueError("OPENAI_API_KEY environment variable must be set for AI enrichment")
    return OpenAI(api_key=key)


def _messages_to_input(messages: list[dict]) -> list[dict]:
    """Convert Chat-style messages to Responses API input items."""
    result = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, str):
            payload = [{"type": "input_text", "text": content}]
        else:
            payload = content
        result.append({"type": "message", "role": role, "content": payload})
    return result


def _extract_text_from_response(response) -> str:
    """Extract output text from Responses API response."""
    output = getattr(response, "output", None) or []
    for item in output:
        if getattr(item, "type", None) == "message":
            content = getattr(item, "content", None) or []
            for part in content:
                if getattr(part, "type", None) == "output_text":
                    return getattr(part, "text", "") or ""
    return ""


def chat_json(
    messages: list[dict],
    *,
    model: str | None = None,
    reasoning_effort: str = REASONING_EFFORT,
) -> dict:
    """
    Call OpenAI Responses API with JSON-only output. Uses reasoning (moderate).

    Args:
        messages: Chat messages (system + user) with role and content
        model: Override default model
        reasoning_effort: "low" | "medium" | "high" for reasoning models

    Returns:
        Parsed JSON object from response content

    Raises:
        ValueError: Missing API key or empty response
        Exception: OpenAI API errors
    """
    client = _get_client()
    model = model or OPENAI_MODEL
    input_items = _messages_to_input(messages)

    kwargs: dict = {
        "model": model,
        "input": input_items,
        "text": {"format": {"type": "json_object"}},
    }
    if any(model.startswith(m) or model == m for m in _REASONING_MODELS):
        kwargs["reasoning"] = {"effort": reasoning_effort}

    response = client.responses.create(**kwargs)
    content = _extract_text_from_response(response)
    if not content or not content.strip():
        raise ValueError("Empty response from OpenAI")
    return json.loads(content)
