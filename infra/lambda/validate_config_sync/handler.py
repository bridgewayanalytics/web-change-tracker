"""
DynamoDB Streams Lambda: auto-validate Bubble syncs to chatkit_production_config.

Triggered on every write to the config table. Detects and corrects:
1. Label count mismatches (output_requested_values vs output_json_schema.required)
2. Garbage labels (instruction text leaked into column headers)
3. Garbage schema property keys (ChatKit generates fake props from instruction text)
4. Column registry (stable snake_case IDs, immutable across renames)
5. Schema normalization (rewrites output_json_schema property keys to stable IDs)

Writes corrections back in-place. Uses _last_validated_at timestamp to prevent
infinite trigger loops (Lambda's own correction write re-triggers the stream).
"""

import json
import logging
import re
import time
from datetime import datetime, timezone

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

TABLE = "chatkit_production_config"
WATCHED_KEYS = frozenset({
    "chat:web-tracking-agent",
    "chat:document-data-extraction",
})

# Maps each config key to its top-level array wrapper key.
# If Bubble sync removes the wrapper, the Lambda re-adds it.
WRAPPER_KEY_MAP: dict[str, str] = {
    "chat:web-tracking-agent": "alerts",
    "chat:document-data-extraction": "agenda_items",
}

# If we validated this row within this many seconds, skip (prevents loops)
DEBOUNCE_SECONDS = 10

# Labels longer than this are likely garbage (instruction text leaked in)
MAX_LABEL_LENGTH = 80

# Canonical human-readable labels for fields whose keys can't be derived cleanly
# (e.g. keys that would lose "&" or other meaningful characters via .title()).
# Used by _fix_label_count when padding missing labels.
CANONICAL_LABELS: dict[str, str] = {
    "alert_date_time": "Alert Date & Time",
    "event_start_date_time": "Event Start Date & Time",
    "event_end_date_time": "Event End Date & Time",
    "event_call_in_number_access_code": "Event Call-In Number & Access Code",
    "agenda_item_title_chronicle_topics": "Agenda Item Title & Chronicle Topics",
}

# Phrases that indicate a label is actually instruction text
GARBAGE_PHRASES = [
    "report",
    "org_tree",
    "if the",
    "organization is not",
    "use the",
    "provide the",
    "vector store",
]

# Initial stable snake_case IDs for each config, ordered by column position.
# Used to bootstrap _column_registry on first sync.
INITIAL_REGISTRIES = {
    "chat:web-tracking-agent": [
        "alert_type", "alert_title", "alert_description", "alert_url",
        "organization", "alert_date_time", "event_title",
        "event_start_date_time", "event_end_date_time", "event_duration",
        "event_is_full_day", "event_url", "event_call_in_number_access_code",
        "agenda_item_title_chronicle_topics",
        "agenda_item_title_official", "agenda_item_standardized_id", "agenda_item_official_id",
        "library_item_preliminary_title", "library_item_url",
        "library_items_file_name", "is_the_alert_relevant_for_an_art_newsreel_article",
    ],
    "chat:document-data-extraction": [
        "number", "data_extraction_date_time", "document_description",
        "organization_author", "organization_publisher",
        "document_title", "document_title_source", "document_type",
        "date_published", "meeting_date_or_last_comment_date",
        "existing_updated_or_new_document",
        "agenda_item_title", "chronicle_topics",
        "agenda_item_title_official", "agenda_item_standardized_id", "agenda_item_official_id",
        "relevant_for_future_newsreel_article",
        "document_url_web_tracking_agent", "web_page_url",
    ],
}

# Pipeline columns: fields stamped by the pipeline at known label positions in
# output_requested_values. These are NOT in the agent output schema but are valid
# display columns. {config_key: {label_position: stable_id}}
_PIPELINE_COLUMN_POSITIONS: dict[str, dict[int, str]] = {
    "chat:document-data-extraction": {
        1: "data_extraction_datetime",
    },
}

