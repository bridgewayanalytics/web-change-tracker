import { NextRequest, NextResponse } from "next/server";
import { GetObjectCommand, S3Client } from "@aws-sdk/client-s3";

export const dynamic = "force-dynamic";

const BUCKET = process.env.BUBBLE_ARTIFACT_BUCKET ?? "";

let s3: S3Client | null = null;
function getS3(): S3Client {
  if (!s3) s3 = new S3Client({ region: process.env.AWS_REGION ?? "us-east-1" });
  return s3;
}

export async function GET(request: NextRequest) {
  const key = request.nextUrl.searchParams.get("key") ?? "";

  if (!key.startsWith("alerts/contexts/")) {
    return NextResponse.json(
      { error: "Invalid key: must start with alerts/contexts/" },
      { status: 400 }
    );
  }

  if (!BUCKET) {
    return NextResponse.json(
      { error: "BUBBLE_ARTIFACT_BUCKET not configured" },
      { status: 500 }
    );
  }

  try {
    const resp = await getS3().send(
      new GetObjectCommand({ Bucket: BUCKET, Key: key })
    );
    const text = await resp.Body?.transformToString("utf-8");
    if (!text) {
      return NextResponse.json({ error: "Context not found" }, { status: 404 });
    }
    return new NextResponse(text, {
      status: 200,
      headers: { "Content-Type": "text/plain; charset=utf-8" },
    });
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    if (msg.includes("NoSuchKey") || msg.includes("404")) {
      return NextResponse.json({ error: "Context not stored for this row (only available for runs after this feature was deployed)" }, { status: 404 });
    }
    console.error("[/api/agent-context]", msg);
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
