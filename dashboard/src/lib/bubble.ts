/**
 * Read-only Bubble Data API client.
 * No write endpoints. In-memory cache (60s) to reduce API calls.
 */

export const TYPE_CALENDAR_ITEM = "Calendar item";
export const TYPE_RESOURCE = "Resource";
export const TYPE_TREE_NODE = "Tree node";
export const TYPE_CHRONICLE = "Chronicle";
export const TYPE_AGENDA_ITEM = "Agenda Item";
export const TYPE_CHRONICLE_LINK = "Chronicle Link";
export const TYPE_ALERT = "Alert";

/** Type names to try when resolving topic suggestion / chronicle IDs */
const CHRONICLE_TYPE_NAMES = [
  "Chronicle Topic", // confirmed working type in this Bubble app (API name: chronicletopic)
  "Resource", // topic suggestion may reference another Resource
  "Chronicle",
  "Chronicles",
  "Topic suggestion",
  "Topic Suggestion",
  "Topic",
  "Chronicle item",
  "topic_suggestion",
  "Potentially relevant chronicle",
  "Relevant chronicle",
];

const CACHE_TTL_MS = 60_000;
const GROUPS_CACHE_TTL_MS = 600_000; // 10 minutes

// --- Typed interfaces ---

export interface CalendarItem {
  _id: string;
  title?: string;
  date?: string;
  "NAIC Group (tree node)"?: string;
  [key: string]: unknown;
}

export interface Resource {
  _id: string;
  Name?: string;
  URL?: string;
  Type1?: string;
  "topic suggestion"?: string;
  "Related calendar items"?: string | string[];
  date?: string;
  [key: string]: unknown;
}

// --- API primitives ---

export interface BubbleConstraint {
  key: string;
  constraint_type: string;
  value: string | number;
}

export interface BubbleSearchParams {
  constraints?: BubbleConstraint[];
  limit?: number;
  cursor?: number;
  sort_field?: string;
  descending?: boolean;
}

export interface BubbleSearchResponse<T> {
  response: {
    cursor: number;
    results: T[];
    count: number;
    remaining: number;
  };
}

export class BubbleAPIError extends Error {
  constructor(
    message: string,
    public statusCode?: number,
    public body?: unknown
  ) {
    super(message);
    this.name = "BubbleAPIError";
  }
}

async function getApiConfig(): Promise<{ url: string; key: string }> {
  let url = process.env.BUBBLE_API_URL;
  let key = process.env.BUBBLE_API_KEY;
  if (!url || !key) {
    const { getBubbleApiUrl, getBubbleApiKey } = await import("./ssm");
    url = url ?? (await getBubbleApiUrl()) ?? "";
    key = key ?? (await getBubbleApiKey()) ?? "";
  }
  if (!url || !key) {
    throw new BubbleAPIError(
      "BUBBLE_API_URL and BUBBLE_API_KEY must be set (or available from SSM)"
    );
  }
  const base = url.replace(/\/$/, "");
  return { url: base, key };
}

function buildObjEndpoint(baseUrl: string, typeName: string): string {
  const encoded = encodeURIComponent(typeName);
  if (baseUrl.endsWith("/obj")) {
    return `${baseUrl}/${encoded}`;
  }
  return `${baseUrl}/obj/${encoded}`;
}

/** Bubble API expects typename as lowercase, no spaces (e.g. "Tree node" -> "treenode") */
function toApiTypeName(displayName: string): string {
  return displayName.toLowerCase().replace(/\s+/g, "");
}

// --- In-memory cache ---

interface CacheEntry<T> {
  data: T;
  expiresAt: number;
}

const cache = new Map<string, CacheEntry<unknown>>();

function cacheGet<T>(key: string): T | null {
  const entry = cache.get(key) as CacheEntry<T> | undefined;
  if (!entry || Date.now() > entry.expiresAt) {
    cache.delete(key);
    return null;
  }
  return entry.data;
}

function cacheSet<T>(key: string, data: T, ttlMs = CACHE_TTL_MS): void {
  cache.set(key, {
    data,
    expiresAt: Date.now() + ttlMs,
  });
}

/**
 * Search for things with constraints.
 */
