"""
Normalize JSONL row field keys to stable IDs at write time.

Every JSONL row (alerts, doc extractions, QA eval scores) is written through
normalize_row_keys() or normalize_score_keys() before hitting S3.  This ensures
the dashboard can always do a direct row[col] lookup against the _column_registry
stable ID — no alias guessing, no schema-version branching.

The normalization map is built per-agent from three sources (lowest → highest priority):
  1. _HISTORICAL_ALIASES — hardcoded list of every known pre-DynamoDB rename
  2. _field_aliases in DynamoDB — detected renames written by the validate_config_sync Lambda
  3. _column_registry in DynamoDB — each {id, label} pair adds identity + label variants

Unknown keys not in the map pass through unchanged (forward compatibility).
Pipeline metadata keys (run_id, agent_call_id, etc.) are always skipped.
"""

import logging

log = logging.getLogger(__name__)

_WEB_AGENT_ID = "web-tracking-agent"
_DOC_AGENT_ID = "document-data-extraction"

# Historical aliases not captured in DynamoDB _field_aliases.
# Ported from LEGACY_ALIASES (AlertsTable.tsx) and DOC_FIELD_ALIASES (DocExtractionsTable.tsx).
# Format: {old_key: stable_id}  — resolved to the FINAL stable ID, not a chain.
_HISTORICAL_ALIASES: dict[str, dict[str, str]] = {
    _WEB_AGENT_ID: {
        # Label-as-key era → current stable IDs
        "Alert Type1":                                              "alert_type",
        "Alert Title":                                              "alert_title",
        "Alert Description":                                        "alert_description",
        "Alert URL":                                                "alert_url",
        "Organization":                                             "organization",
        "Alert Date & Time (ET)":                                   "alert_date_time",
        "Event Title":                                              "event_title",
        "Event Start Date & Time (ET)":                             "event_start_date_time",
        "Event End Date & Time (ET)":                               "event_end_date_time",
        "Event Duration":                                           "event_duration",
        "Event is Full Day":                                        "event_is_full_day",
        "Event URL":                                                "event_url",
        "Event Call-In Number & Access Code":                       "event_call_in_number_access_code",
        "Is the Alert Relevant for an ART Newsreel article?":       "is_the_alert_relevant_for_an_art_newsreel_article",
        "Library Item Preliminary Title":                           "library_item_preliminary_title",
        "Library Item URL":                                         "library_item_url",
        "Library Items File Name":                                  "library_items_file_name",
        "Agenda Item Title & Chronicle Topics":                     "agenda_item_title_chronicle_topics",
        # Intermediate stable ID era → current stable IDs
        "alert_datetime_et":                                        "alert_date_time",
        "event_start_datetime_et":                                  "event_start_date_time",
        "event_end_datetime_et":                                    "event_end_date_time",
        "event_call_in_number_and_access_code":                     "event_call_in_number_access_code",
        "agenda_items":                                             "agenda_item_title_chronicle_topics",
        "art_newsreel_relevance":                                   "is_the_alert_relevant_for_an_art_newsreel_article",
        "is_alert_relevant_for_art_newsreel":                       "is_the_alert_relevant_for_an_art_newsreel_article",
        "is_relevant_for_art_newsreel":                             "is_the_alert_relevant_for_an_art_newsreel_article",
        # Even older intermediate names
        "event_start_datetime":                                     "event_start_date_time",
        "event_end_datetime":                                       "event_end_date_time",
        "event_call_in_access_code":                                "event_call_in_number_access_code",
        "agenda_item_title_and_chronicle_topics":                   "agenda_item_title_chronicle_topics",
        "agenda_item_official_title":                               "agenda_item_title_official",
    },
    _DOC_AGENT_ID: {
        # Old data field names → current stable IDs
        "organization":                             "organization_or_publisher",
        "meeting_date_or_last_comment_date":        "meeting_or_last_comment_date",
        "updated_or_new_document":                  "existing_updated_or_new_document",
        "existing_or_new_document":                 "existing_updated_or_new_document",
        "agenda_item_title":                        "agenda_items",
        "agenda_item_title_official":               "agenda_items_official",
        "agenda_item_standardized_id":              "agenda_items_standardized_id",
        "agenda_item_official_id":                  "agenda_items_official_id",
        "is_newsreel_relevant":                     "newsreel_relevance",
        # Label variants from QA eval scores (LLM used display names as keys)
        "Organization Author":                      "organization_or_publisher",
        "Organization Author ":                     "organization_or_publisher",
        "organization_author":                      "organization_or_publisher",
        "Organization Publisher":                   "organization_publisher",
        "Organization Publisher ":                  "organization_publisher",
        "Agenda Item Title & Chronicle Topic":      "agenda_items",
        "Agenda Item Title & Chronicle Topics":     "agenda_items",
        "agenda_item_title_chronicle_topic":        "agenda_items",
        "agenda_item_title_and_chronicle_topic":    "agenda_items",
        "agenda_item_title_chronicle_topics":       "agenda_items",
        "Agenda Item Title - Official":             "agenda_items_official",
        "Agenda Item - Standardized ID":            "agenda_items_standardized_id",
        "Agenda Item - Official ID":                "agenda_items_official_id",
        "Document Description":                     "document_description",
        "Document description":                     "document_description",
        "Document Type":                            "document_type",
        "Document Title":                           "document_title",
        "Date Published":                           "date_published",
        "Meeting Date or Last Comment Date":        "meeting_or_last_comment_date",
        "Existing, Updated, or New Document":       "existing_updated_or_new_document",
        "Is the document relevant for a Newsreel Article?":         "newsreel_relevance",
        "Is the document relevant for a future Newsreel Article?":  "newsreel_relevance",
        "Number":                                   "number",
        "Document URL (Web Tracking Agent)":        "document_url_web_tracking_agent",
        "Document URL (Web Tracking Agent) ":       "document_url_web_tracking_agent",
        "Web Page URL":                             "web_page_url",
        "Web Page Url":                             "web_page_url",
    },
}

