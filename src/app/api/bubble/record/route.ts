import { NextRequest, NextResponse } from "next/server";
import { S3Client, GetObjectCommand } from "@aws-sdk/client-s3";

export const dynamic = "force-dynamic";

const BUCKET = process.env.BUBBLE_ARTIFACT_BUCKET ?? "web-change-tracker-prod-artifacts-815039343351";
const ALERTS_KEY = "alerts/alerts_table.jsonl";

const EIDARIX_VERSION = process.env.EIDARIX_VERSION ?? "test";
const SPACE_IDS: Record<string, string> = {
  test: "1768998437948x865417918648382000",
  live: "1770642377799x775210694699370900",
};
const SPACE_ID = SPACE_IDS[EIDARIX_VERSION] ?? SPACE_IDS.test;
const BUBBLE_API_BASE = "https://eidarix.bridgewayanalytics.com/api/1.1/obj";
// calendaritemtype requires versioned URL — returns 404 at the non-versioned base
const BUBBLE_API_VERSIONED = `https://eidarix.bridgewayanalytics.com/version-${EIDARIX_VERSION}/api/1.1/obj`;
const BUBBLE_API_KEY = process.env.BUBBLE_API_KEY ?? "0a951ec86c08a59e274411913ce6aec3";

// Fields to drop entirely from display
const DROP_FIELDS = new Set([
  "body", "Body", "space", "Space", "name for search", "Name for search",
  "Outlook Event ID", "Outlook last sync", "length", "Length",
  "slug", "Slug", "_id", "Created Date", "Modified Date", "Created By",
  "Modified By", "Space", "Outlook_event_id", "outlook_event_id",
]);

let s3: S3Client | null = null;
function getS3() {
  if (!s3) s3 = new S3Client({ region: process.env.AWS_REGION ?? "us-east-1" });
  return s3;
}

async function loadRows(): Promise<Record<string, unknown>[]> {
  const res = await getS3().send(new GetObjectCommand({ Bucket: BUCKET, Key: ALERTS_KEY }));
  const text = await res.Body!.transformToString("utf-8");
  return text.split("\n").flatMap(line => {
    const t = line.trim();
    if (!t) return [];
    try { return [JSON.parse(t) as Record<string, unknown>]; } catch { return []; }
  });
}

async function bubbleSearch(
  type: string,
  constraints: Record<string, unknown>[]
): Promise<Record<string, unknown>[]> {
  const params = new URLSearchParams({
    constraints: JSON.stringify(constraints),
    limit: "5",
  });
  const res = await fetch(`${BUBBLE_API_BASE}/${type}?${params}`, {
    headers: { Authorization: `Bearer ${BUBBLE_API_KEY}` },
    cache: "no-store",
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`Bubble API ${res.status}: ${body.slice(0, 200)}`);
  }
  const data = await res.json() as { response?: { results?: Record<string, unknown>[] } };
  return data.response?.results ?? [];
}

/** Resolve a single Bubble ID to its display name. Falls back to the ID string on failure. */
async function resolveId(type: string, id: string, ...nameFields: string[]): Promise<string> {
  if (!id || id.length < 10) return id; // not a real Bubble ID
  // calendaritemtype requires the versioned URL; all others work at the non-versioned base
  const base = type === "calendaritemtype" ? BUBBLE_API_VERSIONED : BUBBLE_API_BASE;
  try {
    const res = await fetch(`${base}/${type}/${id}`, {
      headers: { Authorization: `Bearer ${BUBBLE_API_KEY}` },
      cache: "no-store",
    });
    if (!res.ok) return id;
    const data = await res.json() as { response?: Record<string, unknown> };
    const r = data.response ?? {};
    for (const f of nameFields) {
      const v = r[f];
      if (v != null && String(v).trim()) return String(v).trim();
    }
    return id;
  } catch {
    return id;
  }
}

async function resolveIds(type: string, ids: string[], ...nameFields: string[]): Promise<string[]> {
  if (!ids.length) return [];
  return Promise.all(ids.map(id => resolveId(type, id, ...nameFields)));
}

function toStringIds(v: unknown): string[] {
  if (Array.isArray(v)) return (v as unknown[]).map(String).filter(s => s && s.length > 5);
  if (typeof v === "string" && v.length > 5) return [v];
  return [];
}

/** Build clean, human-readable fields for a calendaritem Bubble record. */
async function cleanCalendarRecord(
  raw: Record<string, unknown>
): Promise<Record<string, string | string[]>> {
  const typeId   = typeof raw.type === "string" ? raw.type : null;
  const orgIds   = toStringIds(raw.Orgs ?? raw.orgs ?? raw["organizations_list_custom_organization"]);
  const topicIds = toStringIds(raw["Topics - dt"] ?? raw["topics___dt_list_custom_newsreel_update"]);
  const agendaIds = toStringIds(raw["attached agenda items"] ?? raw["agenda_items_list_custom_agendaitem"]);

  const [typeName, orgNames, topicNames, agendaNames] = await Promise.all([
    typeId ? resolveId("calendaritemtype", typeId, "display", "Title", "Name") : Promise.resolve(null),
    resolveIds("organization", orgIds, "Name", "Title"),
    resolveIds("chronicletopic", topicIds, "Title", "Name"),
    resolveIds("agendaitem", agendaIds, "BA title", "Title", "title_text", "Name"),
  ]);

  const out: Record<string, string | string[]> = {};

  const title = raw.title ?? raw.Title;
  if (title != null) out["Title"] = String(title);

  if (typeName) out["Type"] = typeName;

  const start = raw.date ?? raw["date_date"] ?? raw["Start"];
  if (start != null) out["Start Date/Time"] = String(start);

  const end = raw["End time"] ?? raw["length_end_time_date"] ?? raw["End"];
  if (end != null) out["End Date/Time"] = String(end);

  const fullDay = raw["Full day"] ?? raw["full_day"];
  if (fullDay != null) out["Full Day"] = fullDay ? "Yes" : "No";

  if (orgNames.length) out["Organizations"] = orgNames.join(", ");

  const loc = raw.location ?? raw.location_url ?? raw["location_text"];
  if (loc != null && String(loc) !== "N/A") out["Location URL"] = String(loc);

  const callIn = raw.phone_number_and_access_code ?? raw["phone_number_and_access_code_text"];
  if (callIn != null && String(callIn) !== "N/A") out["Call-In"] = String(callIn);

  const tz = raw.timezone_code ?? raw["timezone_code_text"];
  if (tz != null) out["Timezone"] = String(tz);

  // Always include topics and agenda items (even empty)
  out["Chronicle Topics"] = topicNames;
  out["Linked Agenda Items"] = agendaNames;

  return out;
}

