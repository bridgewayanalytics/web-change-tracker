import { NextRequest, NextResponse } from "next/server";
import { S3Client, PutObjectCommand } from "@aws-sdk/client-s3";
import { getSignedUrl } from "@aws-sdk/s3-request-presigner";
import { randomUUID } from "crypto";

export const dynamic = "force-dynamic";

const BUCKET = process.env.BUBBLE_ARTIFACT_BUCKET ?? "web-change-tracker-prod-artifacts-815039343351";

let s3: S3Client | null = null;
function getS3() {
  if (!s3) s3 = new S3Client({ region: process.env.AWS_REGION ?? "us-east-1" });
  return s3;
}

// POST /api/ingest/upload-document-url
// Body: { filename: string }
// Returns a presigned S3 PUT URL + the s3_key for the uploaded file.
export async function POST(request: NextRequest) {
  try {
    const { filename } = await request.json() as { filename?: string };
    if (!filename) return NextResponse.json({ error: "filename required" }, { status: 400 });

    const safeFilename = filename.replace(/[^a-zA-Z0-9._\-]/g, "_");
    const id = randomUUID();
    const s3Key = `documents/manual/${id}/${safeFilename}`;

    const url = await getSignedUrl(
      getS3(),
      new PutObjectCommand({ Bucket: BUCKET, Key: s3Key, ContentType: "application/pdf" }),
      { expiresIn: 900 },
    );

    return NextResponse.json({ upload_url: url, s3_key: s3Key });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[/api/ingest/upload-document-url]", msg);
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
