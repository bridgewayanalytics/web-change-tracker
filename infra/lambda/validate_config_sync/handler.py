"""
DynamoDB Streams Lambda: auto-validate Bubble syncs to chatkit_production_config.

Triggered on every write to the config table. Detects and corrects:
1. Label count mismatches (output_requested_values vs output_json_schema.required)
2. Garbage labels (instruction text leaked into column headers)
3. Field key renames (updates field_key_aliases + _previous_required_keys)

Writes corrections back in-place. Uses _last_validated_at timestamp to prevent
infinite trigger loops (Lambda's own correction write re-triggers the stream).
"""

import json
import logging
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


def _detect_renames(prev_keys, current_keys, existing_aliases):
    """Detect positional key renames and update aliases.

    Returns (updated_aliases, list_of_changes).
    Same logic as /api/schema detectAndStoreAliases.
    """
    aliases = dict(existing_aliases)
    changes = []

    if not prev_keys:
        return aliases, changes

    length = min(len(prev_keys), len(current_keys))
    for i in range(length):
        if prev_keys[i] != current_keys[i]:
            old_key = prev_keys[i]
            new_key = current_keys[i]
            # Chain to ultimate original
            ultimate = aliases.get(old_key, old_key)
            aliases[new_key] = ultimate
            # Remove old entry if it existed
            aliases.pop(old_key, None)
            changes.append(f"{old_key} -> {new_key}")

    return aliases, changes


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

        # Extract current state.
        # _previous_required_keys and field_key_aliases are Lambda-managed fields
        # that Bubble's PutItem wipes on every sync. Fall back to old_image so
        # rename detection survives Bubble overwrites.
        required_keys = _extract_required_keys(new_image)
        labels = _extract_labels(new_image)
        prev_keys = (
            _extract_str_list(new_image, "_previous_required_keys")
            or _extract_str_list(old_image, "_previous_required_keys")
        )
        aliases = (
            _extract_str_map(new_image, "field_key_aliases")
            or _extract_str_map(old_image, "field_key_aliases")
        )

        if not required_keys:
            log.info("No output_json_schema.required for %s — skipping", config_key)
            continue

        # 0. Remove garbage schema property keys (ChatKit may generate fake props
        #    from instruction text via its label-extraction regex)
        full_schema = _extract_full_schema(new_image)
        cleaned_schema = None
        if full_schema:
            cleaned_schema, schema_garbage = _remove_garbage_schema_keys(full_schema)
            if schema_garbage:
                garbage_set = set(schema_garbage)
                required_keys = [k for k in required_keys if k not in garbage_set]
                corrections.append(
                    f"Removed {len(schema_garbage)} garbage schema key(s): "
                    + ", ".join(repr(k[:60]) for k in schema_garbage)
                )
            else:
                cleaned_schema = None  # nothing changed, no write needed

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

        # 3. Detect key renames
        new_aliases, rename_changes = _detect_renames(prev_keys, required_keys, aliases)
        if rename_changes:
            aliases = new_aliases
            corrections.append(f"Detected renames: {', '.join(rename_changes)}")

        # Always update _previous_required_keys if they changed
        keys_changed = prev_keys != required_keys

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

            # Update aliases
            expr_parts.append("#aliases = :aliases")
            expr_names["#aliases"] = "field_key_aliases"
            expr_values[":aliases"] = _ser_str_map(aliases)

            # Update schema if garbage properties were removed
            if cleaned_schema is not None:
                expr_parts.append("#schema = :schema")
                expr_names["#schema"] = "output_json_schema"
                expr_values[":schema"] = _ser_val(cleaned_schema)

        if keys_changed or rename_changes:
            # Update previous keys baseline
            expr_parts.append("#prev = :prev")
            expr_names["#prev"] = "_previous_required_keys"
            expr_values[":prev"] = _ser_str_list(required_keys)

        # Debounce timestamp
        expr_parts.append("#validated = :validated")
        expr_names["#validated"] = "_last_validated_at"
        expr_values[":validated"] = {"S": datetime.now(timezone.utc).isoformat()}

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
            if keys_changed and not rename_changes:
                log.info("Updated _previous_required_keys baseline for %s (%d keys)", config_key, len(required_keys))
        except Exception:
            log.exception("Failed to write corrections for %s", config_key)
