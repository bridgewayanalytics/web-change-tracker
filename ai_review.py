"""
Optional AI normalization for Bubble Resource objects.

Triggered only when AI_SCHEMA_REVIEW=1. Uses OpenAI API to improve Name, notes,
optionally Date display and date. Ensures output has same length, same keys per object.
"""

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "bubble_resource_normalize.txt"
_OPENAI_MODEL = "gpt-5.2"
_REASONING_EFFORT = "medium"


def normalize_bubble_resources(resources: list[dict]) -> list[dict]:
    """
    Optionally apply AI normalization to Bubble Resource objects.

    When AI_SCHEMA_REVIEW=1 and OPENAI_API_KEY is set, calls OpenAI to improve
    Name, notes, and optionally Date display/date. Otherwise returns input unchanged.

    Guarantees: same list length, same keys per object. Logs "AI review skipped"
    if key is missing and continues.
    """
    if os.environ.get("AI_SCHEMA_REVIEW", "").strip() != "1":
        return resources

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        log.info("AI review skipped (OPENAI_API_KEY not set)")
        return resources

    if not resources:
        return resources

    try:
        return _call_openai_normalize(resources)
    except Exception as e:
        log.warning("AI review failed: %s; returning original objects", e)
        return resources


def _load_prompt() -> str:
    path = _PROMPT_PATH
    if not path.exists():
        raise FileNotFoundError(f"Prompt not found: {path}")
    return path.read_text(encoding="utf-8").strip()


def _call_openai_normalize(resources: list[dict]) -> list[dict]:
    from openai import OpenAI

    prompt_template = _load_prompt()
    input_json = json.dumps(resources, ensure_ascii=False, indent=0)
    prompt = prompt_template.replace("{{INPUT}}", input_json)

    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    kwargs: dict = {"model": _OPENAI_MODEL, "messages": [{"role": "user", "content": prompt}]}
    # GPT-5.2 reasoning: effort "medium" for moderate (optional; some clients support it)
    kwargs["reasoning"] = {"effort": _REASONING_EFFORT}

    response = client.chat.completions.create(**kwargs)
    content = (response.choices[0].message.content or "").strip()
    if not content:
        return resources

    # Strip markdown code block if present
    if content.startswith("```"):
        lines = content.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        content = "\n".join(lines)

    parsed = json.loads(content)
    if not isinstance(parsed, list):
        return resources

    # Enforce: same length, same keys per object
    result: list[dict] = []
    for i, orig in enumerate(resources):
        orig_keys = set(orig.keys())
        if i >= len(parsed):
            result.append(orig)
            continue
        item = parsed[i]
        if not isinstance(item, dict):
            result.append(orig)
            continue
        # Keep only original keys; use original values for any missing
        normalized = {k: item.get(k, orig.get(k)) for k in orig_keys}
        result.append(normalized)
    return result
