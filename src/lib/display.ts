/**
 * Pure display helpers for calendar items.
 * No backend behavior; UI formatting only.
 */

export interface CalendarItemLike {
  title?: string;
  "NAIC Group (tree node)"?: string | { name?: string; Name?: string };
  "NAIC Group"?: string;
  Organization?: string;
  "Meeting Type"?: string;
  "Meeting type"?: string;
  Type?: string;
  [key: string]: unknown;
}

/**
 * Derive display group name from calendar item.
 * Prefer dedicated group fields; else parse title to avoid topic concatenations.
 */
export function getDisplayGroupName(calendarItem: CalendarItemLike): string {
  const item = calendarItem ?? {};

  const naicGroup = item["NAIC Group (tree node)"];
  if (naicGroup != null) {
    if (typeof naicGroup === "object") {
      const name = (naicGroup as { name?: string; Name?: string }).name ??
        (naicGroup as { name?: string; Name?: string }).Name;
      if (name && String(name).trim()) return String(name).trim();
    }
  }

  const naicGroupStr = item["NAIC Group"];
  if (naicGroupStr && String(naicGroupStr).trim()) {
    return String(naicGroupStr).trim();
  }

  const org = item.Organization;
  if (org != null) {
    if (typeof org === "object") {
      const name = (org as { name?: string; Name?: string }).name ??
        (org as { name?: string; Name?: string }).Name;
      if (name && String(name).trim()) return String(name).trim();
    } else if (String(org).trim()) {
      return String(org).trim();
    }
  }

  const title = (item.title ?? "").toString().trim();
  if (!title) return "Untitled";

  if (title.includes(" | ")) {
    const left = title.split(" | ")[0]?.trim();
    if (left) return left;
  }

  if (title.includes(";")) {
    const beforeSemicolon = title.split(";")[0]?.trim();
    if (beforeSemicolon) return beforeSemicolon;
  }

  return title;
}

/**
 * Get optional meeting type for display.
 */
export function getMeetingType(calendarItem: CalendarItemLike): string | undefined {
  const item = calendarItem ?? {};
  const type =
    item["Meeting Type"] ??
    item["Meeting type"] ??
    item.Type;
  if (type != null && String(type).trim()) return String(type).trim();
  return undefined;
}

/**
 * Build display title: "<NAIC_GROUP_NAME> | <Meeting Type (optional)>"
 */
export function getDisplayTitle(calendarItem: CalendarItemLike): string {
  const group = getDisplayGroupName(calendarItem);
  const meetingType = getMeetingType(calendarItem);
  if (meetingType) return `${group} | ${meetingType}`;
  return group;
}

/**
 * Convert group display name to short form for table: "NAIC <short_code>".
 * Examples: "NAIC Big Working Group" -> "NAIC BWG", "NAIC BWG" -> "NAIC BWG".
 */
export function getGroupShortDisplayName(displayName: string | undefined): string {
  if (!displayName || !String(displayName).trim()) return "—";
  const s = String(displayName).trim();
  const naicPrefix = "NAIC ";
  const afterNaic = s.toLowerCase().startsWith(naicPrefix.toLowerCase())
    ? s.slice(naicPrefix.length).trim()
    : s;
  if (!afterNaic) return "NAIC";
  const words = afterNaic.split(/\s+/).filter(Boolean);
  if (words.length === 1 && words[0].length <= 8 && /^[A-Za-z0-9]+$/.test(words[0])) {
    return `NAIC ${words[0].toUpperCase()}`;
  }
  const acronym = words.map((w) => w[0]?.toUpperCase() ?? "").join("");
  if (acronym) return `NAIC ${acronym}`;
  return s.startsWith("NAIC") ? s : `NAIC ${s}`;
}

/**
 * Format time range in ET (America/New_York), 12-hour, no seconds.
 * Returns e.g. "1:00 – 2:30 ET" or "1:00 ET" if no end.
 * Returns "" if start has no time component (date-only) or is invalid.
 */
export function formatTimeRangeET(
  startISO: string | null | undefined,
  endISO: string | null | undefined
): string {
  const start = (startISO ?? "").trim();
  if (!start || !start.includes("T")) return "";

  const startDate = new Date(start);
  if (isNaN(startDate.getTime())) return "";

  const opts: Intl.DateTimeFormatOptions = {
    timeZone: "America/New_York",
    hour12: true,
    hour: "numeric",
    minute: "2-digit",
  };

  const startStr = startDate.toLocaleTimeString("en-US", opts);

  const end = (endISO ?? "").trim();
  if (!end || !end.includes("T")) return `${startStr} ET`;

  const endDate = new Date(end);
  if (isNaN(endDate.getTime())) return `${startStr} ET`;

  const endStr = endDate.toLocaleTimeString("en-US", opts);
  if (startStr === endStr) return `${startStr} ET`;

  return `${startStr} – ${endStr} ET`;
}

/**
 * Format date as YYYY-MM-DD only (no time, no timezone).
 * Handles ISO strings like "2026-03-05T17:00:00.000Z".
 */
export function formatDateOnly(dateValue: string | Date | undefined): string {
  if (dateValue == null || String(dateValue).trim() === "") return "";
  const date =
    dateValue instanceof Date ? dateValue : new Date(String(dateValue).trim());
  if (isNaN(date.getTime())) return "";
  return date.toISOString().slice(0, 10);
}

/**
 * Parse combined "phone_number_and_access_code" into separate parts.
 * Handles formats like "+1-415-655-0003,,23483040251##" or "phone, access".
 */
export function parsePhoneAndAccessCode(
  s: string | null | undefined
): { phone: string; accessCode: string } {
  const raw = (s ?? "").toString().trim();
  if (!raw) return { phone: "", accessCode: "" };
  const parts = raw.split(/,,+|,\s*/).map((p) => p.trim()).filter(Boolean);
  if (parts.length >= 2) {
    const phone = cleanDisplayText(parts[0]);
    let accessCode = cleanDisplayText(parts[1]).replace(/#+$/, "").trim();
    return { phone, accessCode };
  }
  return { phone: cleanDisplayText(raw), accessCode: "" };
}

/**
 * Strip BBCode-like tags and normalize whitespace for display.
 * Removes [b], [/b], [i], [/i], [u], [/u]; collapses repeated whitespace; trims.
 */
export function cleanDisplayText(s: string | null | undefined): string {
  if (s == null || typeof s !== "string") return "";
  return s
    .replace(/\[\/?(b|i|u)\]/gi, "")
    .replace(/\s+/g, " ")
    .trim();
}

/**
 * Format date as "Thursday, March 05, 2026".
 */
export function formatMeetingDate(date: string | undefined): string {
  if (!date || !String(date).trim()) return "—";
  try {
    const s = String(date).trim().slice(0, 10);
    const [y, mo, day] = s.split("-").map(Number);
    if (!y || !mo || !day) return "—";
    const dateObj = new Date(y, mo - 1, day);
    if (isNaN(dateObj.getTime())) return "—";
    return dateObj.toLocaleDateString("en-US", {
      weekday: "long",
      year: "numeric",
      month: "long",
      day: "2-digit",
    });
  } catch {
    return "—";
  }
}
