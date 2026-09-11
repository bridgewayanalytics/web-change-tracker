import { NextRequest, NextResponse } from "next/server";
import { S3Client, GetObjectCommand } from "@aws-sdk/client-s3";

export const dynamic = "force-dynamic";

const BUCKET = process.env.CHANGELOG_BUCKET ?? "web-change-tracker-prod-artifacts-815039343351";

let s3: S3Client | null = null;
function getS3() {
  if (!s3) s3 = new S3Client({ region: process.env.AWS_REGION ?? "us-east-1" });
  return s3;
}

interface Chunk {
  chunk_index?: number;
  event_title?: string;
  event_start_date_time?: string;
  event_duration?: string;
  event_url?: string;
  organization?: string | string[];
  alert_type?: string;
  alert_title?: string;
  alert_description?: string;
  alert_url?: string;
  library_item_title?: string;
  library_item_url?: string;
  agenda_item_title?: string;
  agenda_item_status?: string;
  agenda_item_official_title?: string;
  agenda_item_standardized_id?: string;
  agenda_item_official_id?: string;
  chronicle_topics?: string[];
  text?: string;
}

function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function fmt(val: unknown): string {
  if (!val) return "";
  if (Array.isArray(val)) return val.filter(Boolean).join(", ");
  return String(val);
}

function isNA(val: unknown): boolean {
  if (!val) return true;
  const s = String(val).trim();
  return s === "" || s === "N/A" || s === "N/A." || s === "-";
}

function fmtDate(iso: string): string {
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric", timeZone: "UTC" });
  } catch { return iso; }
}

