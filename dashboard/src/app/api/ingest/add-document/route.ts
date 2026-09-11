import { NextRequest, NextResponse } from "next/server";
import { S3Client } from "@aws-sdk/client-s3";
import { ECSClient, RunTaskCommand } from "@aws-sdk/client-ecs";
import { appendJsonlRow } from "@/lib/patch-jsonl";
import { bustDocExtractionsCache } from "@/lib/doc-extractions-cache";
import { randomUUID } from "crypto";

export const dynamic = "force-dynamic";

const BUCKET = process.env.BUBBLE_ARTIFACT_BUCKET ?? "web-change-tracker-prod-artifacts-815039343351";
const DOC_KEY = "alerts/document_extractions_table.jsonl";
const WCT_CLUSTER = "web-change-tracker-prod";
const WCT_TASK_DEFINITION = "web-change-tracker-prod";
const WCT_CONTAINER = "web-change-tracker-prod";
const WCT_SECURITY_GROUP = "sg-0813b03be31d51bbb";
const WCT_SUBNETS = [
  "subnet-0cd0f843fd5a199c3", "subnet-02ed42b574ccb2aeb", "subnet-0593f56de1cf0f4a5",
  "subnet-09cbe5386e755836e", "subnet-0bb36a8620ea48716", "subnet-085bf95f96dcfd5c9",
];

let s3: S3Client | null = null;
function getS3() {
  if (!s3) s3 = new S3Client({ region: process.env.AWS_REGION ?? "us-east-1" });
  return s3;
}

let ecs: ECSClient | null = null;
function getEcs() {
  if (!ecs) ecs = new ECSClient({ region: process.env.AWS_REGION ?? "us-east-1" });
  return ecs;
}

// POST /api/ingest/add-document
// Body: { filename: string, url?: string, s3_key?: string }
// Creates a new stub row in document_extractions_table.jsonl with ingest_status="pending".
// One of url or s3_key must be provided.
export async function POST(request: NextRequest) {
  try {
    const body = await request.json() as { filename?: string; url?: string; s3_key?: string };
    const { filename, url, s3_key } = body;

    if (!filename) return NextResponse.json({ error: "filename required" }, { status: 400 });
    if (!url && !s3_key) return NextResponse.json({ error: "url or s3_key required" }, { status: 400 });

    const agent_call_id = randomUUID();
    const row: Record<string, unknown> = {
      agent_call_id,
      run_id: "manual",
      target_id: "manual",
      run_timestamp: new Date().toISOString(),
      source_url: url ?? "",
      library_item_title: filename.replace(/\.[^.]+$/, "").replace(/[_-]/g, " "),
      library_item_url: url ?? "",
      library_item_file_name: filename,
      ingest_status: "pending",
    };

    if (s3_key) {
      row.manual_doc_s3_key = s3_key;
      // For file uploads, library_item_url is empty until publish generates a presigned URL
      row.library_item_url = "";
    }

    await appendJsonlRow(getS3(), BUCKET, DOC_KEY, row);
    bustDocExtractionsCache();

    // Trigger ECS task to run document extraction agent on the stub row
    try {
      await getEcs().send(new RunTaskCommand({
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
          containerOverrides: [{
            name: WCT_CONTAINER,
            environment: [{ name: "MANUAL_DOC_AGENT_CALL_ID", value: agent_call_id }],
          }],
        },
      }));
    } catch (ecsErr) {
      console.error("[/api/ingest/add-document] ECS trigger failed:", ecsErr);
      // Non-fatal — stub row is written, user can retry
    }

    return NextResponse.json({ ok: true, agent_call_id, row });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[/api/ingest/add-document]", msg);
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