STRUCTURAL_SCHEMAS: dict = {
    "organization": {
        "type": "array",
        "description": "Organization",
        "items": {"type": "string"},
    },
    "agenda_item_title_chronicle_topics": {
        "type": "array",
        "description": "Agenda Item Title & Chronicle Topics",
        "minItems": 1,
        "items": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "status": {"type": "string", "enum": ["New", "Existing", "N/A"]},
                "agenda_item_title": {"type": "string"},
                "chronicle_topics": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["status", "agenda_item_title", "chronicle_topics"],
        },
    },
    "agenda_item_title_official": {
        "type": "array",
        "description": "Agenda Item Title - Official",
        "minItems": 1,
        "items": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "status": {"type": "string", "enum": ["New", "Existing", "N/A"]},
                "official_title": {"type": "string"},
            },
            "required": ["status", "official_title"],
        },
    },
    "agenda_item_standardized_id": {
        "type": "array",
        "description": "Agenda Item - Standardized ID",
        "minItems": 1,
        "items": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "status": {"type": "string", "enum": ["New", "Existing", "N/A"]},
                "standardized_id": {"type": "string"},
            },
            "required": ["status", "standardized_id"],
        },
    },
    "agenda_item_official_id": {
        "type": "array",
        "description": "Agenda Item - Official ID",
        "minItems": 1,
        "items": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "status": {"type": "string", "enum": ["New", "Existing", "N/A"]},
                "official_id": {"type": "string"},
            },
            "required": ["status", "official_id"],
        },
    },
    "library_item_preliminary_title": {
        "type": "object",
        "description": "Library Item Preliminary Title",
        "additionalProperties": False,
        "properties": {
            "status": {"type": "string", "enum": ["New", "Updated", "Existing", "Old", "N/A"]},
            "title": {"type": "string"},
        },
        "required": ["status", "title"],
    },
    "is_the_alert_relevant_for_an_art_newsreel_article": {
        "type": "object",
        "description": "Is the Alert Relevant for an ART Newsreel article?",
        "additionalProperties": False,
        "properties": {
            "status": {"type": "string", "enum": ["Yes", "No", "Additional review needed"]},
            "details": {"type": "string"},
        },
        "required": ["status", "details"],
    },
    # document-data-extraction structural fields
    "chronicle_topics": {
        "type": "array",
        "description": "Chronicle Topics",
        "items": {"type": "string"},
    },
    "organization_publisher": {
        "type": "array",
        "description": "Organization Publisher",
        "items": {"type": "string"},
    },
    "organization_or_publisher": {
        "type": "object",
        "description": "Organization or Publisher",
        "additionalProperties": False,
        "properties": {
            "status": {"type": "string", "enum": ["Listed", "NEW ORGANIZATION"]},
            "name": {"type": "string"},
        },
        "required": ["status", "name"],
    },
    "is_the_document_relevant_for_a_newsreel_article": {
        "type": "object",
        "description": "Is the document relevant for a Newsreel Article?",
        "additionalProperties": False,
        "properties": {
            "status": {"type": "string", "enum": ["Yes", "No", "Additional review needed"]},
            "explanation_or_reference": {"type": "string"},
        },
        "required": ["status", "explanation_or_reference"],
    },
}

dynamo = boto3.client("dynamodb")


# ---------------------------------------------------------------------------
# DynamoDB deserialization helpers
# ---------------------------------------------------------------------------

def _deser(val):
    """Recursively deserialize a DynamoDB typed value to plain Python."""
    if "S" in val:
        return val["S"]
    if "N" in val:
        return float(val["N"]) if "." in val["N"] else int(val["N"])
    if "BOOL" in val:
        return val["BOOL"]
    if "NULL" in val:
        return None
    if "L" in val:
        return [_deser(v) for v in val["L"]]
    if "M" in val:
        return {k: _deser(v) for k, v in val["M"].items()}
    return None


def _ser_str_list(lst):
    """Serialize a list of strings to DynamoDB L type."""
    return {"L": [{"S": s} for s in lst]}