export async function search<T = Record<string, unknown>>(
  typeName: string,
  params: BubbleSearchParams = {}
): Promise<BubbleSearchResponse<T>> {
  const { url, key } = await getApiConfig();
  const endpoint = buildObjEndpoint(url, typeName);

  const searchParams = new URLSearchParams();
  if (params.constraints && params.constraints.length > 0) {
    searchParams.set("constraints", JSON.stringify(params.constraints));
  }
  if (params.limit != null) {
    searchParams.set("limit", String(params.limit));
  }
  if (params.cursor != null) {
    searchParams.set("cursor", String(params.cursor));
  }
  if (params.sort_field) {
    searchParams.set("sort_field", params.sort_field);
  }
  if (params.descending != null) {
    searchParams.set("descending", String(params.descending));
  }

  const fullUrl = `${endpoint}?${searchParams.toString()}`;
  const res = await fetch(fullUrl, {
    method: "GET",
    headers: {
      Authorization: `Bearer ${key}`,
      "Content-Type": "application/json",
    },
  });

  if (!res.ok) {
    const body = await res.text();
    throw new BubbleAPIError(
      `Bubble API error: ${res.status} ${res.statusText}`,
      res.status,
      body
    );
  }

  const data = (await res.json()) as BubbleSearchResponse<T>;
  return data;
}

/**
 * Fetch a single thing by ID.
 * GET /obj/{typename}/{id}
 * Uses Bubble API typename format: lowercase, no spaces.
 */
export async function getById<T = Record<string, unknown>>(
  typeName: string,
  id: string
): Promise<T | null> {
  if (!id?.trim()) return null;
  const cacheKey = `get:${typeName}:${id}`;
  const cached = cacheGet<T>(cacheKey);
  if (cached) return cached;

  const { url, key } = await getApiConfig();
  const apiTypeName = toApiTypeName(typeName);
  const endpoint = buildObjEndpoint(url, apiTypeName);
  const fullUrl = `${endpoint}/${id}`;

  const res = await fetch(fullUrl, {
    method: "GET",
    headers: {
      Authorization: `Bearer ${key}`,
      "Content-Type": "application/json",
    },
  });

  if (!res.ok) {
    if (res.status === 404) return null;
    const body = await res.text();
    throw new BubbleAPIError(
      `Bubble API error: ${res.status} ${res.statusText}`,
      res.status,
      body
    );
  }

  const json = (await res.json()) as { response?: T };
  const data = (json.response ?? json) as T;
  cacheSet(cacheKey, data);
  return data;
}

/** Possible display name fields for Chronicle / Topic suggestion objects */
const CHRONICLE_NAME_KEYS = [
  "shortened topic", // Chronicle Topic type: short display name (e.g. "Funding Agreements")
  "name",
  "Name",
  "text",
  "Text",
  "title",
  "Title",
  "display_name",
  "Display name",
  "value",
  "Value",
];

const SKIP_KEYS = new Set(["_id", "id", "created_date", "modified_date", "slug"]);

/** Diagnostic: log HTTP status for each type when resolving chronicle ID (run once per session) */
let _chronicleDiagnosticLogged = false;
async function logChronicleTypeDiagnostic(id: string): Promise<void> {
  if (!process.env.DEBUG_LINKING || _chronicleDiagnosticLogged) return;
  _chronicleDiagnosticLogged = true;
  const customType = process.env.BUBBLE_CHRONICLE_TYPE?.trim();
  const types = customType
    ? [customType, ...CHRONICLE_TYPE_NAMES.filter((t) => t !== customType)]
    : CHRONICLE_TYPE_NAMES;
  const { url, key } = await getApiConfig();
  const results: string[] = [];
  for (const typeName of types) {
    const apiName = toApiTypeName(typeName);
    const endpoint = buildObjEndpoint(url, apiName);
    const fullUrl = `${endpoint}/${id}`;
    try {
      const res = await fetch(fullUrl, {
        method: "GET",
        headers: {
          Authorization: `Bearer ${key}`,
          "Content-Type": "application/json",
        },
      });
      results.push(`${typeName}=${res.status}`);
      if (res.ok) {
        const json = (await res.json()) as { response?: Record<string, unknown> };
        const obj = (json.response ?? json) as Record<string, unknown>;
        console.debug(
          `[DEBUG_CHRONICLES] Type "${typeName}" returned 200. Keys:`,
          Object.keys(obj)
        );
      }
    } catch (e) {
      results.push(`${typeName}=error:${String(e)}`);
    }
  }
  console.debug(
    "[DEBUG_CHRONICLES] HTTP status for chronicle ID by type:",
    results.join(", ")
  );
}

