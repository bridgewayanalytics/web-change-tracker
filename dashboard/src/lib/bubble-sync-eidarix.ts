/**
 * Eidarix Bubble sync executor — ports bubble/bubble_sync.py to TypeScript.
 * Replaces the Lambda intermediary: calls Eidarix workflow endpoints directly.
 *
 * Processing order: agenda items → library item → calendar item
 * Each step can run independently (action param) or as a full sequence (action="all").
 */

const EIDARIX_VERSION = process.env.EIDARIX_VERSION ?? "test";

// All Bubble records (organizations, chronicle topics, library items, agenda items,
// calendar items, library/calendar item types) live in the content space. Every data-
// API lookup must scope by this space, and every workflow payload must set `space_id`
// to this value so Eidarix workflows can resolve linked chronicle topics / orgs.
// EIDARIX_VERSION only controls which Bubble database version receives writes
// (`version-test` vs `version-live`), NOT which space records are tagged with.
const CONTENT_SPACE_ID = "1768998437948x865417918648382000";
const CONTENT_SPACE_CONSTRAINT = [{ key: "space", constraint_type: "equals", value: CONTENT_SPACE_ID }];

const OBJ_BASE = "https://eidarix.bridgewayanalytics.com/api/1.1/obj";
// calendaritemtype requires the versioned URL — returns 404 at the non-versioned base
const OBJ_VERSIONED = `https://eidarix.bridgewayanalytics.com/version-${EIDARIX_VERSION}/api/1.1/obj`;
const WF_BASE = `https://eidarix.bridgewayanalytics.com/version-${EIDARIX_VERSION}/api/1.1/wf`;

// Module-level cache so SSM is fetched at most once per process lifetime
let _cachedKey: string | null = null;

async function getBubbleKey(): Promise<string> {
  if (process.env.BUBBLE_API_KEY) return process.env.BUBBLE_API_KEY;
  if (_cachedKey) return _cachedKey;
  try {
    const { getBubbleApiKey } = await import("./ssm");
    _cachedKey = await getBubbleApiKey() ?? "";
  } catch {
    _cachedKey = "";
  }
  return _cachedKey ?? "";
}

// Matches: "New - ", "Existing — ", "Updated – ", "Revised: " etc. Accepts hyphen (-),
// en-dash (–), em-dash (—), and colon as the separator.
const STATUS_PREFIX_RE = /^(New|Existing|Updated|Revised)\s*[-–—:]\s*/i;
function stripStatusPrefix(s: string): string {
  return s.replace(STATUS_PREFIX_RE, "").trim();
}

// ---------------------------------------------------------------------------
// Low-level Bubble API helpers
// ---------------------------------------------------------------------------

async function bubbleGet(url: string): Promise<Response> {
  return fetch(url, {
    headers: { Authorization: `Bearer ${await getBubbleKey()}` },
    cache: "no-store",
  });
}

/** Fetch all pages of a Bubble Data API list. Paginates until `remaining` is 0. */
async function bubbleListAll(
  type: string,
  constraints: object[],
  versioned = false,
): Promise<Record<string, unknown>[]> {
  const base = versioned ? OBJ_VERSIONED : OBJ_BASE;
  const all: Record<string, unknown>[] = [];
  let cursor = 0;
  const limit = 100;
  for (;;) {
    const params = new URLSearchParams({
      constraints: JSON.stringify(constraints),
      limit: String(limit),
      cursor: String(cursor),
    });
    const res = await bubbleGet(`${base}/${type}?${params}`);
    if (!res.ok) throw new Error(`Bubble GET /${type} ${res.status}: ${(await res.text()).slice(0, 200)}`);
    const data = await res.json() as { response?: { results?: Record<string, unknown>[]; remaining?: number } };
    const batch = data.response?.results ?? [];
    all.push(...batch);
    if (!batch.length || (data.response?.remaining ?? 0) <= 0) break;
    cursor += limit;
  }
  return all;
}

async function bubbleSearch(
  type: string,
  constraints: object[],
  limit = 10,
): Promise<Record<string, unknown>[]> {
  const params = new URLSearchParams({
    constraints: JSON.stringify(constraints),
    limit: String(limit),
  });
  const res = await bubbleGet(`${OBJ_BASE}/${type}?${params}`);
  if (!res.ok) throw new Error(`Bubble GET /${type} ${res.status}: ${(await res.text()).slice(0, 200)}`);
  const data = await res.json() as { response?: { results?: Record<string, unknown>[] } };
  return data.response?.results ?? [];
}

async function eidarixWfPost(
  workflow: string,
  payload: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  const url = `${WF_BASE}/${workflow}`;
  console.info(`[bubble-sync] POST ${workflow}`, JSON.stringify(payload));
  const res = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${await getBubbleKey()}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(30_000),
  });
  const text = await res.text();
  console.info(`[bubble-sync] ${workflow} response status=${res.status} body=${text.slice(0, 500)}`);
  if (!res.ok) throw new Error(`Eidarix POST ${workflow} ${res.status}: ${text.slice(0, 300)}`);
  let data: unknown;
  try { data = JSON.parse(text); } catch { data = {}; }
  const d = (data ?? {}) as Record<string, unknown>;
  if (d.success === false) throw new Error(`Eidarix error: ${String(d.error ?? "unknown")}`);
  return d;
}

// ---------------------------------------------------------------------------
// Name → ID resolution
// ---------------------------------------------------------------------------