def _ser_str_map(m):
    """Serialize a dict of string→string to DynamoDB M type."""
    return {"M": {k: {"S": v} for k, v in m.items()}}


def _ser_val(val):
    """Recursively serialize a plain Python value to DynamoDB typed form."""
    if val is None:
        return {"NULL": True}
    if isinstance(val, bool):
        return {"BOOL": val}
    if isinstance(val, int):
        return {"N": str(val)}
    if isinstance(val, float):
        return {"N": str(val)}
    if isinstance(val, str):
        return {"S": val}
    if isinstance(val, list):
        return {"L": [_ser_val(v) for v in val]}
    if isinstance(val, dict):
        return {"M": {k: _ser_val(v) for k, v in val.items()}}
    return {"S": str(val)}


def _ser_registry(registry):
    """Serialize registry list of {id, label} dicts to DynamoDB L type."""
    return {"L": [{"M": {"id": {"S": e["id"]}, "label": {"S": e.get("label", "")}}} for e in registry]}


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

def _schema_inner(schema):
    """Return the inner schema (drilling through array wrapper if present)."""
    if not isinstance(schema, dict):
        return schema
    props = schema.get("properties", {})
    for wrapper_key in WRAPPER_KEY_MAP.values():
        if wrapper_key in props:
            wrapper_prop = props.get(wrapper_key, {})
            if isinstance(wrapper_prop, dict):
                items = wrapper_prop.get("items")
                if isinstance(items, dict):
                    return items
    return schema


def _extract_required_keys(image):
    """Extract the ordered required keys from output_json_schema.

    Drills through the alerts array wrapper if present, returning the
    inner schema's required fields (not the top-level ["alerts"] key).
    """
    raw = image.get("output_json_schema")
    if not raw:
        return []
    schema = _deser(raw)
    if not isinstance(schema, dict):
        return []
    return _schema_inner(schema).get("required", [])


_WRAPPER_DESCRIPTIONS: dict[str, str] = {
    "alerts": "List of alerts detected in the page change",
    "agenda_items": "List of extracted agenda items, one per array element",
}


def _wrap_flat_schema(flat_schema, wrapper_key):
    """Wrap a flat schema in the given wrapper array for multi-row support."""
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            wrapper_key: {
                "type": "array",
                "description": _WRAPPER_DESCRIPTIONS.get(wrapper_key, f"List of {wrapper_key}"),
                "items": flat_schema,
            }
        },
        "required": [wrapper_key],
    }


def _extract_full_schema(image):
    """Deserialize and return the full output_json_schema dict, or None."""
    raw = image.get("output_json_schema")
    if not raw:
        return None
    schema = _deser(raw)
    return schema if isinstance(schema, dict) else None


def _extract_labels(image):
    """Extract output_requested_values as a list of strings."""
    raw = image.get("output_requested_values")
    if not raw:
        return []
    result = _deser(raw)
    return result if isinstance(result, list) else []


def _extract_str_list(image, field):
    """Extract a string list field from the image."""
    raw = image.get(field)
    if not raw:
        return []
    result = _deser(raw)
    return result if isinstance(result, list) else []


def _extract_str_map(image, field):
    """Extract a string→string map field from the image."""
    raw = image.get(field)
    if not raw:
        return {}
    result = _deser(raw)
    return result if isinstance(result, dict) else {}


def _extract_column_registry(image):
    """Extract _column_registry as list of {id, label} dicts."""
    raw = image.get("_column_registry")
    if not raw:
        return []
    result = _deser(raw)
    if not isinstance(result, list):
        return []
    return [r for r in result if isinstance(r, dict) and "id" in r]


# ---------------------------------------------------------------------------
# Validation logic
# ---------------------------------------------------------------------------

def _is_garbage_label(label):
    """Heuristic: is this label actually instruction text, not a column name?"""
    if not isinstance(label, str):
        return True
    if len(label) > MAX_LABEL_LENGTH:
        return True
    lower = label.lower()
    return any(phrase in lower for phrase in GARBAGE_PHRASES)


