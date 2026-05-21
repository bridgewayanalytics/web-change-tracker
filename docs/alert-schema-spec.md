# Alert Schema Specification

This document is the authoritative reference for anyone building or modifying
the sync function that writes `output_json_schema` and `output_requested_values`
to the DynamoDB `chatkit_production_config` table (key: `chat:web-tracking-agent`).

See also: `structured-output-schema.md` (backend design doc).

---

## One alert = one flat JSON object

The web-tracking agent outputs **one object per alert**. No top-level wrapper
array. Each object has 21 top-level keys. Some values are plain strings; a
small set of known fields have structured types (array or object).

---

## Field ID derivation

Field IDs are derived deterministically from the Bubble label using:

```python
import re
def to_field_id(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')
```

Any run of non-alphanumeric characters (spaces, `&`, `-`, `?`, parentheses)
collapses to a single underscore. **The label is the contract** — renaming a
label changes the field ID, which is a breaking change.

---

## Field registry (ordered, 21 fields)

`output_json_schema.required` and `output_requested_values` must be in the same
positional order. Field IDs below are the `to_field_id()` output of each label.

| # | Field ID | Label | Type |
|---|----------|-------|------|
| 0 | `alert_type` | Alert Type | string |
| 1 | `alert_title` | Alert Title | string |
| 2 | `alert_description` | Alert Description | string |
| 3 | `alert_url` | Alert URL | string |
| 4 | `organization` | Organization | **structured** |
| 5 | `alert_date_time` | Alert Date & Time | string |
| 6 | `event_title` | Event Title | string |
| 7 | `event_start_date_time` | Event Start Date & Time | string |
| 8 | `event_end_date_time` | Event End Date & Time | string |
| 9 | `event_duration` | Event Duration | string |
| 10 | `event_is_full_day` | Event is Full Day | string |
| 11 | `event_url` | Event URL | string |
| 12 | `event_call_in_number_access_code` | Event Call-In Number & Access Code | string |
| 13 | `agenda_item_title_chronicle_topics` | Agenda Item Title & Chronicle Topics | **structured** |
| 14 | `agenda_item_title_official` | Agenda Item Title - Official | **structured** |
| 15 | `agenda_item_standardized_id` | Agenda Item - Standardized ID | **structured** |
| 16 | `agenda_item_official_id` | Agenda Item - Official ID | **structured** |
| 17 | `library_item_preliminary_title` | Library Item Preliminary Title | **structured** |
| 18 | `library_item_url` | Library Item URL | string |
| 19 | `library_items_file_name` | Library Items File Name | string |
| 20 | `is_the_alert_relevant_for_an_art_newsreel_article` | Is the Alert Relevant for an ART Newsreel article? | **structured** |

---

## Two tiers of field types

### Tier 1 — Structural fields (hardcoded in `bubble_schema.py`)

Seven fields have non-string types. Their schemas are fixed in code and do not
change when a label is renamed or a field is reordered. Adding a new structural
field requires a code change (update `STRUCTURAL_SCHEMAS`, the agent
instructions, and any downstream renderers).

### Tier 2 — String fields (everything else)

Every field not in the structural set is `{"type": "string"}`. This is
automatic for all current string fields and any new field added in Bubble.

**New field added in Bubble → `{"type": "string"}`, no code change needed.**

---

## Structural field schemas

These are the exact schemas to use in `STRUCTURAL_SCHEMAS`. **Do not add
`minItems`, `maxItems`, `format`, `oneOf`, `anyOf`, or `$schema`** — OpenAI
strict mode rejects them.

### `organization`

```json
{
  "type": "array",
  "description": "Organization",
  "items": { "type": "string" }
}
```

### `agenda_item_title_chronicle_topics`

The only field with deeper nesting. Each item pairs a title with its chronicle
topics because the instructions say *"the list of associated Chronicle Topics
for each Agenda Item"* — you need to know which topics belong to which item.

```json
{
  "type": "array",
  "description": "Agenda Item Title & Chronicle Topics",
  "items": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "status":             { "type": "string", "enum": ["New", "Existing", "N/A"] },
      "agenda_item_title":  { "type": "string" },
      "chronicle_topics":   { "type": "array", "items": { "type": "string" } }
    },
    "required": ["status", "agenda_item_title", "chronicle_topics"]
  }
}
```

