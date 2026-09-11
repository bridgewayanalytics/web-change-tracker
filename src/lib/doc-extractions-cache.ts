/**
 * Shared in-memory cache for document extractions table rows.
 * Lives in a module so both the document-extractions route and the doc-rerun/accept route
 * can read/write/bust the same cache instance within the same process.
 */

export type DocExtractionRow = Record<string, unknown>;

export const CACHE_TTL = 60_000; // 60s

let cache: { rows: DocExtractionRow[]; ts: number } | null = null;

export function getDocExtractionsCache() {
  return cache;
}

export function setDocExtractionsCache(rows: DocExtractionRow[]) {
  cache = { rows, ts: Date.now() };
}

export function bustDocExtractionsCache() {
  cache = null;
}
