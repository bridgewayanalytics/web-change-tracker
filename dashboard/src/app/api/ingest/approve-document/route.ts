import { NextRequest, NextResponse } from "next/server";
import { S3Client, GetObjectCommand, PutObjectCommand, HeadObjectCommand } from "@aws-sdk/client-s3";
import { getSignedUrl } from "@aws-sdk/s3-request-presigner";
import { createHash } from "crypto";
import { bustDocExtractionsCache } from "@/lib/doc-extractions-cache";
import { patchJsonlRows } from "@/lib/patch-jsonl";
import { getChatkitApiKey } from "@/lib/chatkit-api-key";

export const dynamic = "force-dynamic";

const BUCKET = process.env.BUBBLE_ARTIFACT_BUCKET ?? "";
const DOC_KEY = "alerts/document_extractions_table.jsonl";
const CHATKIT_API_URL =
  process.env.CHATKIT_API_URL ?? "https://chat-api.bridgewayanalytics.com";

let s3: S3Client | null = null;
function getS3() {
  if (!s3) s3 = new S3Client({ region: process.env.AWS_REGION ?? "us-east-1" });
  return s3;
}

// NAIC's CDN WAF returns 403 for python-requests / httpx / most browser UAs.
// Plain curl UA is one of the few that passes through — the ChatKit backend's
// Python httpx client gets blocked, which is why we proxy via S3.
const FETCH_UA = "curl/8.5.0";

// Download a remote document, cache it in S3, and return a presigned GET URL.
// Cache key: ingest-cache/<sha256(url)>/<filename>. Head-check first so re-approvals
// don't re-download. Throws with a descriptive error on any non-2xx so the route
// can surface it to the client as a 502.
async function proxyRemoteUrlToS3(url: string, filename: string): Promise<string> {
  const urlHash = createHash("sha256").update(url).digest("hex").slice(0, 16);
  // Agent titles can be long descriptive paragraphs; keep S3 key under the 1024-byte
  // limit and readable. Prefer the URL's own basename when the title is suspiciously long.
  const rawName = (filename || "").trim();
  const urlBase = url.split("?")[0].split("/").pop() || "document.pdf";
  const chosen = rawName && rawName.length <= 120 ? rawName : urlBase;
  const safeName = chosen.replace(/[^\w.-]+/g, "_").slice(0, 120) || "document.pdf";
  const key = `ingest-cache/${urlHash}/${safeName}`;

  // Skip re-download if we already have it
  let needUpload = true;
  try {
    await getS3().send(new HeadObjectCommand({ Bucket: BUCKET, Key: key }));
    needUpload = false;
  } catch { /* 404 — need to fetch */ }

  if (needUpload) {
    const resp = await fetch(url, { headers: { "User-Agent": FETCH_UA } });
    if (!resp.ok) {
      throw new Error(`Failed to fetch document URL (HTTP ${resp.status}): ${url}`);
    }
    const buf = Buffer.from(await resp.arrayBuffer());
    const contentType = resp.headers.get("content-type") || "application/octet-stream";
    await getS3().send(new PutObjectCommand({
      Bucket: BUCKET,
      Key: key,
      Body: buf,
      ContentType: contentType,
    }));
  }

  return await getSignedUrl(getS3(), new GetObjectCommand({ Bucket: BUCKET, Key: key }), { expiresIn: 1800 });
}

// POST /api/ingest/approve-document
// Allowed destination namespaces (per docs/integration/newsreel-ingest-api.md).
// Anything outside this set gets a 400 from the upstream API, so we gate it here too.
const ALLOWED_NAMESPACES = new Set([
  "newsreel-generation:ART",
  "naic-proceedings",
  "bridgeway-sharepoint",
  "art-state-system",
  "ba:chronicles",
  "ba:newsreels",
  "ba:NAIC Guidelines",
  "ba:International Guidelines",
  "ba:Rating Agencies",
]);
const DEFAULT_NAMESPACE = "newsreel-generation:ART";

