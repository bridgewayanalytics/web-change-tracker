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

from storage.doc_schema import DOC_PIPELINE_FIELDS

log = logging.getLogger(__name__)

_CHAT_ID = "document-extraction-qa-agent"

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

# Pipeline-owned fields are excluded from QA scoring — see storage/doc_schema.py
_EXCLUDE_KEYS = DOC_PIPELINE_FIELDS


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


def _fetch_single_pdf(url: str) -> str | None:
    """Fetch and extract text from a single PDF URL. Returns None on any failure."""
    url = url.strip()
    if not url or not url.lower().split("?")[0].endswith(".pdf"):
        log.info("doc_eval_agent: skipping non-PDF URL %s", url[:80])
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
            log.info("doc_eval_agent: fetched PDF text (%d chars) from %s", len(text), url[:80])
            return text.strip()
        log.warning("doc_eval_agent: PDF fetched but extracted no text from %s", url[:80])
    except Exception as e:
        log.warning("doc_eval_agent: could not fetch PDF from %s: %s", url[:80], e)
    return None


def _fetch_pdf_text(url: str) -> str | None:
    """Fetch PDF text — mirrors document_agent._fetch_pdf_text exactly.
    Tries each semicolon-separated URL in order; returns first successful result.
    Does NOT concatenate — one QA call covers exactly one document."""
    if not url:
        return None
    candidates = [u.strip() for u in url.split(";") if u.strip()]
    for candidate in candidates:
        text = _fetch_single_pdf(candidate)
        if text:
            return text
    return None


_ALERT_INCLUDE_KEYS = {"organization", "alert_type", "event_title", "event_start_date_time", "source_url"}
_HTML_CONTEXT_LIMIT = 8000  # chars per before/after HTML snippet

_NA_VALUES = frozenset({"N/A", "N/A.", "-", ""})


def _extract_std_id(val: object) -> str:
    """Extract plain standardized_id string from list-of-dicts or plain string."""
    if isinstance(val, list) and val:
        first = val[0]
        return str(first.get("standardized_id", "") if isinstance(first, dict) else first).strip()
    return str(val or "").strip()


def _extract_agenda_title(val: object) -> str:
    """Extract plain agenda_item_title string from list-of-dicts or plain string."""
    if isinstance(val, list) and val:
        first = val[0]
        return str(first.get("agenda_item_title", "") if isinstance(first, dict) else first).strip()
    return str(val or "").strip()


def _extract_official_title(val: object) -> str:
    """Extract official_title string from list-of-dicts or plain string."""
    if isinstance(val, list) and val:
        first = val[0]
        return str(first.get("official_title", "") if isinstance(first, dict) else first).strip()
    return str(val or "").strip()


def make_doc_eval_row_key(row: dict) -> str:
    """Stable per-row eval key: call_id | url_filename | discriminator.

    The URL filename is included to prevent collision when the same agent_call_id
    produces rows for multiple documents that happen to share a std_id.
    Discriminator priority: std_id → agenda title → official title → number → bare.
    Handles both list-of-dicts (post-normalization) and plain string (old rows)."""
    call_id = row.get("agent_call_id", "unknown")

    lib_url = (row.get("library_item_url") or "").strip()
    url_slug = lib_url.rstrip("/").split("/")[-1].split("?")[0] if lib_url and lib_url.upper() not in _NA_VALUES else ""

    def _k(*parts: str) -> str:
        segments = [call_id] + [p for p in parts if p]
        return "|".join(segments)

    std_id = _extract_std_id(
        row.get("agenda_item_standardized_id") or row.get("agenda_items_standardized_id")
    )
    if std_id and std_id.upper() not in _NA_VALUES:
        return _k(url_slug, std_id)
    title = _extract_agenda_title(
        row.get("agenda_item_title_chronicle_topic")
        or row.get("agenda_item_bridgeway_title_chronicle_topic")
        or row.get("agenda_item_title")
        or row.get("agenda_items")
    )
    if title and title.upper() not in _NA_VALUES:
        return _k(url_slug, title)
    official = _extract_official_title(row.get("agenda_item_title_official"))
    if official and official.upper() not in _NA_VALUES:
        return _k(url_slug, official)
    number = str(row.get("number") or "").strip()
    if number and number.upper() not in _NA_VALUES and number != "0":
        return _k(url_slug, f"item_{number}")
    return _k(url_slug)


