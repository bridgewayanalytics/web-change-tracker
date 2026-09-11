import { NextRequest, NextResponse } from "next/server";
import { S3Client, GetObjectCommand, PutObjectCommand } from "@aws-sdk/client-s3";
import { LambdaClient, InvokeCommand } from "@aws-sdk/client-lambda";

export const dynamic = "force-dynamic";
export const maxDuration = 120;

const BUCKET = process.env.BUBBLE_ARTIFACT_BUCKET ?? "web-change-tracker-prod-artifacts-815039343351";
const ALERTS_KEY = "alerts/alerts_table.jsonl";
const REGION = process.env.AWS_REGION ?? "us-east-1";

const EIDARIX_VERSION = process.env.EIDARIX_VERSION ?? "test";
const SPACE_IDS: Record<string, string> = {
  test: "1768998437948x865417918648382000",
  live: "1770642377799x775210694699370900",
};
const SPACE_ID = SPACE_IDS[EIDARIX_VERSION] ?? SPACE_IDS.test;
const BUBBLE_API_BASE = "https://eidarix.bridgewayanalytics.com/api/1.1/obj";
const BUBBLE_API_KEY = process.env.BUBBLE_API_KEY ?? "0a951ec86c08a59e274411913ce6aec3";
const BUBBLE_SYNC_LAMBDA = "web-change-tracker-prod-bubble-sync";

let s3: S3Client | null = null;
function getS3() {
  if (!s3) s3 = new S3Client({ region: REGION });
  return s3;
}

let lambdaClient: LambdaClient | null = null;
function getLambda() {
  if (!lambdaClient) lambdaClient = new LambdaClient({ region: REGION });
  return lambdaClient;
}

async function findMissingOrgs(orgNames: string[]): Promise<string[]> {
  if (!orgNames.length) return [];
  try {
    const params = new URLSearchParams({
      constraints: JSON.stringify([{ key: "space", constraint_type: "equals", value: SPACE_ID }]),
      limit: "200",
    });
    const res = await fetch(`${BUBBLE_API_BASE}/organization?${params}`, {
      headers: { Authorization: `Bearer ${BUBBLE_API_KEY}` },
      cache: "no-store",
      signal: AbortSignal.timeout(8000),
    });
    if (!res.ok) {
      console.warn("[bubble/sync/validate] org lookup failed status=%d — skipping pre-validation", res.status);
      return [];
    }
    const data = await res.json() as { response?: { results?: Array<Record<string, unknown>> } };
    const results = data.response?.results ?? [];
    const known = new Set(results.map(o => ((o.Name as string) ?? "").trim()));
    console.info("[bubble/sync/validate] org lookup: %d orgs in space, checking %j", results.length, orgNames);
    const missing = orgNames.filter(n => !known.has(n));
    if (missing.length) console.warn("[bubble/sync/validate] missing orgs: %j", missing);
    return missing;
  } catch (err) {
    console.warn("[bubble/sync/validate] org lookup threw — skipping pre-validation: %s", String(err));
    return [];
  }
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

async function saveRows(rows: Record<string, unknown>[]): Promise<void> {
  const body = rows.map(r => JSON.stringify(r)).join("\n");
  await getS3().send(new PutObjectCommand({
    Bucket: BUCKET, Key: ALERTS_KEY,
    Body: body, ContentType: "application/x-ndjson",
  }));
}

// GET /api/bubble/sync?agent_call_id=xxx — returns bubble_action preview for the modal
export async function GET(request: NextRequest) {
  const agent_call_id = request.nextUrl.searchParams.get("agent_call_id");
  if (!agent_call_id) return NextResponse.json({ error: "agent_call_id required" }, { status: 400 });
  try {
    const rows = await loadRows();
    const row = rows.find(r => r.agent_call_id === agent_call_id);
    if (!row) return NextResponse.json({ error: "row not found" }, { status: 404 });
    return NextResponse.json({ bubble_action: row.bubble_action ?? null, bubble_sync_status: row.bubble_sync_status ?? null });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[/api/bubble/sync GET]", msg);
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}

// POST /api/bubble/sync
// Body: { agent_call_id, action?, skip_validation? }
// Invokes the bubble-sync Lambda synchronously — waits for completion and returns the result.
// No polling needed; the response contains the final sync outcome directly.
export async function POST(request: NextRequest) {
  try {
    const body = await request.json() as { agent_call_id?: string; action?: string; skip_validation?: boolean };
    const { agent_call_id, action = "all", skip_validation = false } = body;
    console.info("[bubble/sync POST] agent_call_id=%s action=%s skip_validation=%s", agent_call_id, action, skip_validation);
    if (!agent_call_id) return NextResponse.json({ error: "agent_call_id required" }, { status: 400 });

    const rows = await loadRows();
    const row = rows.find(r => r.agent_call_id === agent_call_id);
    if (!row) return NextResponse.json({ error: "row not found" }, { status: 404 });

    const plan = row.bubble_action as Record<string, unknown> | null | undefined;
    if (!plan) return NextResponse.json({ error: "no bubble_action on row" }, { status: 400 });

    // Pre-validate org names before invoking Lambda
    if (!skip_validation) {
      const ep = (plan.event_preview ?? {}) as Record<string, unknown>;
      const lp = (plan.library_item_preview ?? {}) as Record<string, unknown>;
      const orgNames = ((ep.group ?? lp.group ?? []) as string[]).filter(Boolean);
      console.info("[bubble/sync/validate] agent_call_id=%s action=%s orgNames=%j", agent_call_id, action, orgNames);
      const missingOrgs = await findMissingOrgs(orgNames);
      if (missingOrgs.length > 0) {
        return NextResponse.json({
          error: `Organization(s) not found in Bubble: ${JSON.stringify(missingOrgs)}. Check that the org names match exactly and that the correct Eidarix space is configured (EIDARIX_VERSION=${EIDARIX_VERSION}).`,
        }, { status: 400 });
      }
    }

    // Mark "syncing" optimistically for full syncs so the badge updates immediately
    if (action === "all") {
      for (const r of rows) {
        if (r.agent_call_id === agent_call_id) r.bubble_sync_status = "syncing";
      }
      await saveRows(rows);
    }

    // Invoke Lambda synchronously — waits for completion, returns final result
    console.info("[bubble/sync] invoking Lambda agent_call_id=%s action=%s", agent_call_id, action);
    const invokeResult = await getLambda().send(new InvokeCommand({
      FunctionName: BUBBLE_SYNC_LAMBDA,
      InvocationType: "RequestResponse",
      Payload: JSON.stringify({ agent_call_id, action }),
    }));

    if (invokeResult.FunctionError) {
      const errPayload = invokeResult.Payload
        ? JSON.parse(new TextDecoder().decode(invokeResult.Payload))
        : {};
      const msg = errPayload.errorMessage ?? invokeResult.FunctionError;
      console.error("[bubble/sync] Lambda function error: %s", msg);
      return NextResponse.json({ error: msg }, { status: 500 });
    }

    const result = invokeResult.Payload
      ? JSON.parse(new TextDecoder().decode(invokeResult.Payload)) as Record<string, unknown>
      : {};

    console.info("[bubble/sync] Lambda complete ok=%s lib_id=%s event_id=%s", result.ok, result.bubble_library_item_id, result.bubble_event_id);

    if (!result.ok) {
      return NextResponse.json({ error: result.error ?? "Sync failed" }, { status: 400 });
    }

    return NextResponse.json({ ok: true, ...result });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[/api/bubble/sync POST]", msg);
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