/**
 * Get Chronicle (or similar) display name by ID.
 * Tries BUBBLE_CHRONICLE_TYPE first if set, then CHRONICLE_TYPE_NAMES.
 * Uses getById first; falls back to search when getById returns 404.
 */
export async function getChronicleDisplayName(id: string): Promise<string | null> {
  const debug = process.env.DEBUG_LINKING === "1";
  await logChronicleTypeDiagnostic(id);
  const customType = process.env.BUBBLE_CHRONICLE_TYPE?.trim();
  const typesToTry = customType
    ? [customType, ...CHRONICLE_TYPE_NAMES.filter((t) => t !== customType)]
    : CHRONICLE_TYPE_NAMES;
  for (const typeName of typesToTry) {
    let obj: Record<string, unknown> | null = await getById<Record<string, unknown>>(
      typeName,
      id
    );
    if (!obj) {
      try {
        const resp = await search<Record<string, unknown>>(typeName, {
          constraints: [{ key: "_id", constraint_type: "equals", value: id }],
          limit: 1,
        });
        obj = resp.response?.results?.[0] ?? null;
      } catch {
        obj = null;
      }
    }
    if (!obj) continue;
    if (debug) {
      console.debug(
        `[DEBUG_CHRONICLES] Resolved via type "${typeName}", keys:`,
        Object.keys(obj)
      );
    }
    for (const key of CHRONICLE_NAME_KEYS) {
      const val = obj[key];
      if (val != null && String(val).trim()) return String(val).trim();
    }
    // Fallback: use first string-like value (excluding IDs, dates)
    for (const [key, val] of Object.entries(obj)) {
      if (SKIP_KEYS.has(key)) continue;
      if (val != null && typeof val === "string" && val.trim()) return val.trim();
    }
    if (debug) {
      console.debug(
        `[DEBUG_CHRONICLES] Type "${typeName}" returned object but no string name. Keys:`,
        Object.keys(obj)
      );
    }
  }
  return null;
}

/**
 * Get Tree node display name by ID.
 * Returns null if not found or no name.
 */
export async function getTreeNodeDisplayName(nodeId: string): Promise<string | null> {
  const node = await getById<Record<string, unknown>>(TYPE_TREE_NODE, nodeId);
  if (!node) return null;
  const name = (node.name ?? node.Name ?? "").toString().trim();
  return name || null;
}

const DEBUG_GROUPS = process.env.DEBUG_GROUPS === "1";
let _debugGroupsLogged = 0;

/** Possible Bubble field names for short/group code */
const GROUP_CODE_KEYS = [
  "code",
  "Code",
  "short_code",
  "Short Code",
  "Short code",
  "group_code",
  "Group Code",
  "Group code",
  "abbreviation",
  "Abbreviation",
];

/** Possible Bubble field names for short display name */
const SHORT_NAME_KEYS = ["short name", "Short name", "Short Name"];

export interface TreeNodeDisplayInfo {
  fullName: string | null;
  shortName: string | null;
  code: string | null;
}

/**
 * Get Tree node display name, short name, and optional code.
 * fullName = node["name"] or node["Name"]
 * shortName = node["short name"] if present, else fullName
 * code = from code/short_code/group_code etc. (for backward compatibility)
 */
export async function getTreeNodeWithCode(
  nodeId: string
): Promise<TreeNodeDisplayInfo> {
  const node = await getById<Record<string, unknown>>(TYPE_TREE_NODE, nodeId);
  if (!node) return { fullName: null, shortName: null, code: null };

  const fullName = (node.name ?? node.Name ?? "").toString().trim() || null;
  let shortName: string | null = null;
  for (const key of SHORT_NAME_KEYS) {
    const val = node[key];
    if (val != null && String(val).trim()) {
      shortName = String(val).trim();
      break;
    }
  }
  if (!shortName) shortName = fullName;

  let code: string | null = null;
  for (const key of GROUP_CODE_KEYS) {
    const val = node[key];
    if (val != null && String(val).trim()) {
      code = String(val).trim();
      break;
    }
  }

  if (DEBUG_GROUPS && _debugGroupsLogged < 10) {
    _debugGroupsLogged += 1;
    console.debug("[DEBUG_GROUPS] Tree node resolved:", {
      nodeId,
      fullName,
      shortName,
      code,
    });
  }

  return { fullName, shortName, code };
}

