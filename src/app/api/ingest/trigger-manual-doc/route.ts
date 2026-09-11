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

// POST /api/ingest/trigger-manual-doc
// Body: { agent_call_id: string }
// Triggers an ECS task in manual_doc mode. The task reads the stub row by
// agent_call_id from document_extractions_table.jsonl, runs extract_document_data(),
// and patches the row with extracted fields + ingest_status: "pending".
export async function POST(request: NextRequest) {
  try {
    const body = await request.json() as { agent_call_id?: string };
    const { agent_call_id } = body;
    if (!agent_call_id) {
      return NextResponse.json({ error: "agent_call_id required" }, { status: 400 });
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
                { name: "MANUAL_DOC_AGENT_CALL_ID", value: agent_call_id },
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

    return NextResponse.json({ task_arn: task.taskArn });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[/api/ingest/trigger-manual-doc]", msg);
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
