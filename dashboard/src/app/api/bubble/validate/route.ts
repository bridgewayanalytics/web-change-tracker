import { NextRequest, NextResponse } from "next/server";
import { S3Client, GetObjectCommand } from "@aws-sdk/client-s3";
import { validateSyncPlan } from "@/lib/bubble-sync-eidarix";

export const dynamic = "force-dynamic";
export const maxDuration = 30;

const BUCKET = process.env.BUBBLE_ARTIFACT_BUCKET ?? "web-change-tracker-prod-artifacts-815039343351";
const ALERTS_KEY = "alerts/alerts_table.jsonl";
const REGION = process.env.AWS_REGION ?? "us-east-1";

let s3: S3Client | null = null;
function getS3() {
  if (!s3) s3 = new S3Client({ region: REGION });
  return s3;
}

async function loadRows(): Promise<Record<string, unknown>[]> {
  const res = await getS3().send(new GetObjectCommand({ Bucket: BUCKET, Key: ALERTS_KEY }));
  const text = await res.Body!.transformToString("utf-8");
  return text.split("\n").flatMap(line => {
    const t = line.trim();
    if (!t) return [];
    try { return [JSON.parse(t) as Record<string, unknown>]; } catch { return []; }
  });
}

export async function GET(req: NextRequest) {
  const agentCallId = req.nextUrl.searchParams.get("agent_call_id");
  if (!agentCallId) {
    return NextResponse.json({ error: "agent_call_id required" }, { status: 400 });
  }
  try {
    const rows = await loadRows();
    const result = await validateSyncPlan(agentCallId, rows);
    return NextResponse.json(result);
  } catch (err) {
    console.error("[bubble/validate] error:", err);
    return NextResponse.json(
      { error: err instanceof Error ? err.message : String(err) },
      { status: 500 },
    );
  }
}
