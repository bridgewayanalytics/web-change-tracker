# Page Change Agent — Prompt Improvement Recommendations
**Date:** June 24, 2026 | **Based on:** scoresheet_2026_06_23.md (20-row general eval) + targeted field evals

---

## Overview

Each section below shows the **current instruction text** for a field, the **root cause** of observed errors, and the **suggested replacement/addition**.

Priority order: Event Title > Chronicle Topics > Document Title > Newsreel Relevance > Event Start/End Time. The top three account for the majority of the accuracy gap; fixing them is expected to push overall accuracy from ~85% to ~92%+.

---

## 🔴 Event Title — 61.5% | Field 7

**Current instruction:**
> "For events, identify if the change is tied to an existing upcoming Event listed under 01_calendar_items.txt, in which case report the Event Title from 01_calendar_items.txt. If the change is tied to an upcoming Event not listed under 01_calendar_items.txt, **generate a descriptive Event Title in the style of the titles in the file.** If the alert is related to a past event report Old event and the event title. Otherwise report N/A."

**Root cause:** "In the style of the titles in the file" is unanchored. The agent reads whatever format happens to be in the calendar file and invents its own variants. Four distinct broken formats observed across 10 applicable rows:
- `NAIC Interim Meeting: [Group Name]` — invented prefix (rows 4, 14)
- `[Group Name] - 06252026` — date-code suffix (rows 7, 9)
- `NAIC LATF | [date]` — pipe-separated format (rows 7, 9 variants)
- `NAIC LATF | ` — incomplete (row 10)

**Suggested fix — replace the italicized sentence with:**
> "If the change is tied to an upcoming Event not listed under 01_calendar_items.txt, generate the Event Title using this exact format: `[Full Group Name] — [Month D, YYYY]`. Examples: `Life Actuarial (A) Task Force — June 25, 2026`, `Property and Casualty Insurance (C) Committee — August 12, 2026`. Rules: do NOT add a 'NAIC' prefix; do NOT append numeric date codes (e.g., '06252026'); do NOT use pipe characters or colons."

---

## 🟡 Chronicle Topics — 70% | Field 14

**Current instruction:**
> "...list of associated Chronicle Topics. At least one Agenda Item Title Should always be listed. **If no Chronicle Topic is relevant report N/A.**"

**Root cause:** Two compounding problems:
1. "If no Chronicle Topic is relevant" is an easy escape hatch — the agent defaults to N/A rather than searching. In the targeted eval, 4 of 7 agenda-type rows had N/A where a specific topic clearly applied.
2. No canonical name list is provided. The agent uses slightly wrong names (`RBC Covariance & Asset Concentration Risk` instead of `Life RBC Covariance & Asset Concentration Risk`) that would fail at Bubble sync time.

**Suggested fix — two changes:**

**Change 1** — Replace the escape hatch sentence:
> "Chronicle Topics must be assigned wherever possible. Search the vector store (ba:chronicles) to identify applicable topics. Only use N/A if you have confirmed no topic applies after searching. When in doubt, assign the closest matching topic rather than N/A."

**Change 2** — Add immediately after the instruction:
> "Use ONLY the following exact topic names — do not paraphrase or abbreviate. Using a slightly different name (e.g., omitting 'Life' from 'Life RBC Covariance & Asset Concentration Risk') will break downstream matching.
>
> Investment topics: `CLOs & ABS`, `NAIC Designations & Use of Agency Ratings`, `Principles-Based Bond Definition`, `Funds Under Schedule BA`, `Residential Mortgage Funds Under Schedule BA`, `Digital Assets`, `C-1, R-1 & H-1 Bond Factors`, `CMBS & RMBS`, `Commercial Real Estate Equity`, `Tax Credit Structures`, `Repurchase Agreements`, `Short-Term Investments`, `ALM Derivatives & Derivative Wrapped Investments`, `Collateral Loans`, `Residual Interests of ABS`, `Exchange Traded Funds (ETFs)`, `NAIC US Government Money Market Fund List`, `NAIC Fixed Income-Like SEC Registered Funds List`
>
> Capital & reserving topics: `RBC C-3 (Interest Rate & Market Risk)`, `Life RBC Covariance & Asset Concentration Risk`, `Negative IMR`, `Principle-Based Reserving (PBR) & VM-22`, `Generator of Economic Scenarios (GOES)`, `Liquidity Stress Tests (LSTs)`, `Actuarial Guideline (AG) LIII`, `Group Capital Calculations`, `Credit for Reinsurance`, `Private Equity Owned Insurers`, `Affiliate and Related Party Investments`, `Funding Agreements`
>
> International & federal topics: `IAIS`, `U.K. Solvency`, `EIOPA`, `Bermuda Monetary Authority (BMA)`, `Financial Stability Board (FSB)`, `FSOC`, `Federal Insurance Office (FIO)`, `Federal Reserve Board (FRB)`
>
> Other: `NAIC Climate Initiatives`, `Standard & Poor (S&P)`, `Moody's`, `Fitch`, `AM Best`"

