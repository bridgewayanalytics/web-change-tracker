import { NextRequest, NextResponse } from "next/server";
import { ECSClient, DescribeTasksCommand } from "@aws-sdk/client-ecs";

export const dynamic = "force-dynamic";

const WCT_CLUSTER = "web-change-tracker-prod";

let ecs: ECSClient | null = null;
function getEcs() {
  if (!ecs) ecs = new ECSClient({ region: process.env.AWS_REGION ?? "us-east-1" });
  return ecs;
}

export async function GET(
  _request: NextRequest,
  { params }: { params: { taskId: string } }
) {
  try {
    const { taskId } = params;
    const res = await getEcs().send(
      new DescribeTasksCommand({ cluster: WCT_CLUSTER, tasks: [taskId] })
    );

    const task = res.tasks?.[0];
    if (!task) {
      return NextResponse.json({ status: "unknown", error: "Task not found" });
    }

    const lastStatus = task.lastStatus ?? "";
    if (lastStatus !== "STOPPED") {
      return NextResponse.json({ status: "running", lastStatus });
    }

    const container = task.containers?.[0];
    const exitCode = container?.exitCode ?? -1;
    if (exitCode !== 0) {
      return NextResponse.json({
        status: "failed",
        error: container?.reason ?? `Container exited with code ${exitCode}`,
      });
    }

    return NextResponse.json({ status: "complete" });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[/api/eval/documents/[taskId]]", msg);
    return NextResponse.json({ status: "error", error: msg }, { status: 500 });
  }
}