def _remove_garbage_labels(labels):
    """Remove garbage labels, return (cleaned, removed_indices)."""
    cleaned = []
    removed = []
    for i, label in enumerate(labels):
        if _is_garbage_label(label):
            removed.append((i, label))
        else:
            cleaned.append(label)
    return cleaned, removed


def _fix_label_count(labels, required_keys):
    """Ensure labels list has at least len(required_keys) entries.

    Extra labels beyond required_keys are preserved — they correspond to pipeline
    metadata columns (e.g. data_extraction_datetime) that are valid display columns
    in the dashboard but are not in the agent output schema.
    If too few: pad with canonical or title-cased key names.
    """
    labels, _ = _remove_garbage_labels(labels)

    if len(labels) < len(required_keys):
        for i in range(len(labels), len(required_keys)):
            key = required_keys[i]
            label = CANONICAL_LABELS.get(key) or key.replace("_", " ").title()
            labels.append(label)

    return labels


def _remove_garbage_schema_keys(schema):
    """Remove garbage property keys from a JSON Schema object (and its required array).

    ChatKit's label-extraction regex can pick up instruction text (e.g.
    'If the organization is not in org_tree.txt...') as a fake field name.
    GPT-4.1 then creates a schema property for it.  This function removes
    those properties from the schema in-place, keeping the schema valid.

    Works on both flat schemas and schemas wrapped in an alerts array.
    Returns (cleaned_schema, list_of_removed_keys).
    """
    def _clean_object_schema(obj):
        if not isinstance(obj, dict) or obj.get("type") != "object":
            return obj, []
        properties = obj.get("properties", {})
        required = obj.get("required", [])
        removed = []
        clean_props = {}
        for key, val in properties.items():
            # Convert snake_case key to spaced form for garbage detection
            readable = key.replace("_", " ")
            if _is_garbage_label(readable) or _is_garbage_label(key):
                removed.append(key)
            else:
                clean_props[key] = val
        if not removed:
            return obj, []
        clean_required = [k for k in required if k not in removed]
        cleaned = {**obj, "properties": clean_props, "required": clean_required}
        return cleaned, removed

    if not isinstance(schema, dict):
        return schema, []

    # Handle array wrapper
    props = schema.get("properties", {})
    for wrapper_key in WRAPPER_KEY_MAP.values():
        if wrapper_key in props:
            wrapper_prop = props[wrapper_key]
            items = wrapper_prop.get("items") if isinstance(wrapper_prop, dict) else None
            if isinstance(items, dict):
                cleaned_items, removed = _clean_object_schema(items)
                if removed:
                    new_wrapper = {**wrapper_prop, "items": cleaned_items}
                    return {**schema, "properties": {**props, wrapper_key: new_wrapper}}, removed
            return schema, []

    # Flat schema
    return _clean_object_schema(schema)


def _label_to_id(label):
    """Normalize a human-readable label to a stable snake_case ID."""
    s = label.lower()
    s = re.sub(r'[^a-z0-9\s]', ' ', s)
    s = re.sub(r'\s+', '_', s.strip())
    s = re.sub(r'_+', '_', s)
    return s.strip('_') or 'field'


