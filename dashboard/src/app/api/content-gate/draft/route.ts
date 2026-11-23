import { NextRequest, NextResponse } from "next/server";
import { getDraft, putDraft, deleteDraft, type ContentGateDraft } from "@/lib/content-gate-drafts";

export const dynamic = "force-dynamic";

// GET /api/content-gate/draft?agent_call_id=X&library_item_url=Y
// Returns the stored draft object or { draft: null } when none exists.
export async function GET(request: NextRequest) {
  const url = new URL(request.url);
  const agent_call_id = url.searchParams.get("agent_call_id") ?? "";
  const library_item_url = url.searchParams.get("library_item_url");
  if (!agent_call_id) {
    return NextResponse.json({ error: "agent_call_id required" }, { status: 400 });
  }
  try {
    const draft = await getDraft(agent_call_id, library_item_url);
    return NextResponse.json({ draft });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[/api/content-gate/draft GET]", msg);
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}

// POST /api/content-gate/draft
// Body: { agent_call_id, library_item_url?, draft: {...} } where draft matches
// ContentGateDraft without `version`/`updated_at` (added server-side).
export async function POST(request: NextRequest) {
  try {
    const body = await request.json() as {
      agent_call_id?: string;
      library_item_url?: string | null;
      draft?: Omit<ContentGateDraft, "version" | "updated_at">;
    };
    const { agent_call_id, library_item_url, draft } = body;
    if (!agent_call_id || !draft) {
      return NextResponse.json({ error: "agent_call_id and draft required" }, { status: 400 });
    }
    await putDraft(agent_call_id, library_item_url ?? null, draft);
    return NextResponse.json({ ok: true });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[/api/content-gate/draft POST]", msg);
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}

// DELETE /api/content-gate/draft
// Body: { agent_call_id, library_item_url? }
// Idempotent — succeeds whether or not a draft existed.
export async function DELETE(request: NextRequest) {
  try {
    const body = await request.json() as { agent_call_id?: string; library_item_url?: string | null };
    const { agent_call_id, library_item_url } = body;
    if (!agent_call_id) {
      return NextResponse.json({ error: "agent_call_id required" }, { status: 400 });
    }
    await deleteDraft(agent_call_id, library_item_url ?? null);
    return NextResponse.json({ ok: true });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[/api/content-gate/draft DELETE]", msg);
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
