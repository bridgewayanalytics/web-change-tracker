import { NextResponse } from "next/server";
import { S3Client, GetObjectCommand } from "@aws-sdk/client-s3";

export const dynamic = "force-dynamic";

const BUCKET = process.env.BUBBLE_ARTIFACT_BUCKET ?? "";
const KEY = "health/latest.json";

let s3: S3Client | null = null;
function getS3() {
  if (!s3) s3 = new S3Client({ region: process.env.AWS_REGION ?? "us-east-1" });
  return s3;
}

export interface HealthReport {
  run_id: string;
  run_timestamp: number;
  generated_at: string;
  duration_seconds: number;
  status: "green" | "yellow" | "red";
  flags: string[];
  summary: {
    targets_total: number;
    targets_fetched: number;
    targets_fetch_failed: number;
    targets_changed: number;
    playwright_tried: number;
    playwright_ok: number;
    playwright_failed: number;
    agents_called: number;
    agents_ok: number;
    pgvector_connected: number;
    real_alerts: number;
    doc_extractions: number;
    storage_ok: boolean | null;
  };
  playwright_errors?: { target_id: string; url: string; error: string }[];
  target_errors?: { target_id: string; error: string }[];
}

export async function GET() {
  if (!BUCKET) {
    return NextResponse.json({ report: null, error: "BUBBLE_ARTIFACT_BUCKET not set" });
  }
  try {
    const resp = await getS3().send(new GetObjectCommand({ Bucket: BUCKET, Key: KEY }));
    const body = await resp.Body!.transformToString("utf-8");
    const report: HealthReport = JSON.parse(body);
    return NextResponse.json({ report });
  } catch (err: unknown) {
    const code = (err as { name?: string }).name;
    if (code === "NoSuchKey" || code === "NotFound") {
      return NextResponse.json({ report: null });
    }
    console.error("Failed to load health/latest.json:", err);
    return NextResponse.json({ report: null, error: "Failed to load health report" });
  }
}
