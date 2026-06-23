# NAIC Alert Agent — Accuracy Scoresheet
**Date:** June 23, 2026 | **Sample:** 20 alerts across 12 alert types | **Overall: 85.4% (91.7% excl. legacy rows)**

---

## Field Accuracy at a Glance

| Field | Accuracy | Status |
|-------|----------|--------|
| Document URL | 100% | ✅ Perfect |
| Document Filename | 100% | ✅ Perfect |
| Alert Type | 92.5% | ✅ Strong |
| Alert Title | 92.5% | ✅ Strong |
| Alert Description | 92.5% | ✅ Strong |
| Event URL (Webex link) | 91.7% | ✅ Strong |
| Organization | 90.0% | ✅ Strong |
| Call-In / Access Code | 83.3% | ✅ Good |
| Newsreel Relevance | 82.5% | 🟡 Good |
| Event Start Time | 80.8% | 🟡 Good |
| Event End Time | 80.8% | 🟡 Good |
| Document Title | 73.3% | 🟡 Needs Work |
| Chronicle Topics | 70.0% | 🟡 Needs Work |
| **Event Title (format)** | **61.5%** | 🔴 Weakest |

---

## Row-by-Row Summary

| # | Alert Type | Score | Issues |
|---|-----------|-------|--------|
| 1 | New/Upd Report | 95% | Chronicle topic N/A when specific topic could apply |
| 2 | Other | 67% | Borderline type; article swap not verifiable from live page |
| 3 | Updated RFC | 94% | Clean — only minor chronicle gap |
| 4 | New Meeting | 95% | Event title uses non-standard "NAIC Interim Meeting:" prefix |
| 5 | New Agenda & Materials | 96% | Joint meeting org partially captured |
| 6 | New RFC | 89% | Description too brief; title is filename not readable name |
| 7 | Updated Meeting | 95% | Event title uses date-code suffix (e.g., "- 06252026") |
| 8 | New Materials | 96% | Newsreel details field says "N/A" while status says "Yes" |
| 9 | Updated Materials | 93% | Event title date-code; AG 55 chronicle topic N/A |
| 10 | Updated Agenda & Materials | 96% | Only 1 of 7 materials captured in document title field |
| **11** | **Updated Effective Date** | **29%** | ⚠️ LEGACY SCHEMA: null fields, wrong types — needs rerun |
| 12 | New Agenda | 83% | Agenda PDF not captured as document; newsreel details vague |
| 13 | New/Upd Report | 94% | Agenda items all N/A |
| 14 | Other | 89% | Borderline type for past-meeting removal; event title prefix |
| 15 | Updated RFC | 94% | Document title is just proposal number, no description |
| **16** | **New Meeting** | **56%** | ⚠️ MISCLASSIFIED: newsroom article treated as calendar event |
| 17 | New Agenda & Materials | **100%** | Perfect — all fields complete and accurate |
| 18 | New RFC | 94% | Description too brief (matches row 6 from same page change) |
| 19 | Updated Meeting | 91% | Newsreel "Yes" for duration-only change (questionable) |
| **20** | **New Materials** | **54%** | ⚠️ LEGACY SCHEMA: null fields, wrong types — needs rerun |

---

## Top 3 Things Working Well

1. **Document detection is reliable** — URLs and filenames extracted correctly in every applicable row (100%). The agent reliably identifies the right PDF/document from the page.

2. **Core summarization is accurate** — In 18 of 20 rows, the alert type, title, and description correctly describe what changed on the NAIC page. The agent reads before/after HTML well.

3. **Meeting time/location extraction is solid** — Webex links, start times, and end times are correct in all rows not affected by the schema bug.

---

## Top 3 Things Needing Improvement

1. **Event title has no standard format** — Four different naming conventions appear across rows. This causes inconsistent event matching when syncing to Bubble. Fixing requires one clear format defined in the system prompt.

2. **Chronicle topics default to N/A** — The NAIC topic taxonomy (87 categories) is under-utilized. In 10 of 20 rows, all chronicle topics are "N/A" when a specific category clearly applies. Adding the full taxonomy to agent context should fix this.

3. **Two rows have breaking schema errors** — Rows 11 and 20 have `null` values where strings are required and plain strings where dicts are required. These need to be rerun through the current pipeline to fix.
