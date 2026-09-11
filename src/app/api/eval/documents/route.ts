import { NextRequest, NextResponse } from "next/server";
import {
  S3Client,
  GetObjectCommand,
  PutObjectCommand,
} from "@aws-sdk/client-s3";
import { ECSClient, RunTaskCommand } from "@aws-sdk/client-ecs";

export const dynamic = "force-dynamic";

const BUCKET = process.env.BUBBLE_ARTIFACT_BUCKET ?? "";
const EVAL_KEY = "alerts/doc_eval_results_table.jsonl";

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

let s3: S3Client | null = null;
let ecs: ECSClient | null = null;
function getS3() {
  if (!s3) s3 = new S3Client({ region: process.env.AWS_REGION ?? "us-east-1" });
  return s3;
}
function getEcs() {
  if (!ecs) ecs = new ECSClient({ region: process.env.AWS_REGION ?? "us-east-1" });
  return ecs;
}

type DocEvalEntry = {
  eval_scores: Record<string, { score: string; reasoning: string }>;
  eval_timestamp: string;
  eval_run_id: string;
  agent_call_id: string;
  eval_row_key?: string;
  document_title?: string;
  library_item_url?: string;
};

async function loadEvalMap(): Promise<Record<string, DocEvalEntry>> {
  try {
    const resp = await getS3().send(new GetObjectCommand({ Bucket: BUCKET, Key: EVAL_KEY }));
    const body = await resp.Body?.transformToString();
    if (!body) return {};
    const map: Record<string, DocEvalEntry> = {};
    for (const line of body.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      try {
        const row = JSON.parse(trimmed) as DocEvalEntry;
        const key = row.eval_row_key || row.agent_call_id;
        if (key) map[key] = row;
      } catch { /* skip malformed */ }
    }
    return map;
  } catch {
    return {};
  }
}

async function saveEvalMap(map: Record<string, DocEvalEntry>): Promise<void> {
  const body = Object.values(map).map((r) => JSON.stringify(r)).join("\n");
  await getS3().send(
    new PutObjectCommand({
      Bucket: BUCKET,
      Key: EVAL_KEY,
      Body: body,
      ContentType: "application/x-ndjson",
    })
  );
}

// GET — return map of agent_call_id → eval result
export async function GET() {
  try {
    const map = await loadEvalMap();
    const trimmed: Record<string, DocEvalEntry> = {};
    for (const [id, entry] of Object.entries(map)) {
      trimmed[id] = {
        agent_call_id: entry.agent_call_id,
        eval_scores: entry.eval_scores ?? {},
        eval_timestamp: entry.eval_timestamp ?? "",
        eval_run_id: entry.eval_run_id ?? "",
        document_title: entry.document_title,
        library_item_url: entry.library_item_url,
      };
    }
    return NextResponse.json({ results: trimmed, count: Object.keys(trimmed).length });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[/api/eval/documents GET]", msg);
    return NextResponse.json({ results: {}, count: 0, error: msg }, { status: 500 });
  }
}

// POST { agent_call_id? } — trigger ECS doc eval task
export async function POST(request: NextRequest) {
  try {
    const body = await request.json() as { agent_call_id?: string; library_item_url?: string } | null;
    const agent_call_id = body?.agent_call_id ?? null;
    const library_item_url = body?.library_item_url ?? null;

    let command: string[];
    if (agent_call_id) {
      command = ["python", "-m", "eval.run_doc_eval", "--agent-call-ids", agent_call_id];
      if (library_item_url) command.push("--library-item-url", library_item_url);
    } else {
      command = ["python", "-m", "eval.run_doc_eval"];
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
              command,
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

    const taskId = task.taskArn.split("/").pop()!;
    console.info("[/api/eval/documents POST] ECS task started taskId=%s agent_call_id=%s", taskId, agent_call_id ?? "all");
    return NextResponse.json({ taskId, taskArn: task.taskArn, mode: agent_call_id ? "single" : "all" });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[/api/eval/documents POST]", msg);
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}

// DELETE { agent_call_id } — remove eval result from JSONL
export async function DELETE(request: NextRequest) {
  try {
    const body = await request.json() as { agent_call_id?: string };
    const { agent_call_id } = body;
    if (!agent_call_id) {
      return NextResponse.json({ error: "agent_call_id is required" }, { status: 400 });
    }

    const map = await loadEvalMap();
    const keysToDelete = Object.keys(map).filter(
      (k) => k === agent_call_id || k.startsWith(agent_call_id + "|")
    );
    if (keysToDelete.length === 0) {
      return NextResponse.json({ ok: true, deleted: false, message: "Not found" });
    }
    for (const k of keysToDelete) delete map[k];
    await saveEvalMap(map);
    return NextResponse.json({ ok: true, deleted: true });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[/api/eval/documents DELETE]", msg);
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
