/**
 * Eidarix Bubble sync executor — ports bubble/bubble_sync.py to TypeScript.
 * Replaces the Lambda intermediary: calls Eidarix workflow endpoints directly.
 *
 * Processing order: agenda items → library item → calendar item
 * Each step can run independently (action param) or as a full sequence (action="all").
 */

const EIDARIX_VERSION = process.env.EIDARIX_VERSION ?? "test";
const SPACE_IDS: Record<string, string> = {
  test: "1768998437948x865417918648382000",
  live: "1770642377799x775210694699370900",
};
const SPACE_ID = SPACE_IDS[EIDARIX_VERSION] ?? SPACE_IDS.test;
const SPACE_CONSTRAINT = [{ key: "space", constraint_type: "equals", value: SPACE_ID }];

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

const STATUS_PREFIX_RE = /^(New|Existing|Updated)\s*[-–]\s*/i;
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

async function resolveOrgIds(orgNames: string[]): Promise<string[]> {
  if (!orgNames.length) return [];
  const orgs = await bubbleListAll("organization", SPACE_CONSTRAINT);
  const nameToId = new Map(orgs.map(o => [String(o.Name ?? "").trim(), String(o._id ?? "")]));
  const ids: string[] = [];
  const missing: string[] = [];
  for (const name of orgNames) {
    const id = nameToId.get(name);
    if (id) ids.push(id);
    else missing.push(name);
  }
  if (missing.length) console.warn("[bubble-sync] org(s) not found in Bubble:", missing, `EIDARIX_VERSION=${EIDARIX_VERSION}`);
  return ids;
}

async function resolveTopicIds(
  topicNames: string[],
): Promise<{ ids: string[]; nameToId: Map<string, string> }> {
  if (!topicNames.length) return { ids: [], nameToId: new Map() };
  const topics = await bubbleListAll("chronicletopic", SPACE_CONSTRAINT);
  const nameToId = new Map(topics.map(t => [String(t.Title ?? "").trim(), String(t._id ?? "")]));
  const ids: string[] = [];
  for (const name of topicNames) {
    const id = nameToId.get(name);
    if (id) ids.push(id);
    else console.warn("[bubble-sync] chronicle topic not found:", name);
  }
  return { ids, nameToId };
}

async function resolveLibraryItemTypeId(typeName: string): Promise<string | null> {
  try {
    const types = await bubbleSearch("libraryitemtype", SPACE_CONSTRAINT, 50);
    for (const t of types) {
      if (String(t.Title ?? "").trim().toLowerCase() === typeName.trim().toLowerCase()) return String(t._id);
    }
    console.warn("[bubble-sync] libraryitemtype not found:", typeName);
  } catch (e) { console.warn("[bubble-sync] libraryitemtype lookup failed:", e); }
  return null;
}

