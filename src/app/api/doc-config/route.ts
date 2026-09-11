import { NextResponse } from "next/server";
import { DynamoDBClient, GetItemCommand } from "@aws-sdk/client-dynamodb";
import { createHash } from "crypto";

export const dynamic = "force-dynamic";

const TABLE = "chatkit_production_config";
const CONFIG_KEY = "chat:document-data-extraction";

let dynamo: DynamoDBClient | null = null;
function getDynamo() {
  if (!dynamo) dynamo = new DynamoDBClient({ region: process.env.AWS_REGION ?? "us-east-1" });
  return dynamo;
}

export async function GET() {
  try {
    const res = await getDynamo().send(
      new GetItemCommand({
        TableName: TABLE,
        Key: { config_key: { S: CONFIG_KEY } },
      })
    );

    const item = res.Item;
    if (!item) {
      return NextResponse.json({ error: "Config not found" }, { status: 404 });
    }

    const instructions = item.instructions?.S ?? "";
    const model = item.model?.S ?? "";
    const updated_at = item.updated_at?.S ?? "";
    const hash = createHash("md5").update(instructions + "|" + model).digest("hex");

    return NextResponse.json({ hash, model, updated_at });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[/api/doc-config]", msg);
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