function matchOrgName(name: string, nameToId: Map<string, string>): string | null {
  const n = name.trim();
  // 1. Exact match
  let id = nameToId.get(n);
  if (id) return id;
  // 2. Strip " (NAIC XXX)" acronym suffix the agent appends and retry
  const stripped = n.replace(/\s*\(NAIC\s+[A-Z0-9\-]+\)\s*$/, "").trim();
  if (stripped !== n) { id = nameToId.get(stripped); if (id) return id; }
  // 3. Prefix match: Bubble org name is a prefix of the agent-provided name
  const entries = Array.from(nameToId.entries());
  for (const [bubbleName, bubbleId] of entries) {
    if (n.startsWith(bubbleName + " ") && n !== bubbleName) return bubbleId;
  }
  return null;
}

async function resolveOrgIds(orgNames: string[]): Promise<string[]> {
  if (!orgNames.length) return [];
  const orgs = await bubbleListAll("organization", CONTENT_SPACE_CONSTRAINT);
  const nameToId = new Map(orgs.map(o => [String(o.Name ?? "").trim(), String(o._id ?? "")]));
  const ids: string[] = [];
  const missing: string[] = [];
  for (const rawName of orgNames) {
    const id = matchOrgName(rawName, nameToId);
    if (id) ids.push(id);
    else missing.push(rawName.trim());
  }
  if (missing.length) console.warn("[bubble-sync] org(s) not found in Bubble:", missing, `EIDARIX_VERSION=${EIDARIX_VERSION}`);
  return ids;
}

async function resolveTopicIds(
  topicNames: string[],
): Promise<{ ids: string[]; nameToId: Map<string, string> }> {
  if (!topicNames.length) return { ids: [], nameToId: new Map() };
  const topics = await bubbleListAll("chronicletopic", CONTENT_SPACE_CONSTRAINT);
  const nameToId = new Map(topics.map(t => [String(t.Title ?? "").trim(), String(t._id ?? "")]));
  const ids: string[] = [];
  for (const name of topicNames) {
    const id = nameToId.get(name);
    if (id) ids.push(id);
    else console.warn("[bubble-sync] chronicle topic not found:", name);
  }
  return { ids, nameToId };
}

// Maps classifier-generated type names (in bubble_action.library_item_preview.type)
// to the exact Title values present in the Bubble libraryitemtype table.
// Necessary because the classifier uses human-readable internal names that predate
// the Bubble taxonomy.
const LIB_TYPE_ALIASES: Record<string, string> = {
  "agenda":                      "Agenda & Materials",
  "materials":                   "Agenda & Materials",
  "request for comment":         "Proposed Guidance & Support Materials",
  "adopted guideline":           "Existing Requirements & Guidance",
  "report / resource":           "Publication",
  "document":                    "Publication",
  "meeting transcript":          "Other",
};

async function resolveLibraryItemTypeId(typeName: string): Promise<string | null> {
  try {
    // Use versioned URL (no space constraint — versioned URL already scopes to the correct space)
    const types = await bubbleListAll("libraryitemtype", [], true);
    const needle = typeName.trim().toLowerCase();
    // Resolve alias first (classifier name → Bubble name)
    const resolved = (LIB_TYPE_ALIASES[needle] ?? typeName).trim().toLowerCase();
    for (const t of types) {
      const title = String(t.display ?? t.Title ?? "").trim().toLowerCase();
      if (title === resolved || title === needle) return String(t._id);
    }
    console.warn("[bubble-sync] libraryitemtype not found:", typeName, "→", resolved, "available:", types.map(t => t.display ?? t.Title));
  } catch (e) { console.warn("[bubble-sync] libraryitemtype lookup failed:", e); }
  return null;
}

async function resolveCalendarItemTypeId(typeName: string): Promise<string | null> {
  try {
    // No space constraint — versioned URL already scopes to the correct space
    const params = new URLSearchParams({ limit: "50" });
    const res = await bubbleGet(`${OBJ_VERSIONED}/calendaritemtype?${params}`);
    if (!res.ok) { console.warn("[bubble-sync] calendaritemtype GET failed:", res.status); return null; }
    const data = await res.json() as { response?: { results?: Record<string, unknown>[] } };
    for (const t of data.response?.results ?? []) {
      if (String(t.display ?? t.Title ?? "").trim().toLowerCase() === typeName.trim().toLowerCase()) return String(t._id);
    }
    console.warn("[bubble-sync] calendaritemtype not found:", typeName);
  } catch (e) { console.warn("[bubble-sync] calendaritemtype lookup failed:", e); }
  return null;
}

async function getEmptyTopicId(): Promise<string | null> {
  try {
    const results = await bubbleSearch("chronicletopic", [
      ...CONTENT_SPACE_CONSTRAINT,
      { key: "empty topic", constraint_type: "equals", value: true },
    ], 5);
    return results[0]?._id ? String(results[0]._id) : null;
  } catch { return null; }
}

// ---------------------------------------------------------------------------
// Record finders
// ---------------------------------------------------------------------------

async function findAgendaItemByTitle(title: string): Promise<string | null> {
  for (const key of ["title", "BA title"]) {
    try {
      const results = await bubbleSearch("agendaitem", [
        ...CONTENT_SPACE_CONSTRAINT,
        { key, constraint_type: "equals", value: title },
      ], 5);
      if (results.length) {
        console.info(`[bubble-sync] found agendaitem '${title}' via field '${key}' → ${results[0]._id}`);
        return String(results[0]._id);
      }
    } catch { /* try next field key */ }
  }
  console.warn(`[bubble-sync] agendaitem not found by title: '${title}'`);
  return null;
}

