import { NextRequest, NextResponse } from "next/server";
import { S3Client } from "@aws-sdk/client-s3";
import { bustAlertsCache } from "@/lib/alerts-cache";
import { bustDocExtractionsCache } from "@/lib/doc-extractions-cache";
import { patchJsonlRows } from "@/lib/patch-jsonl";

export const dynamic = "force-dynamic";

const BUCKET = process.env.BUBBLE_ARTIFACT_BUCKET ?? "";
const ALERTS_KEY = "alerts/alerts_table.jsonl";
const DOC_KEY = "alerts/document_extractions_table.jsonl";

let s3: S3Client | null = null;
function getS3() {
  if (!s3) s3 = new S3Client({ region: process.env.AWS_REGION ?? "us-east-1" });
  return s3;
}

// POST /api/ingest/reject
// Body: { table: "alerts" | "docs", agent_call_id: string, library_item_url?: string }
// Patches ingest_status to "rejected".
export async function POST(request: NextRequest) {
  try {
    const body = await request.json() as {
      table?: string;
      agent_call_id?: string;
      library_item_url?: string;
    };
    const { table, agent_call_id, library_item_url } = body;
    if (!table || !agent_call_id) {
      return NextResponse.json({ error: "table and agent_call_id required" }, { status: 400 });
    }

    const key = table === "alerts" ? ALERTS_KEY : DOC_KEY;
    const matchFields: Record<string, unknown> = { agent_call_id };
    if (library_item_url) matchFields.library_item_url = library_item_url;

    const patched = await patchJsonlRows(getS3(), BUCKET, key, matchFields, { ingest_status: "rejected" });

    if (table === "alerts") bustAlertsCache();
    else bustDocExtractionsCache();

    return NextResponse.json({ ok: true, patched });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[/api/ingest/reject]", msg);
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