async function resolveCalendarItemTypeId(typeName: string): Promise<string | null> {
  try {
    const params = new URLSearchParams({
      constraints: JSON.stringify(SPACE_CONSTRAINT),
      limit: "50",
    });
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
      ...SPACE_CONSTRAINT,
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
        ...SPACE_CONSTRAINT,
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
  if (matchSearch.url) {
    const results = await bubbleSearch("libraryitem", [
      ...SPACE_CONSTRAINT,
      { key: "url_text", constraint_type: "equals", value: matchSearch.url },
    ], 5);
    if (results.length) return String(results[0]._id);
  }
  if (matchSearch.title) {
    const results = await bubbleSearch("libraryitem", [
      ...SPACE_CONSTRAINT,
      { key: "name_text", constraint_type: "equals", value: matchSearch.title },
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
      ...SPACE_CONSTRAINT,
      { key: "date", constraint_type: "greater than", value: `${dateStr}T00:00:00.000Z` },
      { key: "date", constraint_type: "less than", value: nextDay.toISOString().slice(0, 10) + "T00:00:00.000Z" },
    ];

    if (matchSearch.org) {
      try {
        const orgs = await bubbleListAll("organization", SPACE_CONSTRAINT);
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
    const orgs = await bubbleListAll("organization", SPACE_CONSTRAINT);
    const org = orgs.find(o => String(o.Name ?? "").trim() === matchSearch.org);
    if (!org?._id) return null;
    const results = await bubbleSearch("calendaritem", [
      ...SPACE_CONSTRAINT,
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
    // Fallback: build previews from flat alert fields (old rows without previews)
    const agendaEntries = (row.agenda_item_title_chronicle_topics as Array<Record<string, unknown>> | undefined) ?? [];
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
    let referenceId = stripStatusPrefix(String(item.reference_id ?? "").trim());
    if (officialTitle.toUpperCase() === "N/A") officialTitle = "";
    if (referenceId.toUpperCase() === "N/A") referenceId = "";

    const result = await eidarixWfPost("create-agenda-item/", {
      title,
      official_title: officialTitle,
      reference_id: referenceId,
      chronicle_topics: topicIds,
      alert_id: agentCallId,
      space_id: SPACE_ID,
    });

    const itemId = result.bubble_id ?? result.id ?? result._id;
    if (!itemId) throw new Error(`create-agenda-item returned no id for '${title}': ${JSON.stringify(result)}`);
    console.info(`[bubble-sync] created agenda item '${title}' → ${itemId}`);
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
  const entries = (row.agenda_item_title_chronicle_topics as Array<Record<string, unknown>> | undefined) ?? [];
  return Array.from(new Set(
    entries
      .flatMap(e => (e.chronicle_topics as string[] | undefined) ?? [])
      .filter((t): t is string => typeof t === "string" && t.toUpperCase() !== "N/A" && t !== ""),
  ));
}

function parseDate(raw: string): string {
  if (!raw) return "";
  raw = raw.trim();
  if (raw.length >= 10 && raw[4] === "-") return raw.slice(0, 10);
  const months: Record<string, string> = {
    January: "01", February: "02", March: "03", April: "04",
    May: "05", June: "06", July: "07", August: "08",
    September: "09", October: "10", November: "11", December: "12",
  };
  const m = raw.match(/^([A-Z][a-z]+)\s+(\d{1,2}),\s+(\d{4})$/);
  if (m) {
    const mon = months[m[1]];
    if (mon) return `${m[3]}-${mon}-${m[2].padStart(2, "0")}`;
  }
  return "";
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
          space_id: SPACE_ID,
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
      const startDt = String(ep.start_datetime ?? "");
      const endDt = String(ep.end_datetime ?? "");
      const locationUrl = String(ep.url ?? "").trim() || null;
      const callIn = String(ep.call_in ?? "").trim() || null;

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

        const eventPayload: Record<string, unknown> = {
          alert_id: agentCallId,
          space_id: SPACE_ID,
          chronicle_topics: eventTopicIds,
        };
        if (agendaItemIds.length) eventPayload.agenda_items = agendaItemIds;
        if (bubbleLibraryItemId) eventPayload.agenda = [bubbleLibraryItemId];
        if (startDt) eventPayload.start_datetime = startDt;
        if (endDt) eventPayload.end_datetime = endDt;
        if (locationUrl && locationUrl.toUpperCase() !== "N/A") eventPayload.location_url = locationUrl;
        if (callIn && callIn.toUpperCase() !== "N/A") eventPayload.phone_and_access_code = callIn;
        if (calTypeId) eventPayload.type = calTypeId;
        if (orgIds.length) eventPayload.organizations = orgIds;

        console.info("[bubble-sync] CREATE calendaritem via wf/create-event");
        const eventResult = await eidarixWfPost("create-event", eventPayload);
        bubbleEventId = String(eventResult.bubble_id ?? eventResult.id ?? eventResult._id ?? "").trim() || null;
        if (bubbleEventId) console.info("[bubble-sync] created calendaritem", bubbleEventId);
        else console.warn("[bubble-sync] create-event returned no id:", eventResult);

      } else if (eventAction === "update") {
        const matchSearch = { ...(ep.match_search ?? {}) } as Record<string, string>;
        // bubble_action may have been stamped when date was missing — supplement from live row value
        if (!matchSearch.date) {
          const rawDate = String(row.event_start_date_time ?? "").trim();
          if (rawDate && rawDate.toUpperCase() !== "N/A") matchSearch.date = rawDate.slice(0, 10);
        }
        const existingEventId = await findCalendarItem(matchSearch);
        if (!existingEventId) {
          throw new Error(`No matched calendar item found for match_search=${JSON.stringify(matchSearch)}`);
        }
        // Trimmed payload only — event details (datetime, type, orgs) already on the record
        const updatePayload: Record<string, unknown> = {
          id: existingEventId,
          alert_id: agentCallId,
          space_id: SPACE_ID,
          chronicle_topics: eventTopicIds,
        };
        if (agendaItemIds.length) updatePayload.agenda_items = agendaItemIds;
        if (bubbleLibraryItemId) updatePayload.agenda = [bubbleLibraryItemId];

        console.info("[bubble-sync] UPDATE calendaritem via wf/update-event", existingEventId);
        const updateResult = await eidarixWfPost("update-event", updatePayload);
        bubbleEventId = String(updateResult.bubble_id ?? updateResult.id ?? updateResult._id ?? existingEventId).trim();
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
