import { NextRequest, NextResponse } from "next/server";
import { GetObjectCommand, S3Client } from "@aws-sdk/client-s3";

export const dynamic = "force-dynamic";

const BUCKET = process.env.BUBBLE_ARTIFACT_BUCKET;

let s3: S3Client | null = null;
function getS3(): S3Client {
  if (!s3) s3 = new S3Client({ region: process.env.AWS_REGION ?? "us-east-1" });
  return s3;
}

export async function GET(request: NextRequest) {
  const key = request.nextUrl.searchParams.get("key") || "";

  if (!key.startsWith("pages/") || !key.endsWith(".html")) {
    return NextResponse.json(
      { error: "Invalid key: must start with pages/ and end with .html" },
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
    const body = await resp.Body?.transformToByteArray();
    if (!body) {
      return NextResponse.json({ error: "Empty response from S3" }, { status: 404 });
    }

    // Derive a readable filename from the key, e.g. "naic.newsroom_2026-04-15_before.html"
    const parts = key.replace("pages/", "").split("/");
    const targetId = parts[0] || "snapshot";
    const date = parts.slice(1, 4).join("-");
    const file = parts[parts.length - 1] || "snapshot.html";
    const filename = `${targetId}_${date}_${file}`;

    return new NextResponse(Buffer.from(body), {
      status: 200,
      headers: {
        "Content-Type": "text/html; charset=utf-8",
        "Content-Disposition": `attachment; filename="${filename}"`,
      },
    });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[/api/snapshot]", msg);
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