def _update_column_registry(registry, new_labels, config_key, required_keys=None):
    """
    Update registry using new_labels from output_requested_values.
    Matching priority:
    0. Key-based match — if required_keys provided, match registry entries by stable ID
       directly. Unambiguous for reorders; immune to stale registry labels.
    1. Exact label match against existing registry entries — reorder-safe: a field
       stays bound to its stable ID even if Bubble moved it to a different position.
    2. Positional assignment for unmatched labels — covers field renames (old label
       gone, new label not yet in registry; stable ID is inherited by position).
    3. New entry — genuinely new field, stable ID derived from label or INITIAL_REGISTRIES.
    Returns (updated_registry, changed).
    """
    initial_ids = INITIAL_REGISTRIES.get(config_key, [])
    existing_ids = {e["id"] for e in registry}

    matched_at: dict = {}   # new_labels index → registry entry
    matched_ids: set = set()

    existing_by_id = {e["id"]: e for e in registry}

    # Pass 0: key-based match using required_keys (stable IDs) — handles reorders correctly
    # even when the stored registry labels are stale or wrong.
    if required_keys:
        for i, key in enumerate(required_keys):
            entry = existing_by_id.get(key)
            if entry is not None and entry["id"] not in matched_ids:
                matched_at[i] = dict(entry)
                matched_ids.add(entry["id"])

    # Pass 1: match incoming labels to registry entries by exact label text.
    # Strip trailing/leading whitespace when building the lookup dict and when
    # querying it — Bubble writes labels with trailing spaces (e.g. "Org Author ")
    # but the registry may store them without, causing Pass 1 misses and fallback
    # to positional assignment which corrupts stable ID → field bindings.
    existing_by_label = {e["label"].strip(): e for e in registry}
    for i, label in enumerate(new_labels):
        if i in matched_at:
            continue  # already matched by key
        entry = existing_by_label.get(label.strip())
        if entry is not None and entry["id"] not in matched_ids:
            matched_at[i] = dict(entry)
            matched_ids.add(entry["id"])

    # Unmatched registry entries (renamed or removed), preserved in original order
    unmatched_registry = [e for e in registry if e["id"] not in matched_ids]
    unmatched_idx = 0

    updated = []
    for i, label in enumerate(new_labels):
        if i in matched_at:
            entry = matched_at[i]
            # Propagate label changes from output_requested_values so that Bubble Admin
            # renames immediately reflect in the registry (and therefore the dashboard).
            if label.strip() and entry.get("label") != label:
                entry["label"] = label
            updated.append(entry)
        elif unmatched_idx < len(unmatched_registry):
            # Positional: inherit the stable ID of the next unmatched registry entry
            entry = dict(unmatched_registry[unmatched_idx])
            unmatched_idx += 1
            if entry.get("label") != label:
                entry["label"] = label
            updated.append(entry)
        else:
            # New column: use hardcoded initial ID if available, else derive from label
            if i < len(initial_ids):
                new_id = initial_ids[i]
            else:
                base_id = _label_to_id(label)
                new_id = base_id
                suffix = 1
                while new_id in existing_ids:
                    new_id = f"{base_id}_{suffix}"
                    suffix += 1
            existing_ids.add(new_id)
            updated.append({"id": new_id, "label": label})

    # Enforce canonical labels only when the stored label looks auto-generated from the
    # field key (i.e. matches key.replace("_"," ").title()). Intentional admin renames
    # (e.g. "Alert Date + Time") don't match that pattern and pass through unchanged.
    for entry in updated:
        canonical = CANONICAL_LABELS.get(entry["id"])
        key_derived = entry["id"].replace("_", " ").title()
        if canonical and entry.get("label") == key_derived:
            entry["label"] = canonical

    changed = updated != registry
    return updated, changed


