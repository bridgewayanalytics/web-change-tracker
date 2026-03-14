# Bubble Field Inventory — Agenda & Topic Mapping

## Resource Fields (relevant subset)

| Field | Type | Current Population | Enrichment Path | Notes |
|-------|------|-------------------|-----------------|-------|
| `Name` | string | Always set | Extractor (anchor text / filename) | Primary signal for topic/agenda classification |
| `URL` | string | Always set | Extractor | PDF URLs are key input for meeting metadata extraction |
| `notes` | string | Always set | Auto-generated: `"New doc in {label} ({section})"` | Currently template-only; no PDF content |
| `parent` | string | 199/200 in snapshot | `enrich_refs` — org path like `NAIC › E › Working Groups › Life RBC WG` | Strong signal for topic and group association |
| `Organization` | list[TreeNode ID] | Set via enrichment | `enrich_refs` — deterministic NAIC org lookup | Resolved to Organization tree node IDs |
| `Type1` | list[TreeNode ID] | 191/200 in snapshot | `enrich_refs` — deterministic from `section_type` | Maps to "Resources Types" tree (e.g. "Agenda & Materials", "Publication") |
| **`topic suggestion`** | TreeNode ID (single) | **0/200 in snapshot** | `enrich_refs` → `_resolve_topic_suggestion_ai()` | **AI-only, requires `use_ai=True` + Chronicles tree candidates** |
| `Related calendar items` | list[CalItem ID] | Set via enrichment | `enrich_refs` — NAIC group + date window matching | Links resource to calendar items by group/date |
| `date` | string (ISO) | Set from PDF metadata or null | `apply_pdf_meeting_metadata()` | Extracted from PDF text header |
| `__meeting_meta` | dict (debug) | Set when PDF downloadable | `apply_pdf_meeting_metadata()` | `{group_name, date_iso, start_time, end_time, timezone}` |
| `Notes on chronicles` | string | Never populated by system | Not implemented | Exists in Bubble schema but not touched |
| `Suggestion` | string | Never populated by system | Not implemented | Exists in Bubble schema but not touched |

## Calendar Item Fields (relevant subset)

| Field | Type | Current Population | Enrichment Path | Notes |
|-------|------|-------------------|-----------------|-------|
| `title` | string | Always set | Extractor (`naic_meetings_v1`) | e.g. "NAIC SAPWG WG \| CLOs and ABS" |
| `date` | datetime | Always set | Extractor / PDF metadata | ISO datetime |
| `NAIC Group (tree node)` | TreeNode ID | 199/200 in snapshot | `enrich_refs` — deterministic from org path | Organization tree node reference |
| `Agenda` | list[dict] | Built from extractor | `naic_meetings_v1` → `{url, title}` pairs | PDF/document links from meeting page |
| **`attached agenda items`** | list | **Always `[]` in system output** | **Not implemented** | **Empty list default — never populated** |
| `Relevant Documents` | list | Always `[]` in system output | Not implemented | Chronicle Links — never populated |
| `subtopic` | string | Not populated | Not implemented | Exists in schema |
| `has topic` | bool | Not populated | Not implemented | Exists in schema |
| `alerts` | list[Alert] | Populated via calendar_alerts | `build_calendar_alerts()` + `attach_alerts_to_calendar_items()` | Now stored in S3, not Bubble |

## Tree Structures

### Chronicles Tree (for `topic suggestion`)
- **Tree name:** "Chronicles" (ID: `1710771208905x698956612053237800`)
- **Node count in snapshot:** 0 (snapshot capped at 200 total tree nodes; Chronicles nodes excluded)
- **Resolution method:** AI-only (`_resolve_topic_suggestion_ai()`)
- **AI model:** OpenAI GPT-5 via Responses API
- **Confidence threshold:** 0.65 (configurable via `TOPIC_AI_CONFIDENCE_THRESHOLD`)
- **Constraint:** AI must select from exact candidate list; cannot invent topics

### Organization Tree (for `Organization`, `NAIC Group`)
- **Tree name:** "Organization"
- **Well-represented in snapshot** (most tree nodes are from this tree)
- **Resolution:** Deterministic — `parent` path like `NAIC › E › Working Groups › Life RBC WG`

### Resources Types Tree (for `Type1`)
- **Tree name:** "Resources Types"
- **Options:** Resource, Existing Requirements & Guidance, Publication, Proposed Guidance & Support Materials, Agenda & Materials, Newsreel, In the Weeds, Other, Web Repository, Podcasts & Webinars
- **Resolution:** Deterministic — mapped from `section_type` in extractor config

## Key Gaps Identified

1. **`topic suggestion` has 0% population in existing snapshot** — the AI enrichment path exists but has apparently not been run successfully in production, or historical resources predate it
2. **`attached agenda items` is always `[]`** — the field exists in the schema and payload builder but no code populates it
3. **`Relevant Documents` is always `[]`** — Chronicle Links not implemented
4. **`subtopic` and `has topic` on Calendar Items** — never populated
5. **No `agenda item` data type exists in the codebase** — the system references "attached agenda items" as a list field on Calendar Item, but there is no Agenda Item schema, builder, or extractor

## Where Historical Truth Lives

- **Topic suggestion:** Must be queried from Bubble directly via `client.list_all("Resource")` with broader limits — the 200-item snapshot cap excluded resources with populated `topic suggestion`
- **Agenda items:** Must be queried from Bubble directly — the system never creates them, so any existing ones were manually entered in Bubble
- **Relationship patterns:** Exist only in Bubble production data, not in this codebase's output artifacts
