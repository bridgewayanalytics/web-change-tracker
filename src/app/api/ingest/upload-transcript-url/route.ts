import { NextRequest, NextResponse } from "next/server";
import { S3Client, PutObjectCommand } from "@aws-sdk/client-s3";
import { getSignedUrl } from "@aws-sdk/s3-request-presigner";

export const dynamic = "force-dynamic";

const BUCKET = process.env.BUBBLE_ARTIFACT_BUCKET ?? "";

let s3: S3Client | null = null;
function getS3() {
  if (!s3) s3 = new S3Client({ region: process.env.AWS_REGION ?? "us-east-1" });
  return s3;
}

// POST /api/ingest/upload-transcript-url
// Body: { agent_call_id: string, filename: string }
// Returns a presigned S3 PUT URL so the browser can upload a transcript file directly.
// After upload, caller should POST to /api/ingest/trigger-manual-chunk with the s3_key.
export async function POST(request: NextRequest) {
  try {
    const body = await request.json() as { agent_call_id?: string; filename?: string };
    const { agent_call_id, filename } = body;
    if (!agent_call_id) return NextResponse.json({ error: "agent_call_id required" }, { status: 400 });
    if (!BUCKET) return NextResponse.json({ error: "BUBBLE_ARTIFACT_BUCKET not configured" }, { status: 500 });

    const safeName = (filename ?? "transcript.txt").replace(/[^a-zA-Z0-9._-]/g, "_");
    const s3Key = `transcripts/manual/${agent_call_id}/${safeName}`;

    const uploadUrl = await getSignedUrl(
      getS3(),
      new PutObjectCommand({ Bucket: BUCKET, Key: s3Key, ContentType: "text/plain" }),
      { expiresIn: 900 }
    );

    return NextResponse.json({ upload_url: uploadUrl, s3_key: s3Key });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[/api/ingest/upload-transcript-url]", msg);
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