---

## 🟡 Document Title — 73.3% | Field 18

**Current instruction:**
> "'New' if the change is tied to a new Library Item not listed, along with **a descriptive Title.**"

**Root cause:** Three distinct failure modes:
- Rows 6, 15: Agent uses the raw filename or bare proposal ID (`APF 2025-14`) as the title instead of a human-readable name.
- Row 12: Agent doesn't recognize an agenda PDF as a library item at all — skips it entirely.
- Row 10: Only 1 of 7 materials on a page captured (multi-material page).

**Suggested fix — expand the 'New' bullet and add rules:**
> "'New' if the change is tied to a new Library Item not listed, along with a human-readable title. Rules:
> - Do NOT use the raw filename verbatim as the title.
> - If the document has an ID (e.g., APF 2025-14, Ref #2024-15), include it followed by a dash and a brief description: `APF 2025-14 — VA Scope Clarification`.
> - Agenda PDFs with their own URL or filename ARE library items — always capture them.
> - When a page change includes multiple distinct documents (different URLs or filenames), each must be reported as a separate alert row per the multi-alert rules above."

---

## 🟡 Newsreel Relevance — 82.5% | Field 21

**Current instruction:**
> "**1. If the Alert is related to an existing upcoming event, library item, or agenda item, report 'Yes'** and report the existing upcoming event, library item, or agenda item."

**Root cause:** Part 1 is too broad — virtually every alert relates to *something* upcoming, so the agent returns "Yes" by default. Additionally, row 8 shows `details = "N/A"` while `status = "Yes"` — a direct contradiction the instruction doesn't prevent.

**Suggested fix:**

**Change 1** — Replace Part 1 entirely:
> "Do NOT automatically say 'Yes' just because the alert relates to an upcoming event or document. Ask: *would this specific change be worth writing a dedicated Newsreel article?*
> - A meeting agenda being posted is routine and expected — not newsworthy by itself. → **No**
> - A substantive new exposure draft, capital requirement change, or regulatory update → **Yes**
> - A duration-only or logistics-only meeting update (time change, Webex link added) → **No**
> - A new report with analysis or findings → **Yes**"

**Change 2** — Add after the existing rules:
> "When status is 'Yes', the `details` field MUST name a candidate article title or describe the specific newsworthy aspect — **never 'N/A'**. Example: `Candidate title: 'LATF Posts APF 2025-14 Proposing VA Scope Clarification for June Meeting'`."

---

## 🟡 Event Start & End Time — 80.8% | Fields 8 & 9

**Current instruction:**
> "If the Event Start Date & Time is new, **report 'Updated Start Date & Time', along with the old time and the new time.** Date & Time should be reported in Eastern Time (ISO 8601)."

**Root cause:** The "Updated Start Date & Time: old → new" pattern puts narrative text into a field the Bubble sync expects to be a clean ISO 8601 datetime string. This breaks downstream datetime parsing.

**Suggested fix — replace the update clause in both fields 8 and 9:**
> "Always report the current (most recent) datetime in ISO 8601 format, Eastern Time. Example: `2026-06-25T09:00:00-04:00`. If the time was changed, describe the old and new values in `alert_description` — this field contains only the current datetime value. If no time is available, report N/A."

---

## 🟡 Call-In / Access Code — 83.3% | Field 13

**Current instruction:**
> "For meetings, report the call-in number & access code for meetings. For U.S. numbers, the format should be as follows: `+1-xxx-xxx-xxxx,,yyyyyyyyyyy##`"

**Root cause:** No explicit fallback specified when no number is present. Agents occasionally infer or construct a number rather than saying N/A.

**Suggested fix — add one sentence:**
> "Copy the number exactly as it appears on the page. If no call-in number is listed on the page, report 'N/A' — do not infer or construct a number. Some NAIC pages use `#` instead of `,,` as a separator — always match what is displayed."

---

## Organization — 90% | Field 5

**Current instruction:**
> "Report the organizations associated with this change... Pick the most specific (deepest) organization that applies."

**Root cause:** Row 5 (joint meeting) — the agent picks one org and stops. The instruction only describes picking a single deepest match.

**Suggested fix — add one sentence:**
> "For joint meetings or documents involving multiple organizations, report all participating organizations as separate entries in the array — do not truncate to one."

---

## Event URL — 91.7% | Field 12

**Current instruction:**
> "For meetings, report the URL for weblinks to meetings. If its not a meeting, report N/A."

**Root cause:** Ambiguous — the agent sometimes returns the NAIC page URL (which is already in `alert_url`) rather than the Webex join link.

**Suggested fix:**
> "For meetings, report the virtual meeting join link — a webex.com, teams.microsoft.com, or zoom.us URL. This is NOT the same as `alert_url` (the NAIC page URL). If no virtual join link is present on the page, report 'N/A'."

---

## Alert Type — 92.5% | Field 1

**Current instruction:** Lists the 15 enum values with no disambiguation guidance.

**Root cause:** Row 16 — a newsroom article about a meeting classified as `New Meeting`. No rule distinguishes "a meeting exists" from "a new calendar event is being created".

**Suggested fix — add after the enum list:**
> "'New Meeting' requires that a new calendar event is being created on the NAIC calendar. A newsroom article, press release, or announcement *about* a meeting is NOT 'New Meeting' — use 'Other'. Use 'Other' for: press releases, newsroom articles, government affairs letters, and general announcements not tied to a specific alert type above."

---

## Alert Description — 92.5% | Field 3

**Current instruction:**
> "Provide a brief description of what changed."

**Root cause:** Too vague. Rows 6 and 18 (two RFCs from the same page change) produced near-identical one-line descriptions with no specifics.

**Suggested fix:**
> "2–3 sentences. Describe: (1) what was added or changed, (2) the document name or meeting detail, (3) any key identifiers or dates. Focus on the change itself, not the document's content. Example: 'APF 2025-14 (VA Scope Clarification) was posted to the Life Actuarial (A) Task Force page as call material for the June 25, 2026 meeting.' Not: 'A new document was posted.'"

---

## Alert Title — 92.5% | Field 2

**Current instruction:**
> "Assign a title to the alert."

**Root cause:** One sentence with no format guidance. Most errors are downstream of misclassification (row 16) but a standard format would prevent inconsistency.

**Suggested fix:**
> "Format: `[Alert Type]: [Org or Topic] — [Brief Description]`. Keep under 100 characters. Examples: `New RFC: Life Actuarial (A) Task Force — APF 2025-14 VA Scope Clarification`, `New Agenda & Materials: LATF — June 25, 2026 Meeting`."

---

## Summary Table

| Field | Current Accuracy | Expected After Fix | Change Needed |
|-------|----------------|--------------------|---------------|
| Event Title | 61.5% | ~90% | Add explicit `[Group] — [Month D, YYYY]` format rule |
| Chronicle Topics | 70% | ~85% | Add canonical topic list + strengthen N/A escape hatch |
| Document Title | 73.3% | ~87% | Require readable titles; capture agenda PDFs; flag multi-doc pages |
| Newsreel Relevance | 82.5% | ~90% | Reframe Part 1; require non-null details when Yes |
| Event Start/End Time | 80.8% | ~92% | Always output ISO datetime; no narrative text |
| Call-In/Access Code | 83.3% | ~92% | Add explicit N/A fallback |
| Organization | 90% | ~97% | Add joint-meeting multi-org rule |
| Event URL | 91.7% | ~97% | Clarify Webex link vs. page URL |
| Alert Type | 92.5% | ~97% | Add New Meeting vs. Other disambiguation |
| Alert Description | 92.5% | ~97% | Add length + content guidance |
| Alert Title | 92.5% | ~97% | Add format template |