export interface GroupOption {
  id: string;
  name: string;
}

/**
 * Fetch group options for dropdown from upcoming calendar items.
 * Fetches items within next 120 days (cap 200), extracts unique NAIC Group ids,
 * resolves each to display name via getTreeNodeDisplayName (cached via getById).
 * Returns sorted [{ id, name }]. Cached 10 minutes.
 */
export async function getGroupNodesForDropdown(): Promise<GroupOption[]> {
  const cacheKey = "groups:upcoming";
  const cached = cacheGet<GroupOption[]>(cacheKey);
  if (cached) return cached;

  const today = new Date().toISOString().slice(0, 10);
  const endDate = new Date();
  endDate.setUTCDate(endDate.getUTCDate() + 120);
  const endDateStr = endDate.toISOString().slice(0, 10);

  const items = await getUpcomingCalendarItems({
    limit: 200,
    startDate: today,
    endDate: endDateStr,
  });

  const ids = new Set<string>();
  for (const item of items) {
    const naic = item["NAIC Group (tree node)"];
    const id =
      typeof naic === "string" && naic.trim()
        ? naic.trim()
        : naic != null && typeof naic === "object"
          ? ((naic as { _id?: string; id?: string })._id ??
            (naic as { _id?: string; id?: string }).id) as string | undefined
          : undefined;
    if (id) ids.add(id);
  }

  const pairs = await Promise.all(
    Array.from(ids).map(async (id) => {
      const name = await getTreeNodeDisplayName(id);
      return { id, name: name ?? id };
    })
  );

  const result = pairs.sort((a, b) => a.name.localeCompare(b.name));
  cacheSet(cacheKey, result, GROUPS_CACHE_TTL_MS);
  return result;
}

// --- Public API ---

export interface UpcomingCalendarFilters {
  limit?: number;
  startDate?: string;
  endDate?: string;
  groupId?: string;
}

/**
 * Returns upcoming calendar items sorted by date ascending.
 * Supports date range and group filtering via Bubble constraints.
 */
export async function getUpcomingCalendarItems(
  filters: UpcomingCalendarFilters = {}
): Promise<CalendarItem[]> {
  const limit = Math.min(Math.max(filters.limit ?? 7, 1), 200);
  const startDate = filters.startDate?.trim();
  const endDate = filters.endDate?.trim();
  const groupId = filters.groupId?.trim();

  const today = new Date().toISOString().slice(0, 10);
  const dateFrom = startDate || today;

  const cacheKey = `upcoming:${limit}:${dateFrom}:${endDate ?? ""}:${groupId ?? ""}`;
  const cached = cacheGet<CalendarItem[]>(cacheKey);
  if (cached) return cached;

  // Use day-before so "greater than" yields date >= dateFrom (Bubble has no "greater than or equal")
  const dMin = new Date(dateFrom + "T12:00:00Z");
  dMin.setUTCDate(dMin.getUTCDate() - 1);
  const dateFromExclusive = dMin.toISOString().slice(0, 10);

  const constraints: { key: string; constraint_type: string; value: string }[] = [
    { key: "date", constraint_type: "greater than", value: dateFromExclusive },
  ];
  if (endDate) {
    const d = new Date(endDate + "T12:00:00Z");
    d.setUTCDate(d.getUTCDate() + 1);
    const endExclusive = d.toISOString().slice(0, 10);
    constraints.push({
      key: "date",
      constraint_type: "less than",
      value: endExclusive,
    });
  }
  if (groupId) {
    constraints.push({
      key: "NAIC Group (tree node)",
      constraint_type: "equals",
      value: groupId,
    });
  }

  try {
    const resp = await search<Record<string, unknown>>(TYPE_CALENDAR_ITEM, {
      constraints,
      limit: 200,
      sort_field: "date",
      descending: false,
    });

    const raw = resp.response?.results ?? [];
    const items: CalendarItem[] = raw.map((r) => ({
      _id: String(r._id ?? r.id ?? ""),
      title: r.title as string | undefined,
      date: r.date as string | undefined,
      "NAIC Group (tree node)": r["NAIC Group (tree node)"] as
        | string
        | undefined,
      ...r,
    }));

    items.sort((a, b) => {
      const da = a.date ?? "";
      const db = b.date ?? "";
      return da.localeCompare(db);
    });

    const result = items.slice(0, limit);
    cacheSet(cacheKey, result);
    return result;
  } catch (err) {
    if (err instanceof BubbleAPIError) throw err;
    throw new BubbleAPIError(
      err instanceof Error ? err.message : String(err)
    );
  }
}

