# Bubble Sync Flow — Alert → Action Mapping

> **Status (June 2026):** Implemented. `bubble/bubble_sync_classifier.py` maps
> alert types to actions and builds exact field previews. `bubble/bubble_sync.py`
> executes real Bubble API calls (CREATE/UPDATE calendaritem + libraryitem).
> The dashboard fires an ECS RunTask on confirm; see `CLAUDE.md` for full details.

This document maps how each type of web-change alert translates into actions
in the Bubble data model.

---

## Bubble Data Model (simplified)

```
Calendar Item
  ├── group / organization
  ├── date & time
  ├── status (scheduled / cancelled / updated)
  ├── Agenda Items[]       ← child records on the calendar item
  └── Library Items[]      ← references to library item records

Library Item
  ├── title
  ├── type (document, RFC, agenda, materials, etc.)
  ├── url / file
  └── → Calendar Item (optional reference back)
```

**Key for matching:** A calendar item is identified by **group + date/time**.
If an alert references a meeting that already exists in Bubble, we update it.
If not, we create it.

---

## Alert Type → Action Mapping

### 1. New Meeting Announced
**Trigger:** A new event appears on a committee page (date, time, call-in info posted)

| Step | Action |
|------|--------|
| 1 | **Create** new Calendar Item (group, date/time, event URL, call-in details) |

---

### 2. Meeting Updated
**Trigger:** An existing meeting's time, location, or details change

| Step | Action |
|------|--------|
| 1 | Look up existing Calendar Item by group + date/time |
| 2 | **Update** Calendar Item fields that changed |

---

### 3. Meeting Cancelled
**Trigger:** A meeting is removed or marked cancelled

| Step | Action |
|------|--------|
| 1 | Look up existing Calendar Item |
| 2 | **Update** Calendar Item status → Cancelled |

---

### 4. Materials / Agenda Posted to a Meeting
**Trigger:** Documents, agenda PDFs, or presentation materials are added to an existing meeting page

| Step | Action |
|------|--------|
| 1 | Look up existing Calendar Item (or create if not yet in Bubble) |
| 2 | **Create** new Library Item(s) for each document |
| 3 | **Update** Calendar Item — add references to the new Library Item(s) |
| 4 | If agenda items are identified → **Create** Agenda Item records on the Calendar Item |
| 5 | *(Optional)* If document is relevant for content → send to Knowledge Admin (see Ingest below) |

> **Note:** Materials can be posted incrementally — e.g. agenda posted first,
> then presentation slides added later. Each alert is a separate operation.
> Documents can also be replaced or removed, triggering an update or delete.

---

### 5. New Request for Comment / Standalone Document
**Trigger:** A new RFC, exposure draft, or standalone publication appears

| Step | Action |
|------|--------|
| 1 | **Create** new Library Item (title, URL, type = RFC / Exposure Draft / etc.) |
| 2 | If associated with a known meeting → link to that Calendar Item |

---

### 6. Existing Document Updated or Replaced
**Trigger:** A previously posted document is revised or a new version is uploaded

| Step | Action |
|------|--------|
| 1 | Look up existing Library Item |
| 2 | **Update** Library Item (new URL / file / version) |

---

## Ingest Flow (Newsreel & Knowledge Admin)

These are separate from the calendar/library sync above. They run after a human
reviews and approves content on the dashboard.

```
Transcript available for a meeting
    └── Chunked (transcript chunker agent)
          └── [Human reviews on dashboard]
                └── Approved → chunks sent to Newsreel backend

Document extraction complete (document agent ran on a library item)
    └── [Human reviews on dashboard]
          └── Approved → document sent to Knowledge Admin / Newsreel backend
```

**Key point:** The ingest gate (Publish button on the dashboard) is the human
approval step for both flows. Nothing is pushed to Bubble or the Newsreel
backend automatically.

---

## Summary: What Needs to Be Built

| Capability | Status |
|-----------|--------|
| Detect alert type and extract structured fields | ✅ Done (page change agent) |
| Extract document metadata | ✅ Done (document extraction agent) |
| Chunk transcripts with topic metadata | ✅ Done (transcript chunker) |
| Human review + approve on dashboard (ingest gate) | ✅ Done |
| Match alert → existing Bubble Calendar Item | 🔲 Not built |
| Create / update Calendar Item via Bubble API | 🔲 Not built |
| Create Library Item via Bubble API | 🔲 Not built |
| Create Agenda Items on a Calendar Item | 🔲 Not built |
| Push approved chunks to Newsreel backend | 🔲 Not built |
| Push approved documents to Knowledge Admin | 🔲 Not built |

---

## Open Questions

- What is the exact Bubble API structure for Calendar Item, Library Item, and Agenda Item?
- How do we handle conflicts — e.g. a document that appears in two different alerts for the same meeting?
- Should the dashboard "Publish" action trigger the Bubble sync, the Newsreel push, or both? Or are these separate buttons?
- For the transcript ingest: does the entire chunked file go to Newsreel, or are individual chunks ingested as separate records?
