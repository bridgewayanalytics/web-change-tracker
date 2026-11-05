import { NextRequest, NextResponse } from "next/server";
import { S3Client, DeleteObjectCommand } from "@aws-sdk/client-s3";
import { createHash } from "node:crypto";

export const dynamic = "force-dynamic";

const BUCKET = process.env.BUBBLE_ARTIFACT_BUCKET ?? "";

let s3: S3Client | null = null;
function getS3() {
  if (!s3) s3 = new S3Client({ region: process.env.AWS_REGION ?? "us-east-1" });
  return s3;
}

function rerunResultKey(run_id: string, target_id: string, library_item_url: string): string {
  const base = `alerts/reruns/${run_id}/${target_id}`;
  if (library_item_url) {
    const urlHash = createHash("sha256").update(library_item_url).digest("hex").slice(0, 16);
    return `${base}/doc_${urlHash}.json`;
  }
  return `${base}/result.json`;
}

export async function POST(request: NextRequest) {
  try {
    const body = await request.json();
    const { run_id, target_id, library_item_url } = body as { run_id?: string; target_id?: string; library_item_url?: string };

    if (!run_id || !target_id) {
      return NextResponse.json({ error: "run_id and target_id are required" }, { status: 400 });
    }

    const key = rerunResultKey(run_id, target_id, library_item_url ?? "");
    await getS3().send(new DeleteObjectCommand({ Bucket: BUCKET, Key: key }));

    return NextResponse.json({ ok: true });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[/api/doc-rerun/discard]", msg);
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
