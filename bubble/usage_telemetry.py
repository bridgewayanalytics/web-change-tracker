"""
Fire-and-forget OpenAI usage telemetry.

POSTs one row per agent call to the ChatKit usage ingest endpoint so web-tracker
spend appears on the admin Costs page alongside other services. See the contract
in docs/usage-telemetry.md (or the Usage Telemetry section of CLAUDE.md).

Allowed surfaces: "web_tracking_agent", "document_data_extraction"

Any POST failure is logged at DEBUG and swallowed — telemetry must never break
an agent run.
"""
import logging
import os
import threading

log = logging.getLogger(__name__)

_CHAT_API_BASE = os.environ.get("CHAT_API_BASE", "https://chat-api.bridgewayanalytics.com")
_ENDPOINT = f"{_CHAT_API_BASE}/admin/usage/llm"


def _api_key() -> str:
    return os.environ.get("CHATKIT_INTERNAL_API_KEY", "").strip()


def post_usage(
    surface: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    cached_input_tokens: int = 0,
    reasoning_tokens: int = 0,
    requests_count: int = 1,
    meta: dict | None = None,
) -> None:
    """Post usage in a background daemon thread — never raises, never blocks the caller."""
    key = _api_key()
    if not key:
        log.debug("usage_telemetry: CHATKIT_INTERNAL_API_KEY not set — skipping")
        return
    if not input_tokens and not output_tokens:
        return

    payload: dict = {
        "surface": surface,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "requests": requests_count,
    }
    if cached_input_tokens:
        payload["cached_input_tokens"] = cached_input_tokens
    if reasoning_tokens:
        payload["reasoning_tokens"] = reasoning_tokens
    if meta:
        payload["meta"] = meta

    def _send() -> None:
        try:
            import requests as _req
            r = _req.post(
                _ENDPOINT,
                json=payload,
                headers={"x-api-key": key, "Content-Type": "application/json"},
                timeout=10,
            )
            if r.status_code == 200:
                log.info(
                    "usage_telemetry: recorded surface=%s model=%s in=%d out=%d cached=%d reasoning=%d",
                    surface, model, input_tokens, output_tokens,
                    cached_input_tokens, reasoning_tokens,
                )
            else:
                log.warning(
                    "usage_telemetry: non-200 from endpoint: %s %s",
                    r.status_code, r.text[:200],
                )
        except Exception as exc:
            log.debug("usage_telemetry: POST failed (non-fatal): %s", exc)

    threading.Thread(target=_send, daemon=True).start()


def extract_agents_sdk_usage(run_result) -> dict:
    """Sum token counts across all raw_responses in an Agents SDK RunResult."""
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "reasoning_tokens": 0,
        "requests": 0,
    }
    for resp in getattr(run_result, "raw_responses", []) or []:
        usage = getattr(resp, "usage", None)
        if not usage:
            continue
        totals["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
        totals["output_tokens"] += getattr(usage, "output_tokens", 0) or 0
        in_det = getattr(usage, "input_tokens_details", None)
        if in_det:
            totals["cached_input_tokens"] += getattr(in_det, "cached_tokens", 0) or 0
        out_det = getattr(usage, "output_tokens_details", None)
        if out_det:
            totals["reasoning_tokens"] += getattr(out_det, "reasoning_tokens", 0) or 0
        totals["requests"] += 1
    return totals


def extract_responses_api_usage(response) -> dict:
    """Extract token counts from a single Responses API response object."""
    usage = getattr(response, "usage", None)
    if not usage:
        return {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0, "reasoning_tokens": 0}
    in_det = getattr(usage, "input_tokens_details", None)
    out_det = getattr(usage, "output_tokens_details", None)
    return {
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cached_input_tokens": (getattr(in_det, "cached_tokens", 0) or 0) if in_det else 0,
        "reasoning_tokens": (getattr(out_det, "reasoning_tokens", 0) or 0) if out_det else 0,
    }
