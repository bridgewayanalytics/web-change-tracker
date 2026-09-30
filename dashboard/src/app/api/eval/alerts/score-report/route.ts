import { NextResponse } from "next/server";
import { S3Client, GetObjectCommand } from "@aws-sdk/client-s3";

export const dynamic = "force-dynamic";

const BUCKET = process.env.BUBBLE_ARTIFACT_BUCKET ?? "";
const REPORT_KEY = "alerts/alert_eval_score_report.json";
const PREV_REPORT_KEY = "alerts/alert_eval_score_report_prev.json";

let s3: S3Client | null = null;
function getS3() {
  if (!s3) s3 = new S3Client({ region: process.env.AWS_REGION ?? "us-east-1" });
  return s3;
}

async function fetchReport(key: string): Promise<unknown | null> {
  try {
    const resp = await getS3().send(new GetObjectCommand({ Bucket: BUCKET, Key: key }));
    const body = await resp.Body?.transformToString();
    return body ? JSON.parse(body) : null;
  } catch {
    return null;
  }
}

export async function GET() {
  const [current, prev] = await Promise.all([
    fetchReport(REPORT_KEY),
    fetchReport(PREV_REPORT_KEY),
  ]);
  if (!current) {
    return NextResponse.json({ error: "No report available yet" }, { status: 404 });
  }
  return NextResponse.json({ current, prev });
}
