import { NextResponse } from "next/server";
import { listDraftCallIds } from "@/lib/content-gate-drafts";

export const dynamic = "force-dynamic";

// GET /api/content-gate/drafts
// Returns distinct `agent_call_id`s that have at least one draft on disk.
// Used by AlertsTable to show the "Editing" badge. Cached server-side for 30s.
export async function GET() {
  try {
    const ids = await listDraftCallIds();
    return NextResponse.json({ agent_call_ids: Array.from(ids) });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[/api/content-gate/drafts GET]", msg);
    return NextResponse.json({ error: msg, agent_call_ids: [] }, { status: 500 });
  }
}
