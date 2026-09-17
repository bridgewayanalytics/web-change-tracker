import { NextResponse } from "next/server";
import { S3Client, GetObjectCommand } from "@aws-sdk/client-s3";

export const dynamic = "force-dynamic";

const BUCKET = process.env.BUBBLE_ARTIFACT_BUCKET ?? "";
const REPORT_KEY = "alerts/doc_eval_score_report.json";

let s3: S3Client | null = null;
function getS3() {
  if (!s3) s3 = new S3Client({ region: process.env.AWS_REGION ?? "us-east-1" });
  return s3;
}

export interface FieldStat {
  field: string;
  correct: number;
  partially_correct: number;
  incorrect: number;
  total: number;
  accuracy_pct: number;
}

export interface DocScoreReport {
  generated_at: string;
  triggered_by_run: string;
  total_rows: number;
  overall: {
    correct: number;
    partially_correct: number;
    incorrect: number;
    total_scores: number;
    accuracy_pct: number;
  };
  by_field: FieldStat[];
}

export async function GET() {
  if (!BUCKET) {
    return NextResponse.json({ report: null, error: "BUBBLE_ARTIFACT_BUCKET not set" });
  }
  try {
    const resp = await getS3().send(new GetObjectCommand({ Bucket: BUCKET, Key: REPORT_KEY }));
    const body = await resp.Body!.transformToString("utf-8");
    const report: DocScoreReport = JSON.parse(body);
    return NextResponse.json({ report });
  } catch (err: unknown) {
    const code = (err as { name?: string }).name;
    if (code === "NoSuchKey" || code === "NotFound") {
      return NextResponse.json({ report: null });
    }
    console.error("Failed to load doc_eval_score_report.json:", err);
    return NextResponse.json({ report: null, error: "Failed to load report" });
  }
}
