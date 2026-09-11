import { NextRequest, NextResponse } from "next/server";
import { ECSClient, RunTaskCommand } from "@aws-sdk/client-ecs";

export const dynamic = "force-dynamic";

// web-change-tracker infrastructure (separate deployed stack)
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

export async function POST(request: NextRequest) {
  try {
    const body = await request.json();
    const { run_id, target_id, agent_call_id } = body as { run_id?: string; target_id?: string; agent_call_id?: string };

    if (!run_id || !target_id) {
      return NextResponse.json({ error: "run_id and target_id are required" }, { status: 400 });
    }

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
          containerOverrides: [
            {
              name: WCT_CONTAINER,
              environment: [
                { name: "RERUN_RUN_ID", value: run_id },
                { name: "RERUN_TARGET_ID", value: target_id },
                { name: "RERUN_MODE", value: "alerts" },
                ...(agent_call_id ? [{ name: "RERUN_AGENT_CALL_ID", value: agent_call_id }] : []),
              ],
            },
          ],
        },
      })
    );

    const task = result.tasks?.[0];
    if (!task?.taskArn) {
      const failure = result.failures?.[0];
      throw new Error(failure?.reason ?? "ECS RunTask returned no task ARN");
    }

    return NextResponse.json({ taskArn: task.taskArn });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[/api/rerun POST]", msg);
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
