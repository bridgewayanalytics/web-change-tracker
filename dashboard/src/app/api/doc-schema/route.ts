import { NextResponse } from "next/server";
import { DynamoDBClient, GetItemCommand } from "@aws-sdk/client-dynamodb";

export const dynamic = "force-dynamic";

const REGISTRY_TABLE = "chatkit_production_field_registry";

let dynamo: DynamoDBClient | null = null;
function getDynamo() {
  if (!dynamo) dynamo = new DynamoDBClient({ region: process.env.AWS_REGION ?? "us-east-1" });
  return dynamo;
}

function deser(val: Record<string, unknown>): unknown {
  if ("S" in val) return val.S;
  if ("N" in val) return Number(val.N);
  if ("BOOL" in val) return val.BOOL;
  if ("NULL" in val) return null;
  if ("L" in val) return (val.L as Record<string, unknown>[]).map(deser);
  if ("M" in val) {
    const m = val.M as Record<string, Record<string, unknown>>;
    return Object.fromEntries(Object.entries(m).map(([k, v]) => [k, deser(v)]));
  }
  return val;
}

const NO_CACHE = { headers: { "Cache-Control": "no-store" } };
const EMPTY = { columns: null, labels: null, kinds: {} as Record<string, string>, fieldAliases: {} as Record<string, string>, priorKeys: {} as Record<string, string[]> };

export async function GET() {
  try {
    const res = await getDynamo().send(
      new GetItemCommand({ TableName: REGISTRY_TABLE, Key: { agent_id: { S: "document-data-extraction" } } })
    );
    const item = res.Item;
    if (!item?.fields) return NextResponse.json(EMPTY, NO_CACHE);

    const fields = deser(item.fields as unknown as Record<string, unknown>) as Array<{
      key: string; label: string; kind: string; prior_keys: string[];
    }>;
    if (!Array.isArray(fields) || fields.length === 0) return NextResponse.json(EMPTY, NO_CACHE);

    const columns = fields.map((f) => f.key);
    const labels: Record<string, string> = {};
    const kinds: Record<string, string> = {};
    const priorKeys: Record<string, string[]> = {};

    for (const f of fields) {
      if (f.label) labels[f.key] = f.label;
      if (f.kind) kinds[f.key] = f.kind;
      if (Array.isArray(f.prior_keys) && f.prior_keys.length > 0) priorKeys[f.key] = f.prior_keys;
    }

    return NextResponse.json({ columns, labels, kinds, fieldAliases: {}, priorKeys }, NO_CACHE);
  } catch (err) {
    console.error("[/api/doc-schema]", err instanceof Error ? err.message : String(err));
    return NextResponse.json(EMPTY, NO_CACHE);
  }
}
