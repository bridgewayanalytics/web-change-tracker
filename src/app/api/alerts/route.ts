import { NextRequest, NextResponse } from "next/server";
import { GetObjectCommand, S3Client } from "@aws-sdk/client-s3";
import {
  getAlertsCache,
  setAlertsCache,
  CACHE_TTL,
} from "@/lib/alerts-cache";

export const dynamic = "force-dynamic";

const BUCKET = process.env.BUBBLE_ARTIFACT_BUCKET;
const ALERTS_KEY = "alerts/alerts_table.jsonl";

let s3: S3Client | null = null;
function getS3(): S3Client {
  if (!s3) s3 = new S3Client({ region: process.env.AWS_REGION ?? "us-east-1" });
  return s3;
}

// Schema is dynamic — columns are derived from agent output at runtime.
// No hardcoded field list; any field output by the agent flows through automatically.
// No field transformation — raw agent output is passed through as-is.
export type AlertRow = Record<string, unknown>;

async function fetchAlerts(): Promise<AlertRow[]> {
  const cache = getAlertsCache();
  if (cache && Date.now() - cache.ts < CACHE_TTL) return cache.rows;

  if (!BUCKET) throw new Error("BUBBLE_ARTIFACT_BUCKET not configured");

  const resp = await getS3().send(
    new GetObjectCommand({ Bucket: BUCKET, Key: ALERTS_KEY })
  );
  const body = await resp.Body?.transformToString();
  if (!body) throw new Error("Empty JSONL response");

  const rows: AlertRow[] = [];
  for (const line of body.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    try {
      rows.push(JSON.parse(trimmed) as AlertRow);
    } catch {
      // skip malformed lines
    }
  }

  // Sort newest first
  rows.sort((a, b) => String(b.run_timestamp || "").localeCompare(String(a.run_timestamp || "")));

  setAlertsCache(rows);
  return rows;
}

export async function GET(request: NextRequest) {
  try {
    const { searchParams } = request.nextUrl;
    const targetId = searchParams.get("targetId") || "";
    const alertType = searchParams.get("alertType") || "";
    const startDate = searchParams.get("startDate") || "";
    const endDate = searchParams.get("endDate") || "";
    const q = searchParams.get("q") || "";

    let rows = await fetchAlerts();

    const str = (r: AlertRow, key: string) => String(r[key] || "");
    // alert_date_time is the current field name (new backend); alert_datetime_et was the previous name
    const getAlertDate = (r: AlertRow) =>
      str(r, "alert_date_time") || str(r, "alert_datetime_et") || str(r, "run_timestamp");

    if (targetId) {
      rows = rows.filter((r) => str(r, "target_id") === targetId);
    }
    if (alertType) {
      rows = rows.filter((r) => str(r, "alert_type") === alertType);
    }
    if (startDate) {
      rows = rows.filter((r) => getAlertDate(r).slice(0, 10) >= startDate);
    }
    if (endDate) {
      rows = rows.filter((r) => getAlertDate(r).slice(0, 10) <= endDate);
    }
    if (q) {
      const lower = q.toLowerCase();
      rows = rows.filter(
        (r) =>
          str(r, "alert_title").toLowerCase().includes(lower) ||
          str(r, "alert_description").toLowerCase().includes(lower) ||
          str(r, "organization").toLowerCase().includes(lower) ||
          str(r, "agent_call_id").toLowerCase().includes(lower) ||
          str(r, "run_id").toLowerCase().includes(lower)
      );
    }

    return NextResponse.json({ rows, total: rows.length, error: null });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[/api/alerts]", msg);
    return NextResponse.json(
      { rows: [], total: 0, error: msg },
      { status: 500 }
    );
  }
}
