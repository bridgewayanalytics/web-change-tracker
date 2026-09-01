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
        # Label-as-key era → current registry keys
        "Alert Type1":                                              "alert_type",
        "Alert Title":                                              "alert_title",
        "Alert Description":                                        "alert_description",
        "Alert URL":                                                "alert_url",
        "Organization":                                             "organization",
        "Alert Date & Time (ET)":                                   "alert_date_time_et",
        "Event Title":                                              "event_title",
        "Event Start Date & Time (ET)":                             "event_start_date_time_et",
        "Event End Date & Time (ET)":                               "event_end_date_time_et",
        "Event Duration":                                           "event_duration",
        "Event is Full Day":                                        "event_is_full_day",
        "Event URL":                                                "event_url",
        "Event Call-In Number & Access Code":                       "event_call_in_number_access_code",
        "Is the Alert Relevant for an ART Newsreel article?":       "is_the_alert_relevant_for_an_art_newsreel_article",
        "Library Item Preliminary Title":                           "library_item_preliminary_title",
        "Library Item URL":                                         "library_item_url",
        "Library Items File Name":                                  "library_items_file_name",
        "Agenda Item Title & Chronicle Topics":                     "agenda_item_title_chronicle_topics",
        # Pre-registry snake_case era → current registry keys
        "alert_date_time":                                          "alert_date_time_et",
        "event_start_date_time":                                    "event_start_date_time_et",
        "event_end_date_time":                                      "event_end_date_time_et",
        "alert_datetime_et":                                        "alert_date_time_et",
        "event_start_datetime_et":                                  "event_start_date_time_et",
        "event_end_datetime_et":                                    "event_end_date_time_et",
        "event_call_in_number_and_access_code":                     "event_call_in_number_access_code",
        "event_call_in_access_code":                                "event_call_in_number_access_code",
        "agenda_items":                                             "agenda_item_title_chronicle_topics",
        "art_newsreel_relevance":                                   "is_the_alert_relevant_for_an_art_newsreel_article",
        "is_alert_relevant_for_art_newsreel":                       "is_the_alert_relevant_for_an_art_newsreel_article",
        "is_relevant_for_art_newsreel":                             "is_the_alert_relevant_for_an_art_newsreel_article",
        "event_start_datetime":                                     "event_start_date_time_et",
        "event_end_datetime":                                       "event_end_date_time_et",
        "agenda_item_title_and_chronicle_topics":                   "agenda_item_title_chronicle_topics",
        "agenda_item_official_title":                               "agenda_item_title_official",
    },
    _DOC_AGENT_ID: {
        # Lambda-era storage keys → current registry keys
        # (These are also seeded into prior_keys in the field registry after the Aug 2026 sync,
        # but kept here as a safety net for rows that bypassed normalization.)
        "data_extraction_datetime":             "data_extraction_date_time",
        "organization_or_publisher":            "organization_author",
        "agenda_items":                         "agenda_item_title_chronicle_topic",
        "agenda_items_official":                "agenda_item_title_official",
        "agenda_items_standardized_id":         "agenda_item_standardized_id",
        "meeting_or_last_comment_date":         "meeting_date_or_last_comment_date",
        "newsreel_relevance":                   "is_the_document_relevant_for_a_future_newsreel_article",
        "agenda_items_official_id":             "agenda_item_official_id",
        # Pre-lambda old field names → current registry keys
        "organization":                         "organization_author",
        "is_newsreel_relevant":                 "is_the_document_relevant_for_a_future_newsreel_article",
        "is_the_document_relevant_for_a_newsreel_article": "is_the_document_relevant_for_a_future_newsreel_article",
        "updated_or_new_document":              "existing_updated_or_new_document",
        "existing_or_new_document":             "existing_updated_or_new_document",
        "agenda_item_title":                    "agenda_item_title_chronicle_topic",
        "agenda_item_title_chronicle_topics":   "agenda_item_title_chronicle_topic",
        "agenda_item_title_and_chronicle_topic": "agenda_item_title_chronicle_topic",
        # Rename experiment variants
        "organization_author_s":                "organization_author",
        "organization_author0":                 "organization_author",
        "organization_authors":                 "organization_author",
        # Label variants (QA eval agent uses display names as score keys)
        "Organization Author":                  "organization_author",
        "Organization Author ":                 "organization_author",
        "Organization Publisher":               "organization_publisher",
        "Organization Publisher ":              "organization_publisher",
        "Agenda Item Title & Chronicle Topic":  "agenda_item_title_chronicle_topic",
        "Agenda Item Title & Chronicle Topics": "agenda_item_title_chronicle_topic",
        "Agenda Item Title - Official":         "agenda_item_title_official",
        "Agenda Item - Standardized ID":        "agenda_item_standardized_id",
        "Agenda Item - Official ID":            "agenda_item_official_id",
        "Document Description":                 "document_description",
        "Document description":                 "document_description",
        "Document Type":                        "document_type",
        "Document Title":                       "document_title",
        "Date Published":                       "date_published",
        "Meeting Date or Last Comment Date":    "meeting_date_or_last_comment_date",
        "Existing, Updated, or New Document":   "existing_updated_or_new_document",
        "Is the document relevant for a Newsreel Article?":        "is_the_document_relevant_for_a_future_newsreel_article",
        "Is the document relevant for a future Newsreel Article?": "is_the_document_relevant_for_a_future_newsreel_article",
        "Number":                               "number",
        "Document URL (Web Tracking Agent)":    "document_url_web_tracking_agent",
        "Document URL (Web Tracking Agent) ":   "document_url_web_tracking_agent",
        "Web Page URL":                         "web_page_url",
        "Web Page Url":                         "web_page_url",
    },
}

