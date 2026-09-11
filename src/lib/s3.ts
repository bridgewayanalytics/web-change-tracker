/**
 * S3 client for reading bubble_reports artifacts.
 * Uses IAM task role for S3 read (no explicit credentials in code).
 * Schema inferred from observed JSON; no dependency on scraper codebase.
 */

import {
  GetObjectCommand,
  ListObjectsV2Command,
  S3Client,
} from "@aws-sdk/client-s3";
import { randomUUID } from "crypto";

// --- Env ---

const BUCKET = process.env.BUBBLE_ARTIFACT_BUCKET;
const KEY_LATEST =
  process.env.BUBBLE_REPORT_LATEST_KEY ?? "bubble_reports/latest.json";
const RUNS_PREFIX =
  process.env.BUBBLE_REPORT_RECENT_RUNS_PREFIX ?? "bubble_reports/runs/";
const RUNS_LIMIT = parseInt(
  process.env.BUBBLE_REPORT_RECENT_RUNS_LIMIT ?? "0",
  10
) || 0;

// --- Typed interfaces ---

export interface MeetingMeta {
  date_iso?: string;
  group_name?: string;
  times?: string;
  [key: string]: unknown;
}

export interface BubbleReportResource {
  Name?: string;
  URL?: string;
  "Related calendar items"?: string | string[] | Array<{ _id?: string; id?: string }>;
  __meeting_meta?: MeetingMeta;
  _id?: string;
  [key: string]: unknown;
}

/** Normalize "Related calendar items" to string[]: string→as-is, object→_id or id */
function normalizeRelatedIds(
  rel:
    | string
    | string[]
    | Array<{ _id?: string; id?: string }>
    | undefined
): string[] {
  if (rel == null) return [];
  if (Array.isArray(rel)) {
    return rel
      .map((x) => {
        if (typeof x === "string" && x.trim()) return x.trim();
        if (x != null && typeof x === "object") {
          const id = (x as { _id?: string; id?: string })._id ??
            (x as { _id?: string; id?: string }).id;
          return id != null ? String(id).trim() : "";
        }
        return "";
      })
      .filter(Boolean);
  }
  if (typeof rel === "string" && rel.trim()) return [rel.trim()];
  return [];
}

/**
 * Filter S3 resources linked to the given calendar item.
 * Includes resources whose "Related calendar items" contains calendarItemId.
 * Normalizes string vs object entries.
 */
export function getS3ResourcesLinkedToCalendarItem(
  calendarItemId: string,
  resources: BubbleReportResource[]
): BubbleReportResource[] {
  if (!calendarItemId?.trim()) return [];
  return resources.filter((r) => {
    const ids = normalizeRelatedIds(r["Related calendar items"]);
    return ids.includes(calendarItemId);
  });
}

export interface BubbleReportCalendarItem {
  _id?: string;
  title?: string;
  date?: string;
  [key: string]: unknown;
}

export interface BubbleReport {
  counts?: Record<string, number>;
  web_urls?: string[];
  resources?: BubbleReportResource[];
  calendar_items?: BubbleReportCalendarItem[];
  run_id?: string;
  image_tag?: string;
  bubble_mode?: string;
  dry_run_bubble?: boolean;
  targets_file?: string;
  [key: string]: unknown;
}

// --- Client ---

let client: S3Client | null = null;

function getClient(): S3Client {
  if (!client) {
    client = new S3Client({
      region: process.env.AWS_REGION ?? "us-east-1",
    });
  }
  return client;
}

function parseReport(body: string): BubbleReport | null {
  try {
    const raw = JSON.parse(body) as unknown;
    if (raw === null || typeof raw !== "object") return null;
    return raw as BubbleReport;
  } catch {
    return null;
  }
}

function warn(requestId: string, message: string, detail?: unknown): void {
  const detailStr = detail !== undefined ? ` ${JSON.stringify(detail)}` : "";
  console.warn(`[s3] [${requestId}] ${message}${detailStr}`);
}

/**
 * Fetch bubble_reports/latest.json from S3.
 * Returns null if bucket not configured, object missing, or parse error.
 */
export async function getLatestReport(): Promise<BubbleReport | null> {
  const requestId = randomUUID();

  if (!BUCKET?.trim()) {
    warn(requestId, "BUBBLE_ARTIFACT_BUCKET not configured");
    return null;
  }

  try {
    const s3 = getClient();
    const response = await s3.send(
      new GetObjectCommand({
        Bucket: BUCKET,
        Key: KEY_LATEST,
      })
    );
    const body = await response.Body?.transformToString();
    if (!body) {
      warn(requestId, "Empty response body", { Key: KEY_LATEST });
      return null;
    }
    const report = parseReport(body);
    if (!report) {
      warn(requestId, "Failed to parse report JSON", { Key: KEY_LATEST });
      return null;
    }
    return report;
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    warn(requestId, "S3 unavailable; skipping enrichment", {
      Key: KEY_LATEST,
      error: msg,
    });
    return null;
  }
}

/**
 * List objects under runs prefix, fetch most recent N reports.
 * Returns [] if bucket not configured, prefix empty, or fetch/parse errors.
 */
export async function getRecentReports(
  n: number
): Promise<BubbleReport[]> {
  const requestId = randomUUID();

  if (!BUCKET?.trim()) {
    warn(requestId, "BUBBLE_ARTIFACT_BUCKET not configured");
    return [];
  }

  if (n <= 0) return [];

  const effectiveN = RUNS_LIMIT > 0 ? Math.min(n, RUNS_LIMIT) : n;

  try {
    const s3 = getClient();
    // Layout: bubble_reports/runs/YYYY/MM/DD/<run_id>.json
    const listAllResp = await s3.send(
      new ListObjectsV2Command({
        Bucket: BUCKET,
        Prefix: RUNS_PREFIX,
      })
    );

    const contents = listAllResp.Contents ?? [];
    const jsonKeys = contents
      .filter((c) => c.Key?.endsWith(".json"))
      .map((c) => c.Key!)
      .sort((a, b) => (b > a ? 1 : -1));

    const toFetch = jsonKeys.slice(0, effectiveN);
    const reports: BubbleReport[] = [];

    for (const key of toFetch) {
      try {
        const objResp = await s3.send(
          new GetObjectCommand({ Bucket: BUCKET, Key: key })
        );
        const body = await objResp.Body?.transformToString();
        if (!body) {
          warn(requestId, "Empty body for run", { Key: key });
          continue;
        }
        const report = parseReport(body);
        if (report) reports.push(report);
        else warn(requestId, "Parse failed for run", { Key: key });
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        warn(requestId, "Failed to fetch run", { Key: key, error: msg });
      }
    }

    return reports;
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    warn(requestId, "S3 list/fetch failed", { Prefix: RUNS_PREFIX, error: msg });
    return [];
  }
}