def _normalize_schema_with_registry(schema, registry):
    """
    Rewrite output_json_schema using stable IDs from registry as property keys,
    replacing Bubble's human-readable label-as-key names.
    Works on both flat schemas and schemas wrapped in an alerts array.
    Returns (normalized_schema, detected_renames) where detected_renames is a
    dict mapping unstable_key -> stable_id for any keys that were remapped.
    The caller should invert this to {stable_id: unstable_key} before writing to
    _field_aliases, so the dashboard can resolve old rows stored under the unstable key.
    """
    if not isinstance(schema, dict) or not registry:
        return schema, {}

    label_to_id = {e["label"].strip(): e["id"] for e in registry}
    id_set = {e["id"] for e in registry}

    def _normalize_object(obj):
        if not isinstance(obj, dict) or obj.get("type") != "object":
            return obj, False, {}
        properties = obj.get("properties", {})
        required = obj.get("required", [])
        new_required = []
        new_props = {}
        changed = False
        detected = {}  # unstable_key -> stable_id

        # Deduplicate required before positional processing — duplicates cause
        # the positional fallback to assign wrong schemas to later registry fields
        seen_keys = set()
        deduped = []
        for key in required:
            if key not in seen_keys:
                seen_keys.add(key)
                deduped.append(key)
        if len(deduped) < len(required):
            changed = True
        required = deduped

        for i, key in enumerate(required):
            # Priority 1: key IS already a stable ID (Bubble writes snake_case which matches)
            # Priority 2: key matches a human-readable label (rare, but handles edge cases)
            # Priority 3: positional fallback for completely unrecognized keys
            stable = (key if key in id_set else None) or label_to_id.get(key)
            if not stable and i < len(registry):
                pos_id = registry[i]["id"]
                if pos_id != key:
                    stable = pos_id
                    changed = True
                else:
                    stable = key
            if stable and stable != key:
                changed = True
                detected[key] = stable  # record: old_key -> stable_id
            elif not stable:
                # Key not in registry at all — drop it to prevent schema bloat
                changed = True
                continue
            new_required.append(stable)
            src_key = key if key in properties else stable if stable in properties else None
            if src_key:
                new_props[stable] = properties[src_key]

        # Ensure every registry field is present — add missing ones with string fallback
        present = set(new_required)
        for entry in registry:
            if entry["id"] not in present:
                new_required.append(entry["id"])
                new_props[entry["id"]] = new_props.get(entry["id"]) or {"type": "string"}
                changed = True

        if not changed:
            return obj, False, {}
        return {**obj, "properties": new_props, "required": new_required}, True, detected

    # Handle array wrapper
    props = schema.get("properties", {})
    for wrapper_key in WRAPPER_KEY_MAP.values():
        if wrapper_key in props:
            wrapper_prop = props[wrapper_key]
            if isinstance(wrapper_prop, dict) and isinstance(wrapper_prop.get("items"), dict):
                new_items, changed, detected = _normalize_object(wrapper_prop["items"])
                if changed:
                    return {**schema, "properties": {**props, wrapper_key: {**wrapper_prop, "items": new_items}}}, detected
            return schema, {}

    # Flat schema
    new_schema, changed, detected = _normalize_object(schema)
    return (new_schema if changed else schema), detected


