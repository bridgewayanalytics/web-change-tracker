/**
 * SSM Parameter Store loader for Bubble API credentials.
 * Used when BUBBLE_API_URL / BUBBLE_API_KEY are not set in env (e.g. ECS).
 */

import { SSMClient, GetParameterCommand } from "@aws-sdk/client-ssm";

const SSM_URL_PARAM =
  process.env.BUBBLE_API_URL_SSM_PARAM ?? "/naic-dashboard/prod/bubble_api_url";
const SSM_KEY_PARAM =
  process.env.BUBBLE_API_KEY_SSM_PARAM ??
  "/naic-dashboard/prod/bubble_api_key";

let cachedUrl: string | null = null;
let cachedKey: string | null = null;

export async function getBubbleApiUrl(): Promise<string | null> {
  if (process.env.BUBBLE_API_URL) {
    return process.env.BUBBLE_API_URL;
  }
  if (cachedUrl) return cachedUrl;
  try {
    const client = new SSMClient({
      region: process.env.AWS_REGION ?? "us-east-1",
    });
    const cmd = new GetParameterCommand({
      Name: SSM_URL_PARAM,
      WithDecryption: true,
    });
    const resp = await client.send(cmd);
    cachedUrl = resp.Parameter?.Value ?? null;
    return cachedUrl;
  } catch {
    return null;
  }
}

export async function getBubbleApiKey(): Promise<string | null> {
  if (process.env.BUBBLE_API_KEY) {
    return process.env.BUBBLE_API_KEY;
  }
  if (cachedKey) return cachedKey;
  try {
    const client = new SSMClient({
      region: process.env.AWS_REGION ?? "us-east-1",
    });
    const cmd = new GetParameterCommand({
      Name: SSM_KEY_PARAM,
      WithDecryption: true,
    });
    const resp = await client.send(cmd);
    cachedKey = resp.Parameter?.Value ?? null;
    return cachedKey;
  } catch {
    return null;
  }
}
