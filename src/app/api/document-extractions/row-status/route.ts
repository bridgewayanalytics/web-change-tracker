import { NextRequest, NextResponse } from "next/server";
import { S3Client, GetObjectCommand } from "@aws-sdk/client-s3";

export const dynamic = "force-dynamic";

const BUCKET = process.env.BUBBLE_ARTIFACT_BUCKET ?? "";
const DOC_KEY = "alerts/document_extractions_table.jsonl";

let s3: S3Client | null = null;
function getS3() {
  if (!s3) s3 = new S3Client({ region: process.env.AWS_REGION ?? "us-east-1" });
  return s3;
}

// GET /api/document-extractions/row-status?agent_call_id=<id>
// Returns { found: bool, agent_call_id, ingest_status } for a single doc extraction row.
// Used to poll for ECS add-document task completion (shows shimmer row until agent finishes).
export async function GET(request: NextRequest) {
  const agent_call_id = request.nextUrl.searchParams.get("agent_call_id");
  if (!agent_call_id) return NextResponse.json({ error: "agent_call_id required" }, { status: 400 });

  try {
    const resp = await getS3().send(new GetObjectCommand({ Bucket: BUCKET, Key: DOC_KEY }));
    const text = (await resp.Body?.transformToString()) ?? "";
    for (const line of text.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      try {
        const row = JSON.parse(trimmed) as Record<string, unknown>;
        if (row.agent_call_id === agent_call_id) {
          // Only report found=true if the row has real extraction data beyond the stub
          // (stub rows created by add-document have run_id="manual" and no document-specific fields)
          const hasContent =
            row.run_id !== "manual" ||
            (typeof row.document_title === "string" && row.document_title.trim() !== "") ||
            (typeof row.document_type === "string" && row.document_type.trim() !== "") ||
            (typeof row.organization_or_publisher === "string" && row.organization_or_publisher.trim() !== "");
          return NextResponse.json({
            found: hasContent,
            agent_call_id: row.agent_call_id ?? null,
            ingest_status: row.ingest_status ?? null,
          });
        }
      } catch { continue; }
    }
    return NextResponse.json({ found: false, agent_call_id: null, ingest_status: null });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
