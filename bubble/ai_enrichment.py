"""
AI enrichment for Bubble payloads using OpenAI.
Enriches Resources and Calendar Items with NAIC categorization, subtopic, etc.
 Safe: on OpenAI failure or invalid output, returns input unchanged and logs warning.
Runs automatically in production when OPENAI_ENABLED and conditions are met.
"""

import csv
import logging
import os
from pathlib import Path

from bubble.payload import validate_payload
from bubble.schemas import (
    CALENDAR_ITEM_SCHEMA_FIELDS,
    FULL_RESOURCE_SCHEMA_FIELDS,
)

log = logging.getLogger(__name__)

_SCHEMA_EXPORTS = Path(__file__).resolve().parent / "schema_exports"
_EXAMPLE_ROWS = 3  # 2-3 example objects per type


def _bool_env(name: str, default: bool) -> bool:
    val = os.environ.get(name, "").strip().lower()
    if not val:
        return default
    return val in ("1", "true", "yes")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _is_prod() -> bool:
    env = (os.environ.get("ENVIRONMENT") or "").strip().lower()
    return env in ("production", "prod")


# OPENAI_ENABLED: default true in prod, false in local unless explicitly set
def _openai_enabled() -> bool:
    val = os.environ.get("OPENAI_ENABLED", "").strip().lower()
    if val in ("1", "true", "yes"):
        return True
    if val in ("0", "false", "no"):
        return False
    return _is_prod()


OPENAI_ENRICH_MIN_ITEMS = _int_env("OPENAI_ENRICH_MIN_ITEMS", 1)
OPENAI_ENRICH_MAX_RESOURCES = _int_env("OPENAI_ENRICH_MAX_RESOURCES", 25)
OPENAI_ENRICH_MAX_EVENTS = _int_env("OPENAI_ENRICH_MAX_EVENTS", 10)
OPENAI_ENRICH_ONLY_IF_CHANGED = _bool_env("OPENAI_ENRICH_ONLY_IF_CHANGED", True)


def should_run_ai_enrichment(
    has_changes: bool,
    num_resources: int,
    num_events: int,
    *,
    force: bool = False,
) -> bool:
    """True if AI enrichment should run. force=True bypasses env checks (e.g. from --ai-enrich)."""
    if force:
        return (num_resources > 0 or num_events > 0) and bool(
            os.environ.get("OPENAI_API_KEY", "").strip()
        )
    if not _openai_enabled():
        return False
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        return False
    if OPENAI_ENRICH_ONLY_IF_CHANGED and not has_changes:
        return False
    total = num_resources + num_events
    if total < OPENAI_ENRICH_MIN_ITEMS:
        return False
    return True