function toResource(r: Record<string, unknown>): Resource {
  return {
    _id: String(r._id ?? r.id ?? ""),
    Name: r.Name as string | undefined,
    URL: r.URL as string | undefined,
    Type1: r.Type1 as string | undefined,
    "topic suggestion": r["topic suggestion"] as string | undefined,
    "Related calendar items": r["Related calendar items"] as
      | string
      | string[]
      | undefined,
    date: r.date as string | undefined,
    ...r,
  };
}

/** Normalize "Related calendar items" to string[]: string→as-is, object→_id or id */
function normalizeRelatedIds(
  rel: string | string[] | Record<string, unknown>[] | undefined
): string[] {
  if (rel == null) return [];
  if (Array.isArray(rel)) {
    return rel
      .map((x) => {
        if (typeof x === "string" && x.trim()) return x.trim();
        if (x != null && typeof x === "object") {
          const id = (x as { _id?: string; id?: string })._id ??
            (x as { _id?: string; id?: string }).id;
          return id != null ? String(id).trim() : "";
        }
        return "";
      })
      .filter(Boolean);
  }
  if (typeof rel === "string" && rel.trim()) return [rel.trim()];
  return [];
}

function relatedContainsId(
  rel: string | string[] | undefined,
  id: string
): boolean {
  return normalizeRelatedIds(rel as string | string[] | undefined).includes(id);
}

/**
 * Returns resources linked to the given calendar item.
 * Tries server-side "contains" first; if 0 results or Bubble rejects, falls back to
 * fetching 200 resources and filtering client-side. Normalizes Related calendar items
 * (string vs object with _id).
 */
export async function getResourcesLinkedToCalendarItem(
  calendarItemId: string
): Promise<Resource[]> {
  if (!calendarItemId?.trim()) return [];

  const cacheKey = `resources-by-cal:${calendarItemId}`;
  const cached = cacheGet<Resource[]>(cacheKey);
  if (cached) return cached;

  let results: Resource[];

  try {
    const resp = await search<Record<string, unknown>>(TYPE_RESOURCE, {
      constraints: [
        {
          key: "Related calendar items",
          constraint_type: "contains",
          value: calendarItemId,
        },
      ],
      limit: 200,
    });
    results = (resp.response?.results ?? []).map(toResource);
    if (results.length === 0) {
      results = await fetchResourcesClientSideFallback(calendarItemId);
    }
  } catch (err) {
    const isConstraintError =
      err instanceof BubbleAPIError &&
      (err.statusCode === 400 || err.statusCode === 422);
    if (!isConstraintError) throw err;
    results = await fetchResourcesClientSideFallback(calendarItemId);
  }

  cacheSet(cacheKey, results);
  return results;
}

/**
 * Fetch a single Agenda Item by ID.
 * GET /obj/agendaitem/{id}
 * Uses in-memory cache (60s).
 */
export async function getAgendaItemById(
  id: string
): Promise<Record<string, unknown> | null> {
  if (!id?.trim()) return null;
  return getById<Record<string, unknown>>(TYPE_AGENDA_ITEM, id.trim());
}

/** Extract display label from Agenda Item. First non-empty of: NAIC Title, BA title, Description (truncate), Ref # */
export function getAgendaItemDisplayLabel(
  item: Record<string, unknown> | null
): string {
  if (!item) return "";
  const naicTitle = (item["NAIC Title"] ?? "").toString().trim();
  if (naicTitle) return naicTitle;
  const baTitle = (item["BA title"] ?? "").toString().trim();
  if (baTitle) return baTitle;
  const desc = (item["Description"] ?? "").toString().trim();
  if (desc) return desc.length > 80 ? desc.slice(0, 80) + "…" : desc;
  const ref = (item["Ref #"] ?? "").toString().trim();
  if (ref) return ref;
  return "";
}

