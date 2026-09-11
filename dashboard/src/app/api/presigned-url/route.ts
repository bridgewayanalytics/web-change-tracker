import { NextRequest, NextResponse } from "next/server";
import { S3Client, GetObjectCommand } from "@aws-sdk/client-s3";
import { getSignedUrl } from "@aws-sdk/s3-request-presigner";

export const dynamic = "force-dynamic";

const BUCKET = process.env.BUBBLE_ARTIFACT_BUCKET ?? "";
const RECORDINGS_BUCKET = "recordings-bucket-1";

let s3: S3Client | null = null;
function getS3() {
  if (!s3) s3 = new S3Client({ region: process.env.AWS_REGION ?? "us-east-1" });
  return s3;
}

// GET /api/presigned-url?key=<s3_key>
// Redirects to a presigned S3 GET URL (1-hour expiry).
// Used for recording and transcript download links.
export async function GET(request: NextRequest) {
  try {
    const key = request.nextUrl.searchParams.get("key");
    if (!key) return NextResponse.json({ error: "key required" }, { status: 400 });
    if (!BUCKET) return NextResponse.json({ error: "BUBBLE_ARTIFACT_BUCKET not configured" }, { status: 500 });

    const bucket = key.endsWith(".mp3") ? RECORDINGS_BUCKET : BUCKET;
    const url = await getSignedUrl(
      getS3(),
      new GetObjectCommand({ Bucket: bucket, Key: key }),
      { expiresIn: 3600 }
    );
    return NextResponse.redirect(url);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[/api/presigned-url]", msg);
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
