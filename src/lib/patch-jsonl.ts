import { S3Client, GetObjectCommand, PutObjectCommand, NoSuchKey } from "@aws-sdk/client-s3";

/**
 * Patch rows in a JSONL file in S3 where all matchFields match.
 * Downloads the file, updates matching rows in-place, re-uploads.
 * Returns the number of rows patched.
 */
export async function patchJsonlRows(
  s3: S3Client,
  bucket: string,
  key: string,
  matchFields: Record<string, unknown>,
  updateFields: Record<string, unknown>,
): Promise<number> {
  const resp = await s3.send(new GetObjectCommand({ Bucket: bucket, Key: key }));
  const body = (await resp.Body?.transformToString()) ?? "";

  let patched = 0;
  const lines = body
    .split("\n")
    .filter(Boolean)
    .map((line) => {
      let row: Record<string, unknown>;
      try {
        row = JSON.parse(line) as Record<string, unknown>;
      } catch {
        return line;
      }
      if (Object.entries(matchFields).every(([k, v]) => row[k] === v)) {
        Object.assign(row, updateFields);
        patched++;
      }
      return JSON.stringify(row);
    });

  await s3.send(
    new PutObjectCommand({
      Bucket: bucket,
      Key: key,
      Body: lines.join("\n"),
      ContentType: "application/x-ndjson",
    })
  );

  return patched;
}

/**
 * Append a new row to a JSONL file in S3.
 * Creates the file if it doesn't exist yet.
 */
export async function appendJsonlRow(
  s3: S3Client,
  bucket: string,
  key: string,
  row: Record<string, unknown>,
): Promise<void> {
  let existing = "";
  try {
    const resp = await s3.send(new GetObjectCommand({ Bucket: bucket, Key: key }));
    existing = (await resp.Body?.transformToString()) ?? "";
  } catch (err) {
    if (!(err instanceof NoSuchKey)) throw err;
  }

  const newLine = JSON.stringify(row);
  const body = existing.trimEnd()
    ? `${existing.trimEnd()}\n${newLine}`
    : newLine;

  await s3.send(
    new PutObjectCommand({
      Bucket: bucket,
      Key: key,
      Body: body,
      ContentType: "application/x-ndjson",
    })
  );
}
