# Findings — Agenda Item & Topic Suggestion Analysis

## Data Scope

- **87** Chronicles tree nodes (the topic taxonomy)
- **100** Bubble Resources with `topic suggestion` populated
- **19** resolved Agenda Items (from 9 Calendar Items that have `attached agenda items`)
- **68** Resources referenced by those Agenda Items
- **0** PDF resources with both `topic suggestion` AND a `.pdf` URL via Bubble constraint (see note below)
- **5/100** resources with `topic suggestion` that also appear in Agenda Item resource lists

## The Bubble Data Model (as-built)

```
Calendar Item
  ├── title: "NAIC SAPWG | Topic1; Topic2; ..."   ← topics encoded in title
  ├── NAIC Group (tree node): Organization tree ID
  ├── attached agenda items: [Agenda Item IDs]
  ├── Agenda: [{url, title}]                       ← PDF/doc links from meeting page
  └── Relevant Documents: []                       ← not populated

Agenda Item (distinct Bubble type: "Agenda item")
  ├── BA title / NAIC Title                        ← human-readable name
  ├── BA Ref # / Ref #                             ← e.g. "SAPWG#2024-04"
  ├── Topics: [Chronicles tree node IDs]           ← THE LINK TO CHRONICLES
  ├── Resources: [Resource IDs]                    ← docs associated with this item
  ├── Discussed at / Discussed at list             ← NAIC group tree node ID(s)
  ├── Category, Priority, Status, Date Added
  ├── SSAP Ref. / SSAP Ref. - texts               ← regulatory standard reference
  ├── Proposed By, Relevant Agenda Items
  └── Description                                  ← substantive description text

Resource
  ├── Name, URL, notes, date
  ├── parent                                       ← org hierarchy ID (historical) or path (our system)
  ├── topic suggestion: Chronicles tree node ID    ← set independently from agenda items
  ├── Organization, Type1
  └── Related calendar items: [Calendar Item IDs]
```

## Key Finding 1: Agenda Items Are the Bridge Between Resources and Topics

Agenda Items are the central entity connecting Resources to Chronicles topics. The relationship chain is:

**Resource → (Agenda Item.Resources) → Agenda Item → (Agenda Item.Topics) → Chronicles tree node**

This means: if you can match a new resource to an existing Agenda Item, you get its topic(s) for free.

## Key Finding 2: Calendar Item Titles Encode Topics

Calendar item titles follow a structured pattern:

```
"NAIC {GROUP} | {Topic1}; {Topic2}; {Topic3}; ..."
```

Examples:
- `"NAIC SAPWG | Cryptocurrency; Bond Definition and Reporting; CECL; Tax Credit Investments"` → 10 agenda items
- `"NAIC VOSTF | CLOs and ABS; NAIC Designations and Use of Agency Ratings"` → 3 agenda items
- `"NAIC E-Committee | Investment Oversight Framework"` → 1 agenda item

The semicolon-separated topics in the title are **abbreviated versions of Chronicles tree node names**. This is deterministically parseable.

## Key Finding 3: Topic Suggestion Is Assigned Independently

Only 5/100 resources with `topic suggestion` also appear in Agenda Item resource lists. This means:

- `topic suggestion` on Resources is **not derived from Agenda Items** — it's assigned separately (manually or via our AI enrichment)
- Agenda Items have their own `Topics` field (a list, not single-select)
- These two topic assignment mechanisms are **complementary, not redundant**

## Key Finding 4: Agenda Items Pre-Exist and Are Curated

Agenda Items are created ahead of meetings by Bridgeway analysts. They have:
- Stable reference numbers (e.g., `SAPWG#2024-04`)
- Curated descriptions and status updates
- Cross-references to related agenda items
- SSAP references (regulatory standard numbers)

They are **not auto-generated** — they represent editorial analysis.

## Key Finding 5: PDF Content Is a Strong Structural Signal, but Noisy for Topic Selection

> **Updated after PDF content analysis** — the original finding stated "PDF Content Is Not the Primary Signal for Topic Assignment." After downloading and parsing 91 PDFs, this finding is revised: PDF content provides **strong structural signals** but remains **noisy for topic selection specifically**.

### What PDF content IS good at (91 PDFs analyzed):

- **84.6%** contain numbered agenda items — extractable structured lists
- **75.8%** have the NAIC group name in the header — reliable meeting context
- **44.0%** have an explicit "AGENDA" header — formal agenda identification
- **36.3%** contain reference numbers (e.g., `Ref #2024-16`) — **high-precision identifiers for Agenda Item matching**
- **37.4%** contain SSAP references — specificity for accounting standard items
- **78.0%** contain at least one Chronicle topic name in text

### What PDF content is NOT good at:

- **Topic selection accuracy is low:** Only **31.9%** of PDFs contain the **specific** Chronicle topic that Bubble assigned to the resource. PDFs average 3.4 chronicle topic name matches — too many to pick the right one.
- **External publications** (EIOPA, IAIS, FSB reports) don't follow NAIC conventions at all
- **Supporting materials/proposals** lack agenda structure — they're documents *about* one topic, not meeting agendas listing topics
- **Shared URLs remain a problem:** Multiple resources share the same bulk materials PDF URL

### The revised picture:

