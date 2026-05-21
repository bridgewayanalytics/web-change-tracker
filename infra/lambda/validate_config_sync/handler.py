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

# If we validated this row within this many seconds, skip (prevents loops)
DEBOUNCE_SECONDS = 10

# Labels longer than this are likely garbage (instruction text leaked in)
MAX_LABEL_LENGTH = 80

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
        "number", "data_extraction_datetime", "document_description",
        "organization_or_publisher", "agenda_items", "agenda_items_official",
        "agenda_items_standardized_id", "agenda_items_official_id",
        "existing_or_new_agenda_item", "date_published",
        "meeting_or_last_comment_date", "document_type", "document_title",
        "existing_updated_or_new_document", "newsreel_relevance",
    ],
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

def _extract_required_keys(image):
    """Extract the ordered required keys from output_json_schema."""
    raw = image.get("output_json_schema")
    if not raw:
        return []
    schema = _deser(raw)
    if not isinstance(schema, dict):
        return []
    return schema.get("required", [])


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
    """Ensure labels list matches required_keys length.

    If too many: remove garbage first, then trim from end.
    If too few: pad with title-cased field key names.
    """
    # First remove garbage
    labels, _ = _remove_garbage_labels(labels)

    if len(labels) > len(required_keys):
        # Trim excess from end
        labels = labels[: len(required_keys)]
    elif len(labels) < len(required_keys):
        # Pad with key names converted to title case
        for i in range(len(labels), len(required_keys)):
            key = required_keys[i]
            label = key.replace("_", " ").title()
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

    # Handle alerts-array wrapper
    props = schema.get("properties", {})
    if "alerts" in props:
        alerts_prop = props["alerts"]
        items = alerts_prop.get("items") if isinstance(alerts_prop, dict) else None
        if isinstance(items, dict):
            cleaned_items, removed = _clean_object_schema(items)
            if removed:
                new_alerts = {**alerts_prop, "items": cleaned_items}
                return {**schema, "properties": {**props, "alerts": new_alerts}}, removed
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


def _update_column_registry(registry, new_labels, config_key):
    """
    Update registry by position using new_labels from output_requested_values.
    - Existing positions: keep stable ID, update label if changed.
    - New positions: derive stable snake_case ID from label (frozen at creation).
    - Removed positions: registry shrinks automatically.
    Returns (updated_registry, changed).
    """
    initial_ids = INITIAL_REGISTRIES.get(config_key, [])
    updated = []
    changed = False
    existing_ids = {e["id"] for e in registry}

    for i, label in enumerate(new_labels):
        if i < len(registry):
            # Existing column: keep stable ID, update label if changed
            entry = dict(registry[i])
            if entry.get("label") != label:
                entry["label"] = label
                changed = True
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
            changed = True

    if len(registry) > len(new_labels):
        changed = True  # columns were removed (updated is already shorter)

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

    label_to_id = {e["label"]: e["id"] for e in registry}
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
            stable = label_to_id.get(key) or (key if key in id_set else None)
            # Positional fallback: if key is unknown, use registry stable ID at same position
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

    # Handle alerts array wrapper
    props = schema.get("properties", {})
    if "alerts" in props:
        alerts_prop = props["alerts"]
        if isinstance(alerts_prop, dict) and isinstance(alerts_prop.get("items"), dict):
            new_items, changed, detected = _normalize_object(alerts_prop["items"])
            if changed:
                return {**schema, "properties": {**props, "alerts": {**alerts_prop, "items": new_items}}}, detected
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

    # Handle alerts wrapper
    s_props = schema.get("properties", {})
    if "alerts" in s_props:
        alerts_prop = s_props["alerts"]
        if isinstance(alerts_prop, dict) and isinstance(alerts_prop.get("items"), dict):
            new_items, fixes = _enforce_on_object(alerts_prop["items"])
            if fixes:
                new_alerts = {**alerts_prop, "items": new_items}
                return {**schema, "properties": {**s_props, "alerts": new_alerts}}, fixes
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

        # Debounce: skip if we just validated this row
        last_validated = new_image.get("_last_validated_at", {}).get("S", "")
        if last_validated:
            try:
                validated_time = datetime.fromisoformat(last_validated)
                now = datetime.now(timezone.utc)
                if (now - validated_time).total_seconds() < DEBOUNCE_SECONDS:
                    log.info("Skipping %s — validated %s ago", config_key, now - validated_time)
                    return
            except (ValueError, TypeError):
                pass

        corrections = []

        # Load existing _field_aliases (stable_id -> old_key, for dashboard resolveCell)
        field_aliases = _extract_str_map(new_image, "_field_aliases")

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

        # 0. Remove garbage schema property keys
        full_schema = _extract_full_schema(new_image)
        cleaned_schema = None
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

        # 1. Remove garbage labels
        cleaned_labels, garbage = _remove_garbage_labels(labels)
        if garbage:
            labels = cleaned_labels
            corrections.append(
                f"Removed {len(garbage)} garbage label(s): "
                + ", ".join(f"[{i}] {repr(t[:50])}" for i, t in garbage)
            )

        # 2. Fix label count
        if len(labels) != len(required_keys):
            old_count = len(labels)
            labels = _fix_label_count(labels, required_keys)
            corrections.append(f"Fixed label count: {old_count} -> {len(required_keys)}")

        # 3. Update column registry (stable IDs)
        updated_registry, registry_changed = _update_column_registry(registry, labels, config_key)
        if registry_changed:
            registry = updated_registry
            corrections.append(f"Updated column registry ({len(registry)} columns)")

        # 4. Normalize output_json_schema to use stable IDs
        if full_schema and registry:
            normalized, detected_renames = _normalize_schema_with_registry(full_schema, registry)
            if normalized is not full_schema:
                cleaned_schema = normalized
                corrections.append("Normalized schema to use stable column IDs")
            if detected_renames:
                # Invert to {stable_id: old_key} so dashboard resolveCell can walk backward
                new_aliases = {stable: old for old, stable in detected_renames.items()}
                # Merge with existing (don't overwrite chains already recorded)
                merged = {**new_aliases, **field_aliases}  # existing takes priority for stable chains
                if merged != field_aliases:
                    field_aliases = merged
                    corrections.append(
                        f"Recorded {len(detected_renames)} field alias(es): "
                        + ", ".join(f"{old}->{stable}" for old, stable in detected_renames.items())
                    )

        # 5. Enforce correct field types
        schema_to_check = cleaned_schema or full_schema
        if schema_to_check:
            type_enforced, type_fixes = _enforce_field_types(schema_to_check)
            if type_fixes:
                cleaned_schema = type_enforced
                corrections.append("Type enforcement: " + "; ".join(type_fixes))

        # Always update registry if it changed
        keys_changed = registry_changed

        if not corrections and not keys_changed:
            log.info("No corrections needed for %s", config_key)
            continue

        # Build update expression
        expr_parts = []
        expr_names = {}
        expr_values = {}

        if corrections:
            # Update labels
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

        # Write field aliases if we have any (dashboard uses these for zero-code renames)
        if field_aliases:
            expr_parts.append("#aliases = :aliases")
            expr_names["#aliases"] = "_field_aliases"
            expr_values[":aliases"] = _ser_str_map(field_aliases)

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
