import { SSMClient, GetParameterCommand } from "@aws-sdk/client-ssm";

const SSM_PARAM =
  process.env.CHATKIT_INTERNAL_API_KEY_SSM_PARAM ??
  "/web-change-tracker/prod/chatkit_internal_api_key";

let cached: string | null = null;

export async function getChatkitApiKey(): Promise<string | null> {
  if (process.env.CHATKIT_INTERNAL_API_KEY) return process.env.CHATKIT_INTERNAL_API_KEY;
  if (cached) return cached;
  try {
    const client = new SSMClient({ region: process.env.AWS_REGION ?? "us-east-1" });
    const resp = await client.send(
      new GetParameterCommand({ Name: SSM_PARAM, WithDecryption: true })
    );
    cached = resp.Parameter?.Value ?? null;
    return cached;
  } catch {
    return null;
  }
}
