import { NextRequest, NextResponse } from "next/server";
import { S3Client, GetObjectCommand } from "@aws-sdk/client-s3";

export const dynamic = "force-dynamic";

const BUCKET = process.env.BUBBLE_ARTIFACT_BUCKET ?? "";
const ALERTS_KEY = "alerts/alerts_table.jsonl";

let s3: S3Client | null = null;
function getS3() {
  if (!s3) s3 = new S3Client({ region: process.env.AWS_REGION ?? "us-east-1" });
  return s3;
}

// GET /api/alerts/row-status?agent_call_id=<id>
// Returns transcript_chunks_s3_key and ingest_status for a single alert row.
// Used to poll for ECS manual-chunk task completion.
export async function GET(request: NextRequest) {
  const agent_call_id = request.nextUrl.searchParams.get("agent_call_id");
  if (!agent_call_id) return NextResponse.json({ error: "agent_call_id required" }, { status: 400 });

  try {
    const resp = await getS3().send(new GetObjectCommand({ Bucket: BUCKET, Key: ALERTS_KEY }));
    const text = (await resp.Body?.transformToString()) ?? "";
    for (const line of text.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      try {
        const row = JSON.parse(trimmed) as Record<string, unknown>;
        if (row.agent_call_id === agent_call_id) {
          return NextResponse.json({
            found: true,
            transcript_chunks_s3_key: row.transcript_chunks_s3_key ?? null,
            ingest_status: row.ingest_status ?? null,
          });
        }
      } catch { continue; }
    }
    return NextResponse.json({ found: false, transcript_chunks_s3_key: null, ingest_status: null });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
