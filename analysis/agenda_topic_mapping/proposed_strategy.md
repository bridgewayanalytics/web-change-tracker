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

> **Updated after PDF content analysis** — Step 3 now includes PDF ref # extraction as a first-class signal. This strengthens the deterministic matching tier significantly for SAPWG-style agendas.

```
1. SCOPE: Identify NAIC group from resource's org path / calendar item
   → Already implemented in enrich_refs.py

2. FETCH CANDIDATES: Query Bubble for Agenda Items discussed at this NAIC group
   → New: client.search("Agenda item", [{"key": "Discussed at list", ...}])
   → Bounded: typically 5-20 items per group

3. DETERMINISTIC MATCHING (try first, multiple signal sources):
   a. Ref # match from resource Name: if "SAPWG#2024-04" in Name → exact match
   b. Ref # match from PDF text: download PDF, extract ref numbers via regex
      → 36% of PDFs contain ref numbers; when present, near-100% match rate
   c. Title keyword match: compare resource Name against BA title / NAIC Title
      using token overlap scoring (similar to existing calendar_candidate scoring)
   d. PDF agenda item titles: if PDF is a formal agenda, compare extracted
      numbered items against BA title / NAIC Title
   e. If score > threshold → match without AI

4. LLM RANKING (fallback when deterministic is ambiguous):
   - Send resource {Name, URL, notes, parent} + candidate agenda items
     {BA title, Ref #, Category, Description[:200]}
   - Optionally include: PDF first-page text (up to 500 chars) for context
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
- **PDF content adds a second ref# extraction channel:** SAPWG agendas list `Ref #2024-16: Repacks and Derivative Instruments` as numbered items — extractable by regex with near-perfect precision
- **76% of PDFs contain the NAIC group name in the header** — confirms group scoping independently of org path
- When names are less specific (e.g., `"Meeting Materials"`) the NAIC group scoping narrows candidates enough for LLM to disambiguate

### Implementation Cost: Medium

- New Bubble query function for Agenda Items by NAIC group (~50 lines in `lookups.py`)
- New matching function with ref # extraction + token scoring (~100 lines in `enrich_refs.py`)
- **PDF ref # extraction** — lightweight regex already prototyped in analysis script (~30 lines)
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

> **Updated after PDF content analysis** — Path D added for PDF-based topic narrowing. Path B (AI) now benefits from PDF content as additional context.

```python
def resolve_topic_for_resource(resource, matched_agenda_items, topic_candidates, pdf_text=None):
    # Path A: inherit from agenda item (highest accuracy)
    if matched_agenda_items:
        topics_from_agenda = []
        for ai in matched_agenda_items:
            topics_from_agenda.extend(ai.get("Topics") or [])
        if topics_from_agenda:
            # Pick the most relevant one (first, or use resource context to disambiguate)
            return topics_from_agenda[0]  # or best match

    # Path B: deterministic from calendar item title parsing
    cal_title = get_linked_calendar_title(resource)
    if cal_title:
        parsed_topics = parse_calendar_title_topics(cal_title)
        matched_nodes = fuzzy_match_to_chronicles(parsed_topics, topic_candidates)
        if len(matched_nodes) == 1:
            return matched_nodes[0]

    # Path C: AI classification (existing code, enhanced with PDF context)
    if use_ai and topic_candidates:
        # If PDF text available, include detected topic names as hints
        # (78% of PDFs contain chronicle topics; narrows AI candidates)
        pdf_topic_hints = detect_chronicle_names_in_text(pdf_text) if pdf_text else []
        return _resolve_topic_suggestion_ai(
            resource, context, topic_candidates,
            pdf_topic_hints=pdf_topic_hints,
        )

    # Path D: PDF-detected topics as last resort (no AI)
    # Only if exactly 1 chronicle topic found in PDF text
    if pdf_text:
        pdf_topics = detect_chronicle_names_in_text(pdf_text)
        if len(pdf_topics) == 1:
            return pdf_topics[0]

    return None
```

