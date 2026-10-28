# Schema Registry: pending field updates

**Owner:** Bubble → DynamoDB sync maintainer
**Date raised:** 2026-10-28
**Scope:** Two field changes affecting both `chat:web-tracking-agent` and `chat:document-data-extraction`

---

## How the schema registry works (short version)

Agent output schemas are managed by three layers:

1. **Bubble admin UI** — editors change labels, instructions, and schema values
2. **DynamoDB `chatkit_production_config`** — the table Bubble writes to
3. **`validate_config_sync` Lambda** (DynamoDB Streams trigger) — normalizes every write using an authoritative registry baked into its code:
   - `INITIAL_REGISTRIES` — ordered stable snake_case IDs per config
   - `STRUCTURAL_SCHEMAS` — authoritative shape for every complex (object/array/enum) field, keyed by stable ID
   - On every sync, `_enforce_field_types()` overwrites the shape of each registered structural field with the Lambda's definition

**Implication:** editing structural fields in Bubble admin alone is **not enough** — on the next sync, the Lambda will rewrite them back. All of the changes below must be made in **`infra/lambda/validate_config_sync/handler.py`** (then deployed), with corresponding label updates in Bubble admin.

File to edit: `infra/lambda/validate_config_sync/handler.py`
Deploy: `cd infra/lambda/validate_config_sync && ./deploy.sh` (or redeploy via Terraform in `infra/terraform/`)

---

## Change 1: Split chronicle topics out of the agenda-item compound

Both agents currently nest `chronicle_topics` inside their agenda-item array **and** carry a separate `chronicle_topic` string at the top level. The nested copy is redundant and causes the agent to produce chronicle topics in two places.

### `chat:web-tracking-agent`

Stable ID in registry: today it's `agenda_item_title_chronicle_topics`. **Rename to `agenda_item_title`** so the registry matches what the schema already stores. Update three places:

**a. `INITIAL_REGISTRIES["chat:web-tracking-agent"]`** (around line 70)

- Replace `"agenda_item_title_chronicle_topics"` with `"agenda_item_title"`
- Add `"chronicle_topic"` immediately after it

**b. `STRUCTURAL_SCHEMAS`** (around line 108)

Replace the entire `"agenda_item_title_chronicle_topics": { ... }` entry with:

```python
"agenda_item_title": {
    "type": "array",
    "description": "Agenda items with their New/Existing status.",
    "minItems": 1,
    "items": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {"type": "string", "enum": ["New", "Existing", "N/A"]},
            "agenda_item_title": {"type": "string"},
        },
        "required": ["status", "agenda_item_title"],
    },
},
"chronicle_topic": {
    "type": "string",
    "description": "Chronicle Topic",
},
```

**c. `CANONICAL_LABELS`** (around line 55)

- Remove the `"agenda_item_title_chronicle_topics": "Agenda Item Title & Chronicle Topics"` entry
- No need to add replacements — standard snake→title generation produces usable labels for `agenda_item_title` and `chronicle_topic`

### `chat:document-data-extraction`

Doc extraction already lists `"agenda_item_title"` + `"chronicle_topics"` as separate IDs in `INITIAL_REGISTRIES` (line ~86). The structural work here is lighter:

**a. `INITIAL_REGISTRIES["chat:document-data-extraction"]`** — rename `"chronicle_topics"` → `"chronicle_topic"` to match the actual field name used in the live schema and in `chat:document-extraction-qa-agent`.

**b. `STRUCTURAL_SCHEMAS`** — replace the existing `"chronicle_topics"` entry with:

```python
"chronicle_topic": {
    "type": "string",
    "description": "Chronicle Topic",
},
```

