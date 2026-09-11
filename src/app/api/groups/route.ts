import { getGroupNodesForDropdown, BubbleAPIError } from "@/lib/bubble";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    const groups = await getGroupNodesForDropdown();
    return Response.json(groups);
  } catch (err) {
    if (err instanceof BubbleAPIError) {
      return Response.json(
        { error: err.message },
        { status: 502 }
      );
    }
    const msg = err instanceof Error ? err.message : String(err);
    return Response.json(
      { error: msg },
      { status: 500 }
    );
  }
}
