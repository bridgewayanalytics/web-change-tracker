# Historical Analysis Plan — Agenda & Topic Mapping

## Objective

Determine how `topic suggestion` and `attached agenda items` are populated in production Bubble data, so we can build robust automated logic for new resources.

## What We Know Before Querying

1. **`topic suggestion`** on Resource → references a Chronicles tree node (single ID)
   - The system has AI resolution code (`_resolve_topic_suggestion_ai`) that sends resource metadata + candidate topic names to GPT-5 and picks the best match
   - But 0/200 resources in our local snapshot have this field populated
   - This likely means: (a) most historical resources predate the AI enrichment, or (b) the AI path hasn't been active in prod yet, or (c) the 200-item cap excluded enriched resources

2. **`attached agenda items`** on Calendar Item → always `[]` in system output
   - No builder or extractor populates this field
   - Any populated values in Bubble are manually entered
   - We don't have an "Agenda Item" data type definition in code — it may be a Bubble-native type or a simple record

3. **`Relevant Documents`** on Calendar Item → always `[]` (Chronicle Links)
   - Not implemented in system output

## Phase 2 Queries

### Query 1: Resources with `topic suggestion` populated
**Goal:** Get a representative sample of resources where a human or system assigned a chronicle topic.

```
Type: Resource
Constraints: [{"key": "topic suggestion", "constraint_type": "is_not_empty"}]
Fields needed: _id, Name, URL, notes, parent, topic suggestion, Type1, Related calendar items, date
Limit: 100 items
```

**Analysis:**
- For each resource, resolve `topic suggestion` ID → node name (via tree node lookup)
- Check if topic name appears in resource Name, URL, or notes
- Check if topic correlates with `parent` path (org hierarchy)
- Cluster: which topics appear most frequently? Are they broad categories or specific?

### Query 2: Calendar Items with `attached agenda items` populated
**Goal:** Understand the structure of agenda items.

```
Type: Calendar item
Constraints: [{"key": "attached agenda items", "constraint_type": "is_not_empty"}]
Fields needed: _id, title, date, NAIC Group (tree node), attached agenda items, Agenda, Relevant Documents, subtopic
Limit: 50 items
```

**Analysis:**
- What do agenda items look like? (are they IDs referencing another type, or inline objects?)
- How do they relate to the calendar item's title / group?
- Is there a pattern between agenda items and the PDFs in `Agenda`?

### Query 3: Chronicles tree — all nodes
**Goal:** Map the full topic taxonomy.

```
Type: Tree node
Constraints: [{"key": "parent_tree", "constraint_type": "equals", "value": "<chronicles_tree_id>"}]
Limit: 500
```

**Analysis:**
- How deep is the tree? (flat list vs. hierarchy)
- Are topics broad ("Investments") or specific ("SSAP No. 26R — Bonds")?
- How many leaf nodes exist?

### Query 4: Resources with PDF URLs and populated enrichment fields
**Goal:** Understand the relationship between PDF content, org context, and assigned fields.

```
Type: Resource
Constraints: [
  {"key": "URL", "constraint_type": "text contains", "value": ".pdf"},
  {"key": "topic suggestion", "constraint_type": "is_not_empty"}
]
Limit: 50
```

**Analysis:**
- Download PDFs, extract first-page text
- Check: does topic name appear in PDF text? In headers? In filenames?
- Check: is topic inferable from the meeting group alone?

### Query 5: Resources with both `topic suggestion` AND `Related calendar items`
**Goal:** Understand the triple relationship: resource ↔ calendar item ↔ topic.

```
Type: Resource
Constraints: [
  {"key": "topic suggestion", "constraint_type": "is_not_empty"},
  {"key": "Related calendar items", "constraint_type": "is_not_empty"}
]
Limit: 50
```

**Analysis:**
- For each, resolve both the topic and calendar item
- Check: do different resources on the same calendar item share a topic?
- Check: does the calendar item title/group predict the topic?

## Deliverables

1. **`historical_samples.json`** — Clean dataset with resolved names (not just IDs)
2. **`findings.md`** — Patterns discovered
3. **`proposed_strategy.md`** — Recommended approach for production

## Execution Notes

- All queries use read-only Bubble client (`client.search()` / `client.list_all()`)
- Requires `BUBBLE_API_URL` and `BUBBLE_API_KEY` in environment
- Script: `analysis/agenda_topic_mapping/pull_historical_data.py`
- If API access unavailable, we can work from the existing 200-item snapshot + S3 archives
- No secrets in output files — IDs are not secrets (Bubble object IDs are opaque timestamps)

## Assumptions

1. The Chronicles tree is the canonical topic taxonomy (not a separate "topics" table)
2. `attached agenda items` references a Bubble type we haven't mapped yet — the query will reveal its structure
3. Historical resources in Bubble represent manually curated ground truth suitable for building heuristics
4. The `parent` field on Resources (org path) is a strong predictor of topic assignment
