import { NextRequest, NextResponse } from "next/server";
import { getChatkitApiKey } from "@/lib/chatkit-api-key";

export const dynamic = "force-dynamic";

const CHATKIT_API_URL =
  process.env.CHATKIT_API_URL ?? "https://chat-api.bridgewayanalytics.com";

// POST /api/ingest/manual-document-url
// Body: { url: string, filename: string }
// Direct one-off ingest of a URL. Does not create or patch any row.
export async function POST(request: NextRequest) {
  try {
    const body = await request.json() as { url?: string; filename?: string };
    const { url, filename } = body;
    if (!url) return NextResponse.json({ error: "url required" }, { status: 400 });

    const apiKey = await getChatkitApiKey();
    if (!apiKey) {
      return NextResponse.json({ error: "CHATKIT_INTERNAL_API_KEY not configured" }, { status: 500 });
    }

    const formData = new FormData();
    formData.append("namespace", "newsreel-generation:ART");
    formData.append("filename", filename ?? url);
    formData.append("url", url);

    const ingestResp = await fetch(
      `${CHATKIT_API_URL}/internal/documents/ingest`,
      {
        method: "POST",
        headers: { "x-api-key": apiKey },
        body: formData,
      }
    );
    const ingestBody = await ingestResp.json().catch(() => ({})) as Record<string, unknown>;

    if (!ingestResp.ok) {
      console.error("[/api/ingest/manual-document-url] ingest API error", ingestResp.status, ingestBody);
      return NextResponse.json({ error: ingestBody.detail ?? "Ingest API error" }, { status: 502 });
    }

    return NextResponse.json({
      ok: true,
      status: ingestBody.status ?? "submitted",
      document_id: ingestBody.document_id,
    });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[/api/ingest/manual-document-url]", msg);
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