/**
 * Extract Resource IDs from an Agenda Item (Library item, Resource, etc.).
 */
function extractResourceIdsFromAgendaItem(
  item: Record<string, unknown> | null
): string[] {
  if (!item) return [];
  const keys = [
    "Resources",
    "resources",
    "Library item",
    "library item",
    "Library Item",
    "Resource",
    "resource",
    "Library item (single)",
  ];
  for (const key of keys) {
    const val = item[key];
    if (val == null) continue;
    const ids = normalizeRelatedIds(
      val as string | string[] | Record<string, unknown>[] | undefined
    );
    if (ids.length > 0) return ids;
  }
  return [];
}

/**
 * Fetch resources linked to Agenda Items (e.g. via "Library item" field).
 * Agenda Items can reference Resources; this fetches those.
 */
export async function getResourcesFromAgendaItemIds(
  agendaItemIds: string[]
): Promise<Resource[]> {
  const resourceIds = new Set<string>();
  for (const id of agendaItemIds) {
    if (!id?.trim()) continue;
    const item = await getAgendaItemById(id);
    for (const rid of extractResourceIdsFromAgendaItem(item ?? null)) {
      resourceIds.add(rid);
    }
  }
  return getResourcesByIds(Array.from(resourceIds));
}

/**
 * Fetch a single Chronicle Link by ID.
 * Chronicle Links (from "Relevant Documents" on Calendar Item) may reference Resources.
 */
export async function getChronicleLinkById(
  id: string
): Promise<Record<string, unknown> | null> {
  if (!id?.trim()) return null;
  return getById<Record<string, unknown>>(TYPE_CHRONICLE_LINK, id.trim());
}

/** Extract Resource IDs from a Chronicle Link (Resource, Library item, Chronicle, etc.). */
function extractResourceIdsFromChronicleLink(
  item: Record<string, unknown> | null
): string[] {
  if (!item) return [];
  const keys = [
    "Resource",
    "resource",
    "Library item",
    "library item",
    "Library Item",
    "Chronicle",
    "chronicle",
    "Library item (single)",
  ];
  for (const key of keys) {
    const val = item[key];
    if (val == null) continue;
    const ids = normalizeRelatedIds(
      val as string | string[] | Record<string, unknown>[] | undefined
    );
    if (ids.length > 0) return ids;
  }
  return [];
}

/**
 * Fetch resources linked via Chronicle Links (from "Relevant Documents" on Calendar Item).
 */
export async function getResourcesFromChronicleLinkIds(
  chronicleLinkIds: string[]
): Promise<Resource[]> {
  const resourceIds = new Set<string>();
  for (const id of chronicleLinkIds) {
    if (!id?.trim()) continue;
    const item = await getChronicleLinkById(id);
    for (const rid of extractResourceIdsFromChronicleLink(item ?? null)) {
      resourceIds.add(rid);
    }
  }
  return getResourcesByIds(Array.from(resourceIds));
}

/**
 * Fetch Bubble Resource objects by IDs.
 * Returns resources in order of ids; skips 404s.
 */
export async function getResourcesByIds(ids: string[]): Promise<Resource[]> {
  const resources: Resource[] = [];
  for (const id of ids) {
    if (!id?.trim()) continue;
    const raw = await getById<Record<string, unknown>>(TYPE_RESOURCE, id.trim());
    if (raw) resources.push(toResource(raw));
  }
  return resources;
}

/**
 * Get resources linked to a calendar item via Agenda and attached agenda items.
 * The relationship is on the calendar item: it holds Resource IDs in Agenda and
 * "attached agenda items". Fetches each Resource by ID and returns deduplicated list.
 */
