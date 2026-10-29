import { NextRequest, NextResponse } from "next/server";
import { S3Client, GetObjectCommand } from "@aws-sdk/client-s3";
import { getSignedUrl } from "@aws-sdk/s3-request-presigner";
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

    // For file-upload rows, generate a presigned URL so ChatKit can download the file
    let ingestUrl = library_item_url;
    if (manual_doc_s3_key) {
      ingestUrl = await getSignedUrl(
        getS3(),
        new GetObjectCommand({ Bucket: BUCKET, Key: manual_doc_s3_key }),
        { expiresIn: 1800 },
      );
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