async function findLibraryItem(matchSearch: Record<string, string>): Promise<string | null> {
  // Classifier may stamp status prefixes ("Updated — ", "Existing - ") on title; strip
  // before searching since the Bubble record never has them.
  const url   = matchSearch.url ? stripStatusPrefix(matchSearch.url) : "";
  const title = matchSearch.title ? stripStatusPrefix(matchSearch.title) : "";
  if (url) {
    const results = await bubbleSearch("libraryitem", [
      ...CONTENT_SPACE_CONSTRAINT,
      { key: "url_text", constraint_type: "equals", value: url },
    ], 5);
    if (results.length) return String(results[0]._id);
  }
  if (title) {
    const results = await bubbleSearch("libraryitem", [
      ...CONTENT_SPACE_CONSTRAINT,
      { key: "name_text", constraint_type: "equals", value: title },
    ], 5);
    if (results.length) return String(results[0]._id);
  }
  return null;
}

async function findCalendarItem(matchSearch: Record<string, string>): Promise<string | null> {
  const dateStr = matchSearch.date ?? "";

  if (dateStr) {
    const nextDay = new Date(dateStr + "T00:00:00.000Z");
    nextDay.setUTCDate(nextDay.getUTCDate() + 1);

    const constraints: object[] = [
      ...CONTENT_SPACE_CONSTRAINT,
      { key: "date", constraint_type: "greater than", value: `${dateStr}T00:00:00.000Z` },
      { key: "date", constraint_type: "less than", value: nextDay.toISOString().slice(0, 10) + "T00:00:00.000Z" },
    ];

    if (matchSearch.org) {
      try {
        const orgs = await bubbleListAll("organization", CONTENT_SPACE_CONSTRAINT);
        const org = orgs.find(o => String(o.Name ?? "").trim() === matchSearch.org);
        if (org?._id) constraints.push({ key: "orgs", constraint_type: "contains", value: org._id });
      } catch { /* skip org constraint on failure */ }
    }

    const results = await bubbleSearch("calendaritem", constraints, 10);
    if (results.length > 1) console.warn("[bubble-sync] multiple calendaritems found for", matchSearch);
    return results[0]?._id ? String(results[0]._id) : null;
  }

  // No date — fall back to org-only search
  if (!matchSearch.org) return null;
  console.warn("[bubble-sync] no date in match_search — falling back to org-only calendaritem search for:", matchSearch.org);
  try {
    const orgs = await bubbleListAll("organization", CONTENT_SPACE_CONSTRAINT);
    const org = orgs.find(o => String(o.Name ?? "").trim() === matchSearch.org);
    if (!org?._id) return null;
    const results = await bubbleSearch("calendaritem", [
      ...CONTENT_SPACE_CONSTRAINT,
      { key: "orgs", constraint_type: "contains", value: String(org._id) },
    ], 10);
    if (results.length === 1) {
      console.info("[bubble-sync] org-only fallback matched 1 calendaritem:", results[0]._id);
      return String(results[0]._id);
    }
    if (results.length > 1) {
      throw new Error(
        `No event date available and org-only search returned ${results.length} calendar items for ` +
        `"${matchSearch.org}" — cannot safely update. Add a date to the alert or update Bubble manually.`
      );
    }
  } catch (e) {
    if (e instanceof Error && e.message.startsWith("No event date")) throw e;
    console.warn("[bubble-sync] org-only calendaritem fallback failed:", e);
  }
  return null;
}

// ---------------------------------------------------------------------------
// Agenda item resolution
// ---------------------------------------------------------------------------