_norm_map_cache: dict[str, dict[str, str]] = {}


def _build_norm_map(agent_id: str) -> dict[str, str]:
    """Build {any_variant → stable_id} map from DynamoDB + historical aliases."""
    from config.chatkit_config import get_chat_config
    cfg = get_chat_config(agent_id)
    norm: dict[str, str] = {}

    # 1. Historical aliases (lowest priority)
    norm.update(_HISTORICAL_ALIASES.get(agent_id, {}))

    # 2. _field_aliases from DynamoDB: stored as {stable_id: old_key} — invert
    for stable_id, old_key in (cfg.get("_field_aliases") or {}).items():
        if isinstance(old_key, str):
            norm[old_key] = stable_id

    # 3. _column_registry: identity + label variants (highest priority)
    for entry in (cfg.get("_column_registry") or []):
        if not isinstance(entry, dict):
            continue
        sid = entry.get("id")
        label = entry.get("label") or ""
        if not sid:
            continue
        norm[sid] = sid                      # identity — stable ID maps to itself
        norm[label.strip()] = sid            # trimmed label
        norm[label.strip().lower()] = sid    # lowercase label

    log.debug("field_normalizer: built norm_map for %s with %d entries", agent_id, len(norm))
    return norm


def get_alert_norm_map() -> dict[str, str]:
    """Return cached normalization map for the web-tracking-agent."""
    if _WEB_AGENT_ID not in _norm_map_cache:
        _norm_map_cache[_WEB_AGENT_ID] = _build_norm_map(_WEB_AGENT_ID)
    return _norm_map_cache[_WEB_AGENT_ID]


def get_doc_norm_map() -> dict[str, str]:
    """Return cached normalization map for the document-data-extraction agent."""
    if _DOC_AGENT_ID not in _norm_map_cache:
        _norm_map_cache[_DOC_AGENT_ID] = _build_norm_map(_DOC_AGENT_ID)
    return _norm_map_cache[_DOC_AGENT_ID]


def normalize_row_keys(
    row: dict,
    norm_map: dict[str, str],
    skip_keys: frozenset,
) -> dict:
    """
    Rewrite row keys to stable IDs.

    - Keys in skip_keys (pipeline metadata) pass through unchanged.
    - Keys found in norm_map are rewritten to their stable ID.
    - Unknown keys pass through as-is (forward compatibility).
    - On collision (two source keys map to the same stable ID), the first
      value wins (preserves the row's original field order priority).
    """
    result: dict = {}
    seen_stable: set = set()
    for key, val in row.items():
        if key in skip_keys:
            result[key] = val
            continue
        stable = norm_map.get(key, key)
        if stable in seen_stable:
            log.debug("field_normalizer: dropping duplicate key %r → %r (already set)", key, stable)
            continue
        result[stable] = val
        seen_stable.add(stable)
    return result


def normalize_score_keys(
    scores: dict,
    norm_map: dict[str, str],
) -> dict:
    """
    Rewrite eval_scores dict keys to stable field IDs.

    'overall_summary' always passes through. On collision, first value wins.
    """
    result: dict = {}
    seen_stable: set = set()
    for key, val in scores.items():
        if key == "overall_summary":
            result[key] = val
            seen_stable.add(key)
            continue
        stable = norm_map.get(key) or norm_map.get(key.strip()) or key
        if stable in seen_stable:
            log.debug("field_normalizer: dropping duplicate score key %r → %r", key, stable)
            continue
        result[stable] = val
        seen_stable.add(stable)
    return result