def _build_user_message(
    rows: list[dict],
    eval_row_keys: list[str],
    alert_row: dict | None = None,
    before_html: str | None = None,
    after_html: str | None = None,
) -> str:
    """
    Build the QA agent prompt for one or more agenda item rows from the same extraction call.
    PDF content is accessed exclusively via pgvector search — never injected directly into the prompt.
    """
    first = rows[0]
    document_name = str(first.get("library_item_title") or first.get("document_title") or "N/A")
    document_url = str(first.get("library_item_url") or "N/A")

    parts = [
        "## Source Document",
        f"Document title: {document_name}",
        f"URL: {document_url}",
        "(Full document content is available via your knowledge base search tools — search for specific sections as needed to verify each field.)",
    ]

    if alert_row:
        alert_context = {k: v for k, v in alert_row.items() if k in _ALERT_INCLUDE_KEYS}
        parts += [
            "\n## Alert Context (the pipeline alert that triggered this extraction)",
            json.dumps(alert_context, indent=2, default=str),
        ]

    if before_html or after_html:
        parts.append("\n## Page Change Context (HTML snapshots of the monitored page)")
        if before_html:
            parts.append(f"Before HTML (prior state):\n{before_html[:_HTML_CONTEXT_LIMIT]}")
        if after_html:
            parts.append(f"After HTML (new state with the document now present):\n{after_html[:_HTML_CONTEXT_LIMIT]}")

    from bubble.org_tree import get_org_tree
    org_tree = get_org_tree()
    if org_tree:
        parts += ["\n## Organization Reference (valid org names)", org_tree]

    parts.append(f"\n## Document Extraction Output ({len(rows)} agenda item row(s) to evaluate)")
    for key, row in zip(eval_row_keys, rows):
        extraction_json = json.dumps(
            {k: v for k, v in row.items() if k not in _EXCLUDE_KEYS},
            indent=2,
            default=str,
        )
        parts += [f"\n### Row: {key}\n```json", extraction_json, "```"]

    parts += [
        "\nEvaluate every field in each row above against the source document. "
        "Return a JSON object with one key per eval_row_key. Each value is itself a JSON object "
        "where each field name maps to:\n"
        '{"score": "Correct" | "Partially Correct" | "Incorrect", "reasoning": "<evidence-based explanation>"}\n\n'
        "Reasoning MUST be auditable — cite specific evidence from the document content:\n"
        "- Quote or reference the source document text that supports your score\n"
        "- If the agent output is wrong, state what the correct answer should be\n\n"
        'Each per-row object must also include an "overall_summary" key: '
        '{"correct": N, "partially_correct": N, "incorrect": N, "total": N, "pattern": "<any systematic patterns>"}\n\n'
        "Top-level output structure:\n"
        '{"<eval_row_key>": {"<field>": {"score": ..., "reasoning": ...}, ..., "overall_summary": {...}}, ...}'
    ]

    return "\n".join(parts)


async def _run_with_pgvector(
    system_prompt: str,
    user_content: str,
    model: str,
    reasoning_effort: str,
    namespaces: list[str],
    eval_row_keys: list[str],
    field_names: list[str] | None = None,
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
                    f"Use EXACTLY these field names as the inner score keys (same as the extraction JSON field names): {field_names_str}. "
                    "Do NOT use display labels or human-readable names — use only the exact field names listed above. "
                    if field_names_str else ""
                ) +
                'Each per-row object must also include an "overall_summary" key: '
                '{"correct": N, "partially_correct": N, "incorrect": N, "total": N, "pattern": "<systematic patterns>"}.'
            ),
        },
        {"role": "user", "content": gathered},
    ]
    return chat_json(messages, model=model, reasoning_effort=reasoning_effort)


