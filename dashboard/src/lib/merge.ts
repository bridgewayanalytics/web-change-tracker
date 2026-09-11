/**
 * Merge logic for upcoming calendar items.
 * Combines Bubble resources + S3 report resources with deterministic rules.
 */

import type { CalendarItem, Resource } from "./bubble";
import type { BubbleReportResource } from "./s3";

// --- Output types ---

/** S3 resource payload (from bubble_reports JSON) */
export type ResourcePayload = BubbleReportResource;

export interface UpcomingViewModel {
  calendarItem: CalendarItem;
  resources: {
    bubble: Resource[];
    s3Candidates: ResourcePayload[];
  };
  flags: {
    missingInBubble: ResourcePayload[];
    counts: {
      bubble: number;
      s3Candidates: number;
      missingInBubble: number;
    };
  };
}

// --- Helpers (exported for tests) ---

export function getResourceUrl(r: { URL?: string; url?: string }): string {
  const url = r.URL ?? r.url;
  return (url ?? "").toString().trim();
}

/** Normalize entry: string→as-is, object with _id/id→that value */
function normalizeRelatedEntry(
  x: string | { _id?: string; id?: string } | unknown
): string {
  if (typeof x === "string" && x.trim()) return x.trim();
  if (x != null && typeof x === "object") {
    const id = (x as { _id?: string; id?: string })._id ??
      (x as { _id?: string; id?: string }).id;
    return id != null ? String(id).trim() : "";
  }
  return "";
}

export function getRelatedCalendarIds(
  r: {
    "Related calendar items"?: string | string[] | Array<{ _id?: string; id?: string }>;
  }
): string[] {
  const rel = r["Related calendar items"];
  if (Array.isArray(rel)) {
    return rel.map(normalizeRelatedEntry).filter(Boolean);
  }
  if (typeof rel === "string" && rel) {
    return [rel.trim()];
  }
  return [];
}

export function getMeetingMetaDate(
  r: { __meeting_meta?: { date_iso?: string } }
): string | undefined {
  return r.__meeting_meta?.date_iso;
}

export function normalizeDate(d: string | undefined): string | undefined {
  if (!d) return undefined;
  return String(d).trim().slice(0, 10) || undefined;
}

/** Primary: Related calendar items contains calendar item _id */
export function isLinkedById(
  resource: {
    "Related calendar items"?: string | string[] | Array<{ _id?: string; id?: string }>;
  },
  calendarId: string
): boolean {
  return getRelatedCalendarIds(resource).includes(calendarId);
}

/** Secondary: __meeting_meta.date_iso matches calendarItem.date */
export function isLinkedByDate(
  resource: { __meeting_meta?: { date_iso?: string } },
  calendarDate: string | undefined
): boolean {
  const calNorm = normalizeDate(calendarDate);
  const metaDate = getMeetingMetaDate(resource);
  return !!calNorm && !!metaDate && normalizeDate(metaDate) === calNorm;
}

/** S3 resource links to calendar via primary OR secondary rule */
export function isS3CandidateForCalendar(
  s3Resource: BubbleReportResource,
  calendarId: string,
  calendarDate: string | undefined
): boolean {
  return (
    isLinkedById(s3Resource, calendarId) ||
    isLinkedByDate(s3Resource, calendarDate)
  );
}

/** Build set of URLs present in Bubble resources (across all calendar items) */
export function buildBubbleUrlSet(
  bubbleResourcesByCalendar: Record<string, Resource[]>,
  agendaResourcesByCalendar?: Record<string, Resource[]>
): Set<string> {
  const urls = new Set<string>();
  const addFrom = (list: Resource[]) => {
    for (const r of list) {
      const url = getResourceUrl(r);
      if (url) urls.add(url);
    }
  };
  for (const list of Object.values(bubbleResourcesByCalendar)) {
    addFrom(list);
  }
  if (agendaResourcesByCalendar) {
    for (const list of Object.values(agendaResourcesByCalendar)) {
      addFrom(list);
    }
  }
  return urls;
}

export interface MaterialLinkInput {
  name: string;
  url: string;
  source: "bubble" | "s3";
  missingInBubble: boolean;
  isAgenda: boolean;
}

export interface MaterialLinkOutput {
  name: string;
  url: string;
  source: "bubble" | "s3";
  missingInBubble: boolean;
}

/**
 * Merge agenda materials and linked materials. Dedupe by URL (first wins).
 * Order: agenda first, then by name. Uses each resource's actual name (no abbreviation).
 */