function buildHtml(chunks: Chunk[], s3Key: string): string {
  const first = chunks[0] ?? {};
  const eventTitle = fmt(first.event_title);
  const eventDate = first.event_start_date_time && !isNA(first.event_start_date_time)
    ? fmtDate(first.event_start_date_time) : "";
  const org = fmt(first.organization);
  const duration = !isNA(first.event_duration) ? fmt(first.event_duration) : "";
  const eventUrl = !isNA(first.event_url) ? fmt(first.event_url) : "";
  const alertUrl = !isNA(first.alert_url) ? fmt(first.alert_url) : "";
  const alertDescription = !isNA(first.alert_description) ? fmt(first.alert_description) : "";
  const libTitle = !isNA(first.library_item_title) ? fmt(first.library_item_title) : "";
  const libUrl = !isNA(first.library_item_url) ? fmt(first.library_item_url) : "";
  const fileName = s3Key.split("/").pop() ?? s3Key;

  // Group chunks by agenda item for the sidebar nav
  const agendaItems: string[] = [];
  const seen = new Set<string>();
  for (const c of chunks) {
    const title = c.agenda_item_title || "General Discussion";
    if (!seen.has(title)) { seen.add(title); agendaItems.push(title); }
  }

  const navLinks = agendaItems.map((title, i) =>
    `<a href="#agenda-${i}" class="nav-link">${esc(title)}</a>`
  ).join("\n");

  // Render chunks grouped under agenda headers
  const chunksByAgenda = new Map<string, Chunk[]>();
  for (const c of chunks) {
    const key = c.agenda_item_title || "General Discussion";
    if (!chunksByAgenda.has(key)) chunksByAgenda.set(key, []);
    chunksByAgenda.get(key)!.push(c);
  }

  let bodyHtml = "";
  let agendaIdx = 0;
  for (const [agendaTitle, agendaChunks] of Array.from(chunksByAgenda.entries())) {
    const first = agendaChunks[0];
    const officialTitle = !isNA(first.agenda_item_official_title) ? first.agenda_item_official_title! : "";
    const stdId = !isNA(first.agenda_item_standardized_id) ? first.agenda_item_standardized_id! : "";
    const offId = !isNA(first.agenda_item_official_id) ? first.agenda_item_official_id! : "";
    const status = !isNA(first.agenda_item_status) ? first.agenda_item_status! : "";
    const topics = (first.chronicle_topics ?? []).filter((t: string) => t && !isNA(t));

    bodyHtml += `<section id="agenda-${agendaIdx}" class="agenda-section">`;
    bodyHtml += `<h2 class="agenda-title">${esc(agendaTitle)}`;
    if (status && status !== "N/A") bodyHtml += ` <span class="status-badge">${esc(status)}</span>`;
    bodyHtml += `</h2>`;

    if (officialTitle) bodyHtml += `<div class="meta-row"><span class="meta-label">Official Title</span><span>${esc(officialTitle)}</span></div>`;
    if (stdId) bodyHtml += `<div class="meta-row"><span class="meta-label">Standardized ID</span><code>${esc(stdId)}</code></div>`;
    if (offId) bodyHtml += `<div class="meta-row"><span class="meta-label">Official ID</span><code>${esc(offId)}</code></div>`;
    if (topics.length > 0) {
      bodyHtml += `<div class="meta-row"><span class="meta-label">Topics</span><span class="topics">${topics.map((t: string) => `<span class="topic-tag">${esc(t)}</span>`).join("")}</span></div>`;
    }

    for (const chunk of agendaChunks) {
      const text = (chunk.text ?? "").trim();
      if (!text) continue;
      bodyHtml += `<div class="chunk-text">${esc(text).replace(/\n/g, "<br>")}</div>`;
    }

    bodyHtml += `</section>`;
    agendaIdx++;
  }

  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${esc(eventTitle || fileName)}</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, -apple-system, sans-serif; font-size: 14px; color: #1a1a1a; background: #f8f9fa; display: flex; min-height: 100vh; }

  /* Sidebar */
  nav { width: 260px; min-width: 260px; background: #fff; border-right: 1px solid #e5e7eb; padding: 20px 0; position: sticky; top: 0; height: 100vh; overflow-y: auto; flex-shrink: 0; }
  .nav-header { padding: 0 16px 16px; border-bottom: 1px solid #e5e7eb; margin-bottom: 8px; }
  .nav-header h1 { font-size: 13px; font-weight: 700; color: #111; line-height: 1.4; }
  .nav-header .event-date { font-size: 11px; color: #6b7280; margin-top: 4px; }
  .nav-header .event-org { font-size: 11px; color: #6b7280; }
  .nav-label { padding: 4px 16px; font-size: 10px; font-weight: 600; text-transform: uppercase; color: #9ca3af; letter-spacing: 0.05em; }
  .nav-link { display: block; padding: 6px 16px; font-size: 12px; color: #374151; text-decoration: none; line-height: 1.4; border-left: 2px solid transparent; }
  .nav-link:hover { background: #f3f4f6; border-left-color: #6366f1; color: #111; }

  /* Main content */
  main { flex: 1; padding: 32px 40px; max-width: 900px; }
  .page-header { margin-bottom: 32px; padding-bottom: 20px; border-bottom: 2px solid #e5e7eb; }
  .page-header h1 { font-size: 20px; font-weight: 700; color: #111; }
  .page-header .subtitle { font-size: 13px; color: #6b7280; margin-top: 6px; }
  .page-header .file-ref { font-size: 11px; color: #9ca3af; font-family: monospace; margin-top: 4px; }
  .header-meta { margin-top: 14px; display: flex; flex-direction: column; gap: 6px; }
  .header-meta-row { display: flex; gap: 10px; font-size: 12px; align-items: flex-start; }
  .header-meta-label { color: #9ca3af; font-weight: 600; min-width: 110px; flex-shrink: 0; font-size: 11px; padding-top: 1px; }
  .header-meta-value { color: #374151; line-height: 1.5; }
  .header-meta-value a { color: #2563eb; text-decoration: none; }
  .header-meta-value a:hover { text-decoration: underline; }
  .alert-desc { margin-top: 10px; font-size: 12px; color: #4b5563; background: #f9fafb; border: 1px solid #e5e7eb; border-radius: 6px; padding: 10px 14px; line-height: 1.6; }

  /* Agenda sections */
  .agenda-section { margin-bottom: 48px; }
  .agenda-title { font-size: 16px; font-weight: 700; color: #111; margin-bottom: 10px; display: flex; align-items: center; gap: 8px; }
  .status-badge { font-size: 10px; font-weight: 600; padding: 2px 6px; border-radius: 9999px; background: #eff6ff; color: #1d4ed8; border: 1px solid #bfdbfe; }

  /* Metadata rows */
  .meta-row { display: flex; gap: 10px; font-size: 12px; margin-bottom: 6px; align-items: flex-start; }
  .meta-label { color: #9ca3af; font-weight: 600; min-width: 120px; flex-shrink: 0; font-size: 11px; padding-top: 1px; }
  .meta-row code { background: #f3f4f6; border: 1px solid #e5e7eb; border-radius: 4px; padding: 1px 6px; font-size: 11px; color: #374151; }
  .topics { display: flex; flex-wrap: wrap; gap: 4px; }
  .topic-tag { background: #f3e8ff; color: #7c3aed; border: 1px solid #e9d5ff; border-radius: 9999px; padding: 1px 8px; font-size: 11px; }

  /* Chunk text */
  .chunk-text { margin-top: 14px; line-height: 1.7; color: #374151; background: #fff; border: 1px solid #e5e7eb; border-radius: 8px; padding: 16px 20px; font-size: 13px; white-space: pre-wrap; word-break: break-word; }
</style>
</head>
<body>
<nav>
  <div class="nav-header">
    <h1>${esc(eventTitle || "Transcript Chunks")}</h1>
    ${eventDate ? `<div class="event-date">${esc(eventDate)}</div>` : ""}
    ${org ? `<div class="event-org">${esc(org)}</div>` : ""}
  </div>
  <div class="nav-label">Agenda Items</div>
  ${navLinks}
</nav>
<main>
  <div class="page-header">
    <h1>${esc(eventTitle || "Transcript Chunks")}</h1>
    ${eventDate || duration ? `<div class="subtitle">${[eventDate, duration ? `${esc(duration)}` : ""].filter(Boolean).join(" · ")}</div>` : ""}
    ${org ? `<div class="subtitle" style="margin-top:2px">${esc(org)}</div>` : ""}
    <div class="file-ref">${esc(fileName)}</div>
    <div class="header-meta">
      ${eventUrl ? `<div class="header-meta-row"><span class="header-meta-label">Meeting Link</span><span class="header-meta-value"><a href="${esc(eventUrl)}" target="_blank" rel="noopener">Join / View Recording</a></span></div>` : ""}
      ${alertUrl ? `<div class="header-meta-row"><span class="header-meta-label">Source Page</span><span class="header-meta-value"><a href="${esc(alertUrl)}" target="_blank" rel="noopener">${esc(alertUrl)}</a></span></div>` : ""}
      ${libTitle ? `<div class="header-meta-row"><span class="header-meta-label">Document</span><span class="header-meta-value">${libUrl ? `<a href="${esc(libUrl)}" target="_blank" rel="noopener">${esc(libTitle)}</a>` : esc(libTitle)}</span></div>` : ""}
    </div>
    ${alertDescription ? `<div class="alert-desc">${esc(alertDescription)}</div>` : ""}
  </div>
  ${bodyHtml}
</main>
</body>
</html>`;
}

// GET /api/chunks?key=<s3_key>
export async function GET(request: NextRequest) {
  const key = request.nextUrl.searchParams.get("key");
  if (!key) {
    return new NextResponse("Missing key parameter", { status: 400 });
  }

  try {
    const resp = await getS3().send(new GetObjectCommand({ Bucket: BUCKET, Key: key }));
    const body = await resp.Body?.transformToString("utf-8");
    if (!body) return new NextResponse("Empty file", { status: 404 });

    const chunks: Chunk[] = [];
    for (const line of body.split("\n")) {
      const t = line.trim();
      if (!t) continue;
      try { chunks.push(JSON.parse(t)); } catch { /* skip malformed */ }
    }

    if (chunks.length === 0) {
      return new NextResponse("No chunks found in file", { status: 404 });
    }

    const html = buildHtml(chunks, key);
    return new NextResponse(html, {
      headers: { "Content-Type": "text/html; charset=utf-8" },
    });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[/api/chunks]", msg);
    return new NextResponse(`Error loading chunks: ${msg}`, { status: 500 });
  }
}