### Why This Works

From the data:
- Agenda Items have curated Topics that are ground truth
- When no agenda item match, the existing AI path works for ~65% of cases
- Calendar title parsing provides a deterministic fallback
- **PDF content adds useful context for AI classification:** 78% of PDFs contain at least one chronicle topic name, which narrows the candidate space. However, alone the 32% correct-match rate means PDF topics should **inform** AI, not replace it
- The single-match PDF path (Path D) handles the rare case where a document clearly belongs to exactly one topic

### Implementation Cost: Low-Medium

- Topic inheritance from agenda item: ~20 lines (trivial once agenda matching exists)
- Calendar title topic parsing: ~40 lines (regex `split("|")[1].split(";")` + fuzzy match to Chronicles nodes)
- Integration: minimal — slots into existing `enrich_refs()` between Type1 resolution and the current AI topic path

---

## Architecture Summary

> **Updated after PDF content analysis** — PDF content extraction added as a cross-cutting step that feeds into both agenda item matching and topic suggestion.

```
New Resource Detected
        │
        ▼
[1] Org/Group Resolution (existing, deterministic)
        │
        ├──────────────────────────────────────┐
        ▼                                      ▼
[2] Calendar Item Linking              [2b] PDF Content Extraction ← NEW
    (existing, deterministic)                (if URL ends .pdf)
        │                                      │
        │   ┌──────────────────────────────────┘
        │   │  Outputs: ref_numbers, group_name,
        │   │  extracted_items, chronicle_topics
        ▼   ▼
[3] Agenda Item Matching ← NEW
    ├── Ref # match (from resource Name OR PDF text)
    ├── Title token scoring (resource Name vs BA title)
    ├── PDF agenda items vs BA title (when PDF is agenda)
    └── LLM fallback: rank candidates with PDF context
        │
        ▼
[4] Topic Suggestion ← ENHANCED
    ├── Primary: inherit from matched Agenda Item.Topics
    ├── Secondary: parse Calendar Item title topics
    ├── Tertiary: AI classification (with PDF topic hints)
    └── Last resort: single PDF chronicle topic match
        │
        ▼
[5] Type1 Classification (existing, deterministic)
```

---

## What Needs to Happen

> **Updated after PDF content analysis** — Phase 1b added for PDF extraction infrastructure.

### Phase 1a: Agenda Item Data Access
1. Add `"Agenda item"` type to `bubble/lookups.py` (type constant + query helpers)
2. Add Agenda Items to `bubble/snapshot.py` (include in snapshot for offline matching)
3. Add Agenda Item field picking to `bubble/mapping_context.py`

### Phase 1b: PDF Content Extraction Infrastructure
4. Extend `scrape/pdf_meeting_meta.py` (or create sibling module) with agenda structure extraction:
   - Ref # regex extraction (already prototyped: `RE_REF_NUMBER` pattern)
   - Numbered agenda item extraction (already prototyped: `RE_NUMBERED_ITEM`)
   - Chronicle topic name detection (string matching against tree node names)
5. Integrate into `apply_pdf_meeting_metadata()` or parallel pipeline step
6. Store results in `__pdf_agenda_signals` debug field (stripped before Bubble, like `__meeting_meta`)

### Phase 2: Agenda Item Matching
7. Implement ref # extraction from resource Name (regex)
8. Implement candidate scoring (token overlap on titles)
9. **Add PDF ref # as matching signal:** if PDF downloaded and ref #s extracted, use them alongside Name-based matching
10. Implement LLM ranking fallback (same pattern as topic AI)
11. Wire into `enrich_refs()` pipeline

### Phase 3: Topic Enhancement
12. Implement topic inheritance from matched agenda items
13. Implement calendar title topic parsing
14. **Add PDF chronicle topics as hints for AI classifier** (pass detected names as context to `_resolve_topic_suggestion_ai`)
15. Adjust topic suggestion resolution order: agenda item → calendar title → AI (with PDF hints) → single PDF topic

