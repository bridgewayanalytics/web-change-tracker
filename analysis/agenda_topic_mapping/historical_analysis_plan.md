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

## Phase 3: PDF Content Analysis (Updated after PDF content analysis)

> **Added after initial Bubble metadata analysis.** The original plan focused on Bubble object relationships. This phase evaluates whether the PDFs themselves contain structured agenda information.

### Query 6: PDF resources for content analysis
**Goal:** Download and parse PDFs to detect agenda structures, reference numbers, and topic names.

```
Sources:
  - PDF resources with topic suggestion (from Query 1, filtered to .pdf URLs)
  - PDF resources referenced by Agenda Items (from resolved_agenda_items)
Target: 100 unique PDF resources
```

**Analysis script:** `analysis/pdf_agenda_detection/analyze_pdf_content.py`

**Per-PDF analysis:**
- Extract text using pypdf + pdfminer.six (existing infrastructure)
- Detect: agenda header, numbered items, roman numeral items, reference numbers, SSAP refs
- Detect: group name in PDF header (regex on first page)
- Detect: Chronicle topic names in full text (string matching against 87 tree node names)
- Classify structure type: formal_agenda, numbered_list, meeting_minutes, outline, informal, none

**Cross-comparison:**
- For each resource-agenda item pair: check if BA title keywords and BA Ref # appear in PDF
- For each resource with topic suggestion: check if assigned topic name appears in PDF text
- Compute match rates and identify which signals are strongest

### Results (completed)

See `analysis/pdf_agenda_detection/pdf_detection_stats.md` for full statistics.

Key findings:
- 84.6% of PDFs contain numbered items
- 36.3% contain extractable reference numbers (highest-precision signal)
- 78.0% contain at least one Chronicle topic name
- But only 31.9% contain the *specific* topic assigned in Bubble
- PDF ref # extraction + Bubble Agenda Item matching is the strongest combined approach

## Deliverables

1. **`historical_samples.json`** — Clean dataset with resolved names (not just IDs)
2. **`findings.md`** — Patterns discovered (updated with PDF analysis)
3. **`proposed_strategy.md`** — Recommended approach for production (updated with PDF analysis)
4. **`analysis/pdf_agenda_detection/pdf_sample_dataset.json`** — 100 PDF analysis results
5. **`analysis/pdf_agenda_detection/pdf_agenda_examples.md`** — Concrete agenda structure examples
6. **`analysis/pdf_agenda_detection/pdf_detection_stats.md`** — Signal presence rates and match statistics
7. **`analysis/pdf_agenda_detection/pdf_vs_bubble_comparison.md`** — Side-by-side comparison of PDF vs Bubble signals

## Execution Notes

- All queries use read-only Bubble client (`client.search()` / `client.list_all()`)
- Requires `BUBBLE_API_URL` and `BUBBLE_API_KEY` in environment
- Scripts:
  - `analysis/agenda_topic_mapping/pull_historical_data.py` (Bubble metadata)
  - `analysis/pdf_agenda_detection/analyze_pdf_content.py` (PDF content analysis)
- PDF downloads require network access to `content.naic.org`
- No secrets in output files — IDs are not secrets (Bubble object IDs are opaque timestamps)

## Assumptions

1. The Chronicles tree is the canonical topic taxonomy (not a separate "topics" table)
2. ~~`attached agenda items` references a Bubble type we haven't mapped yet~~ **Resolved:** "Agenda item" is a distinct Bubble data type with BA Ref #, Topics, Resources, Discussed at, Category, etc.
3. Historical resources in Bubble represent manually curated ground truth suitable for building heuristics
4. The `parent` field on Resources (org path) is a strong predictor of topic assignment
5. **(New)** PDF content from NAIC meeting materials is a reliable supplementary signal for agenda item identification, but not sufficient alone for topic selection
