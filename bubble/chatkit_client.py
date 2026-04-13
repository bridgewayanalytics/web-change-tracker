"""
Client for the document-data-extraction chat agent.

Replaces the in-house agenda item matching and Chronicle topic suggestion logic
with a call to the external chatkit API, which has access to vector stores
containing Bubble data, Chronicle topics, and NAIC guidelines.

Configuration:
    CHATKIT_API_URL   - Base URL of the chatkit API (default: https://chat-api.bridgewayanalytics.com)
    CHATKIT_JWT_TOKEN - JWT bearer token for authentication (TODO: obtain via service account)

The agent returns JSON containing suggested Chronicle topic IDs and agenda item IDs.
If the response is not useful or the call fails, empty values are returned and
the resource fields are left blank.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import httpx

log = logging.getLogger(__name__)

CHATKIT_API_URL = os.environ.get("CHATKIT_API_URL", "https://chat-api.bridgewayanalytics.com")
CHATKIT_JWT_TOKEN = os.environ.get("CHATKIT_JWT_TOKEN", "")
CHATKIT_CHAT_ID = "document-data-extraction"

# Timeout for the chatkit API call (seconds)
CHATKIT_TIMEOUT = float(os.environ.get("CHATKIT_TIMEOUT", "60"))


def extract_document_data(
    document_name: str,
    document_url: str,
    pdf_text: str | None = None,
) -> dict[str, Any]:
    """
    Call the document-data-extraction chat agent for a detected document.

    Sends document metadata (and optionally extracted text signals) to the agent,
    which uses vector store search across Bubble data and Chronicle topics to
    identify the most relevant agenda items and Chronicle topics.

    Returns dict with:
        topic_ids          - list of Chronicle topic Bubble IDs (usually 0-2 items)
        agenda_item_ids    - list of agenda item Bubble IDs (usually 0-5 items)

    Returns empty dict if:
        - CHATKIT_JWT_TOKEN is not configured
        - The API call fails
        - The response contains no recognisable structured data
    """
    if not CHATKIT_JWT_TOKEN:
        log.warning("chatkit: CHATKIT_JWT_TOKEN not set — skipping document data extraction")
        return {}

    try:
        response_text = _call_chatkit(document_name, document_url, pdf_text)
        result = _parse_response(response_text)
        return result
    except Exception as e:
        log.warning(
            "chatkit: extraction failed for '%s': %s",
            (document_name or "")[:60], e,
        )
        return {}


def _build_message(
    document_name: str,
    document_url: str,
    pdf_text: str | None,
) -> str:
    """Build the user message to send to the chat agent."""
    lines = [
        f"Document title: {document_name}",
        f"URL: {document_url}",
    ]
    if pdf_text:
        lines.append(f"\nKey content signals:\n{pdf_text[:3000]}")
    lines.append(
        "\nPlease identify the most relevant Chronicle topic(s) and any related "
        "agenda items for this document. Return your response as JSON."
    )
    return "\n".join(lines)


def _call_chatkit(
    document_name: str,
    document_url: str,
    pdf_text: str | None,
) -> str:
    """POST to the chatkit API and collect the full response text from the SSE stream."""
    headers = {
        "Authorization": f"Bearer {CHATKIT_JWT_TOKEN}",
        "X-Chat-Id": CHATKIT_CHAT_ID,
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    payload = {
        "messages": [
            {
                "role": "user",
                "content": _build_message(document_name, document_url, pdf_text),
            }
        ]
    }

    collected: list[str] = []

    with httpx.Client(timeout=CHATKIT_TIMEOUT) as client:
        with client.stream(
            "POST",
            f"{CHATKIT_API_URL}/knowledge/chatkit",
            headers=headers,
            json=payload,
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data.strip() == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                    # OpenAI-style SSE delta
                    delta = (
                        event.get("choices", [{}])[0]
                        .get("delta", {})
                        .get("content", "")
                    )
                    if delta:
                        collected.append(delta)
                except (json.JSONDecodeError, IndexError, KeyError, TypeError):
                    pass

    return "".join(collected)


def _parse_response(response_text: str) -> dict[str, Any]:
    """
    Extract Chronicle topic IDs and agenda item IDs from the agent's JSON response.

    Accepts various key names since the Bubble field naming is in flux.
    Returns empty dict if no useful structured data is found.
    """
    if not response_text:
        return {}

    # Extract the first JSON object from the response
    json_match = re.search(r"\{[\s\S]*\}", response_text)
    if not json_match:
        log.debug("chatkit: no JSON block found in response")
        return {}

    try:
        data = json.loads(json_match.group(0))
    except json.JSONDecodeError:
        log.debug("chatkit: could not parse JSON from response")
        return {}

    result: dict[str, Any] = {}

    # Extract Chronicle topic IDs — accept various key names
    for key in ("chronicle_topics", "topic_ids", "topics", "chronicleTopics", "chronicle_topic_ids"):
        if key in data:
            raw = data[key]
            if isinstance(raw, list) and raw:
                ids = [
                    str(item["id"]) if isinstance(item, dict) and "id" in item else str(item)
                    for item in raw
                    if item
                ]
                if ids:
                    result["topic_ids"] = ids
            break

    # Extract agenda item IDs — accept various key names
    for key in ("agenda_items", "agenda_item_ids", "agendaItems", "agenda_item_suggestions"):
        if key in data:
            raw = data[key]
            if isinstance(raw, list) and raw:
                ids = [
                    str(item["id"]) if isinstance(item, dict) and "id" in item else str(item)
                    for item in raw
                    if item
                ]
                if ids:
                    result["agenda_item_ids"] = ids
            break

    if result:
        log.info(
            "chatkit: extracted %d topic(s), %d agenda item(s)",
            len(result.get("topic_ids", [])),
            len(result.get("agenda_item_ids", [])),
        )
    else:
        log.debug("chatkit: response contained no recognisable topic or agenda item IDs")

    return result
