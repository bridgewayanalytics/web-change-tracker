import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const EIDARIX_VERSION = process.env.EIDARIX_VERSION ?? "test";
const SPACE_IDS: Record<string, string> = {
  test: "1768998437948x865417918648382000",
  live: "1770642377799x775210694699370900",
};
// Reference data (orgs, chronicle topics) always lives in the content/test space.
// Type lookups (libraryitemtype, calendaritemtype) use the versioned URL and need no space constraint.
const CONTENT_SPACE_ID = SPACE_IDS.test;
const OBJ_BASE = "https://eidarix.bridgewayanalytics.com/api/1.1/obj";
const OBJ_VERSIONED = `https://eidarix.bridgewayanalytics.com/version-${EIDARIX_VERSION}/api/1.1/obj`;
const BUBBLE_API_KEY = process.env.BUBBLE_API_KEY ?? "0a951ec86c08a59e274411913ce6aec3";

type BubbleOption = { id: string; name: string };

interface CacheEntry { data: BubbleOption[]; expiresAt: number }
const cache = new Map<string, CacheEntry>();
const CACHE_TTL_MS = 10 * 60 * 1000; // 10 minutes

async function bubbleFetchAll(url: string, params: URLSearchParams): Promise<Record<string, unknown>[]> {
  const all: Record<string, unknown>[] = [];
  let cursor = 0;
  const limit = 100;
  for (;;) {
    const p = new URLSearchParams(params);
    p.set("limit", String(limit));
    p.set("cursor", String(cursor));
    const res = await fetch(`${url}?${p}`, {
      headers: { Authorization: `Bearer ${BUBBLE_API_KEY}` },
      cache: "no-store",
    });
    if (!res.ok) throw new Error(`Bubble GET ${url} ${res.status}`);
    const data = await res.json() as { response?: { results?: Record<string, unknown>[]; remaining?: number } };
    const batch = data.response?.results ?? [];
    all.push(...batch);
    if (!batch.length || (data.response?.remaining ?? 0) <= 0) break;
    cursor += limit;
  }
  return all;
}

async function fetchOrgs(): Promise<BubbleOption[]> {
  const params = new URLSearchParams({
    constraints: JSON.stringify([{ key: "space", constraint_type: "equals", value: CONTENT_SPACE_ID }]),
  });
  const rows = await bubbleFetchAll(`${OBJ_BASE}/organization`, params);
  return rows
    .map(r => ({ id: String(r._id ?? ""), name: String(r.Name ?? "").trim() }))
    .filter(o => o.id && o.name)
    .sort((a, b) => a.name.localeCompare(b.name));
}

async function fetchChronicleTopics(): Promise<BubbleOption[]> {
  const params = new URLSearchParams({
    constraints: JSON.stringify([{ key: "space", constraint_type: "equals", value: CONTENT_SPACE_ID }]),
  });
  const rows = await bubbleFetchAll(`${OBJ_BASE}/chronicletopic`, params);
  return rows
    .map(r => ({ id: String(r._id ?? ""), name: String(r.Title ?? "").trim() }))
    .filter(o => o.id && o.name)
    .sort((a, b) => a.name.localeCompare(b.name));
}

async function fetchLibraryItemTypes(): Promise<BubbleOption[]> {
  const params = new URLSearchParams();
  const rows = await bubbleFetchAll(`${OBJ_VERSIONED}/libraryitemtype`, params);
  return rows
    .map(r => ({ id: String(r._id ?? ""), name: String(r.display ?? r.Title ?? "").trim() }))
    .filter(o => o.id && o.name)
    .sort((a, b) => a.name.localeCompare(b.name));
}

async function fetchCalendarItemTypes(): Promise<BubbleOption[]> {
  const params = new URLSearchParams();
  const rows = await bubbleFetchAll(`${OBJ_VERSIONED}/calendaritemtype`, params);
  return rows
    .map(r => ({ id: String(r._id ?? ""), name: String(r.display ?? r.Title ?? "").trim() }))
    .filter(o => o.id && o.name)
    .sort((a, b) => a.name.localeCompare(b.name));
}

const FETCHERS: Record<string, () => Promise<BubbleOption[]>> = {
  organization:     fetchOrgs,
  chronicletopic:   fetchChronicleTopics,
  libraryitemtype:  fetchLibraryItemTypes,
  calendaritemtype: fetchCalendarItemTypes,
};

// GET /api/bubble/options?type=organization | chronicletopic | libraryitemtype | calendaritemtype
export async function GET(request: NextRequest) {
  const type = request.nextUrl.searchParams.get("type") ?? "";
  const fetcher = FETCHERS[type];
  if (!fetcher) {
    return NextResponse.json({ error: `unknown type: ${type}` }, { status: 400 });
  }

  const now = Date.now();
  const cached = cache.get(type);
  if (cached && cached.expiresAt > now) {
    return NextResponse.json({ options: cached.data });
  }

  try {
    const options = await fetcher();
    cache.set(type, { data: options, expiresAt: now + CACHE_TTL_MS });
    return NextResponse.json({ options });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
