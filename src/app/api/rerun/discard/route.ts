import { NextRequest, NextResponse } from "next/server";
import { S3Client, DeleteObjectCommand } from "@aws-sdk/client-s3";

export const dynamic = "force-dynamic";

const BUCKET = process.env.BUBBLE_ARTIFACT_BUCKET ?? "";

let s3: S3Client | null = null;
function getS3() {
  if (!s3) s3 = new S3Client({ region: process.env.AWS_REGION ?? "us-east-1" });
  return s3;
}

export async function POST(request: NextRequest) {
  try {
    const body = await request.json();
    const { run_id, target_id } = body as { run_id?: string; target_id?: string };

    if (!run_id || !target_id) {
      return NextResponse.json({ error: "run_id and target_id are required" }, { status: 400 });
    }

    const key = `alerts/reruns/${run_id}/${target_id}/result.json`;
    await getS3().send(new DeleteObjectCommand({ Bucket: BUCKET, Key: key }));

    return NextResponse.json({ ok: true });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[/api/rerun/discard]", msg);
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
