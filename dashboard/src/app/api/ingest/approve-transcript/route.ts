import { NextRequest, NextResponse } from "next/server";
import { S3Client, GetObjectCommand } from "@aws-sdk/client-s3";
import { getSignedUrl } from "@aws-sdk/s3-request-presigner";
import { bustAlertsCache } from "@/lib/alerts-cache";
import { patchJsonlRows } from "@/lib/patch-jsonl";
import { getChatkitApiKey } from "@/lib/chatkit-api-key";

export const dynamic = "force-dynamic";

const BUCKET = process.env.BUBBLE_ARTIFACT_BUCKET ?? "";
const ALERTS_KEY = "alerts/alerts_table.jsonl";
const CHATKIT_API_URL =
  process.env.CHATKIT_API_URL ?? "https://chat-api.bridgewayanalytics.com";

let s3: S3Client | null = null;
function getS3() {
  if (!s3) s3 = new S3Client({ region: process.env.AWS_REGION ?? "us-east-1" });
  return s3;
}

// POST /api/ingest/approve-transcript
// Body: { agent_call_id: string }
// Generates a presigned URL for the transcript txt file and submits it to the
// newsreel-generation knowledge base, then patches the alert row to "approved".
export async function POST(request: NextRequest) {
  try {
    const body = await request.json() as { agent_call_id?: string };
    const { agent_call_id } = body;
    if (!agent_call_id) {
      return NextResponse.json({ error: "agent_call_id required" }, { status: 400 });
    }

    // Find the row to get transcript_s3_key
    const resp = await getS3().send(new GetObjectCommand({ Bucket: BUCKET, Key: ALERTS_KEY }));
    const text = (await resp.Body?.transformToString()) ?? "";
    let transcript_s3_key: string | null = null;
    for (const line of text.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      try {
        const row = JSON.parse(trimmed) as Record<string, unknown>;
        if (row.agent_call_id === agent_call_id) {
          transcript_s3_key =
            (row.transcript_s3_key as string) ||
            (row.manual_transcript_s3_key as string) ||
            null;
          break;
        }
      } catch { continue; }
    }
    if (!transcript_s3_key) {
      return NextResponse.json({ error: "No transcript_s3_key found for this agent_call_id" }, { status: 404 });
    }

    // Generate presigned URL for the transcript txt file (1 hour expiry)
    const presignedUrl = await getSignedUrl(
      getS3(),
      new GetObjectCommand({ Bucket: BUCKET, Key: transcript_s3_key }),
      { expiresIn: 3600 }
    );

    const filename = transcript_s3_key.split("/").pop() || "transcript.txt";

    const apiKey = await getChatkitApiKey();
    if (!apiKey) {
      return NextResponse.json({ error: "CHATKIT_INTERNAL_API_KEY not configured" }, { status: 500 });
    }

    const ingestResp = await fetch(
      `${CHATKIT_API_URL}/internal/documents/ingest`,
      {
        method: "POST",
        headers: { "x-api-key": apiKey },
        body: new URLSearchParams({
          namespace: "newsreel-generation:ART",
          filename,
          url: presignedUrl,
        }),
      }
    );
    const ingestBody = await ingestResp.json().catch(() => ({})) as Record<string, unknown>;

    if (!ingestResp.ok) {
      console.error("[/api/ingest/approve-transcript] ingest API error", ingestResp.status, ingestBody);
    }

    await patchJsonlRows(getS3(), BUCKET, ALERTS_KEY, { agent_call_id }, { ingest_status: "approved" });
    bustAlertsCache();

    return NextResponse.json({
      ok: true,
      status: ingestBody.status ?? "submitted",
      document_id: ingestBody.document_id,
    });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[/api/ingest/approve-transcript]", msg);
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
