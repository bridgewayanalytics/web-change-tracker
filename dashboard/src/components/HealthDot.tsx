import { S3Client, GetObjectCommand } from "@aws-sdk/client-s3";
import type { HealthReport } from "@/app/api/health/route";

const BUCKET = process.env.BUBBLE_ARTIFACT_BUCKET ?? "";
const KEY = "health/latest.json";

async function fetchHealthReport(): Promise<HealthReport | null> {
  if (!BUCKET) return null;
  try {
    const s3 = new S3Client({ region: process.env.AWS_REGION ?? "us-east-1" });
    const resp = await s3.send(new GetObjectCommand({ Bucket: BUCKET, Key: KEY }));
    const body = await resp.Body!.transformToString("utf-8");
    return JSON.parse(body) as HealthReport;
  } catch {
    return null;
  }
}

function ageMinutes(isoTimestamp: string): number {
  const ts = Date.parse(isoTimestamp);
  if (isNaN(ts)) return Infinity;
  return (Date.now() - ts) / 60_000;
}

export async function HealthDot() {
  const report = await fetchHealthReport();

  if (!report) {
    return (
      <span
        title="Health: unknown (no report found)"
        className="inline-block w-2.5 h-2.5 rounded-full bg-gray-300"
        aria-label="Pipeline health: unknown"
      />
    );
  }

  const ageMins = ageMinutes(report.generated_at);
  // If we haven't had a run in >9 hours, force red regardless of last status
  const stale = ageMins > 540;
  const effectiveStatus = stale ? "red" : report.status;

  const dotColor =
    effectiveStatus === "green"
      ? "bg-green-500"
      : effectiveStatus === "yellow"
      ? "bg-yellow-400"
      : "bg-red-500";

  const runDt = new Date(report.run_timestamp * 1000).toUTCString().replace(" GMT", " UTC");

  let tooltip = `Pipeline: ${effectiveStatus.toUpperCase()}`;
  if (stale) {
    tooltip += `\nNo run in >${Math.round(ageMins / 60)}h — last run ${runDt}`;
  } else {
    tooltip += `\nLast run: ${runDt}`;
    if (report.flags.length > 0) {
      tooltip += "\n\nIssues:\n" + report.flags.map((f, i) => `${i + 1}. ${f}`).join("\n");
    } else {
      const s = report.summary;
      tooltip += `\n${s.targets_fetched}/${s.targets_total} fetched · ${s.playwright_ok}/${s.playwright_tried} playwright OK · ${s.real_alerts} alerts`;
    }
  }

  return (
    <span
      title={tooltip}
      className={`inline-block w-2.5 h-2.5 rounded-full ${dotColor} cursor-help`}
      aria-label={`Pipeline health: ${effectiveStatus}`}
    />
  );
}
