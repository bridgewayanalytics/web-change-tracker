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

## Key Finding 5: PDF Content Is Not the Primary Signal for Topic Assignment

For the 100 resources with `topic suggestion`:
- 66 are PDFs, 34 are not
- Only 17/100 have significant word overlap between topic name and resource Name
- The `parent` field (org hierarchy) is a stronger predictor of topic than resource title
- Meeting materials PDFs from NAIC often have the same URL (bulk materials PDF) but different agenda items

Example: Resources for SAPWG meeting (5 different agenda items) all share the same URL:
`https://content.naic.org/sites/default/files/national_meeting/Materials-SAPWG-Hearing-8-13-24.pdf`

This means **PDF URL alone cannot distinguish agenda items** — the resource Name (which encodes the specific agenda topic) is the key differentiator.

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

## Summary Table

| Signal | Availability | Reliability for Topic | Reliability for Agenda Item |
|--------|-------------|----------------------|---------------------------|
| Resource Name | Always | Medium (17% word overlap) | High (often contains ref # or topic keywords) |
| Parent / Org Path | Always | Medium-High (narrows to group) | Medium (narrows to group, not item) |
| Calendar Item Title | When linked | High (encodes topics explicitly) | High (encodes topic list) |
| PDF URL | For PDFs | Low (shared URLs) | Low (shared URLs) |
| PDF Text Content | For downloadable PDFs | Medium (group name in header) | Medium (may contain ref # in body) |
| NAIC Group | Via enrichment | Medium (many topics per group) | Medium (scopes candidate items) |
| Agenda Item BA Ref # | In Bubble | N/A | High (exact match if ref # in title) |