export function mergeAndDedupeMaterials(
  agendaMaterials: MaterialLinkInput[],
  linkedMaterials: MaterialLinkInput[]
): MaterialLinkOutput[] {
  const combined = [...agendaMaterials, ...linkedMaterials];
  const byUrl = new Map<string, MaterialLinkInput>();
  for (const m of combined) {
    if (m.url && !byUrl.has(m.url)) {
      byUrl.set(m.url, m);
    }
  }
  const merged = Array.from(byUrl.values());
  merged.sort((a, b) => {
    if (a.isAgenda !== b.isAgenda) return a.isAgenda ? -1 : 1;
    return a.name.localeCompare(b.name);
  });
  return merged.map((m) => ({
    name: m.name,
    url: m.url,
    source: m.source,
    missingInBubble: m.missingInBubble,
  }));
}

/** Merge S3 resources from latest + recent runs, dedup by URL (first wins) */
export function mergeS3Resources(
  latestResources: ResourcePayload[],
  recentRunResources: ResourcePayload[][] = []
): ResourcePayload[] {
  const byUrl = new Map<string, ResourcePayload>();
  for (const r of latestResources) {
    const url = getResourceUrl(r);
    if (url) byUrl.set(url, r);
  }
  for (const list of recentRunResources) {
    for (const r of list) {
      const url = getResourceUrl(r);
      if (url && !byUrl.has(url)) byUrl.set(url, r);
    }
  }
  return Array.from(byUrl.values());
}

// --- Main merge ---

/**
 * Build UpcomingViewModel for a single calendar item.
 * Deterministic: same inputs → same output.
 */
export function mergeForCalendarItem(
  calendarItem: CalendarItem,
  bubbleResources: Resource[],
  s3CandidatesForThisCalendar: ResourcePayload[],
  bubbleUrls: Set<string>
): UpcomingViewModel {
  const calId = calendarItem._id;
  const calDate = calendarItem.date;

  // Bubble resources: already linked, dedup by URL
  const bubbleByUrl = new Map<string, Resource>();
  for (const r of bubbleResources) {
    const url = getResourceUrl(r);
    if (url) bubbleByUrl.set(url, r);
  }

  // S3 candidates: linked by id or date, exclude URLs already in Bubble
  const s3Candidates: ResourcePayload[] = [];
  const missingInBubble: ResourcePayload[] = [];

  for (const r of s3CandidatesForThisCalendar) {
    const url = getResourceUrl(r);
    if (!url) continue;
    if (bubbleByUrl.has(url)) continue; // Bubble wins, skip
    s3Candidates.push(r);
    if (!bubbleUrls.has(url)) {
      missingInBubble.push(r);
    }
  }

  return {
    calendarItem,
    resources: {
      bubble: Array.from(bubbleByUrl.values()),
      s3Candidates,
    },
    flags: {
      missingInBubble,
      counts: {
        bubble: bubbleByUrl.size,
        s3Candidates: s3Candidates.length,
        missingInBubble: missingInBubble.length,
      },
    },
  };
}

/**
 * Build full UpcomingViewModel[] from all inputs.
 * bubbleResourcesByCalendar = linked resources only (from "Related calendar items").
 * agendaResourcesByCalendar = agenda resources (from Calendar Item "Agenda" ids) - used for bubbleUrls only.
 */
export function mergeUpcoming(
  calendarItems: CalendarItem[],
  bubbleResourcesByCalendar: Record<string, Resource[]>,
  s3Resources: ResourcePayload[],
  recentRunResources: ResourcePayload[][] = [],
  agendaResourcesByCalendar?: Record<string, Resource[]>
): UpcomingViewModel[] {
  const allS3 = mergeS3Resources(s3Resources, recentRunResources);
  const bubbleUrls = buildBubbleUrlSet(
    bubbleResourcesByCalendar,
    agendaResourcesByCalendar
  );

  const result: UpcomingViewModel[] = [];

  for (const item of calendarItems) {
    const calId = item._id;
    const calDate = item.date;
    const bubbleResources = bubbleResourcesByCalendar[calId] ?? [];

    const s3CandidatesForThis = allS3.filter((r) =>
      isS3CandidateForCalendar(r, calId, calDate)
    );

    result.push(
      mergeForCalendarItem(
        item,
        bubbleResources,
        s3CandidatesForThis,
        bubbleUrls
      )
    );
  }

  return result;
}
