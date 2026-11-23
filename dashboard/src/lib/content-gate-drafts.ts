/**
 * Content Gate draft storage — autosaved snapshots of the modal's edit state.
 *
 * Storage: per-row S3 objects at `content_gate_drafts/<agent_call_id>/<slug>.json`.
 * The slug is a 16-char SHA256 of the sibling's `library_item_url`, or the
 * literal `__no_url__` for siblings with no URL (e.g. "Updated Meeting" rows).
 *
 * Why not patch the alert row directly?
 * `patch-jsonl.ts` downloads/patches/reuploads and is not atomic — a 3-second
 * debounced autosave across many open modals would race with every other write
 * to `alerts_table.jsonl`. Per-row S3 objects are atomic via PutObject.
 */

import { S3Client, GetObjectCommand, PutObjectCommand, DeleteObjectCommand, ListObjectsV2Command } from "@aws-sdk/client-s3";
import { createHash } from "crypto";

const BUCKET = process.env.BUBBLE_ARTIFACT_BUCKET ?? "";
const PREFIX = "content_gate_drafts/";

let s3: S3Client | null = null;
function getS3() {
  if (!s3) s3 = new S3Client({ region: process.env.AWS_REGION ?? "us-east-1" });
  return s3;
}

// Shape of the serialized draft. Mirrors the modal's editable state.
export type ContentGateDraft = {
  version: 1;
  updated_at: string;              // ISO 8601
  lib_edits: Record<string, string>;
  event_edits: Record<string, string>;
  agenda_item_edits: Record<string, Record<string, string>>;  // index → field edits
  deleted_new_indices: number[];
  deleted_existing_indices: number[];
};

function slugForUrl(libraryItemUrl: string | null | undefined): string {
  const url = (libraryItemUrl ?? "").trim();
  if (!url || url === "N/A") return "__no_url__";
  return createHash("sha256").update(url).digest("hex").slice(0, 16);
}

export function draftKey(agentCallId: string, libraryItemUrl: string | null | undefined): string {
  return `${PREFIX}${agentCallId}/${slugForUrl(libraryItemUrl)}.json`;
}

export async function getDraft(agentCallId: string, libraryItemUrl: string | null | undefined): Promise<ContentGateDraft | null> {
  try {
    const resp = await getS3().send(new GetObjectCommand({ Bucket: BUCKET, Key: draftKey(agentCallId, libraryItemUrl) }));
    const text = (await resp.Body?.transformToString()) ?? "";
    if (!text) return null;
    return JSON.parse(text) as ContentGateDraft;
  } catch (err: unknown) {
    const code = (err as { name?: string; Code?: string }).name ?? (err as { Code?: string }).Code ?? "";
    if (code === "NoSuchKey" || code === "NotFound") return null;
    throw err;
  }
}

export async function putDraft(
  agentCallId: string,
  libraryItemUrl: string | null | undefined,
  draft: Omit<ContentGateDraft, "version" | "updated_at">,
): Promise<void> {
  const payload: ContentGateDraft = {
    version: 1,
    updated_at: new Date().toISOString(),
    ...draft,
  };
  await getS3().send(new PutObjectCommand({
    Bucket: BUCKET,
    Key: draftKey(agentCallId, libraryItemUrl),
    Body: JSON.stringify(payload),
    ContentType: "application/json",
  }));
  bustDraftsListCache();
}

export async function deleteDraft(agentCallId: string, libraryItemUrl: string | null | undefined): Promise<void> {
  try {
    await getS3().send(new DeleteObjectCommand({ Bucket: BUCKET, Key: draftKey(agentCallId, libraryItemUrl) }));
  } catch (err: unknown) {
    const code = (err as { name?: string }).name ?? "";
    if (code !== "NoSuchKey" && code !== "NotFound") throw err;
  }
  bustDraftsListCache();
}

// ─── Drafts-list cache ────────────────────────────────────────────────────
// `/api/content-gate/drafts` runs on every AlertsTable mount. ListObjectsV2
// can be slow on a prefix with thousands of objects, so cache for 30s.

const LIST_CACHE_TTL = 30_000;
let listCache: { ids: Set<string>; ts: number } | null = null;

export function bustDraftsListCache() {
  listCache = null;
}

export async function listDraftCallIds(): Promise<Set<string>> {
  if (listCache && Date.now() - listCache.ts < LIST_CACHE_TTL) {
    return listCache.ids;
  }
  const ids = new Set<string>();
  let continuationToken: string | undefined;
  do {
    const resp = await getS3().send(new ListObjectsV2Command({
      Bucket: BUCKET,
      Prefix: PREFIX,
      ContinuationToken: continuationToken,
    }));
    for (const obj of resp.Contents ?? []) {
      // Key shape: content_gate_drafts/<call_id>/<slug>.json
      const key = obj.Key ?? "";
      const rest = key.slice(PREFIX.length);
      const slash = rest.indexOf("/");
      if (slash > 0) ids.add(rest.slice(0, slash));
    }
    continuationToken = resp.IsTruncated ? resp.NextContinuationToken : undefined;
  } while (continuationToken);
  listCache = { ids, ts: Date.now() };
  return ids;
}
