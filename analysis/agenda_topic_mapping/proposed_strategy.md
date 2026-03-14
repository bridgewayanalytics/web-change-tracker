# Proposed Strategy — Agenda Item & Topic Suggestion

## Overview

Based on the historical data analysis, I recommend a **two-tier hybrid approach**:

1. **Agenda Item Association** → Deterministic candidate retrieval + LLM ranking
2. **Topic Suggestion** → Inherited from agenda item when matched, AI classification as fallback

This leverages the fact that Agenda Items are the bridge entity: they already contain curated Topics, so matching resources to agenda items solves both problems at once.

---

## Tier 1: Agenda Item Association

### Approach: Candidate Retrieval + LLM Ranking

**Why not pure deterministic?**
- Resource names don't always contain the agenda item ref # (e.g., "SAPWG#2024-04")
- Multiple resources can share the same URL (bulk materials PDFs)
- Agenda item titles use varying terminology (BA title vs NAIC Title)

**Why not pure AI?**
- The candidate space is bounded (typically 1-20 agenda items per NAIC group)
- Agenda items have structured identifiers (ref #s) that enable exact matching
- Deterministic pre-filtering reduces hallucination risk

### Pipeline

```
1. SCOPE: Identify NAIC group from resource's org path / calendar item
   → Already implemented in enrich_refs.py

2. FETCH CANDIDATES: Query Bubble for Agenda Items discussed at this NAIC group
   → New: client.search("Agenda item", [{"key": "Discussed at list", ...}])
   → Bounded: typically 5-20 items per group

3. DETERMINISTIC MATCHING (try first):
   a. Ref # match: if resource Name contains "SAPWG#2024-04" or similar → exact match
   b. Title keyword match: compare resource Name against BA title / NAIC Title
      using token overlap scoring (similar to existing calendar_candidate scoring)
   c. If score > threshold → match without AI

4. LLM RANKING (fallback when deterministic is ambiguous):
   - Send resource {Name, URL, notes, parent} + candidate agenda items
     {BA title, Ref #, Category, Description[:200]}
   - Ask: "Which agenda item(s) does this resource relate to? Return list of
     ref numbers or null if none fit."
   - Constrain output to candidate ref #s only
   - Confidence threshold (same pattern as existing topic AI)

5. OUTPUT: Set matched agenda item IDs on the resource or calendar item
```

### Why This Works

From the data:
- Calendar titles like `"NAIC SAPWG | Cryptocurrency; Bond Definition and Reporting"` encode the same topics found in agenda items
- Resource names like `"Bond Definition – Debt Securities Issued by Funds (SAPWG#2024-01)"` often contain the exact ref #
- When names are less specific (e.g., `"Meeting Materials"`) the NAIC group scoping narrows candidates enough for LLM to disambiguate

### Implementation Cost: Medium

- New Bubble query function for Agenda Items by NAIC group (~50 lines in `lookups.py`)
- New matching function with ref # extraction + token scoring (~100 lines in `enrich_refs.py`)
- LLM fallback using existing `openai_client.chat_json` pattern (~80 lines)
- Integration into `enrich_refs()` pipeline (~30 lines)

---

## Tier 2: Topic Suggestion (Chronicles)

### Approach: Inherit from Agenda Item, AI Fallback

**Primary path (when agenda item matched):**
```
Resource → matched Agenda Item → Agenda Item.Topics → Chronicles tree node IDs
```

This is purely deterministic once the agenda item match is established. Agenda Items already have curated `Topics` (list of Chronicles node IDs).

**Fallback path (when no agenda item match):**
```
Resource → existing _resolve_topic_suggestion_ai() → Chronicles tree node
```

This is the existing code path in `enrich_refs.py:711-833`. It works but requires AI and has a 0.65 confidence threshold.

### Decision Logic

```python
def resolve_topic_for_resource(resource, matched_agenda_items, topic_candidates):
    # Path A: inherit from agenda item
    if matched_agenda_items:
        topics_from_agenda = []
        for ai in matched_agenda_items:
            topics_from_agenda.extend(ai.get("Topics") or [])
        if topics_from_agenda:
            # Pick the most relevant one (first, or use resource context to disambiguate)
            return topics_from_agenda[0]  # or best match

    # Path B: AI classification (existing code)
    if use_ai and topic_candidates:
        return _resolve_topic_suggestion_ai(resource, context, topic_candidates)

    # Path C: deterministic from calendar item title parsing
    # Parse "NAIC GROUP | Topic1; Topic2" and fuzzy-match against Chronicles nodes
    cal_title = get_linked_calendar_title(resource)
    if cal_title:
        parsed_topics = parse_calendar_title_topics(cal_title)
        matched_nodes = fuzzy_match_to_chronicles(parsed_topics, topic_candidates)
        if len(matched_nodes) == 1:
            return matched_nodes[0]

    return None
```

### Why This Works

From the data:
- Agenda Items have curated Topics that are ground truth
- When no agenda item match, the existing AI path works for ~65% of cases (based on production confidence distribution)
- Calendar title parsing provides a deterministic fallback that catches many cases the AI would handle

### Implementation Cost: Low-Medium

- Topic inheritance from agenda item: ~20 lines (trivial once agenda matching exists)
- Calendar title topic parsing: ~40 lines (regex `split("|")[1].split(";")` + fuzzy match to Chronicles nodes)
- Integration: minimal — slots into existing `enrich_refs()` between Type1 resolution and the current AI topic path

---

## Architecture Summary

```
New Resource Detected
        │
        ▼
[1] Org/Group Resolution (existing, deterministic)
        │
        ▼
[2] Calendar Item Linking (existing, deterministic + date matching)
        │
        ▼
[3] Agenda Item Matching ← NEW
    ├── Fetch candidates by NAIC group (Bubble query)
    ├── Deterministic: ref # match, title token scoring
    └── LLM fallback: rank candidates with AI
        │
        ▼
[4] Topic Suggestion ← ENHANCED
    ├── Primary: inherit from matched Agenda Item.Topics
    ├── Secondary: parse Calendar Item title topics
    └── Fallback: AI classification (existing)
        │
        ▼
[5] Type1 Classification (existing, deterministic)
```

---

## What Needs to Happen

### Phase 1: Agenda Item Data Access
1. Add `"Agenda item"` type to `bubble/lookups.py` (type constant + query helpers)
2. Add Agenda Items to `bubble/snapshot.py` (include in snapshot for offline matching)
3. Add Agenda Item field picking to `bubble/mapping_context.py`

### Phase 2: Agenda Item Matching
4. Implement ref # extraction regex (e.g., `SAPWG#2024-04`, `VOSTF#2023-005`)
5. Implement candidate scoring (token overlap on titles)
6. Implement LLM ranking fallback (same pattern as topic AI)
7. Wire into `enrich_refs()` pipeline

### Phase 3: Topic Enhancement
8. Implement topic inheritance from matched agenda items
9. Implement calendar title topic parsing
10. Adjust topic suggestion resolution order: agenda item → calendar title → AI

### Phase 4: Validation
11. Back-test against the 19 known agenda item ↔ resource associations
12. Evaluate topic accuracy against the 100 resources with known `topic suggestion`
13. Add to existing test suite

---

## Risk Assessment

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Agenda items not yet created when resource detected | Medium | Fallback to AI topic suggestion; queue for re-matching |
| Multiple agenda items match with similar scores | Low-Medium | LLM disambiguation; log ambiguity for human review |
| Bubble API rate limits on Agenda Item queries | Low | Include in snapshot; cache aggressively |
| Chronicles tree changes (new topics added) | Low | Already handled by dynamic candidate loading |
| Ref # format varies across groups | Medium | Regex handles common patterns; LLM catches the rest |

---

## Recommendation Priority

1. **Start with Agenda Item Matching** — this is the higher-value, harder problem. Topic suggestion improvement follows naturally once matching works.
2. **Calendar title parsing** can be shipped independently as a quick win for topic suggestion improvement (no Bubble writes, no new API calls needed).
3. **Snapshot expansion** (adding Agenda Items) should happen early — it enables offline development and testing without hitting the Bubble API.

---

## Confirming the Initial Hypothesis

> I suspect the answer may be: deterministic or semi-deterministic extraction for agenda items, AI-assisted topic suggestion constrained to valid Bubble Chronicle topics.

**Confirmed with refinement:**

- **Agenda items:** Semi-deterministic. Ref # matching is deterministic. Title matching is semi-deterministic (token scoring). LLM is the fallback, but constrained to Bubble candidates — so it's "AI-assisted candidate selection", not open-ended extraction.
- **Topic suggestion:** Primarily inherited from agenda items (deterministic once matched). AI is the fallback, already implemented and constrained to Chronicles tree candidates. Calendar title parsing adds a deterministic middle tier.

The data strongly supports this hybrid approach: the structured nature of NAIC agenda items (ref #s, group scoping, curated topics) makes deterministic matching viable for the majority of cases, with AI handling the ambiguous tail.