### `agenda_item_title_official`

Parallel array to `agenda_item_title_chronicle_topics` — one entry per agenda
item. Use `[{"status": "N/A", "official_title": "N/A"}]` when not applicable.

```json
{
  "type": "array",
  "description": "Agenda Item Title - Official",
  "items": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "status":         { "type": "string", "enum": ["New", "Existing", "N/A"] },
      "official_title": { "type": "string" }
    },
    "required": ["status", "official_title"]
  }
}
```

### `agenda_item_standardized_id`

```json
{
  "type": "array",
  "description": "Agenda Item - Standardized ID",
  "items": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "status":          { "type": "string", "enum": ["New", "Existing", "N/A"] },
      "standardized_id": { "type": "string" }
    },
    "required": ["status", "standardized_id"]
  }
}
```

### `agenda_item_official_id`

```json
{
  "type": "array",
  "description": "Agenda Item - Official ID",
  "items": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "status":      { "type": "string", "enum": ["New", "Existing", "N/A"] },
      "official_id": { "type": "string" }
    },
    "required": ["status", "official_id"]
  }
}
```

### `library_item_preliminary_title`

Single object — one alert corresponds to at most one document.

```json
{
  "type": "object",
  "description": "Library Item Preliminary Title",
  "additionalProperties": false,
  "properties": {
    "status": { "type": "string", "enum": ["New", "Updated", "Existing", "Old", "N/A"] },
    "title":  { "type": "string" }
  },
  "required": ["status", "title"]
}
```

### `is_the_alert_relevant_for_an_art_newsreel_article`

```json
{
  "type": "object",
  "description": "Is the Alert Relevant for an ART Newsreel article?",
  "additionalProperties": false,
  "properties": {
    "status":  { "type": "string", "enum": ["Yes", "No", "Additional review needed"] },
    "details": { "type": "string" }
  },
  "required": ["status", "details"]
}
```

---

## `build_schema()` implementation

```python
STRUCTURAL_SCHEMAS = {
    "organization":                                        { ... },
    "agenda_item_title_chronicle_topics":                  { ... },
    "agenda_item_title_official":                          { ... },
    "agenda_item_standardized_id":                         { ... },
    "agenda_item_official_id":                             { ... },
    "library_item_preliminary_title":                      { ... },
    "is_the_alert_relevant_for_an_art_newsreel_article":   { ... },
}

def build_schema(column_registry: list[dict]) -> dict:
    """
    column_registry: ordered list of {id: str, label: str}
    Returns a JSON Schema object for OpenAI Structured Outputs strict mode.
    """
    properties = {}
    required = []
    for entry in column_registry:
        field_id = entry["id"]
        schema = STRUCTURAL_SCHEMAS.get(field_id, {"type": "string", "description": entry["label"]})
        properties[field_id] = schema
        required.append(field_id)
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }
```

`output_requested_values` is `[entry["label"] for entry in column_registry]`.

---

## OpenAI strict mode — prohibited keywords

| Prohibited | Why |
|------------|-----|
| `$schema` | Not part of the subset OpenAI supports |
| `oneOf`, `anyOf`, `allOf` | Not allowed at any level |
| `format` | Not allowed |
| `minItems`, `maxItems` | Not allowed |
| `exclusiveMinimum` (boolean form) | Not allowed |

All objects must have `"additionalProperties": false` and every key in
`properties` must appear in `required`.

---

## Historical field name mapping

The backend has used different field IDs over time. The dashboard handles
backward compatibility automatically via `LEGACY_ALIASES` in `AlertsTable.tsx`.
No data migration is needed — old rows continue to display correctly.

| Current ID | Previous ID(s) |
|------------|----------------|
| `alert_date_time` | `alert_datetime_et` |
| `event_start_date_time` | `event_start_datetime_et`, `event_start_datetime` |
| `event_end_date_time` | `event_end_datetime_et`, `event_end_datetime` |
| `event_call_in_number_access_code` | `event_call_in_number_and_access_code`, `event_call_in_access_code` |
| `agenda_item_title_chronicle_topics` | `agenda_items`, `agenda_item_title_and_chronicle_topics` |
| `is_the_alert_relevant_for_an_art_newsreel_article` | `art_newsreel_relevance`, `is_alert_relevant_for_art_newsreel` |
