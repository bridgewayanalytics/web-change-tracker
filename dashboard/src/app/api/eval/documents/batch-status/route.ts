import { NextRequest, NextResponse } from "next/server";
import { ECSClient, DescribeTasksCommand } from "@aws-sdk/client-ecs";

export const dynamic = "force-dynamic";

const WCT_CLUSTER = "web-change-tracker-prod";

let ecs: ECSClient | null = null;
function getEcs() {
  if (!ecs) ecs = new ECSClient({ region: process.env.AWS_REGION ?? "us-east-1" });
  return ecs;
}

type TaskStatus = { status: "running" | "complete" | "failed" | "unknown"; error?: string };

// POST { taskIds: string[] } → { results: { [taskId]: TaskStatus } }
export async function POST(request: NextRequest) {
  try {
    const body = await request.json() as { taskIds?: string[] };
    const taskIds = body?.taskIds ?? [];
    if (!taskIds.length) return NextResponse.json({ results: {} });

    const results: Record<string, TaskStatus> = {};

    for (let i = 0; i < taskIds.length; i += 100) {
      const batch = taskIds.slice(i, i + 100);
      const res = await getEcs().send(new DescribeTasksCommand({ cluster: WCT_CLUSTER, tasks: batch }));

      const taskMap = new Map((res.tasks ?? []).map((t) => [t.taskArn?.split("/").pop() ?? "", t]));

      for (const taskId of batch) {
        const task = taskMap.get(taskId);
        if (!task) {
          results[taskId] = { status: "unknown" };
          continue;
        }
        const lastStatus = task.lastStatus ?? "";
        if (lastStatus !== "STOPPED") {
          results[taskId] = { status: "running", error: lastStatus };
          continue;
        }
        const container = task.containers?.[0];
        const exitCode = container?.exitCode ?? -1;
        if (exitCode !== 0) {
          results[taskId] = {
            status: "failed",
            error: container?.reason ?? `Container exited with code ${exitCode}`,
          };
        } else {
          results[taskId] = { status: "complete" };
        }
      }
    }

    return NextResponse.json({ results });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[/api/eval/documents/batch-status]", msg);
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