### Phase 4: Validation
16. Back-test against the 19 known agenda item ↔ resource associations
17. Evaluate topic accuracy against the 100 resources with known `topic suggestion`
18. **Validate PDF extraction against the 91 analyzed PDFs** — verify ref # extraction precision
19. Add to existing test suite

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

## System Architecture Recommendation (Updated after PDF content analysis)

Three options were evaluated:

### Option A: Agenda item matching primarily from Bubble data

**Strengths:** Bubble Agenda Items have curated Topics, structured ref #s, and explicit resource lists. Group scoping via `Discussed at` is reliable. Calendar item titles encode topics deterministically.

**Weaknesses:** Only 19 Agenda Items in sample; coverage is limited. Agenda Items may not exist when new resources first arrive. No signal from document content.

### Option B: Agenda item extraction primarily from PDF content

**Strengths:** 85% of PDFs have numbered items; 36% contain ref numbers; 76% have group name in header; 78% contain chronicle topic names. Strong structural signal for NAIC meeting agendas.

**Weaknesses:** Only 32% accuracy for topic *selection* from PDF (too noisy — multiple topics per document). Supporting materials/proposals lack agenda structure. External publications don't follow NAIC conventions. PDF-only matching cannot associate resources to Agenda Items (PDF lists items but doesn't link documents to them).

### Option C: Hybrid — PDF extraction → match to Bubble agenda items → inherit Chronicle topics

**This is the recommended approach.**

The evidence shows:
- **PDF ref # extraction** (36% availability, near-perfect precision) feeds directly into Bubble Agenda Item matching via `BA Ref #`
- **Bubble Agenda Items** provide the curated topic assignment (Topics field) and resource associations
- **Calendar item title parsing** provides a deterministic middle tier for topic suggestion
- **AI classification** handles the tail cases where neither PDF nor Bubble metadata suffice

The hybrid approach uses each signal where it's strongest:

| Step | Primary Signal | Accuracy |
|------|---------------|----------|
| Identify document type | PDF structure (numbered items, agenda header) | 85% |
| Scope NAIC group | Org path (deterministic) + PDF header (76%) | ~95% combined |
| Match agenda item | Resource Name ref# + PDF ref# → Bubble BA Ref# | High precision |
| Fall back on ambiguity | LLM ranking of Bubble candidates + PDF context | Medium-High |
| Assign topic | Inherit from matched Agenda Item.Topics | High (curated) |
| Topic fallback | Calendar title parsing → AI with PDF hints | Medium-High |

## Confirming the Initial Hypothesis

> I suspect the answer may be: deterministic or semi-deterministic extraction for agenda items, AI-assisted topic suggestion constrained to valid Bubble Chronicle topics.

**Confirmed and strengthened by PDF analysis:**

- **Agenda items:** Semi-deterministic with PDF as an additional signal channel. Ref # matching is deterministic (from both resource Name and PDF text). Title matching is semi-deterministic (token scoring). LLM is the fallback, constrained to Bubble candidates and informed by PDF content.
- **Topic suggestion:** Primarily inherited from agenda items (deterministic once matched). AI is the fallback, already implemented and constrained to Chronicles tree candidates. **PDF content provides useful narrowing hints** (78% of PDFs contain chronicle topics) that can improve AI accuracy, but is **too noisy to use as the sole signal** (32% correct match rate).

The PDF analysis reveals that document content is a **strong complement to Bubble metadata**, not a replacement. The structured nature of NAIC agenda PDFs (ref numbers, numbered items, group headers) provides exactly the signals needed to match against Bubble's curated Agenda Item catalog. The recommended approach — Option C — uses PDF content to strengthen deterministic matching while relying on Bubble data for authoritative topic assignment.
