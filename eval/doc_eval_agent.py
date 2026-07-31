"""
Document extraction QA evaluation agent — one call per doc extraction row.

The QA agent receives the exact same input as the document-data-extraction agent:
  - Document title
  - Document URL
  - PDF text (fetched from library_item_url, same 12,000-char limit)
  - pgvector access (same namespaces as document_agent)
  - Org tree reference

Plus the extraction output to evaluate against that source material.
"""

import asyncio
import json
import logging
import os

log = logging.getLogger(__name__)

_CHAT_ID = "document-extraction-qa-agent"
_PDF_TEXT_LIMIT = 12000  # mirrors document_agent._PDF_TEXT_LIMIT

_FALLBACK_SYSTEM_PROMPT = """\
You are a QA evaluation agent for document extraction output. Evaluate the accuracy
of each field in the provided document extraction row against the source document content
and reference context. For each field return: score (Correct / Partially Correct / Incorrect)
and a one-sentence reasoning. Return a JSON object with a key per field.
"""

_FALLBACK_PGVECTOR_NAMESPACES = [
    "bubble-data", "art-chronicles", "art-newsreels",
    "naic-guidelines", "naic-proceedings",
    "international-guidelines", "ratings-agencies",
]

_dynamo_config: dict | None = None

_EXCLUDE_KEYS = {
    "run_id", "run_timestamp", "target_id", "source_url", "agent_call_id",
    "library_item_title", "library_item_url", "library_item_file_name",
    "eval_run_id", "eval_timestamp", "eval_scores", "eval_row_key",
    "extraction_source",
}


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


def _fetch_pdf_text(url: str) -> str | None:
    """Fetch PDF and extract text — mirrors document_agent._fetch_pdf_text exactly."""
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
            log.info("doc_eval_agent: fetched PDF text (%d chars) from %s", len(text), url[:80])
            return text.strip()
    except Exception as e:
        log.debug("doc_eval_agent: could not fetch PDF from %s: %s", url[:80], e)
    return None


def _build_user_message(row: dict, pdf_text: str | None) -> str:
    """
    Build the QA agent prompt with the exact same source input the extraction agent
    received (document title, URL, PDF text, pgvector) plus the extraction output
    to evaluate.
    """
    document_name = str(row.get("library_item_title") or row.get("document_title") or "N/A")
    document_url = str(row.get("library_item_url") or "N/A")

    extraction_json = json.dumps(
        {k: v for k, v in row.items() if k not in _EXCLUDE_KEYS},
        indent=2,
        default=str,
    )

    parts = [
        "## Source Document (same input given to the extraction agent)",
        f"Document title: {document_name}",
        f"URL: {document_url}",
    ]

    if pdf_text:
        parts.append(f"\nDocument content:\n{pdf_text[:_PDF_TEXT_LIMIT]}")
    else:
        parts.append("\n(PDF text not available — evaluate based on document title, URL, and knowledge base context)")

    from bubble.org_tree import get_org_tree
    org_tree = get_org_tree()
    if org_tree:
        parts += ["\n## Organization Reference (valid org names)", org_tree]

    parts += [
        "\n## Document Extraction Output (the row to evaluate)\n```json",
        extraction_json,
        "```",
        "\nEvaluate every field in the Document Extraction Output above against the source "
        "document provided above (the same document the extraction agent read). "
        "Return a JSON object where each key is a field name and each value is:\n"
        '{"score": "Correct" | "Partially Correct" | "Incorrect", "reasoning": "<evidence-based explanation>"}\n\n'
        "Reasoning MUST be auditable — cite specific evidence from the document content:\n"
        "- Quote or reference the source document text that supports your score\n"
        "- If the agent output is wrong, state what the correct answer should be\n"
        "- For fields not verifiable from the document (e.g. pgvector-dependent fields with no PDF), "
        "use your knowledge base search results as evidence\n\n"
        'Include an "overall_summary" key: {"correct": N, "partially_correct": N, "incorrect": N, "total": N, "pattern": "<any systematic patterns>"}'
    ]

    return "\n".join(parts)


async def _run_with_pgvector(
    system_prompt: str,
    user_content: str,
    model: str,
    reasoning_effort: str,
    namespaces: list[str],
) -> dict:
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

    from bubble.openai_client import chat_json
    messages = [
        {
            "role": "system",
            "content": (
                "You are a JSON formatter. Given the QA evaluation analysis below, produce a JSON object "
                "where each key is a field name from the evaluated document extraction row and each value is: "
                '{"score": "Correct" | "Partially Correct" | "Incorrect", "reasoning": "<evidence-based explanation>"}. '
                'Also include an "overall_summary" key: '
                '{"correct": N, "partially_correct": N, "incorrect": N, "total": N, "pattern": "<systematic patterns>"}. '
                "Use only the analysis provided — do not invent or omit scores."
            ),
        },
        {"role": "user", "content": gathered},
    ]
    return chat_json(messages, model=model, reasoning_effort=reasoning_effort)


def evaluate_doc_row(row: dict) -> dict:
    """
    Run the doc extraction QA agent on one doc extraction row.

    Fetches the source PDF (same as document_agent does) so the QA agent
    evaluates against the actual document content, not page HTML.

    Returns a dict with per-field scores and overall_summary.
    On failure returns {"error": "<message>"}.
    """
    system_prompt = _get_system_prompt()
    model = _get_model()
    reasoning_effort = _get_reasoning_effort()

    # Fetch PDF text the same way document_agent does
    document_url = str(row.get("library_item_url") or "")
    pdf_text = _fetch_pdf_text(document_url) if document_url and document_url != "N/A" else None

    user_message = _build_user_message(row, pdf_text)

    if _pgvector_enabled():
        namespaces = _get_pgvector_namespaces()
        log.info(
            "doc_eval_agent: running with pgvector (model=%s namespaces=%s) agent_call_id=%s",
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
            log.error("Doc eval agent (pgvector) failed agent_call_id=%s: %s", row.get("agent_call_id"), e)
            return {"error": str(e)}

    from bubble.openai_client import chat_json
    log.info(
        "doc_eval_agent: running via direct API (model=%s, pdf=%s) agent_call_id=%s",
        model, "yes" if pdf_text else "no", row.get("agent_call_id"),
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
        log.error("Doc eval agent failed agent_call_id=%s: %s", row.get("agent_call_id"), e)
        return {"error": str(e)}
