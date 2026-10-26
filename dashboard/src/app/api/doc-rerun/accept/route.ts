import { NextRequest, NextResponse } from "next/server";
import { S3Client, GetObjectCommand, PutObjectCommand } from "@aws-sdk/client-s3";
import { bustDocExtractionsCache, type DocExtractionRow } from "@/lib/doc-extractions-cache";

export const dynamic = "force-dynamic";

const BUCKET = process.env.BUBBLE_ARTIFACT_BUCKET ?? "";
const DOC_KEY = "alerts/document_extractions_table.jsonl";
const EVAL_KEY = "alerts/doc_eval_results_table.jsonl";

let s3: S3Client | null = null;
function getS3() {
  if (!s3) s3 = new S3Client({ region: process.env.AWS_REGION ?? "us-east-1" });
  return s3;
}

export async function POST(request: NextRequest) {
  try {
    const body = await request.json();
    const { run_id, target_id, library_item_url } = body as { run_id?: string; target_id?: string; library_item_url?: string };

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

    const result = JSON.parse(resultBody) as Record<string, unknown>;
    const rerunRows: DocExtractionRow[] = (result.doc_rerun_rows as DocExtractionRow[]) ?? [];

    if (rerunRows.length === 0) {
      throw new Error("Rerun result contains no document extraction rows");
    }

    // 1b. Force-copy immutable metadata fields from the original row unconditionally.
    //     run_timestamp must match original so the API sort (newest-first) keeps the
    //     row in its original table position. agent_call_id preserves the row's identity
    //     so QA results and eval keys stay linked. data_extraction_datetime is NOT forced
    //     here — if the original had "N/A" the rerun result carries the correct timestamp
    //     and should win.
    const originals: DocExtractionRow[] = (result.doc_original_rows as DocExtractionRow[]) ?? [];
    const firstOrig = originals[0] ?? {};
    const forceFields = ["run_id", "target_id", "run_timestamp", "source_url", "agent_call_id"];
    const lastRerunAt = new Date().toISOString();
    for (const row of rerunRows) {
      for (const f of forceFields) {
        const origVal = firstOrig[f] ?? (f === "run_id" ? run_id : f === "target_id" ? target_id : undefined);
        if (origVal !== undefined) row[f] = origVal;
      }
      row.last_rerun_at = lastRerunAt;
    }

    // 2. Read existing document_extractions_table.jsonl
    const existingRes = await getS3().send(
      new GetObjectCommand({ Bucket: BUCKET, Key: DOC_KEY })
    );
    const existingBody = await existingRes.Body?.transformToString();
    if (!existingBody) throw new Error("document_extractions_table.jsonl is empty");

    const existingRows: DocExtractionRow[] = [];
    for (const line of existingBody.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      try {
        existingRows.push(JSON.parse(trimmed) as DocExtractionRow);
      } catch {
        // skip malformed
      }
    }

    // 3. Replace rows in-place: insert rerun rows at the position of the first removed row
    //    so the table order is preserved exactly.
    //    When library_item_url is specified, only replace that specific item's rows and
    //    filter the rerun output to the matching URL (the rerun task may have re-extracted
    //    all items for the run/target, not just the one requested).
    const isRemoved = (r: DocExtractionRow) => {
      if (r.run_id !== run_id || r.target_id !== target_id) return false;
      return library_item_url ? r.library_item_url === library_item_url : true;
    };

    const rowsToInsert = library_item_url
      ? rerunRows.filter((r) => r.library_item_url === library_item_url)
      : rerunRows;
    const insertRows = rowsToInsert.length > 0 ? rowsToInsert : rerunRows;

    const patched: DocExtractionRow[] = [];
    let inserted = false;
    for (const r of existingRows) {
      if (isRemoved(r)) {
        if (!inserted) {
          patched.push(...insertRows);
          inserted = true;
        }
        // skip the old row
      } else {
        patched.push(r);
      }
    }
    if (!inserted) patched.push(...insertRows); // fallback: append if none matched

    // 3b. Carry forward eval_row_key onto new extraction rows.
    //     Without this, the dashboard sees the new rows with empty eval_row_key
    //     and all four fallback lookups fail — old QA appears to vanish until
    //     carry_forward_eval_row_keys() runs on the Python side (during QA start,
    //     ~10-30s after accept). Stamping here closes that gap so old QA stays
    //     visible immediately after accept until the new QA replaces it.
    try {
      const evalRes = await getS3().send(
        new GetObjectCommand({ Bucket: BUCKET, Key: EVAL_KEY })
      );
      const evalBody = await evalRes.Body?.transformToString();
      if (evalBody) {
        // Build (agent_call_id, library_item_url) → eval_row_key map from most recent eval entry.
        const keyMap = new Map<string, string>();
        for (const line of evalBody.split("\n")) {
          const t = line.trim();
          if (!t) continue;
          try {
            const er = JSON.parse(t) as { agent_call_id?: string; library_item_url?: string; eval_row_key?: string };
            const cid = er.agent_call_id ?? "";
            const url = er.library_item_url ?? "";
            const erk = er.eval_row_key ?? "";
            if (!cid || !erk) continue;
            // Last entry wins (JSONL order = chronological, upserts preserved)
            keyMap.set(`${cid}|${url}`, erk);
            // Also index by just agent_call_id as a weaker fallback (for sibling rows
            // or rows where library_item_url differs slightly between extraction and eval)
            if (!keyMap.has(cid)) keyMap.set(cid, erk);
          } catch {
            // skip malformed lines
          }
        }

        let stamped = 0;
        for (const row of insertRows) {
          if (row.eval_row_key) continue; // already stamped by extraction pipeline
          const cid = String(row.agent_call_id ?? "");
          const url = String(row.library_item_url ?? "");
          if (!cid) continue;
          const found = keyMap.get(`${cid}|${url}`) ?? keyMap.get(cid);
          if (found) {
            row.eval_row_key = found;
            stamped += 1;
          }
        }
        if (stamped > 0) {
          console.log(`[/api/doc-rerun/accept] carried forward eval_row_key onto ${stamped}/${insertRows.length} new rows`);
        }
      }
    } catch (e) {
      // Non-fatal: if eval table doesn't exist yet or is unreadable, continue without carry-forward.
      // Python's carry_forward_eval_row_keys will handle it at QA-start time (just with a brief gap).
      console.warn("[/api/doc-rerun/accept] carry-forward skipped (non-fatal):", e instanceof Error ? e.message : e);
    }

    // 4. Write back
    const newJSONL = patched.map((r) => JSON.stringify(r)).join("\n") + "\n";
    await getS3().send(
      new PutObjectCommand({
        Bucket: BUCKET,
        Key: DOC_KEY,
        Body: newJSONL,
        ContentType: "application/x-ndjson",
      })
    );

    // 5. Bust in-process cache
    bustDocExtractionsCache();

    return NextResponse.json({ ok: true, replaced: rerunRows.length });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[/api/doc-rerun/accept]", msg);
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
