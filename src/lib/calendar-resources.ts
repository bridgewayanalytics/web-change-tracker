/**
 * Classify and deduplicate resources for calendar item display.
 * Agenda vs related, dedupe by URL, track source and missingInBubble.
 */

export interface ResourceLike {
  Name?: string;
  URL?: string;
  [k: string]: unknown;
}

export interface ResourceLink {
  name: string;
  url: string;
  source: "bubble" | "s3";
  missingInBubble: boolean;
}

export interface ClassifiedResources {
  agendas: ResourceLink[];
  related: ResourceLink[];
}

function getUrl(r: ResourceLike): string {
  return (r.URL ?? (r as { url?: string }).url ?? "").toString().trim();
}

const GENERIC_NAMES = new Set([
  "agenda",
  "agenda & materials",
  "agenda and materials",
  "materials",
  "document",
  "file",
]);

function nameFromUrl(url: string): string {
  try {
    const path = new URL(url).pathname;
    const basename = path.split("/").filter(Boolean).pop() ?? path;
    const decoded = decodeURIComponent(basename);
    // Strip file extension for cleaner display
    return decoded.replace(/\.[^.]+$/, "").replace(/[_-]/g, " ") || decoded || url;
  } catch {
    return url;
  }
}

function getName(r: ResourceLike): string {
  const name = (r.Name ?? (r as { name?: string }).name ?? "").toString().trim();
  const url = getUrl(r);
  if (name && !GENERIC_NAMES.has(name.toLowerCase())) return name;
  if (!url) return name || "—";
  return nameFromUrl(url);
}

/**
 * Heuristic: resource suggests agenda if document title/name or URL contains "agenda".
 * Checks Name, name, title, Title and URL.
 */
export function isAgenda(resource: ResourceLike): boolean {
  const title = (
    resource.Name ??
    (resource as { name?: string }).name ??
    (resource as { title?: string }).title ??
    (resource as { Title?: string }).Title ??
    ""
  ).toString();
  const url = (resource.URL ?? (resource as { url?: string }).url ?? "").toString();
  const lower = title.toLowerCase();
  if (lower.includes("agenda")) return true;
  if (url.toLowerCase().includes("agenda")) return true;
  return false;
}

/**
 * Build ResourceLink from resource with source and missingInBubble.
 */
function toResourceLink(
  r: ResourceLike,
  source: "bubble" | "s3",
  missingInBubble: boolean
): ResourceLink {
  return {
    name: getName(r),
    url: getUrl(r),
    source,
    missingInBubble,
  };
}

/**
 * Classify merged resources into agendas and related (non-agendas).
 * Deduplicate by URL; Bubble wins. Preserve order (Bubble first, then S3).
 */
export function classifyResourcesForCalendarItem(
  bubbleResources: ResourceLike[],
  s3Candidates: ResourceLike[],
  missingInBubbleUrls: Set<string>
): ClassifiedResources {
  const seenUrls = new Set<string>();
  const agendas: ResourceLink[] = [];
  const related: ResourceLink[] = [];

  const process = (
    r: ResourceLike,
    source: "bubble" | "s3"
  ) => {
    const url = getUrl(r);
    if (!url) return;
    if (seenUrls.has(url)) return;
    seenUrls.add(url);
    const missing = missingInBubbleUrls.has(url);
    const link = toResourceLink(r, source, missing);
    if (isAgenda(r)) {
      agendas.push(link);
    } else {
      related.push(link);
    }
  };

  for (const r of bubbleResources) {
    process(r, "bubble");
  }
  for (const r of s3Candidates) {
    process(r, "s3");
  }

  return { agendas, related };
}