_norm_map_cache: dict[str, dict[str, str]] = {}


def _read_field_registry(agent_id: str) -> list[dict]:
    """Read field registry from chatkit_production_field_registry."""
    import os
    import boto3
    try:
        table = os.environ.get("FIELD_REGISTRY_TABLE", "chatkit_production_field_registry")
        region = os.environ.get("AWS_REGION", "us-east-1")
        dynamo = boto3.client("dynamodb", region_name=region)
        resp = dynamo.get_item(TableName=table, Key={"agent_id": {"S": agent_id}})
        item = resp.get("Item", {})
        fields_raw = item.get("fields", {})
        if "L" not in fields_raw:
            return []
        fields = []
        for f_raw in fields_raw["L"]:
            if "M" not in f_raw:
                continue
            f: dict = {}
            for k, v in f_raw["M"].items():
                if "S" in v:
                    f[k] = v["S"]
                elif "L" in v:
                    f[k] = [x.get("S", "") for x in v["L"] if "S" in x]
            fields.append(f)
        return fields
    except Exception as e:
        log.warning("field_normalizer: failed to read field registry for %s: %s", agent_id, e)
        return []


def _build_norm_map(agent_id: str) -> dict[str, str]:
    """Build {any_variant → stable_id} map from field registry + historical aliases.

    Priority (highest wins):
      2. chatkit_production_field_registry — identity, label, and prior_keys for each field
      1. _HISTORICAL_ALIASES — hardcoded safety net for very old keys not yet in prior_keys
    """
    norm: dict[str, str] = {}

    # 1. Historical aliases (lowest priority — safety net for pre-registry-era keys)
    norm.update(_HISTORICAL_ALIASES.get(agent_id, {}))

    # 2. Field registry (highest priority — authoritative current + rename history)
    for field in _read_field_registry(agent_id):
        current_key = field.get("key", "")
        if not current_key:
            continue
        norm[current_key] = current_key          # identity
        label = field.get("label", "")
        if label:
            norm[label.strip()] = current_key       # trimmed label
            norm[label.strip().lower()] = current_key  # lowercase label
        for old_key in field.get("prior_keys", []):
            norm[old_key] = current_key          # all prior keys → current

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
