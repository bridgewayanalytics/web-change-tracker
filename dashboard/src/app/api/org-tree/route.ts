import { NextResponse } from "next/server";
import { GetObjectCommand, S3Client } from "@aws-sdk/client-s3";

export const dynamic = "force-dynamic";

const BUCKET = process.env.BUBBLE_ARTIFACT_BUCKET ?? "";
const KEY = "alerts/contexts/org_tree.txt";

let s3: S3Client | null = null;
function getS3(): S3Client {
  if (!s3) s3 = new S3Client({ region: process.env.AWS_REGION ?? "us-east-1" });
  return s3;
}

export async function GET() {
  if (!BUCKET) {
    return NextResponse.json({ error: "BUBBLE_ARTIFACT_BUCKET not configured" }, { status: 500 });
  }

  try {
    const resp = await getS3().send(new GetObjectCommand({ Bucket: BUCKET, Key: KEY }));
    const text = await resp.Body?.transformToString("utf-8");
    if (!text) return NextResponse.json({ error: "Org tree not found" }, { status: 404 });
    return new NextResponse(text, {
      status: 200,
      headers: { "Content-Type": "text/plain; charset=utf-8" },
    });
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    if (msg.includes("NoSuchKey") || msg.includes("404")) {
      return NextResponse.json(
        { error: "Org tree not yet stored — it is written to S3 on each pipeline run." },
        { status: 404 }
      );
    }
    console.error("[/api/org-tree]", msg);
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
