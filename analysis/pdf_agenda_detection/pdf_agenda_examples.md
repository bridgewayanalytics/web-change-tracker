# PDF Agenda Structure Examples

Concrete examples showing the structure of agenda sections in NAIC meeting material PDFs, and how they map to Bubble Agenda Items and Chronicle topics.

## Example 1: Formal NAIC Task Force Agenda

**Resource:** Climate and Resiliency (EX) Task Force - November 19, 2024 - Meeting Agenda

**PDF first-page structure:**
```
Draft date: 11/7/24

2024 Fall National Meeting
Denver, Colorado

CLIMATE AND RESILIENCY (EX) TASK FORCE
Tuesday, November 19, 2024
9:30 – 10:45 a.m.
Gaylord Rockies Hotel— Aurora Ballroom A— Level 2

ROLL CALL
...

1. Consider Adoption of its Summer National Meeting Minutes
2. Receive a Status Report on Deliverables from the National Climate...
3. Hear a Presentation from United Policyholders (UP) on Wildfire Effects
4. Hear an Update from BC Financial Services Authority (BCFSA)...
5. Hear a Federal Update— Alexander Swindle (NAIC)
6. Hear an International Update— Ryan Workman (NAIC)
7. Discuss Any Other Matters Brought Before the Task Force
8. Adjournment
```

**Extractable signals:**
- Group name: "CLIMATE AND RESILIENCY (EX) TASK FORCE" (from header)
- Date: November 19, 2024 (from header)
- 8 numbered agenda items
- Formal meeting structure (roll call → items → adjournment)

**Bubble topic suggestion:** NAIC Climate Initiatives
**PDF topic detection:** No chronicle topic names matched — topics are implicit in meeting context, not explicit in agenda text

---

## Example 2: Working Group Agenda with Reference Numbers

**Resource:** SAPWG Meeting Agenda & Materials - February 25, 2025

**PDF extracted agenda items:**
```
1. Ref #2024-16: Repacks and Derivative Instruments
2. Ref #2024-22: ASU 2024-01, Scope Application of Profits Interest...
3. Ref #2024-25: SSAP No. 16 Clarifications
1. Ref #2022-14: Tax Credits Project
2. Ref #2024-10: SSAP No. 56 – Book Value and Separate Account
3. Ref #2024-23: Derivative Premium Clarifications
4. Ref #2024-24: Medicare Part D – Prescription Payment Plan
5. Ref #2024-27: Issue Papers in the Statutory Hierarchy
6. Ref #2024-28: Holders of Capital Notes
```

**Linked Bubble Agenda Items:**
1. Tax credit structures (ref: SAPWG#2022-14 and LRBCWG#2024-L9)
2. Repacks And Derivative Wrapper Investments (ref: SAPWG#2024-16 and BWG#2025-01)

**Match analysis:**
- **Ref #2024-16** in PDF directly matches SAPWG#2024-16 Bubble agenda item
- **Ref #2022-14** in PDF directly matches SAPWG#2022-14 Bubble agenda item
- **Both items found by ref number alone** — no title matching needed

---

## Example 3: Capital Adequacy Task Force (Multi-Topic Agenda)

**Resource:** Capital Adequacy (E) Task Force - November 18, 2024

**PDF structure:** 44 numbered items spanning multiple working group reports

**Reference numbers found in PDF:** 2024-22, 2022-14, 2024-26, 2023-07, 2023-12, 2024-11, 2024-25, 2024-23

**Chronicle topics found in PDF text:**
- Collateralized Loan Obligations (CLOs) and Asset-Backed Securities (ABS)
- Tax Credit Structures
- Short-Term Investments
- NAIC U.S. Government Money Market (Mutual) Funds List
- Generator of Economic Scenarios (GOES)
- Collateral Loans
- Repurchase Agreements

**Key observation:** This is a parent committee agenda that covers multiple working groups. The PDF contains **7 distinct Chronicle topic names** as natural text within the agenda items. These directly correspond to Chronicles tree nodes.

---

## Example 4: Materials PDF with Embedded Proposal

**Resource:** Life RBC Tax Credit MOD (LRBCWG#2024-21)

**PDF structure:** This is a specific proposal document, not a meeting agenda. Only 1 extracted "numbered item" (a table column).

**Linked Bubble Agenda Item:** Tax credit structures (ref: SAPWG#2022-14 and LRBCWG#2024-L9)

**Key observation:** Not all PDFs are agendas — some are supporting documents for a specific agenda item. The resource Name contains the ref number `LRBCWG#2024-21` which identifies the proposal. These documents map 1:1 to an agenda item but the PDF itself doesn't list agenda items.

---

## Example 5: External Publication (Non-NAIC)

**Resource:** December 2024 Financial Stability Report

**PDF structure:** A report from an external regulatory body (EIOPA). No agenda structure, no ref numbers.

**Bubble topic:** European Insurance and Occupational Pensions Authority (EIOPA)
**PDF topics detected:** State Investments, Commercial Real Estate Equity (false positives — generic words appear in many financial documents)

**Key observation:** External publications don't follow NAIC agenda conventions. Topic assignment here is editorial and based on the source organization, not PDF content. The PDF topic detection produces noise for non-NAIC documents.

---

## Structural Patterns Observed

### Pattern A: Formal NAIC Meeting Agenda
```
[HEADER]
  Meeting name / Group name
  Date, Time
  Location

ROLL CALL / CALL TO ORDER

1. Consider Adoption of [previous] Minutes
2. [Substantive item] — [Presenter Name] ([State])
3. [Substantive item]
...
N. Adjournment
```
**Frequency:** 42.9% of PDFs
**Best signals:** Group name in header, numbered items, roll call marker

### Pattern B: SAPWG-style with Reference Numbers
```
[HEADER]

ITEMS FOR ADOPTION:
1. Ref #YYYY-NN: [Title]
2. Ref #YYYY-NN: [Title]

ITEMS FOR EXPOSURE:
1. Ref #YYYY-NN: [Title]
2. Ref #YYYY-NN: [Title]
```
**Frequency:** ~15-20% of formal agendas
**Best signals:** Ref # patterns, direct match to Bubble agenda item BA Ref #

### Pattern C: Supporting Materials / Proposals
```
[TITLE: Proposal Name (RefNumber)]
[Document body — no agenda structure]
```
**Frequency:** ~30% of PDFs
**Best signals:** Ref number in title/filename, topic keywords in body

### Pattern D: External Publications
```
[Third-party document — no NAIC structure]
```
**Frequency:** ~20% of PDFs
**Best signals:** None reliable from PDF content; must rely on source context