/** Build clean, human-readable fields for a libraryitem Bubble record. */
async function cleanLibraryRecord(
  raw: Record<string, unknown>
): Promise<Record<string, string | string[]>> {
  const typeId   = typeof raw.type === "string" ? raw.type : typeof raw["type_custom_libraryitemtype"] === "string" ? raw["type_custom_libraryitemtype"] as string : null;
  const orgIds   = toStringIds(raw["organizations_list_custom_organization"] ?? raw.Organizations ?? raw.orgs);
  const topicIds = toStringIds(raw["topics___dt_list_custom_newsreel_update"] ?? raw["Topics - dt"]);

  const [typeName, orgNames, topicNames] = await Promise.all([
    typeId ? resolveId("libraryitemtype", typeId, "display", "Title", "Name") : Promise.resolve(null),
    resolveIds("organization", orgIds, "Name", "Title"),
    resolveIds("chronicletopic", topicIds, "Title", "Name"),
  ]);

  const out: Record<string, string | string[]> = {};

  const name = raw.name_text ?? raw.Name ?? raw.name ?? raw.title_text ?? raw.title;
  if (name != null) out["Name"] = String(name);

  const url = raw.url_text ?? raw.Url ?? raw.url;
  if (url != null && String(url) !== "N/A") out["URL"] = String(url);

  const file = raw.file_name_text ?? raw["File name"] ?? raw.filename;
  if (file != null && String(file) !== "N/A") out["File"] = String(file);

  if (typeName) out["Type"] = typeName;

  if (orgNames.length) out["Organizations"] = orgNames.join(", ");

  const date = raw.date_date ?? raw.date ?? raw.Date;
  if (date != null) out["Date"] = String(date);

  const summary = raw.description_text ?? raw.description ?? raw.Summary;
  if (summary != null && String(summary) !== "N/A") out["Summary"] = String(summary);

  out["Chronicle Topics"] = topicNames;

  return out;
}

// GET /api/bubble/record?agent_call_id=xxx&object_type=calendaritem|libraryitem
export async function GET(request: NextRequest) {
  const agent_call_id = request.nextUrl.searchParams.get("agent_call_id");
  const object_type = request.nextUrl.searchParams.get("object_type");

  if (!agent_call_id || !object_type) {
    return NextResponse.json({ error: "agent_call_id and object_type required" }, { status: 400 });
  }

  try {
    const rows = await loadRows();
    const row = rows.find(r => r.agent_call_id === agent_call_id);
    if (!row) return NextResponse.json({ error: "Row not found" }, { status: 404 });

    const plan = row.bubble_action as Record<string, unknown> | null;
    if (!plan) return NextResponse.json({ found: false, fields: {} });

    const spaceConstraint = { key: "space", constraint_type: "equals", value: SPACE_ID };

    if (object_type === "calendaritem") {
      const ep = (plan.event_preview ?? {}) as Record<string, unknown>;
      const matchSearch = (ep.match_search ?? {}) as Record<string, string>;
      const date = matchSearch.date ?? "";
      if (!date) return NextResponse.json({ found: false, fields: {} });

      const constraints = [
        spaceConstraint,
        { key: "date", constraint_type: "greater than", value: `${date}T00:00:00.000Z` },
        { key: "date", constraint_type: "less than", value: `${date}T23:59:59.000Z` },
      ];
      const results = await bubbleSearch("calendaritem", constraints);
      if (!results.length) return NextResponse.json({ found: false, fields: {} });

      const record = results[0];
      const fields = await cleanCalendarRecord(record);
      return NextResponse.json({ found: true, id: record._id, fields });

    } else if (object_type === "libraryitem") {
      const lp = (plan.library_item_preview ?? {}) as Record<string, unknown>;
      const matchSearch = (lp.match_search ?? {}) as Record<string, string>;

      const constraints: Record<string, unknown>[] = [spaceConstraint];
      if (matchSearch.url) {
        constraints.push({ key: "url_text", constraint_type: "equals", value: matchSearch.url });
      } else if (matchSearch.title) {
        constraints.push({ key: "name_text", constraint_type: "equals", value: matchSearch.title });
      } else {
        return NextResponse.json({ found: false, fields: {} });
      }
      const results = await bubbleSearch("libraryitem", constraints);
      if (!results.length) return NextResponse.json({ found: false, fields: {} });

      const record = results[0];
      const fields = await cleanLibraryRecord(record);
      return NextResponse.json({ found: true, id: record._id, fields });
    }

    return NextResponse.json({ error: "Unknown object_type" }, { status: 400 });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[/api/bubble/record]", msg);
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