// Body: { agent_call_id: string, library_item_url: string, namespace?: string }
// Calls ingest-document API, then patches the doc extraction row to "approved".
export async function POST(request: NextRequest) {
  try {
    const body = await request.json() as { agent_call_id?: string; library_item_url?: string; namespace?: string };
    const { agent_call_id, library_item_url } = body;
    const namespace = (body.namespace && body.namespace.trim()) || DEFAULT_NAMESPACE;
    if (!agent_call_id || !library_item_url) {
      return NextResponse.json({ error: "agent_call_id and library_item_url required" }, { status: 400 });
    }
    if (!ALLOWED_NAMESPACES.has(namespace)) {
      return NextResponse.json({ error: `unknown namespace: ${namespace}` }, { status: 400 });
    }

    // Find the row to get library_item_title and manual_doc_s3_key
    const resp = await getS3().send(new GetObjectCommand({ Bucket: BUCKET, Key: DOC_KEY }));
    const text = (await resp.Body?.transformToString()) ?? "";
    let library_item_title = "";
    let manual_doc_s3_key = "";
    for (const line of text.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      try {
        const row = JSON.parse(trimmed) as Record<string, unknown>;
        if (row.agent_call_id === agent_call_id) {
          library_item_title = String(row.library_item_title ?? "");
          manual_doc_s3_key = String(row.manual_doc_s3_key ?? "");
          break;
        }
      } catch { continue; }
    }

    const apiKey = await getChatkitApiKey();
    if (!apiKey) {
      return NextResponse.json({ error: "CHATKIT_INTERNAL_API_KEY not configured" }, { status: 500 });
    }

    // Resolve the URL to send the ingest API. ChatKit's httpx client gets 403 from
    // naic.org's WAF, so we download the file ourselves (with a UA naic.org accepts)
    // and stash it in S3, then hand ChatKit a presigned GET URL — ingest-cache keyed
    // by url hash so re-approvals skip the re-download.
    let ingestUrl: string;
    try {
      if (manual_doc_s3_key) {
        ingestUrl = await getSignedUrl(
          getS3(),
          new GetObjectCommand({ Bucket: BUCKET, Key: manual_doc_s3_key }),
          { expiresIn: 1800 },
        );
      } else {
        ingestUrl = await proxyRemoteUrlToS3(library_item_url, library_item_title);
      }
    } catch (fetchErr) {
      const msg = fetchErr instanceof Error ? fetchErr.message : String(fetchErr);
      console.error("[/api/ingest/approve-document] proxy fetch failed:", msg);
      return NextResponse.json({ error: msg }, { status: 502 });
    }

    const formData = new FormData();
    formData.append("namespace", namespace);
    formData.append("filename", library_item_title || library_item_url || manual_doc_s3_key);
    formData.append("url", ingestUrl);

    const ingestResp = await fetch(
      `${CHATKIT_API_URL}/internal/documents/ingest`,
      {
        method: "POST",
        headers: { "x-api-key": apiKey },
        body: formData,
      }
    );
    const ingestBody = await ingestResp.json().catch(() => ({})) as Record<string, unknown>;

    if (!ingestResp.ok) {
      console.error("[/api/ingest/approve-document] ingest API error", ingestResp.status, ingestBody);
      return NextResponse.json(
        {
          error: String(ingestBody.error ?? ingestBody.message ?? `Upstream ingest API returned ${ingestResp.status}`),
          upstream_status: ingestResp.status,
          upstream_body: ingestBody,
        },
        { status: 502 },
      );
    }

    await patchJsonlRows(
      getS3(), BUCKET, DOC_KEY,
      { agent_call_id, library_item_url },
      { ingest_status: "approved" }
    );
    bustDocExtractionsCache();

    return NextResponse.json({
      ok: true,
      status: ingestBody.status ?? "submitted",
      document_id: ingestBody.document_id,
    });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[/api/ingest/approve-document]", msg);
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
