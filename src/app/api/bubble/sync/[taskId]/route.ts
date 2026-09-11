import { NextRequest, NextResponse } from "next/server";
import { ECSClient, DescribeTasksCommand } from "@aws-sdk/client-ecs";
import { S3Client, GetObjectCommand } from "@aws-sdk/client-s3";

export const dynamic = "force-dynamic";

const WCT_CLUSTER = "web-change-tracker-prod";
const BUCKET = process.env.BUBBLE_ARTIFACT_BUCKET ?? "web-change-tracker-prod-artifacts-815039343351";
const ALERTS_KEY = "alerts/alerts_table.jsonl";

let ecs: ECSClient | null = null;
function getEcs() {
  if (!ecs) ecs = new ECSClient({ region: process.env.AWS_REGION ?? "us-east-1" });
  return ecs;
}

let s3: S3Client | null = null;
function getS3() {
  if (!s3) s3 = new S3Client({ region: process.env.AWS_REGION ?? "us-east-1" });
  return s3;
}

async function findRow(agent_call_id: string): Promise<Record<string, unknown> | null> {
  try {
    const res = await getS3().send(new GetObjectCommand({ Bucket: BUCKET, Key: ALERTS_KEY }));
    const text = await res.Body!.transformToString("utf-8");
    for (const line of text.split("\n")) {
      const t = line.trim();
      if (!t) continue;
      try {
        const row = JSON.parse(t) as Record<string, unknown>;
        if (row.agent_call_id === agent_call_id) return row;
      } catch { continue; }
    }
  } catch { /* row not found is fine */ }
  return null;
}

// GET /api/bubble/sync/[taskId]?agent_call_id=xxx
// Polls ECS task. When STOPPED, reads the row to return the actual sync outcome.
// Returns:
//   { status: "running" }
//   { status: "complete", bubble_sync_status?, bubble_library_item_id?, bubble_event_id?, eidarix_agenda_item_ids? }
//   { status: "failed", error: string }
export async function GET(
  request: NextRequest,
  { params }: { params: { taskId: string } }
) {
  try {
    const { taskId } = params;
    const agent_call_id = request.nextUrl.searchParams.get("agent_call_id") ?? "";

    const res = await getEcs().send(
      new DescribeTasksCommand({ cluster: WCT_CLUSTER, tasks: [taskId] })
    );

    const task = res.tasks?.[0];
    if (!task) {
      console.warn("[bubble/sync/poll] task not found taskId=%s", taskId);
      return NextResponse.json({ status: "unknown", error: "Task not found" });
    }

    const lastStatus = task.lastStatus ?? "";
    if (lastStatus !== "STOPPED") {
      return NextResponse.json({ status: "running", lastStatus });
    }

    const container = task.containers?.[0];
    const exitCode = container?.exitCode ?? -1;

    // Read the row regardless of exit code — bubble_sync.py may have patched error details
    const row = agent_call_id ? await findRow(agent_call_id) : null;

    if (exitCode !== 0) {
      const syncError = (row?.bubble_sync_error as string | undefined)
        ?? container?.reason
        ?? `ECS container exited with code ${exitCode}`;
      console.warn("[bubble/sync/poll] task failed taskId=%s exitCode=%d error=%s", taskId, exitCode, syncError);
      return NextResponse.json({ status: "failed", error: syncError });
    }

    const result = {
      status: "complete",
      bubble_sync_status: row?.bubble_sync_status ?? null,
      bubble_library_item_id: row?.bubble_library_item_id ?? null,
      bubble_event_id: row?.bubble_event_id ?? null,
      eidarix_agenda_item_ids: row?.eidarix_agenda_item_ids ?? null,
    };
    console.info("[bubble/sync/poll] task complete taskId=%s lib_id=%s event_id=%s",
      taskId, result.bubble_library_item_id, result.bubble_event_id);
    return NextResponse.json(result);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[/api/bubble/sync/[taskId]]", msg);
    return NextResponse.json({ status: "error", error: msg }, { status: 500 });
  }
}
