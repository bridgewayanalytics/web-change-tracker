import { NextRequest, NextResponse } from "next/server";
import { ECSClient, RunTaskCommand } from "@aws-sdk/client-ecs";

export const dynamic = "force-dynamic";

const WCT_CLUSTER = "web-change-tracker-prod";
const WCT_TASK_DEFINITION = "web-change-tracker-prod";
const WCT_CONTAINER = "web-change-tracker-prod";
const WCT_SECURITY_GROUP = "sg-0813b03be31d51bbb";
const WCT_SUBNETS = [
  "subnet-0cd0f843fd5a199c3",
  "subnet-02ed42b574ccb2aeb",
  "subnet-0593f56de1cf0f4a5",
  "subnet-09cbe5386e755836e",
  "subnet-0bb36a8620ea48716",
  "subnet-085bf95f96dcfd5c9",
];

let ecs: ECSClient | null = null;
function getEcs() {
  if (!ecs) ecs = new ECSClient({ region: process.env.AWS_REGION ?? "us-east-1" });
  return ecs;
}

// POST { agent_call_ids: string[] }
// Triggers eval/run_bulk_reeval.py as an ECS task:
//   1. Re-runs page_change_agent on original HTML snapshots for each row's run_id+target_id
//   2. Auto-accepts rerun results into alerts_table.jsonl
//   3. Runs QA eval on the updated rows
export async function POST(request: NextRequest) {
  try {
    const body = await request.json() as { agent_call_ids?: string[] } | null;
    const ids = body?.agent_call_ids ?? [];
    if (!ids.length) {
      return NextResponse.json({ error: "agent_call_ids is required" }, { status: 400 });
    }

    const command = ["python", "-m", "eval.run_bulk_reeval", "--agent-call-ids", ids.join(",")];

    const result = await getEcs().send(
      new RunTaskCommand({
        cluster: WCT_CLUSTER,
        taskDefinition: WCT_TASK_DEFINITION,
        launchType: "FARGATE",
        networkConfiguration: {
          awsvpcConfiguration: {
            subnets: WCT_SUBNETS,
            securityGroups: [WCT_SECURITY_GROUP],
            assignPublicIp: "ENABLED",
          },
        },
        overrides: {
          containerOverrides: [{ name: WCT_CONTAINER, command }],
        },
      })
    );

    const task = result.tasks?.[0];
    if (!task?.taskArn) {
      const failure = result.failures?.[0];
      throw new Error(failure?.reason ?? "ECS RunTask returned no task ARN");
    }

    const taskId = task.taskArn.split("/").pop()!;
    console.info("[/api/rerun/bulk-qa POST] ECS task started taskId=%s ids=%s", taskId, ids.join(","));
    return NextResponse.json({ taskId, taskArn: task.taskArn });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[/api/rerun/bulk-qa POST]", msg);
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
