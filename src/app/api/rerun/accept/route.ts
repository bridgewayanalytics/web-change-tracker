import { NextRequest, NextResponse } from "next/server";
import { S3Client, GetObjectCommand, PutObjectCommand } from "@aws-sdk/client-s3";
import { bustAlertsCache, type AlertRow } from "@/lib/alerts-cache";

export const dynamic = "force-dynamic";

const BUCKET = process.env.BUBBLE_ARTIFACT_BUCKET ?? "";
const ALERTS_KEY = "alerts/alerts_table.jsonl";

let s3: S3Client | null = null;
function getS3() {
  if (!s3) s3 = new S3Client({ region: process.env.AWS_REGION ?? "us-east-1" });
  return s3;
}

export async function POST(request: NextRequest) {
  try {
    const body = await request.json();
    const { run_id, target_id } = body as { run_id?: string; target_id?: string };

    if (!run_id || !target_id) {
      return NextResponse.json({ error: "run_id and target_id are required" }, { status: 400 });
    }

    // 1. Read rerun result
    const resultKey = `alerts/reruns/${run_id}/${target_id}/result.json`;
    const resultRes = await getS3().send(
      new GetObjectCommand({ Bucket: BUCKET, Key: resultKey })
    );
    const resultBody = await resultRes.Body?.transformToString();
    if (!resultBody) throw new Error("Rerun result is empty");

    const result = JSON.parse(resultBody) as {
      rerun_rows?: AlertRow[];
      rerun?: AlertRow;
      rerun_timestamp?: string;
    };

    // Support both array (rerun_rows) and single-object (rerun) schemas
    const rerunRows: AlertRow[] = result.rerun_rows
      ? result.rerun_rows
      : result.rerun
      ? [result.rerun]
      : [];

    if (rerunRows.length === 0) {
      throw new Error("Rerun result contains no rows");
    }

    // 1b. Restore identity + position fields from original rows.
    //     spike.py stamps a fresh run_timestamp on rerun rows, which would
    //     sort them to the top of the table and break QA eval lookup.
    //     run_timestamp must ALWAYS come from the original.  Other fields
    //     are only filled in when the rerun row is missing them.
    const originals: AlertRow[] = (result as Record<string, unknown>).original_rows
      ? ((result as Record<string, unknown>).original_rows as AlertRow[])
      : (result as Record<string, unknown>).original
      ? [(result as Record<string, unknown>).original as AlertRow]
      : [];
    const firstOrig = originals[0] ?? {};
    // Always overwrite identity fields from the original row.
    // run_timestamp: spike stamps current time, would re-sort the row to top
    // agent_call_id: rerun generates a fresh UUID, breaking QA eval association
    // alert_date_time: agent re-outputs current time, should reflect original detection
    // run_id: rerun gets a fresh run_id; html_fetcher.py needs the ORIGINAL run_id
    //   to reconstruct the S3 path pages/<target>/<date>/<run_id>/before.html —
    //   without this, QA eval runs with empty HTML and scores blindly
    const alwaysPreserve = ["run_timestamp", "agent_call_id", "alert_date_time", "run_id"];
    // Conditional: only fill when the rerun row is missing the field
    const conditionalPreserve = ["target_id", "source_url"];
    // These fields represent work done after the original pipeline run (sync state,
    // transcripts, recordings, ingest gate). The rerun agent never outputs them, so
    // they would be silently lost when the row is replaced. Preserve from the original
    // whenever the rerun row doesn't already have them.
    const syncPreserve = [
      "bubble_action",
      "bubble_sync_status",
      "bubble_sync_error",
      "bubble_event_id",
      "bubble_library_item_id",
      "eidarix_agenda_item_ids",
      "transcript_s3_key",
      "manual_transcript_s3_key",
      "transcript_chunks_s3_key",
      "recording_s3_key",
      "ingest_status",
    ];
    // Build a url→original map for best-effort per-row matching in multi-doc reruns
    const origByUrl = new Map<string, AlertRow>();
    for (const orig of originals) {
      const url = String(orig.library_item_url ?? "");
      if (url && url !== "N/A") origByUrl.set(url, orig);
    }
    for (const row of rerunRows) {
      for (const f of alwaysPreserve) {
        const origVal = firstOrig[f] ?? undefined;
        if (origVal !== undefined) row[f] = origVal;
      }
      for (const f of conditionalPreserve) {
        if (row[f] === undefined || row[f] === null || row[f] === "") {
          const origVal = firstOrig[f] ?? (f === "target_id" ? target_id : undefined);
          if (origVal !== undefined) row[f] = origVal;
        }
      }
      // Match this rerun row to its original by library_item_url, fall back to firstOrig
      const rowUrl = String(row.library_item_url ?? "");
      const matchedOrig: AlertRow = (rowUrl && rowUrl !== "N/A" && origByUrl.get(rowUrl)) || firstOrig;
      for (const f of syncPreserve) {
        if (row[f] === undefined || row[f] === null) {
          const origVal = matchedOrig[f] ?? undefined;
          if (origVal !== undefined) row[f] = origVal;
        }
      }
    }

    // Stamp last_rerun_at on all accepted rows so the dashboard can show it
    const rerunTs = result.rerun_timestamp ?? new Date().toISOString();
    for (const row of rerunRows) {
      row.last_rerun_at = rerunTs;
    }

    // 2. Read existing alerts_table.jsonl
    const alertsRes = await getS3().send(
      new GetObjectCommand({ Bucket: BUCKET, Key: ALERTS_KEY })
    );
    const alertsBody = await alertsRes.Body?.transformToString();
    if (!alertsBody) throw new Error("alerts_table.jsonl is empty");

    const existingRows: AlertRow[] = [];
    for (const line of alertsBody.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      try {
        existingRows.push(JSON.parse(trimmed) as AlertRow);
      } catch {
        // skip malformed
      }
    }

    // 3. Replace only the rows that were part of the original comparison,
    //    inserting the rerun rows at the exact position of the first original
    //    so the row stays in the same place in the table (same run_timestamp
    //    group means sort order alone can't preserve position).
    const originalCallIds = new Set(
      originals.map((r) => String(r.agent_call_id ?? "")).filter(Boolean)
    );
    const isOriginal = (r: AlertRow) => {
      if (r.run_id !== run_id || r.target_id !== target_id) return false;
      if (originalCallIds.size === 0) return true;
      return originalCallIds.has(String(r.agent_call_id ?? ""));
    };
    const patched: AlertRow[] = [];
    let rerunInserted = false;
    for (const r of existingRows) {
      if (isOriginal(r)) {
        if (!rerunInserted) {
          patched.push(...rerunRows);
          rerunInserted = true;
        }
        // drop the original row — replaced by rerun
      } else {
        patched.push(r);
      }
    }
    if (!rerunInserted) patched.push(...rerunRows);

    // 4. Write back
    const newJSONL = patched.map((r) => JSON.stringify(r)).join("\n") + "\n";
    await getS3().send(
      new PutObjectCommand({
        Bucket: BUCKET,
        Key: ALERTS_KEY,
        Body: newJSONL,
        ContentType: "application/x-ndjson",
      })
    );

    // 5. Bust the in-process alerts cache so the next request picks up new data
    bustAlertsCache();

    return NextResponse.json({ ok: true, replaced: rerunRows.length });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[/api/rerun/accept]", msg);
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
