# PDF Agenda Detection — Statistics

## Dataset

- **100** PDF resources analyzed
- **91** had extractable text (9 download failures)
- Sources: 52 from resources with topic suggestion, 48 from agenda-item-linked resources
- Coverage: multiple NAIC groups (CATF, SAPWG, VOSTF, LATF, LRBCWG, RBC-IRE-WG, E-Committee, EX Committee, Climate TF, International G Committee)

## Agenda Structure Detection

| Structure Type | Count | Percentage | Description |
|---------------|-------|-----------|-------------|
| formal_agenda | 39 | 42.9% | Has "AGENDA" header + numbered items |
| numbered_list | 38 | 41.8% | Numbered items without explicit agenda header |
| informal | 7 | 7.7% | Discussion keywords but no structured list |
| none | 3 | 3.3% | No agenda structure detected |
| meeting_minutes | 2 | 2.2% | Roll call + adjournment, past-tense |
| outline | 2 | 2.2% | Roman numeral or letter items |

**84.6% of PDFs contain numbered items** — this is the strongest structural signal.

## Signal Presence Rates (91 PDFs with text)

| Signal | Count | Rate | Reliability |
|--------|-------|------|-------------|
| Discussion keywords | 85 | 93.4% | High (but common in any document) |
| Numbered items | 77 | 84.6% | **High** — strongest structural signal |
| Chronicle topic in text | 71 | 78.0% | **Medium-High** — but noisy (avg 3.4 topics/PDF) |
| Group name in header | 69 | 75.8% | **High** — reliable for meeting context |
| Roll call / opening | 44 | 48.4% | High — formal meeting indicator |
| Agenda header | 40 | 44.0% | **High** — strong when present |
| SSAP references | 34 | 37.4% | High specificity for SAPWG-family docs |
| Reference numbers | 33 | 36.3% | **Very high specificity** — direct agenda item ID |
| Roman numeral items | 24 | 26.4% | Medium — some false positives from TOC |

## Key Matching Statistics

### Chronicle Topic Matching: PDF Text vs Bubble Assignment

| Metric | Count | Rate |
|--------|-------|------|
| Resources with Bubble topic suggestion | 91 | 100% |
| Bubble topic found in PDF text | 29 | **31.9%** |
| Bubble topic NOT in PDF text | 62 | 68.1% |
| PDFs with ANY chronicle topic in text | 71 | 78.0% |

**Interpretation:** PDFs frequently contain Chronicle topic keywords (78%), but the **specific topic assigned in Bubble** only appears in the PDF 32% of the time. This means:
- PDF text matches **some** chronicle topic ~78% of the time
- But it matches **the correct/assigned** topic only ~32% of the time
- Topic assignment requires editorial judgment beyond string matching

### Why the 68% Mismatch?

Three main causes:

1. **Multi-topic documents (40% of mismatches):** Parent committee agendas list items across many topics. The PDF contains 5-7 topic names but the resource is assigned to just one. The system would need to pick the right one.

2. **External publications (25% of mismatches):** Documents from EIOPA, IAIS, FSB, BMA don't use NAIC topic names. The topic is assigned based on the publishing organization, not document content.

3. **Topic names too generic/specific (35% of mismatches):** "Private Equity Owned Insurers" doesn't appear verbatim in PDFs about PE-owned insurers. "ALM Derivatives & Derivative Investments" doesn't appear exactly even in derivatives proposals.

### Agenda Item Matching: PDF Content vs Bubble

From 9 resource-agenda item pairs (limited by our sample):

| Signal | Matches | Rate |
|--------|---------|------|
| BA title keywords in PDF first page | 1 | 11.1% |
| BA Ref # in PDF | 1 | 11.1% |
| BA title words in any extracted item | 3 | 33.3% |
| Ref # in PDF ref_numbers list | 2 | 22.2% |

**Note:** These rates appear low because:
1. The sample is small (only 9 pairs with agenda item links)
2. Many resources are supporting materials, not the agenda itself
3. The SAPWG example shows perfect ref# matching when the PDF IS an agenda

### SAPWG Agenda Deep Dive

For the SAPWG Meeting Agenda PDF (the most structured example):
- PDF contained **8 ref numbers** (2024-16, 2024-22, 2024-25, 2022-14, 2024-10, 2024-23, 2024-24, 2024-27, 2024-28)
- 2 of 2 linked Bubble Agenda Items had their ref numbers in the PDF: **100% match rate**
- This is the strongest evidence that ref# extraction from NAIC agendas is a reliable matching strategy

## Signal Strength Assessment

| Signal | Precision | Recall | Best For |
|--------|-----------|--------|----------|
| **Ref # extraction** | Very High | Medium (36%) | Agenda item matching (when present, almost always correct) |
| **Group name in header** | High | High (76%) | Scoping candidates (which group's items to search) |
| **Numbered item extraction** | High | Very High (85%) | Detecting agenda-type documents |
| **Chronicle topic in text** | Medium | High (78%) | Narrowing topic candidates (but too noisy alone) |
| **SSAP reference** | Very High | Low (37%) | Specific SAPWG document identification |
| **Agenda header** | Very High | Medium (44%) | Confirming document is a meeting agenda |