export async function getResourcesForCalendarItem(
  calendarItem: CalendarItem
): Promise<Resource[]> {
  const ids = [
    ...normalizeRelatedIds(
      (calendarItem.Agenda ?? calendarItem.agenda) as
        | string
        | string[]
        | Record<string, unknown>[]
        | undefined
    ),
    ...normalizeRelatedIds(
      (calendarItem["attached agenda items"] ??
        calendarItem["Attached agenda items"]) as
        | string
        | string[]
        | Record<string, unknown>[]
        | undefined
    ),
  ];
  const uniqueIds = Array.from(new Set(ids.filter(Boolean)));
  if (uniqueIds.length === 0) return [];

  const cacheKey = `resources-for-cal:${calendarItem._id}:${uniqueIds.join(",")}`;
  const cached = cacheGet<Resource[]>(cacheKey);
  if (cached) return cached;

  const resources: Resource[] = [];
  for (const id of uniqueIds) {
    const raw = await getById<Record<string, unknown>>(TYPE_RESOURCE, id);
    if (raw) resources.push(toResource(raw));
  }
  cacheSet(cacheKey, resources);
  return resources;
}

/** @deprecated Use getResourcesLinkedToCalendarItem */
export async function findResourcesByCalendarItemId(
  calendarItemId: string
): Promise<Resource[]> {
  return getResourcesLinkedToCalendarItem(calendarItemId);
}

async function fetchResourcesClientSideFallback(
  calendarItemId: string
): Promise<Resource[]> {
  let resp: BubbleSearchResponse<Record<string, unknown>>;
  try {
    resp = await search<Record<string, unknown>>(TYPE_RESOURCE, {
      limit: 200,
    });
  } catch {
    return [];
  }
  const raw = resp.response?.results ?? [];
  return raw
    .filter((r) => {
      const ids = normalizeRelatedIds(
        r["Related calendar items"] as
          | string
          | string[]
          | Record<string, unknown>[]
          | undefined
      );
      return ids.includes(calendarItemId);
    })
    .map(toResource);
}

async function getCalendarItemById(id: string): Promise<CalendarItem | null> {
  try {
    const resp = await search<Record<string, unknown>>(TYPE_CALENDAR_ITEM, {
      constraints: [{ key: "_id", constraint_type: "equals", value: id }],
      limit: 1,
    });
    const r = resp.response?.results?.[0];
    return r ? toCalendarItem(r) : null;
  } catch {
    return null;
  }
}

function toCalendarItem(r: Record<string, unknown>): CalendarItem {
  return {
    _id: String(r._id ?? r.id ?? ""),
    title: r.title as string | undefined,
    date: r.date as string | undefined,
    "NAIC Group (tree node)": r["NAIC Group (tree node)"] as
      | string
      | undefined,
    ...r,
  };
}

function parseDate(s: string): Date | null {
  try {
    const d = new Date(s.slice(0, 10));
    return isNaN(d.getTime()) ? null : d;
  } catch {
    return null;
  }
}

function formatDate(d: Date): string {
  return d.toISOString().slice(0, 10);
}

/**
 * Find resources by URL (dedupe helper).
 * Uses Bubble search equals on "URL".
 */
export async function findResourcesByUrl(url: string): Promise<Resource[]> {
  if (!url?.trim()) return [];

  const cacheKey = `resources-by-url:${url}`;
  const cached = cacheGet<Resource[]>(cacheKey);
  if (cached) return cached;

  try {
    const resp = await search<Record<string, unknown>>(TYPE_RESOURCE, {
      constraints: [
        { key: "URL", constraint_type: "equals", value: url.trim() },
      ],
      limit: 100,
    });
    const results = (resp.response?.results ?? []).map(toResource);
    cacheSet(cacheKey, results);
    return results;
  } catch (err) {
    if (err instanceof BubbleAPIError) throw err;
    throw new BubbleAPIError(
      err instanceof Error ? err.message : String(err)
    );
  }
}

/**
 * Fetch alert objects by IDs.
 * Returns { alertType, date } for each resolved alert.
 */
export interface AlertInfo {
  alertType: string;
  date: string;
}

export async function getAlertsByIds(ids: string[]): Promise<AlertInfo[]> {
  const alerts: AlertInfo[] = [];
  for (const id of ids) {
    if (!id?.trim()) continue;
    const raw = await getById<Record<string, unknown>>(TYPE_ALERT, id.trim());
    if (!raw) continue;
    const alertType = (raw["Alert type"] ?? raw["alert type"] ?? "").toString().trim();
    const date = (raw["date"] ?? raw["Date"] ?? "").toString().trim();
    if (alertType) alerts.push({ alertType, date });
  }
  return alerts;
}

/**
 * Clear in-memory cache (e.g. for tests or after writes elsewhere).
 */
export function clearBubbleCache(): void {
  cache.clear();
}
