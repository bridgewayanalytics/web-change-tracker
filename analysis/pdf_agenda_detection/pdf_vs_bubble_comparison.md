# PDF Content vs Bubble Data — Comparison

## The Central Question

Can PDF content replace or supplement Bubble metadata for agenda item matching and topic suggestion?

## Side-by-Side: Signal Sources

### For Agenda Item Matching

| Signal | From PDF | From Bubble | Winner |
|--------|----------|-------------|--------|
| Reference number (e.g., SAPWG#2024-04) | Found in 36% of PDFs; very high precision when present | Stored on every Agenda Item as `BA Ref #` / `Ref #` | **Tie — complementary.** PDF provides the ref# to match against Bubble's catalog |
| Agenda item title | Extracted from numbered items in 85% of PDFs | Stored as `BA title` / `NAIC Title` on every Agenda Item | **Bubble wins.** PDF titles are abbreviated; Bubble titles are curated |
| NAIC group scoping | Group name in PDF header (76%) | `Discussed at` field on Agenda Item; org path from scraping | **Bubble wins.** More reliable and structured |
| Item-to-resource association | Not in PDF (agenda lists items, doesn't link to doc URLs) | `Resources` field on Agenda Item links to Resource IDs | **Bubble wins.** The association is explicit |

### For Chronicle Topic Suggestion

| Signal | From PDF | From Bubble | Winner |
|--------|----------|-------------|--------|
| Topic name presence in text | 78% of PDFs contain some chronicle topic name | Topic already curated on Agenda Items | **PDF is noisy.** Multiple topics per doc; only 32% match the assigned one |
| Topic specificity | Low — avg 3.4 topics per PDF; can't pick the "right" one | Single curated topic per resource, or list on Agenda Item | **Bubble wins.** Curated > detected |
| Topic coverage | Some topics never appear verbatim (EIOPA, PE insurers) | Complete coverage — all assigned topics are valid tree nodes | **Bubble wins.** |
| Topic for new/unseen resources | Can detect candidate topics from PDF text | Must match to existing Agenda Item first | **PDF useful as fallback** when no Agenda Item match |

## Detailed Comparison: NAIC Meeting Agenda PDFs

### What PDFs are good at:

1. **Identifying the document as an agenda** (85% have numbered items, 44% have explicit "AGENDA" header)
2. **Extracting NAIC group** from the header (76% success rate)
3. **Extracting ref numbers** when present (36% of PDFs — but nearly 100% of SAPWG-style agendas)
4. **Listing all agenda item titles** as numbered items — this gives the full scope of a meeting
5. **Detecting that multiple topics are covered** — parent committee agendas list 5-7 distinct topic areas

### What PDFs are bad at:

1. **Picking the "right" topic** for a specific resource (32% match rate vs. Bubble's curated assignment)
2. **Associating a specific resource to a specific agenda item** — the PDF lists items but doesn't link docs
3. **Handling non-NAIC documents** — external publications don't follow agenda conventions
4. **Handling materials/proposals** vs. agendas — supporting documents have no agenda structure

### What Bubble data is good at:

1. **Curated topic assignment** — each Agenda Item has explicit `Topics` (Chronicles tree node IDs)
2. **Resource → Agenda Item linking** — the `Resources` field on Agenda Item is explicit
3. **Structured reference numbers** — `BA Ref #` is always populated and standardized
4. **Cross-references** — `Relevant Agenda Items` links related items

### What Bubble data is bad at:

1. **Coverage** — only 19 Agenda Items in sample; many meetings don't have them
2. **Timeliness** — Agenda Items may not exist when new resources are first detected
3. **Completeness** — not all resources are linked to Agenda Items (only 5% overlap in our sample)

## The Key Insight

**PDF content and Bubble metadata are complementary, not competing signals.**

The strongest PDF signal — **reference numbers** — is exactly what you need to match against Bubble Agenda Items. The matching flow is:

```
PDF text → extract ref numbers → lookup Agenda Item by ref# in Bubble → inherit topics
```

This is more reliable than:
```
PDF text → search for chronicle topic names → pick the right one
```

Because ref numbers are **unambiguous identifiers** while topic names are **noisy multi-matches**.

## Evidence Matrix

| Scenario | Best Primary Signal | Best Fallback |
|----------|-------------------|---------------|
| SAPWG/VOSTF-style agenda PDF | PDF ref# → Bubble Agenda Item | PDF extracted items + LLM ranking |
| Parent committee agenda (CATF, E-Committee) | PDF group header + Bubble Agenda Items by group | PDF chronicle topic detection + LLM selection |
| Supporting materials/proposals | Resource Name (often contains ref#) | PDF body text + Bubble Agenda Item title matching |
| External publications (EIOPA, IAIS) | Resource source context (org path) | LLM topic classification (existing path) |
| New resource, no Agenda Item exists | PDF content + LLM | Existing AI topic suggestion path |

## Revised Signal Strength Ranking

For agenda item matching, in order of reliability:

1. **Reference number match** (PDF or resource name → Bubble Agenda Item.BA Ref#) — highest precision
2. **NAIC group scoping** (PDF header or org path → Bubble Agenda Item.Discussed at) — narrows candidates
3. **Title keyword matching** (PDF extracted items or resource name → Bubble Agenda Item.BA title) — medium precision
4. **Chronicle topic detection in PDF** — useful for narrowing, not for final selection
5. **LLM ranking** of candidates — best for ambiguous cases

For topic suggestion, in order of reliability:

1. **Inherit from matched Agenda Item** — highest accuracy (Agenda Items have curated Topics)
2. **Calendar item title parsing** — structured ("GROUP | topic1; topic2")
3. **LLM topic classification** against Chronicles tree — existing fallback
4. **PDF chronicle topic detection** — too noisy alone (78% presence, 32% correct match)