def _load_csv_samples(csv_path: Path, max_rows: int = _EXAMPLE_ROWS) -> list[dict]:
    """
    Load CSV and return up to max_rows, each as a dict.
    Only include non-empty fields per row (filter empty strings).
    """
    if not csv_path.exists():
        return []
    rows: list[dict] = []
    with open(csv_path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        for i, row in enumerate(reader):
            if i >= max_rows:
                break
            filtered = {k: v for k, v in row.items() if k and v and str(v).strip()}
            if filtered:
                rows.append(filtered)
    return rows


def _get_resource_examples() -> list[dict]:
    """Load 2-3 example rows from resources.csv (non-empty fields only)."""
    return _load_csv_samples(_SCHEMA_EXPORTS / "resources.csv", max_rows=_EXAMPLE_ROWS)


def _get_calendar_item_examples() -> list[dict]:
    """Load 2-3 example rows from calendar_items.csv (non-empty fields only)."""
    return _load_csv_samples(_SCHEMA_EXPORTS / "calendar_items.csv", max_rows=_EXAMPLE_ROWS)


def _call_openai_for_resources(resources: list[dict], context: dict) -> list[dict]:
    """Call OpenAI to enrich resources. Returns enriched list or raises."""
    from bubble.openai_client import chat_json

    schema_fields = FULL_RESOURCE_SCHEMA_FIELDS
    examples = _get_resource_examples()
    items_ctx = context.get("items", [])
    if len(items_ctx) != len(resources):
        items_ctx = [{"org_id": None, "org_path": [], "label": "unknown", "url": ""}] * len(resources)

    system = f"""You enrich Bubble Resource objects. Return a JSON object with key "resources" containing an array of objects.
Each output object MUST have only these allowed keys: {schema_fields}
Only populate fields you can confidently infer. Leave others null or empty.
MVP fields to fill when applicable: parent (tree path), Organization (Tree Node), Type, Type1, topic suggestion (Tree Node), and any categorization from the CSV examples.
Preserve existing non-null values unless refining. Legacy/unknown fields stay null."""

    payload_text = []
    for i, (r, ctx) in enumerate(zip(resources, items_ctx)):
        payload_text.append(f"Item {i+1} (context: org_path={ctx.get('org_path')}, label={ctx.get('label')}, url={ctx.get('url')}): {r}")

    user = f"""Schema fields: {schema_fields}

Example populated rows from production (non-empty fields only):
{examples}

Resources to enrich (same order, return one object per item):
{chr(10).join(payload_text)}

Return JSON: {{"resources": [ ... ]}} with exactly {len(resources)} objects."""

    out = chat_json([{"role": "system", "content": system}, {"role": "user", "content": user}])
    enriched = out.get("resources", [])
    if not isinstance(enriched, list) or len(enriched) != len(resources):
        raise ValueError(f"Expected {len(resources)} resources, got {len(enriched) if isinstance(enriched, list) else 'non-list'}")
    return enriched


def _call_openai_for_calendar_items(items: list[dict], context: dict) -> list[dict]:
    """Call OpenAI to enrich calendar items. Returns enriched list or raises."""
    from bubble.openai_client import chat_json

    schema_fields = CALENDAR_ITEM_SCHEMA_FIELDS
    examples = _get_calendar_item_examples()
    items_ctx = context.get("items", [])
    if len(items_ctx) != len(items):
        items_ctx = [{"org_id": None, "org_path": [], "label": "unknown", "url": ""}] * len(items)

    system = f"""You enrich Bubble Calendar Item objects. Return a JSON object with key "calendar_items" containing an array of objects.
Each output object MUST have only these allowed keys: {schema_fields}
Only populate fields you can confidently infer. Leave others null or empty.
MVP fields to fill: "NAIC Group (tree node)", "NAIC Group (legacy)" (if applicable), "NAIC Date/Meeting Type", "subtopic", "has topic" (yes/no), and refine "title" if needed.
Preserve existing non-null values (Agenda, date, etc.) unless refining. Relevant Documents stays empty for MVP.
Legacy/unknown fields stay null."""

    payload_text = []
    for i, (item, ctx) in enumerate(zip(items, items_ctx)):
        payload_text.append(f"Item {i+1} (context: org_path={ctx.get('org_path')}, label={ctx.get('label')}, url={ctx.get('url')}): {item}")

    user = f"""Schema fields: {schema_fields}

Example populated rows from production (non-empty fields only):
{examples}

Calendar items to enrich (same order, return one object per item):
{chr(10).join(payload_text)}

Return JSON: {{"calendar_items": [ ... ]}} with exactly {len(items)} objects."""

    out = chat_json([{"role": "system", "content": system}, {"role": "user", "content": user}])
    enriched = out.get("calendar_items", [])
    if not isinstance(enriched, list) or len(enriched) != len(items):
        raise ValueError(f"Expected {len(items)} calendar_items, got {len(enriched) if isinstance(enriched, list) else 'non-list'}")
    return enriched


def _merge_enriched(base: dict, enriched: dict, schema_fields: list[str]) -> dict | None:
    """
    Merge AI-enriched values into base, validate, return result.
    Returns None if validation fails (caller should fall back to base).
    """
    if not isinstance(enriched, dict):
        return None
    result = dict(base)
    for k, v in enriched.items():
        if k not in schema_fields:
            continue
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        if isinstance(v, list) and not v:
            continue
        result[k] = v
    try:
        return validate_payload(schema_fields, result)
    except Exception:
        return None


def _enrich_resources_internal(
    resources: list[dict], context: dict, schema_fields: list[str]
) -> list[dict]:
    """Enrich resources via OpenAI. Validates each; on invalid, falls back to base. Returns enriched list."""
    enriched_raw = _call_openai_for_resources(resources, context)
    result: list[dict] = []
    for base, enc in zip(resources, enriched_raw):
        merged = _merge_enriched(base, enc, schema_fields)
        if merged is None:
            log.warning("AI enrichment produced invalid Resource, using original")
            result.append(base)
        else:
            result.append(merged)
    return result


def _enrich_calendar_items_internal(
    items: list[dict], context: dict, schema_fields: list[str]
) -> list[dict]:
    """Enrich calendar items via OpenAI. Validates each; on invalid, falls back to base."""
    enriched_raw = _call_openai_for_calendar_items(items, context)
    result: list[dict] = []
    for base, enc in zip(items, enriched_raw):
        merged = _merge_enriched(base, enc, schema_fields)
        if merged is None:
            log.warning("AI enrichment produced invalid Calendar Item, using original")
            result.append(base)
        else:
            result.append(merged)
    return result


def enrich_resources(resources: list[dict], context: dict) -> list[dict]:
    """
    Enrich Resource payloads using OpenAI.
    On failure, returns input unchanged and logs warning.
    """
    if not resources:
        return resources
    try:
        return _enrich_resources_internal(resources, context, FULL_RESOURCE_SCHEMA_FIELDS)
    except Exception as e:
        log.warning("AI enrichment of Resources failed, using original: %s", e)
        return resources


def enrich_calendar_items(items: list[dict], context: dict) -> list[dict]:
    """
    Enrich Calendar Item payloads using OpenAI.
    On failure, returns input unchanged and logs warning.
    """
    if not items:
        return items
    try:
        return _enrich_calendar_items_internal(items, context, CALENDAR_ITEM_SCHEMA_FIELDS)
    except Exception as e:
        log.warning("AI enrichment of Calendar Items failed, using original: %s", e)
        return items


def enrich_payloads(
    resources: list[dict],
    calendar_items: list[dict],
    resource_ctx: list[dict],
    calendar_ctx: list[dict],
    *,
    has_changes: bool = True,
    force: bool = False,
) -> tuple[list[dict], list[dict]]:
    """
    Enrich resources and calendar items when conditions are met.
    Truncates to OPENAI_ENRICH_MAX_* before API call; unenriched tail is preserved.
    Logs: AI enrichment: resources <enriched>/<total>, events <enriched>/<total>, model=..., effort=...
    """
    from bubble.openai_client import OPENAI_MODEL, REASONING_EFFORT

    nr_total, ne_total = len(resources), len(calendar_items)
    if not should_run_ai_enrichment(has_changes, nr_total, ne_total, force=force):
        return (resources, calendar_items)

    # Truncate to max (stable ordering)
    resources_to_enrich = resources[:OPENAI_ENRICH_MAX_RESOURCES]
    items_to_enrich = calendar_items[:OPENAI_ENRICH_MAX_EVENTS]
    resource_ctx_trunc = resource_ctx[: len(resources_to_enrich)]
    calendar_ctx_trunc = calendar_ctx[: len(items_to_enrich)]

    # Enrich truncated portions
    try:
        enriched_resources = _enrich_resources_internal(
            resources_to_enrich, {"items": resource_ctx_trunc}, FULL_RESOURCE_SCHEMA_FIELDS
        )
    except Exception as e:
        log.warning("AI enrichment of Resources failed, using original: %s", e)
        enriched_resources = resources_to_enrich

    try:
        enriched_items = _enrich_calendar_items_internal(
            items_to_enrich, {"items": calendar_ctx_trunc}, CALENDAR_ITEM_SCHEMA_FIELDS
        )
    except Exception as e:
        log.warning("AI enrichment of Calendar Items failed, using original: %s", e)
        enriched_items = items_to_enrich

    # Rebuild full lists: enriched prefix + unenriched tail
    result_resources = enriched_resources + resources[OPENAI_ENRICH_MAX_RESOURCES :]
    result_items = enriched_items + calendar_items[OPENAI_ENRICH_MAX_EVENTS :]

    log.info(
        "AI enrichment: resources %d/%d, events %d/%d, model=%s, effort=%s",
        len(enriched_resources),
        nr_total,
        len(enriched_items),
        ne_total,
        OPENAI_MODEL,
        REASONING_EFFORT,
    )
    return (result_resources, result_items)
