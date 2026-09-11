import { NextRequest, NextResponse } from "next/server";
import { GetObjectCommand, S3Client } from "@aws-sdk/client-s3";
import { getDocExtractionsCache, setDocExtractionsCache, CACHE_TTL, type DocExtractionRow } from "@/lib/doc-extractions-cache";

export const dynamic = "force-dynamic";

export type { DocExtractionRow };

const BUCKET = process.env.BUBBLE_ARTIFACT_BUCKET;
const KEY = "alerts/document_extractions_table.jsonl";

let s3: S3Client | null = null;
function getS3(): S3Client {
  if (!s3) s3 = new S3Client({ region: process.env.AWS_REGION ?? "us-east-1" });
  return s3;
}

async function fetchRows(): Promise<DocExtractionRow[]> {
  const cache = getDocExtractionsCache();
  if (cache && Date.now() - cache.ts < CACHE_TTL) return cache.rows;

  if (!BUCKET) throw new Error("BUBBLE_ARTIFACT_BUCKET not configured");

  const resp = await getS3().send(
    new GetObjectCommand({ Bucket: BUCKET, Key: KEY })
  );
  const body = await resp.Body?.transformToString();
  if (!body) throw new Error("Empty JSONL response");

  const rows: DocExtractionRow[] = [];
  for (const line of body.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    try {
      rows.push(JSON.parse(trimmed) as DocExtractionRow);
    } catch {
      // skip malformed lines
    }
  }

  // Sort newest first — raw timestamp string comparison works for ISO format
  rows.sort((a, b) =>
    String(b.run_timestamp ?? "").localeCompare(String(a.run_timestamp ?? ""))
  );

  setDocExtractionsCache(rows);
  return rows;
}

export async function GET(request: NextRequest) {
  try {
    const { searchParams } = request.nextUrl;
    const targetId = searchParams.get("targetId") ?? "";
    const startDate = searchParams.get("startDate") ?? "";
    const endDate = searchParams.get("endDate") ?? "";
    const q = searchParams.get("q") ?? "";

    const agentCallId = searchParams.get("agent_call_id") ?? "";

    let rows = await fetchRows();

    if (agentCallId) {
      rows = rows.filter((r) => String(r.agent_call_id ?? "") === agentCallId);
    }
    if (targetId) {
      rows = rows.filter((r) => String(r.target_id ?? "") === targetId);
    }
    if (startDate) {
      rows = rows.filter((r) => String(r.run_timestamp ?? "").slice(0, 10) >= startDate);
    }
    if (endDate) {
      rows = rows.filter((r) => String(r.run_timestamp ?? "").slice(0, 10) <= endDate);
    }
    if (q) {
      const lower = q.toLowerCase();
      rows = rows.filter((r) =>
        Object.values(r).some((v) =>
          typeof v === "string"
            ? v.toLowerCase().includes(lower)
            : JSON.stringify(v).toLowerCase().includes(lower)
        )
      );
    }

    return NextResponse.json({ rows, total: rows.length, error: null });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[/api/document-extractions]", msg);
    return NextResponse.json(
      { rows: [], total: 0, error: msg },
      { status: 500 }
    );
  }
}