def _enforce_field_types(schema):
    """Enforce correct types on all fields after key normalization.

    Structural fields are overwritten with STRUCTURAL_SCHEMAS definitions.
    Everything else is forced to {"type": "string"} if it isn't already.
    Works on both flat schemas and alerts-wrapped schemas.
    Returns (corrected_schema, corrections_list).
    """
    def _enforce_on_object(obj):
        if not isinstance(obj, dict) or obj.get("type") != "object":
            return obj, []
        props = dict(obj.get("properties", {}))
        required = obj.get("required", [])
        fixes = []
        changed = False
        for field_id in required:
            correct = STRUCTURAL_SCHEMAS.get(field_id)
            if correct is not None:
                if props.get(field_id) != correct:
                    props[field_id] = correct
                    fixes.append(f"enforced structural type on {field_id}")
                    changed = True
            else:
                current = props.get(field_id, {})
                if current.get("type") != "string":
                    props[field_id] = {
                        "type": "string",
                        "description": current.get("description", field_id),
                    }
                    fixes.append(f"forced string type on {field_id} (was {current.get('type', '?')})")
                    changed = True
        if not changed:
            return obj, []
        return {**obj, "properties": props}, fixes

    if not isinstance(schema, dict):
        return schema, []

    # Handle array wrapper
    s_props = schema.get("properties", {})
    for wrapper_key in WRAPPER_KEY_MAP.values():
        if wrapper_key in s_props:
            wrapper_prop = s_props[wrapper_key]
            if isinstance(wrapper_prop, dict) and isinstance(wrapper_prop.get("items"), dict):
                new_items, fixes = _enforce_on_object(wrapper_prop["items"])
                if fixes:
                    new_wrapper = {**wrapper_prop, "items": new_items}
                    return {**schema, "properties": {**s_props, wrapper_key: new_wrapper}}, fixes
            return schema, []

    return _enforce_on_object(schema)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def handler(event, context):
    for record in event.get("Records", []):
        if record["eventName"] not in ("MODIFY", "INSERT"):
            continue

        new_image = record["dynamodb"].get("NewImage", {})
        old_image = record["dynamodb"].get("OldImage", {})
        config_key = new_image.get("config_key", {}).get("S", "")

        if config_key not in WATCHED_KEYS:
            continue

        # Debounce: skip if we just validated this row — but ONLY when output_requested_values
        # hasn't changed. When the user edits labels in Bubble Admin, output_requested_values
        # changes between OldImage and NewImage, so we must process it even if recently validated.
        # The debounce only needs to block the Lambda's own writes (registry + _last_validated_at),
        # which do NOT change output_requested_values.
        last_validated = new_image.get("_last_validated_at", {}).get("S", "")
        if last_validated:
            try:
                validated_time = datetime.fromisoformat(last_validated)
                now = datetime.now(timezone.utc)
                if (now - validated_time).total_seconds() < DEBOUNCE_SECONDS:
                    old_labels = _extract_labels(old_image)
                    new_labels_check = _extract_labels(new_image)
                    if old_labels == new_labels_check:
                        log.info("Skipping %s — validated %s ago", config_key, now - validated_time)
                        return
            except (ValueError, TypeError):
                pass

        corrections = []

        # Extract current state
        required_keys = _extract_required_keys(new_image)
        # Deduplicate required_keys immediately — duplicates from GPT-4.1 inflate the
        # count, causing _fix_label_count to pad labels rather than trim them, which
        # then causes _update_column_registry to append fake new columns.
        required_keys = list(dict.fromkeys(required_keys))
        labels = _extract_labels(new_image)
        registry = (
            _extract_column_registry(new_image)
            or _extract_column_registry(old_image)
        )

        if not required_keys:
            log.info("No output_json_schema.required for %s — skipping", config_key)
            continue

        # 0a. Restore array wrapper if Bubble sync removed it or replaced it with a flat field
        full_schema = _extract_full_schema(new_image)
        cleaned_schema = None
        wrapper_key = WRAPPER_KEY_MAP.get(config_key)
        if full_schema and wrapper_key:
            props = full_schema.get("properties", {})
            wrapper_prop = props.get(wrapper_key, {})
            is_proper_wrapper = (
                isinstance(wrapper_prop, dict)
                and wrapper_prop.get("type") == "array"
                and isinstance(wrapper_prop.get("items"), dict)
            )
            if not is_proper_wrapper:
                # Bubble wrote a flat schema (or agenda_items is a plain field); re-wrap it
                full_schema = _wrap_flat_schema(full_schema, wrapper_key)
                cleaned_schema = full_schema
                # required_keys were already extracted from the flat schema (inner fields)
                corrections.append(
                    f"Re-wrapped flat schema in {wrapper_key} array — "
                    "Bubble sync removed wrapper or replaced it with a flat field"
                )

        # 0b. Remove garbage schema property keys
        if full_schema:
            schema_candidate, schema_garbage = _remove_garbage_schema_keys(full_schema)
            if schema_garbage:
                garbage_set = set(schema_garbage)
                required_keys = [k for k in required_keys if k not in garbage_set]
                full_schema = schema_candidate
                corrections.append(
                    f"Removed {len(schema_garbage)} garbage schema key(s): "
                    + ", ".join(repr(k[:60]) for k in schema_garbage)
                )

        # Track whether labels were actually modified (garbage removed or padding added).
        # We only write output_requested_values back if we changed it — writing it back
        # unconditionally causes a race: Bubble syncs schema and labels as separate DynamoDB
        # writes; Lambda fires on the schema write (which still has old labels), reads old
        # labels, and overwrites Bubble's subsequent label write with the old value.
        labels_modified = False

        # 1. Remove garbage labels
        cleaned_labels, garbage = _remove_garbage_labels(labels)
        if garbage:
            labels = cleaned_labels
            labels_modified = True
            corrections.append(
                f"Removed {len(garbage)} garbage label(s): "
                + ", ".join(f"[{i}] {repr(t[:50])}" for i, t in garbage)
            )

        # 2. Fix label count (only pads when too few; extra labels are preserved)
        if len(labels) < len(required_keys):
            old_count = len(labels)
            labels = _fix_label_count(labels, required_keys)
            labels_modified = True
            corrections.append(f"Padded label count: {old_count} -> {len(labels)}")
        elif len(labels) > len(required_keys):
            # Extra labels are valid pipeline metadata columns — just remove garbage
            labels, garbage = _remove_garbage_labels(labels)
            if garbage:
                labels_modified = True
                corrections.append(
                    f"Removed {len(garbage)} garbage label(s) from excess: "
                    + ", ".join(f"[{i}] {repr(t[:50])}" for i, t in garbage)
                )

        # 3. Update column registry (stable IDs)
        updated_registry, registry_changed = _update_column_registry(registry, labels, config_key, required_keys=required_keys)
        if registry_changed:
            registry = updated_registry
            corrections.append(f"Updated column registry ({len(registry)} columns)")

        # Steps 4 (key normalization) and 5 (type enforcement) removed.
        # The backend now derives schema keys directly from the field registry in the
        # same request, so schema and registry keys are always in sync at write time.
        # Rewriting the schema here against the stale _column_registry was the root
        # cause of all key mismatches. The whole Lambda will be deleted once verified.

        # Always update registry if it changed OR if it's absent from new_image
        # (Bubble full-item PUT can wipe Lambda-written fields; OldImage fallback in
        # _extract_column_registry masks the loss and prevents re-write without this check)
        registry_absent_from_new = not bool(_extract_column_registry(new_image))
        keys_changed = registry_changed or registry_absent_from_new

        if not corrections and not keys_changed:
            log.info("No corrections needed for %s", config_key)
            continue

        # Build update expression
        expr_parts = []
        expr_names = {}
        expr_values = {}

        if corrections:
            # Only write labels back if we actually modified them (garbage removed or padded).
            # Never write them back just because schema changed — that races with Bubble's
            # separate label update and overwrites the new label with the old one.
            if labels_modified:
                expr_parts.append("#labels = :labels")
                expr_names["#labels"] = "output_requested_values"
                expr_values[":labels"] = _ser_str_list(labels)

            # Update schema if changed (garbage removal or normalization)
            if cleaned_schema is not None:
                expr_parts.append("#schema = :schema")
                expr_names["#schema"] = "output_json_schema"
                expr_values[":schema"] = _ser_val(cleaned_schema)

        # Always write registry if changed
        if keys_changed or corrections:
            expr_parts.append("#registry = :registry")
            expr_names["#registry"] = "_column_registry"
            expr_values[":registry"] = _ser_registry(registry)

        # Debounce timestamp
        expr_parts.append("#validated = :validated")
        expr_names["#validated"] = "_last_validated_at"
        expr_values[":validated"] = {"S": datetime.now(timezone.utc).isoformat()}

        if not expr_parts:
            continue

        update_expr = "SET " + ", ".join(expr_parts)

        try:
            dynamo.update_item(
                TableName=TABLE,
                Key={"config_key": {"S": config_key}},
                UpdateExpression=update_expr,
                ExpressionAttributeNames=expr_names,
                ExpressionAttributeValues=expr_values,
            )
            if corrections:
                log.info("Corrected %s: %s", config_key, "; ".join(corrections))
        except Exception:
            log.exception("Failed to write corrections for %s", config_key)
