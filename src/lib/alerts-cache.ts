/**
 * Shared in-memory cache for alerts table rows.
 * Lives in a module so both the alerts route and the accept route
 * can read/write/bust the same cache instance within the same process.
 */

export type AlertRow = Record<string, unknown>;

export const CACHE_TTL = 60_000; // 60s

let cache: { rows: AlertRow[]; ts: number } | null = null;

export function getAlertsCache() {
  return cache;
}

export function setAlertsCache(rows: AlertRow[]) {
  cache = { rows, ts: Date.now() };
}

export function bustAlertsCache() {
  cache = null;
}