async function resolveAgendaItems(
  row: Record<string, unknown>,
  agentCallId: string,
  topicNameToId: Map<string, string>,
): Promise<string[]> {
  const plan = row.bubble_action as Record<string, unknown> | undefined;
  let previews = (plan?.agenda_item_previews as Array<Record<string, unknown>> | undefined) ?? [];

  if (!previews.length) {
    // Fallback: build previews from flat alert fields (old rows without previews).
    // Field name was renamed `agenda_item_title_chronicle_topics` → `agenda_item_title`
    // in the 2026-10 schema sync; keep both so historical rows still resolve.
    const agendaEntries = (
      (row.agenda_item_title as Array<Record<string, unknown>> | undefined) ??
      (row.agenda_item_title_chronicle_topics as Array<Record<string, unknown>> | undefined) ??
      []
    );
    const officialTitles = (row.agenda_item_title_official as Array<Record<string, unknown>> | undefined) ?? [];
    const standardizedIds = (row.agenda_item_standardized_id as Array<Record<string, unknown>> | undefined) ?? [];
    previews = agendaEntries.map((entry, i) => ({
      title: String(entry.agenda_item_title ?? "").trim(),
      status: String(entry.status ?? "New"),
      chronicle_topics: (entry.chronicle_topics as string[] | undefined) ?? [],
      official_title: String(officialTitles[i]?.official_title ?? "").trim(),
      reference_id: String(standardizedIds[i]?.standardized_id ?? "").trim(),
    }));
  }

  const resolvedIds: string[] = [];

  for (const item of previews) {
    const title = stripStatusPrefix(String(item.title ?? "").trim());
    if (!title || title.toUpperCase() === "N/A") continue;

    const status = String(item.status ?? "New").trim();
    const isNew = status.toLowerCase() === "new";
    const topicNames = (item.chronicle_topics as string[] | undefined) ?? [];
    const topicIds = topicNames.map(n => topicNameToId.get(n)).filter((id): id is string => !!id);

    if (!isNew) {
      const existingId = await findAgendaItemByTitle(title);
      if (existingId) {
        resolvedIds.push(existingId);
      } else {
        throw new Error(
          `Agenda item not found in Bubble: '${title}' (status=${status}). ` +
          `The item must exist before the library item can link to it. ` +
          `Check EIDARIX_VERSION=${EIDARIX_VERSION}.`
        );
      }
      continue;
    }

    let officialTitle = stripStatusPrefix(String(item.official_title ?? "").trim());
    let referenceId   = stripStatusPrefix(String(item.reference_id ?? "").trim());
    let officialId    = stripStatusPrefix(String(item.official_id ?? "").trim());
    if (officialTitle.toUpperCase() === "N/A") officialTitle = "";
    if (referenceId.toUpperCase()   === "N/A") referenceId   = "";
    if (officialId.toUpperCase()    === "N/A") officialId    = "";

    // If no topics, use the empty-topic fallback chronicle topic
    let finalTopicIds = topicIds;
    if (!finalTopicIds.length) {
      const fallbackId = await getEmptyTopicId();
      if (fallbackId) finalTopicIds = [fallbackId];
    }

    // Bubble field mapping:
    //   reference_id   → BA Ref #    (Bridgeway's standardized identifier)
    //   official_title → NAIC Title
    // Note: the create-agenda-item/ workflow does NOT expose Ref # as an input parameter,
    // so we PATCH the record directly via the Data API after creation to set it.
    const result = await eidarixWfPost("create-agenda-item/", {
      title,
      official_title: officialTitle,
      reference_id: referenceId,
      chronicle_topics: finalTopicIds,
      alert_id: agentCallId,
      space_id: CONTENT_SPACE_ID,
    });

    const itemId = result.bubble_id ?? result.id ?? result._id;
    if (!itemId) throw new Error(`create-agenda-item returned no id for '${title}': ${JSON.stringify(result)}`);
    console.info(`[bubble-sync] created agenda item '${title}' → ${itemId}`);

    // PATCH Ref # (NAIC's official identifier) via Data API — the workflow writes BA Ref #
    // from reference_id but can't set Ref # separately. Field name uses the exact Bubble
    // display name "Ref #" per docs/bubble_api_reference.md §5. Non-fatal on failure.
    if (officialId) {
      try {
        const patchRes = await fetch(`${OBJ_VERSIONED}/agendaitem/${itemId}`, {
          method: "PATCH",
          headers: {
            Authorization: `Bearer ${await getBubbleKey()}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ "Ref #": officialId }),
          signal: AbortSignal.timeout(10_000),
        });
        if (!patchRes.ok) {
          const body = await patchRes.text();
          console.warn(`[bubble-sync] PATCH Ref # on agendaitem ${itemId} failed: ${patchRes.status} ${body.slice(0, 200)}`);
        } else {
          console.info(`[bubble-sync] set agendaitem ${itemId} 'Ref #' → ${officialId}`);
        }
      } catch (e) {
        console.warn(`[bubble-sync] PATCH Ref # on agendaitem ${itemId} threw:`, e);
      }
    }

    resolvedIds.push(String(itemId));
  }

  return resolvedIds;
}

// ---------------------------------------------------------------------------
// Field extraction helpers (backward compat: new display names + old underscore keys)
// ---------------------------------------------------------------------------

function getTopicNamesFromFieldIds(fieldIds: Record<string, unknown>): string[] {
  // New key (Bubble display name) or old underscore key
  const val = fieldIds["Topics - dt"] ?? fieldIds["topics___dt_list_custom_newsreel_update"];
  if (Array.isArray(val)) return val.filter((v): v is string => typeof v === "string");
  return [];
}

function getAgendaTopicNames(row: Record<string, unknown>): string[] {
  // Primary source: bubble_action.agenda_item_previews (patched by the modal; reflects
  // doc-extraction-synthesized agenda items). Fallback: the alert row's agent-output
  // agenda_item_title_chronicle_topics field (often "N/A" placeholders for alerts
  // where the page-change agent couldn't extract agenda items from HTML).
  const notNA = (t: unknown): t is string =>
    typeof t === "string" && t.toUpperCase() !== "N/A" && t !== "";

  const plan = row.bubble_action as Record<string, unknown> | undefined;
  const previews = (plan?.agenda_item_previews as Array<Record<string, unknown>> | undefined) ?? [];
  const fromPreviews = Array.from(new Set(
    previews.flatMap(p => (p.chronicle_topics as string[] | undefined) ?? []).filter(notNA),
  ));
  if (fromPreviews.length) return fromPreviews;

  // Fallback: read from the alert row's agent-output compound field. Renamed from
  // `agenda_item_title_chronicle_topics` → `agenda_item_title` in the 2026-10 schema
  // sync; also fold in the new top-level `chronicle_topic` scalar if present (the
  // post-sync Web Tracking Agent emits the overarching doc topic there).
  const entries = (
    (row.agenda_item_title as Array<Record<string, unknown>> | undefined) ??
    (row.agenda_item_title_chronicle_topics as Array<Record<string, unknown>> | undefined) ??
    []
  );
  const inline = Array.from(new Set(
    entries.flatMap(e => (e.chronicle_topics as string[] | undefined) ?? []).filter(notNA),
  ));
  // Top-level `chronicle_topic` is schema-typed as a scalar but the agent sometimes
  // emits multiple topics joined with `;`, `,`, or `|` — split and dedupe each piece.
  for (const t of splitTopicNames(row.chronicle_topic)) {
    if (!inline.includes(t)) inline.push(t);
  }
  return inline;
}

// Split a possibly-joined topic string into clean individual topic names.
// The post-2026-10 Web Agent schema defines `chronicle_topic` as a single string,
// but in practice the agent sometimes concatenates multiple topics with `;` or
// `|`. This helper normalises both cases so each resulting topic can be
// resolved against the Bubble chronicletopic table independently.
// NOTE: comma is intentionally NOT a delimiter — real Bubble topic titles
// routinely contain commas (e.g. "Markets, Risk-Based Capital, and Reserves").
export function splitTopicNames(raw: unknown): string[] {
  if (typeof raw !== "string") return [];
  return raw
    .split(/[;|]/)
    .map(s => s.trim())
    .filter(s => s && s.toUpperCase() !== "N/A");
}

// Bubble Data API expects ISO 8601 UTC (e.g. "2026-06-11T18:00:00.000Z").
function toUtcIso(raw: string): string {
  if (!raw || raw.toUpperCase() === "N/A") return "";
  try {
    const d = new Date(raw.trim());
    return isNaN(d.getTime()) ? "" : d.toISOString();
  } catch { return ""; }
}

function parseDate(raw: string): string {
  if (!raw) return "";
  // Strip trailing parenthetical annotations like "(HTML document link title)"
  raw = raw.trim().replace(/\s*\(.*$/, "").trim();
  if (raw.length >= 10 && raw[4] === "-") return raw.slice(0, 10);
  const months: Record<string, string> = {
    January: "01", February: "02", March: "03", April: "04",
    May: "05", June: "06", July: "07", August: "08",
    September: "09", October: "10", November: "11", December: "12",
  };
  // No $ anchor — allow trailing text after the date pattern
  const m = raw.match(/^([A-Z][a-z]+)\s+(\d{1,2}),\s+(\d{4})/);
  if (m) {
    const mon = months[m[1]];
    if (mon) return `${m[3]}-${mon}-${m[2].padStart(2, "0")}`;
  }
  return "";
}

// ---------------------------------------------------------------------------
// Computed calendaritem fields (not auto-populated when writing via Data API)
// ---------------------------------------------------------------------------

function computeEventTimeFields(
  startUtc: string,
  endUtc: string,
  isFullDay: boolean,
): Record<string, unknown> {
  const fields: Record<string, unknown> = { "full day": isFullDay, "datewasset": false };

  if (!startUtc) return fields;
  const start = new Date(startUtc);
  if (isNaN(start.getTime())) return fields;

  fields["datewasset"] = true;

  if (!isFullDay) {
    const h24 = start.getUTCHours();
    const h12 = h24 % 12 || 12;
    const min = start.getUTCMinutes();
    fields["AMPM from"]    = h24 < 12 ? "AM" : "PM";
    fields["Hour from"]    = String(h12).padStart(2, "0");
    fields["Minutes from"] = String(min).padStart(2, "0");

    if (endUtc) {
      const end = new Date(endUtc);
      if (!isNaN(end.getTime())) {
        const eh24 = end.getUTCHours();
        const eh12 = eh24 % 12 || 12;
        const emin = end.getUTCMinutes();
        fields["AMPM until"]    = eh24 < 12 ? "AM" : "PM";
        fields["Hours until"]   = String(eh12).padStart(2, "0");
        fields["Minutes until"] = String(emin).padStart(2, "0");

        const durationMins = Math.round((end.getTime() - start.getTime()) / 60_000);
        if (durationMins > 0) {
          const dh = Math.floor(durationMins / 60);
          const dm = durationMins % 60;
          fields["length"] = `${dh}:${String(dm).padStart(2, "0")}`;
        }
      }
    }
  }

  return fields;
}

// ---------------------------------------------------------------------------
// Public validation types + pre-flight check
// ---------------------------------------------------------------------------

export interface ValidationIssue {
  field: string;
  message: string;
  unresolved: string[];
  severity: "error" | "warning";
}

export interface ValidationResult {
  valid: boolean; // true = no errors (warnings allowed)
  issues: ValidationIssue[];
}

export async function validateSyncPlan(
  agentCallId: string,
  rows: Record<string, unknown>[],
): Promise<ValidationResult> {
  const row = rows.find(r => r.agent_call_id === agentCallId);
  if (!row) return { valid: false, issues: [{ field: "Row", message: `No row found for agent_call_id=${agentCallId}`, unresolved: [], severity: "error" }] };

  // When there's no stored bubble_action, the Content Gate may still have synthesized
  // agenda items from doc extraction and will patch the row before syncing. Nothing to
  // validate at open time — return success so the banner stays silent.
  const plan = row.bubble_action as Record<string, unknown> | undefined;
  if (!plan) return { valid: true, issues: [] };

  const ep = (plan.event_preview  ?? {}) as Record<string, unknown>;
  const lp = (plan.library_item_preview ?? {}) as Record<string, unknown>;
  const epFieldIds = (ep.field_ids ?? {}) as Record<string, unknown>;
  const lpFieldIds = (lp.field_ids ?? {}) as Record<string, unknown>;

  const orgNames = ((ep.group ?? lp.group ?? []) as unknown[]).map(String).filter(Boolean);
  const epTopicNames    = getTopicNamesFromFieldIds(epFieldIds);
  const lpTopicNames    = getTopicNamesFromFieldIds(lpFieldIds);
  const agendaTopicNames = getAgendaTopicNames(row);
  const allTopicNames   = Array.from(new Set([...epTopicNames, ...lpTopicNames, ...agendaTopicNames]));
  const libTypeName     = plan.library_item ? String((lp as Record<string, unknown>).type ?? "Agenda & Materials") : null;

  // Run all lookups in parallel
  const [unresolvedOrgs, unresolvedTopics, libTypeId, calTypeId] = await Promise.all([
    orgNames.length
      ? bubbleListAll("organization", CONTENT_SPACE_CONSTRAINT).then(orgs => {
          const nameToId = new Map(orgs.map(o => [String(o.Name ?? "").trim(), String(o._id ?? "")]));
          return orgNames.filter(n => !matchOrgName(n, nameToId));
        })
      : Promise.resolve([] as string[]),

    allTopicNames.length
      ? bubbleListAll("chronicletopic", CONTENT_SPACE_CONSTRAINT).then(topics => {
          const nameToId = new Map(topics.map(t => [String(t.Title ?? "").trim(), String(t._id ?? "")]));
          return allTopicNames.filter(n => !nameToId.has(n));
        })
      : Promise.resolve([] as string[]),

    libTypeName ? resolveLibraryItemTypeId(libTypeName) : Promise.resolve("skip" as string | null),
    plan.event  ? resolveCalendarItemTypeId("Meeting")  : Promise.resolve("skip" as string | null),
  ]);

  const issues: ValidationIssue[] = [];

  if (unresolvedOrgs.length) {
    issues.push({
      field: "Organizations",
      message: `${unresolvedOrgs.length} org name${unresolvedOrgs.length > 1 ? "s" : ""} not found in Bubble — record will publish with empty Organizations field`,
      unresolved: unresolvedOrgs,
      severity: "error",
    });
  }

  if (unresolvedTopics.length) {
    issues.push({
      field: "Chronicle Topics",
      message: `${unresolvedTopics.length} topic name${unresolvedTopics.length > 1 ? "s" : ""} not found — "Calendar Events with no Topic" fallback will be used`,
      unresolved: unresolvedTopics,
      severity: "warning",
    });
  }

  if (libTypeId !== "skip" && !libTypeId) {
    issues.push({
      field: "Library Item Type",
      message: `Type "${libTypeName}" not found in Bubble — library item will publish with no type`,
      unresolved: libTypeName ? [libTypeName] : [],
      severity: "error",
    });
  }

  if (calTypeId !== "skip" && !calTypeId) {
    issues.push({
      field: "Calendar Item Type",
      message: `Type "Meeting" not found in Bubble — event will publish with no type`,
      unresolved: ["Meeting"],
      severity: "error",
    });
  }

  return { valid: !issues.some(i => i.severity === "error"), issues };
}

// ---------------------------------------------------------------------------
// Public sync result type
// ---------------------------------------------------------------------------

export interface SyncResult {
  ok: boolean;
  error?: string;
  bubble_event_id?: string | null;
  bubble_library_item_id?: string | null;
  eidarix_agenda_item_ids?: string[];
  bubble_sync_status?: string;
}

// ---------------------------------------------------------------------------
// Main sync function
// ---------------------------------------------------------------------------

/**
 * Execute Bubble sync for an alert identified by agentCallId.
 * Mutates `rows` in-place (patches status/IDs onto the matching row).
 * Caller is responsible for saving the mutated rows back to S3.
 *
 * action: "all" | "agenda_items" | "library_item" | "event"
 *   - "all": run agenda items → library item → event in sequence
 *   - "agenda_items": create/link agenda items, patch IDs, return early
 *   - "library_item": sync library item only (picks up existing agenda IDs)
 *   - "event": sync event only (picks up existing library item and agenda IDs)
 */
export async function syncAlert(
  agentCallId: string,
  action: string,
  rows: Record<string, unknown>[],
): Promise<SyncResult> {
  const row = rows.find(r => r.agent_call_id === agentCallId);
  if (!row) return { ok: false, error: `no row for agent_call_id=${agentCallId}` };

  const plan = row.bubble_action as Record<string, unknown> | undefined;
  if (!plan) return { ok: false, error: "no bubble_action on row — run backfill_bubble_action.py" };

  const eventAction = plan.event as string | null;
  const libAction = plan.library_item as string | null;
  const hasAgendaItems = !!plan.agenda_items;
  const ep = (plan.event_preview ?? {}) as Record<string, unknown>;
  const lp = (plan.library_item_preview ?? {}) as Record<string, unknown>;
  const epFieldIds = (ep.field_ids ?? {}) as Record<string, unknown>;
  const lpFieldIds = (lp.field_ids ?? {}) as Record<string, unknown>;

  const runAgenda = action === "all" || action === "agenda_items";
  const runLib    = action === "all" || action === "library_item";
  const runEvent  = action === "all" || action === "event";

  let bubbleLibraryItemId: string | null = null;
  let bubbleEventId: string | null = null;
  let agendaItemIds: string[] = [];

  // When action=event, carry forward IDs already on the row
  if (action === "event") {
    const existingLibId = String(row.bubble_library_item_id ?? "").trim() || null;
    if (existingLibId) bubbleLibraryItemId = existingLibId;
    const existingAgendaIds = (row.eidarix_agenda_item_ids as unknown[] | undefined) ?? [];
    if (existingAgendaIds.length) agendaItemIds = existingAgendaIds.map(String).filter(Boolean);
  }

  try {
    // Org resolution
    const orgNames = ((ep.group ?? lp.group ?? []) as unknown[]).map(String).filter(Boolean);
    const orgIds = orgNames.length ? await resolveOrgIds(orgNames) : [];

    // Chronicle topic resolution — collect topic names from both previews
    const epTopicNames = getTopicNamesFromFieldIds(epFieldIds);
    const lpTopicNames = getTopicNamesFromFieldIds(lpFieldIds);
    const allTopicNames = Array.from(new Set([...epTopicNames, ...lpTopicNames]));
    const { ids: topicIds, nameToId: topicNameToId } = allTopicNames.length
      ? await resolveTopicIds(allTopicNames)
      : { ids: [], nameToId: new Map<string, string>() };

    // ── Agenda items ────────────────────────────────────────────────────────
    if (hasAgendaItems && (runAgenda || runLib)) {
      const existingAgendaIds = ((row.eidarix_agenda_item_ids as unknown[] | undefined) ?? []).map(String).filter(Boolean);
      if (existingAgendaIds.length && action === "library_item") {
        // Already created in a prior agenda_items step — pick them up
        agendaItemIds = existingAgendaIds;
      } else {
        // Ensure all agenda item topics are in the topicNameToId map
        const previewsForTopics = (plan.agenda_item_previews as Array<Record<string, unknown>> | undefined) ?? [];
        const agendaTopicNames = Array.from(new Set(
          previewsForTopics.flatMap(p => (p.chronicle_topics as string[] | undefined) ?? []).filter(Boolean),
        ));
        let agendaTopicNameToId = topicNameToId;
        if (agendaTopicNames.some(n => !topicNameToId.has(n))) {
          const { nameToId } = await resolveTopicIds(agendaTopicNames);
          agendaTopicNameToId = new Map([...Array.from(topicNameToId), ...Array.from(nameToId)]);
        }
        agendaItemIds = await resolveAgendaItems(row, agentCallId, agendaTopicNameToId);
        console.info(`[bubble-sync] resolved ${agendaItemIds.length} agenda item(s)`);

        if (runAgenda) {
          // Patch IDs and return so the next step (library_item) can pick them up
          patchRow(rows, agentCallId, { eidarix_agenda_item_ids: agendaItemIds });
          return { ok: true, eidarix_agenda_item_ids: agendaItemIds, bubble_event_id: null, bubble_library_item_id: null };
        }
      }
    }

    // ── Library item ────────────────────────────────────────────────────────
    if (runLib) {
      if (libAction === "create") {
        // Topic IDs for library item: union of agenda item topics + doc extraction topics
        const agendaTopicNames = getAgendaTopicNames(row);
        const libTopicNamesSet = new Set([...agendaTopicNames, ...lpTopicNames]);
        const libTopicIds = libTopicNamesSet.size
          ? (await resolveTopicIds(Array.from(libTopicNamesSet))).ids
          : topicIds;

        // Extract fields — support both new Bubble display names and old underscore keys
        const title = String(lpFieldIds["Name"] ?? lpFieldIds["name_text"] ?? lp.title ?? "").trim();
        const url = String(lpFieldIds["URL"] ?? lpFieldIds["url_text"] ?? lp.url ?? "").trim();
        const summary = String(lpFieldIds["summary"] ?? lpFieldIds["description_text"] ?? "").trim() || undefined;

        // Date: prefer event start date, fall back to doc extraction date in field_ids
        let date = parseDate(String(row.event_start_date_time ?? "").slice(0, 10));
        if (!date) date = parseDate(String(lpFieldIds["date"] ?? lpFieldIds["date_date"] ?? ""));

        const typeName = String(lp.type ?? "Agenda & Materials");
        const typeId = await resolveLibraryItemTypeId(typeName);

        const payload: Record<string, unknown> = {
          title,
          alert_id: agentCallId,
          space_id: CONTENT_SPACE_ID,
          chronicle_topics: libTopicIds,
        };
        if (agendaItemIds.length) payload.agenda_items = agendaItemIds;
        if (date) { payload.date = date; payload.date_display = "Full date"; }
        if (summary) payload.summary = summary;
        if (url && url.toUpperCase() !== "N/A") payload.url = url;
        if (typeId) payload.type = typeId;
        if (orgIds.length) payload.organizations = orgIds;

        console.info("[bubble-sync] CREATE libraryitem via wf/create-library-item/");
        const result = await eidarixWfPost("create-library-item/", payload);
        bubbleLibraryItemId = String(result.bubble_id ?? result.id ?? result._id ?? "").trim() || null;
        if (bubbleLibraryItemId) console.info("[bubble-sync] created libraryitem", bubbleLibraryItemId);
        else console.warn("[bubble-sync] create-library-item returned no id:", result);

      } else if (libAction === "update") {
        const matchSearch = (lp.match_search ?? {}) as Record<string, string>;
        const existingLibId = await findLibraryItem(matchSearch);
        if (!existingLibId) {
          throw new Error(`No matched library item found for match_search=${JSON.stringify(matchSearch)}`);
        }
        bubbleLibraryItemId = existingLibId;
        // PATCH chronicle topics onto existing record (trimmed payload — no overwriting core fields)
        const patchRes = await fetch(`${OBJ_BASE}/libraryitem/${existingLibId}`, {
          method: "PATCH",
          headers: {
            Authorization: `Bearer ${await getBubbleKey()}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ "Topics - dt": topicIds }),
        });
        if (!patchRes.ok) {
          const body = await patchRes.text();
          console.warn("[bubble-sync] libraryitem PATCH failed:", patchRes.status, body.slice(0, 300));
        } else {
          console.info("[bubble-sync] updated libraryitem", existingLibId);
        }
      }
    }

    // ── Calendar item ────────────────────────────────────────────────────────
    if (runEvent) {
      const r = row as Record<string, unknown>;
      const startDt = toUtcIso(String(ep.start_datetime || r.event_start_date_time || r.event_start_date_time_et || ""));
      const endDt = toUtcIso(String(ep.end_datetime || r.event_end_date_time || r.event_end_date_time_et || ""));
      const locationUrl = String(ep.url ?? "").trim() || null;

      // Event topics: union of doc-extraction topics + all agenda item topics
      const agendaTopicNames = getAgendaTopicNames(row);
      let eventTopicIds = [...topicIds];
      if (agendaTopicNames.length) {
        const extra = await resolveTopicIds(agendaTopicNames);
        eventTopicIds = Array.from(new Set([...eventTopicIds, ...extra.ids]));
      }
      if (!eventTopicIds.length) {
        const fallbackId = await getEmptyTopicId();
        if (fallbackId) eventTopicIds = [fallbackId];
      }

      if (eventAction === "create") {
        const calTypeId = await resolveCalendarItemTypeId("Meeting");
        const isFullDay = String(row.event_is_full_day ?? ep.is_full_day ?? "").trim() === "Full Day";
        const timeFields = computeEventTimeFields(startDt, endDt, isFullDay);

        // Use Bubble Data API directly — field names are display names (see bubble_api_reference.md)
        const eventPayload: Record<string, unknown> = {
          "Space": CONTENT_SPACE_ID,
          "Topics - dt": eventTopicIds,
          "has topic": eventTopicIds.length > 0,
          "Timezone Code": "America/New_York",
          "Eiderix Ref": agentCallId,
          ...timeFields,
        };
        const evTitle = String(ep.title ?? "").trim();
        if (evTitle && evTitle.toUpperCase() !== "N/A") {
          eventPayload["title"] = evTitle;
          eventPayload["name for search"] = evTitle.toLowerCase();
        }
        const evDescription = String(row.alert_description ?? "").trim();
        if (evDescription && evDescription.toUpperCase() !== "N/A") eventPayload["event description"] = evDescription;
        if (startDt) eventPayload["date"] = startDt;
        if (endDt) eventPayload["End time"] = endDt;
        if (calTypeId) eventPayload["type"] = calTypeId;
        if (orgIds.length) eventPayload["Orgs "] = orgIds;
        if (agendaItemIds.length) eventPayload["attached agenda items"] = agendaItemIds;
        if (bubbleLibraryItemId) eventPayload["Agenda"] = [bubbleLibraryItemId];
        if (locationUrl && locationUrl.toUpperCase() !== "N/A") eventPayload["location"] = locationUrl;

        console.info("[bubble-sync] CREATE calendaritem via Data API", JSON.stringify(eventPayload));
        const createRes = await fetch(`${OBJ_VERSIONED}/calendaritem`, {
          method: "POST",
          headers: { Authorization: `Bearer ${await getBubbleKey()}`, "Content-Type": "application/json" },
          body: JSON.stringify(eventPayload),
          signal: AbortSignal.timeout(30_000),
        });
        const createData = await createRes.json() as { status: string; id?: string };
        if (createData.status !== "success" || !createData.id) {
          throw new Error(`Bubble create calendaritem failed: ${JSON.stringify(createData)}`);
        }
        bubbleEventId = createData.id;
        console.info("[bubble-sync] created calendaritem", bubbleEventId);

      } else if (eventAction === "update") {
        const matchSearch = { ...(ep.match_search ?? {}) } as Record<string, string>;
        // bubble_action may have been stamped when date was missing — supplement from live row value
        if (!matchSearch.date) {
          const rr = row as Record<string, unknown>;
          const rawDate = String(rr.event_start_date_time ?? rr.event_start_date_time_et ?? "").trim();
          if (rawDate && rawDate.toUpperCase() !== "N/A") matchSearch.date = rawDate.slice(0, 10);
        }
        const existingEventId = await findCalendarItem(matchSearch);
        if (!existingEventId) {
          throw new Error(`No matched calendar item found for match_search=${JSON.stringify(matchSearch)}`);
        }
        // PATCH only the linking fields — datetime/type/orgs already on the record
        const updatePayload: Record<string, unknown> = {
          "Topics - dt": eventTopicIds,
          "Eiderix Ref": agentCallId,
        };
        if (agendaItemIds.length) updatePayload["attached agenda items"] = agendaItemIds;
        if (bubbleLibraryItemId) updatePayload["Agenda"] = [bubbleLibraryItemId];

        console.info("[bubble-sync] UPDATE calendaritem via Data API PATCH", existingEventId, JSON.stringify(updatePayload));
        const patchRes = await fetch(`${OBJ_VERSIONED}/calendaritem/${existingEventId}`, {
          method: "PATCH",
          headers: { Authorization: `Bearer ${await getBubbleKey()}`, "Content-Type": "application/json" },
          body: JSON.stringify(updatePayload),
          signal: AbortSignal.timeout(30_000),
        });
        if (!patchRes.ok) {
          const errText = await patchRes.text();
          throw new Error(`Bubble PATCH calendaritem ${patchRes.status}: ${errText.slice(0, 300)}`);
        }
        bubbleEventId = existingEventId;
        console.info("[bubble-sync] updated calendaritem", bubbleEventId);
      }
    }

    // Patch the in-memory row with results; caller saves rows to S3
    const patchFields: Record<string, unknown> = {};
    if (action === "all") patchFields.bubble_sync_status = "synced";
    if (bubbleEventId) patchFields.bubble_event_id = bubbleEventId;
    if (bubbleLibraryItemId && runLib) patchFields.bubble_library_item_id = bubbleLibraryItemId;
    if (agendaItemIds.length) patchFields.eidarix_agenda_item_ids = agendaItemIds;
    if (Object.keys(patchFields).length) patchRow(rows, agentCallId, patchFields);

    return {
      ok: true,
      bubble_event_id: bubbleEventId,
      bubble_library_item_id: bubbleLibraryItemId,
      eidarix_agenda_item_ids: agendaItemIds,
      bubble_sync_status: patchFields.bubble_sync_status as string | undefined,
    };

  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error(`[bubble-sync] error for agent_call_id=${agentCallId} action=${action}:`, msg);
    patchRow(rows, agentCallId, { bubble_sync_status: "error", bubble_sync_error: msg });
    return { ok: false, error: msg };
  }
}

function patchRow(
  rows: Record<string, unknown>[],
  agentCallId: string,
  fields: Record<string, unknown>,
): void {
  for (const r of rows) {
    if (r.agent_call_id === agentCallId) Object.assign(r, fields);
  }
}