def evaluate_doc_extraction_call(rows: list[dict], alert_row: dict | None = None) -> list[dict]:
    """
    Run the doc extraction QA agent on all agenda item rows from one extraction call.

    All rows share the same agent_call_id and library_item_url (same document).
    Returns a list of per-row result dicts, each including eval_scores and overall_summary.
    On failure returns [{"error": "<message>"}].
    """
    if not rows:
        return []

    system_prompt = _get_system_prompt()
    model = _get_model()
    reasoning_effort = _get_reasoning_effort()

    # Fetch PDF text for vectorization only — never injected into the prompt.
    # The agent accesses document content exclusively via pgvector search tools.
    first = rows[0]
    document_url = str(first.get("library_item_url") or "")
    pdf_text = _fetch_pdf_text(document_url) if document_url and document_url != "N/A" else None
    log.info(
        "doc_eval_agent: PDF for vectorization agent_call_id=%s url=%s — %s",
        first.get("agent_call_id", "unknown"),
        document_url[:80],
        f"{len(pdf_text)} chars fetched" if pdf_text else "FAILED — will use cached namespace if available",
    )

    # Fetch before/after HTML from S3 using the first row's run metadata
    before_html, after_html = "", ""
    run_id = str(first.get("run_id") or "")
    target_id = str(first.get("target_id") or "")
    run_timestamp = first.get("run_timestamp")
    if run_id and target_id and run_timestamp:
        try:
            from storage.page_change_s3 import fetch_page_html
            if isinstance(run_timestamp, str):
                from datetime import datetime, timezone
                run_timestamp = datetime.fromisoformat(run_timestamp).timestamp()
            before_html, after_html = fetch_page_html(run_id, target_id, run_timestamp)
            if before_html or after_html:
                log.info("doc_eval_agent: fetched page HTML (before=%d chars, after=%d chars) for run_id=%s", len(before_html), len(after_html), run_id)
        except Exception as e:
            log.debug("doc_eval_agent: could not fetch page HTML: %s", e)

    # Build stable per-row keys for the agent to reference
    eval_row_keys = [make_doc_eval_row_key(row) for row in rows]

    user_message = _build_user_message(
        rows, eval_row_keys,
        alert_row=alert_row, before_html=before_html, after_html=after_html,
    )

    agent_call_id = first.get("agent_call_id", "unknown")

    # Collect the actual field names from the rows (excluding pipeline metadata).
    # Passed to the formatter so scores are keyed by field ID, not display labels.
    seen_fields: set[str] = set()
    field_names: list[str] = []
    for row in rows:
        for k in row.keys():
            if k not in _EXCLUDE_KEYS and k not in seen_fields:
                field_names.append(k)
                seen_fields.add(k)

    if _pgvector_enabled():
        namespaces = list(_get_pgvector_namespaces())
        if document_url and document_url.strip() and document_url != "N/A":
            from bubble.doc_extraction_ingest import ingest_and_arm
            doc_namespace = ingest_and_arm(document_url, pdf_text, str(first.get("library_item_title") or ""))
            if doc_namespace:
                namespaces.append(doc_namespace)
                log.info("doc_eval_agent: armed document namespace %s", doc_namespace)

        log.info(
            "doc_eval_agent: running with pgvector (model=%s namespaces=%s) agent_call_id=%s rows=%d",
            model, namespaces, agent_call_id, len(rows),
        )
        try:
            raw = asyncio.run(
                _run_with_pgvector(system_prompt, user_message, model, reasoning_effort, namespaces, eval_row_keys, field_names)
            )
            if not isinstance(raw, dict):
                return [{"error": "Agent returned non-dict response"}]
            return _flatten_scores(raw, rows, eval_row_keys)
        except Exception as e:
            log.error("Doc eval agent (pgvector) failed agent_call_id=%s: %s", agent_call_id, e)
            return [{"error": str(e)}]

    from bubble.openai_client import chat_json
    log.info(
        "doc_eval_agent: running via direct API (model=%s, pdf=%s) agent_call_id=%s rows=%d",
        model, "yes" if pdf_text else "no", agent_call_id, len(rows),
    )
    keys_str = ", ".join(f'"{k}"' for k in eval_row_keys)
    field_names_str = ", ".join(f'"{k}"' for k in field_names)
    direct_system_prompt = (
        system_prompt
        + f"\n\nReturn a JSON object with exactly these top-level keys: {keys_str}. "
        f"Each value is an object whose score keys are EXACTLY these field names (same as the extraction JSON, not display labels): {field_names_str}. "
        "Each field maps to: {\"score\": \"Correct\" | \"Partially Correct\" | \"Incorrect\", \"reasoning\": \"...\"}. "
        "Also include an overall_summary key per row."
    )
    messages = [
        {"role": "system", "content": direct_system_prompt},
        {"role": "user", "content": user_message},
    ]
    try:
        raw = chat_json(messages, model=model, reasoning_effort=reasoning_effort)
        if not isinstance(raw, dict):
            return [{"error": "Agent returned non-dict response"}]
        return _flatten_scores(raw, rows, eval_row_keys)
    except Exception as e:
        log.error("Doc eval agent failed agent_call_id=%s: %s", agent_call_id, e)
        return [{"error": str(e)}]


def _flatten_scores(raw: dict, rows: list[dict], eval_row_keys: list[str]) -> list[dict]:
    """Convert the agent's {eval_row_key: {field_scores}} response into per-row result dicts."""
    results = []
    for key, row in zip(eval_row_keys, rows):
        scores = raw.get(key, {})
        overall_summary = scores.pop("overall_summary", None) if isinstance(scores, dict) else None
        results.append({
            "eval_row_key": key,
            "eval_scores": scores,
            "overall_summary": overall_summary,
        })
    return results