PDF content provides two distinct value streams:
1. **Agenda item matching** via ref # extraction (high precision, medium recall at 36%)
2. **Candidate narrowing** for topic suggestion (78% have topic hits, but 32% accuracy for the *correct* topic)

PDF URL alone still cannot distinguish agenda items — but **PDF text content** can, via reference numbers.

For the 100 resources with `topic suggestion`:
- 66 are PDFs, 34 are not
- Only 17/100 have significant word overlap between topic name and resource Name
- The `parent` field (org hierarchy) is a stronger predictor of topic than resource title alone
- Meeting materials PDFs from NAIC often have the same URL (bulk materials PDF) but different agenda items

Example: Resources for SAPWG meeting (5 different agenda items) all share the same URL:
`https://content.naic.org/sites/default/files/national_meeting/Materials-SAPWG-Hearing-8-13-24.pdf`

## Key Finding 6: The Chronicles Tree Is Manageable

87 nodes total, 1 root ("Chronicles") with ~10 top-level categories:
- NAIC Investments (10 subtopics)
- NAIC Strategic Asset Allocation and ALM (6 subtopics)
- NAIC Ownership and Capital Structure (5 subtopics)
- Climate Guidelines (10 subtopics)
- The International Landscape (6 subtopics)
- State Investments (10 subtopics)
- Rating Agencies (5 subtopics)
- United States Federal Guidelines (3 subtopics)
- NAIC Investments – Retrospectives (7 subtopics)
- NAIC Investments – Treatment of Funds (8 subtopics)

This is small enough for a constrained AI classifier (existing code already does this) but also structured enough for deterministic matching in many cases.

## Key Finding 7: Agenda Item Association Is the Hard Problem

The system already handles:
- Organization resolution (deterministic from org path)
- Type1 classification (deterministic from section_type)
- Calendar item linking (NAIC group + date matching)
- Topic suggestion (AI-assisted from Chronicles tree)

What it **does not handle**:
- Matching a new resource to existing Agenda Items in Bubble
- Setting `attached agenda items` on Calendar Items
- Setting `Relevant Documents` on Calendar Items

The agenda item association requires understanding:
1. Which NAIC group's agenda items are relevant (from the calendar item / org path)
2. Which specific agenda item matches the resource title/content
3. Whether the resource is a new document for an existing agenda item, or something unrelated

## Key Finding 8: NAIC PDFs Have Reliable Agenda Structure (Updated after PDF content analysis)

> **Added after PDF content analysis.**

Analysis of 91 downloadable NAIC PDF resources reveals that meeting agenda PDFs follow consistent structural patterns:

### Four PDF document types identified:

| Type | Frequency | Key Feature |
|------|-----------|-------------|
| **Formal NAIC agenda** | 42.9% | "AGENDA" header + numbered items + roll call/adjournment |
| **Numbered item list** | 41.8% | Numbered items without explicit header |
| **Supporting materials** | ~10% | Proposals, reports — no agenda structure |
| **External publications** | ~6% | Non-NAIC documents — no NAIC conventions |

### Concrete SAPWG example (strongest case for PDF-based matching):

A SAPWG meeting agenda PDF contained:
```
1. Ref #2024-16: Repacks and Derivative Instruments
2. Ref #2024-22: ASU 2024-01, Scope Application...
...
1. Ref #2022-14: Tax Credits Project
```

Both Bubble Agenda Items linked to this resource had their ref numbers in the PDF: **100% match rate** via ref # alone.

### Parent committee agendas are multi-topic:

The Capital Adequacy (E) Task Force agenda PDF contained 44 numbered items and **7 distinct Chronicle topic names** in the text:
- CLOs and ABS, Tax Credit Structures, Short-Term Investments, Collateral Loans, Generator of Economic Scenarios (GOES), NAIC U.S. Government Money Market Funds, Repurchase Agreements

This makes parent committee agendas useful for **detecting which topics are active at a given meeting**, even if they can't select the one "correct" topic for a specific resource.

## Summary Table

> **Updated after PDF content analysis** — PDF Text Content row revised with empirical data.

| Signal | Availability | Reliability for Topic | Reliability for Agenda Item |
|--------|-------------|----------------------|---------------------------|
| Resource Name | Always | Medium (17% word overlap) | High (often contains ref # or topic keywords) |
| Parent / Org Path | Always | Medium-High (narrows to group) | Medium (narrows to group, not item) |
| Calendar Item Title | When linked | High (encodes topics explicitly) | High (encodes topic list) |
| PDF URL | For PDFs | Low (shared URLs) | Low (shared URLs) |
| **PDF Text: Ref numbers** | **36% of PDFs** | N/A | **Very High (near-perfect when present)** |
| **PDF Text: Group header** | **76% of PDFs** | Medium (scoping) | Medium (scoping) |
| **PDF Text: Agenda items** | **85% of PDFs** | Medium (multi-topic noise) | **High (lists all items at meeting)** |
| **PDF Text: Chronicle names** | **78% of PDFs** | **Low-Medium (32% correct match)** | Low (too noisy) |
| NAIC Group | Via enrichment | Medium (many topics per group) | Medium (scopes candidate items) |
| Agenda Item BA Ref # | In Bubble | N/A | High (exact match if ref # in title) |
