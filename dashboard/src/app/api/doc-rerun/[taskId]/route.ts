import { NextRequest, NextResponse } from "next/server";
import { ECSClient, DescribeTasksCommand } from "@aws-sdk/client-ecs";
import { S3Client, GetObjectCommand } from "@aws-sdk/client-s3";
import { createHash } from "node:crypto";

export const dynamic = "force-dynamic";

const WCT_CLUSTER = "web-change-tracker-prod";
const BUCKET = process.env.BUBBLE_ARTIFACT_BUCKET ?? "";

function rerunResultKey(run_id: string, target_id: string, library_item_url: string): string {
  const base = `alerts/reruns/${run_id}/${target_id}`;
  if (library_item_url) {
    const urlHash = createHash("sha256").update(library_item_url).digest("hex").slice(0, 16);
    return `${base}/doc_${urlHash}.json`;
  }
  return `${base}/result.json`;
}

let ecs: ECSClient | null = null;
let s3: S3Client | null = null;
function getEcs() {
  if (!ecs) ecs = new ECSClient({ region: process.env.AWS_REGION ?? "us-east-1" });
  return ecs;
}
function getS3() {
  if (!s3) s3 = new S3Client({ region: process.env.AWS_REGION ?? "us-east-1" });
  return s3;
}

export async function GET(
  request: NextRequest,
  { params }: { params: { taskId: string } }
) {
  try {
    const taskId = params.taskId;
    const run_id = request.nextUrl.searchParams.get("run_id") ?? "";
    const target_id = request.nextUrl.searchParams.get("target_id") ?? "";
    const library_item_url = request.nextUrl.searchParams.get("library_item_url") ?? "";

    const res = await getEcs().send(
      new DescribeTasksCommand({ cluster: WCT_CLUSTER, tasks: [taskId] })
    );

    const task = res.tasks?.[0];
    if (!task) {
      // ECS DescribeTasks has ~1h retention after a task stops. If the task
      // isn't returned, the rerun may already be done — check S3 for the
      // result.json before giving up, so the dashboard's auto-accept flow
      // works even when the browser tab closed before polling caught the
      // terminal state.
      if (run_id && target_id) {
        const key = rerunResultKey(run_id, target_id, library_item_url);
        try {
          const s3res = await getS3().send(
            new GetObjectCommand({ Bucket: BUCKET, Key: key })
          );
          const body = await s3res.Body?.transformToString();
          if (body) {
            const fullResult = JSON.parse(body);
            const docResult = {
              run_id: fullResult.run_id,
              target_id: fullResult.target_id,
              rerun_timestamp: fullResult.rerun_timestamp,
              config_hash: fullResult.config_hash,
              original_rows: fullResult.doc_original_rows ?? [],
              rerun_rows: fullResult.doc_rerun_rows ?? [],
            };
            return NextResponse.json({ status: "complete", result: docResult });
          }
        } catch {
          // no result.json either — fall through to unknown
        }
      }
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

    if (!run_id || !target_id) {
      return NextResponse.json({ status: "complete", result: null });
    }

    // Fetch result.json — reads doc_rerun_rows / doc_original_rows written by spike.py
    const key = rerunResultKey(run_id, target_id, library_item_url);
    try {
      const s3res = await getS3().send(
        new GetObjectCommand({ Bucket: BUCKET, Key: key })
      );
      const body = await s3res.Body?.transformToString();
      if (!body) throw new Error("Empty body");
      const fullResult = JSON.parse(body);
      // Return doc-specific subset: doc_rerun_rows + doc_original_rows from the result
      const docResult = {
        run_id: fullResult.run_id,
        target_id: fullResult.target_id,
        rerun_timestamp: fullResult.rerun_timestamp,
        config_hash: fullResult.config_hash,
        original_rows: fullResult.doc_original_rows ?? [],
        rerun_rows: fullResult.doc_rerun_rows ?? [],
      };
      return NextResponse.json({ status: "complete", result: docResult });
    } catch {
      // ECS reported STOPPED but the final S3 PUT hasn't landed yet (tail race).
      // Report as still-running so the dashboard keeps polling instead of
      // surfacing a terminal "no result found" error to the user.
      return NextResponse.json({ status: "running", lastStatus: "STOPPED" });
    }
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[/api/doc-rerun/[taskId]]", msg);
    return NextResponse.json({ status: "error", error: msg }, { status: 500 });
  }
}
