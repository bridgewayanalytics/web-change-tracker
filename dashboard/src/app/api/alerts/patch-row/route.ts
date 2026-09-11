import { NextRequest, NextResponse } from "next/server";
import { S3Client } from "@aws-sdk/client-s3";
import { bustAlertsCache } from "@/lib/alerts-cache";
import { patchJsonlRows } from "@/lib/patch-jsonl";

export const dynamic = "force-dynamic";

const BUCKET = process.env.BUBBLE_ARTIFACT_BUCKET ?? "web-change-tracker-prod-artifacts-815039343351";
const ALERTS_KEY = "alerts/alerts_table.jsonl";

let s3: S3Client | null = null;
function getS3() {
  if (!s3) s3 = new S3Client({ region: process.env.AWS_REGION ?? "us-east-1" });
  return s3;
}

// PATCH /api/alerts/patch-row
// Body: { agent_call_id: string, fields: Record<string, unknown> }
// Patches arbitrary fields on alert rows matching agent_call_id.
export async function PATCH(request: NextRequest) {
  try {
    const body = await request.json() as { agent_call_id?: string; fields?: Record<string, unknown> };
    const { agent_call_id, fields } = body;

    if (!agent_call_id) {
      return NextResponse.json({ error: "agent_call_id required" }, { status: 400 });
    }
    if (!fields || typeof fields !== "object" || Array.isArray(fields)) {
      return NextResponse.json({ error: "fields object required" }, { status: 400 });
    }

    const patched = await patchJsonlRows(
      getS3(), BUCKET, ALERTS_KEY,
      { agent_call_id },
      fields,
    );

    bustAlertsCache();

    return NextResponse.json({ ok: true, patched });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[/api/alerts/patch-row PATCH]", msg);
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