If `agenda_item_bridgeway_title` ever gets a structural entry (it currently doesn't), apply the same "no chronicle_topics inside" pattern as `agenda_item_title` above.

### Bubble admin follow-up

After the Lambda is deployed:

1. In each agent's config, rename the "Agenda Item Title & Chronicle Topics" column header to **"Agenda Item Title"** and leave the standalone **"Chronicle Topic"** column as its own entry.
2. In the agent **instructions** text, scrub any remaining `agenda_item_title_chronicle_topics` or "put chronicle topics inside each agenda item" phrasing. Agents now output chronicle topics only in the standalone `chronicle_topic` field.

---

## Change 2: Restrict ART Newsreel relevance to Yes / No

Both relevance fields currently allow a third enum value — `"Additional review needed"` — which creates ambiguous rows. The downstream newsreel pipeline treats "Additional review needed" the same as "No", so the extra value adds confusion without changing behavior.

### Both configs (`chat:web-tracking-agent` and `chat:document-data-extraction`)

In `STRUCTURAL_SCHEMAS`, update both:

```python
"is_the_alert_relevant_for_an_art_newsreel_article": {
    ...
    "properties": {
        "status": {"type": "string", "enum": ["Yes", "No"]},   # was: ["Yes", "No", "Additional review needed"]
        "details": {"type": "string"},
    },
    ...
},

"is_the_document_relevant_for_a_newsreel_article": {
    ...
    "properties": {
        "status": {"type": "string", "enum": ["Yes", "No"]},   # was: ["Yes", "No", "Additional review needed"]
        "explanation_or_reference": {"type": "string"},
    },
    ...
},
```

### Bubble admin follow-up

In each agent's instructions, update the newsreel relevance rubric to force a binary decision — remove "Additional review needed" language from guidance and prompt the agent to choose Yes or No with a brief justification in `details` / `explanation_or_reference`.

---

## Deploy & verify

1. Edit `infra/lambda/validate_config_sync/handler.py` as above
2. Deploy the Lambda (zip + update function, or `terraform apply` in `infra/terraform/`)
3. Trigger a resync by making any trivial edit in Bubble admin for each config (or write a no-op change to DynamoDB) — this fires the Stream and runs the new validator
4. Verify from the shell:

```bash
AWS_PROFILE=bridgeway aws dynamodb get-item \
  --table-name chatkit_production_config \
  --key '{"config_key":{"S":"chat:web-tracking-agent"}}' \
  --output json \
  | jq '.Item.output_json_schema.M.properties.M.alerts.M.items.M.properties.M.agenda_item_title'
```

Expected: no `chronicle_topics` property inside the agenda item compound.

```bash
AWS_PROFILE=bridgeway aws dynamodb get-item \
  --table-name chatkit_production_config \
  --key '{"config_key":{"S":"chat:web-tracking-agent"}}' \
  --output json \
  | jq '.Item.output_json_schema.M.properties.M.alerts.M.items.M.properties.M.is_the_alert_relevant_for_an_art_newsreel_article.M.properties.M.status.M.enum'
```

Expected: `["Yes", "No"]` only.

5. Fire a dashboard rerun on any alert row (any row works — accept the amber result if it looks right) and confirm:
   - Agent output has `agenda_item_title` and `chronicle_topic` as separate fields
   - `chronicle_topics` is **not** present inside the agenda item objects
   - The `is_the_alert_relevant_for_an_art_newsreel_article.status` is either `"Yes"` or `"No"` (never "Additional review needed")

---

## Open questions / out of scope

- **Historical rows:** `alerts_table.jsonl` has rows that stored `chronicle_topics` inside the compound. The dashboard's `FIELD_ALIASES` (`dashboard/src/app/alerts/AlertsTable.tsx`) handles backward display, so these rows will still render — but they'll show the old nested chronicle data, not the new standalone `chronicle_topic`. No action needed unless we want to backfill.
- **`chat:web-extraction-qa-agent` typo:** the QA schema has a field named `chronicle_topics` (plural, string) where it should be `chronicle_topic` (singular) to match the extraction field. This is purely a label/key fix in Bubble admin for the QA config — it doesn't flow through the Lambda registry (QA configs aren't in `WATCHED_KEYS`). Fix it directly in Bubble admin as part of this round.
