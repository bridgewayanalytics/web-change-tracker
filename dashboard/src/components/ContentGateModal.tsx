"use client";

import React, { useEffect, useState, useMemo, useRef } from "react";
import type { AlertRow } from "@/app/alerts/AlertsTable";
import { SearchableCombobox, type BubbleOption } from "@/components/SearchableCombobox";

type BubbleOptionsType = "organization" | "chronicletopic" | "libraryitemtype" | "calendaritemtype";
type BubbleOptionsMap = Record<BubbleOptionsType, BubbleOption[] | undefined>;

interface ContentGateModalProps {
  row: AlertRow;
  onClose: () => void;
  onFieldPatched: (fields: Partial<AlertRow>) => void;
}

type SectionStatus = "idle" | "loading" | "done" | "error";

interface ValidationIssue {
  field: string;
  message: string;
  unresolved: string[];
  severity: "error" | "warning";
}

interface ValidationResult {
  valid: boolean;
  issues: ValidationIssue[];
}

function ValidationBanner({ result }: { result: ValidationResult | null | "loading" }) {
  if (!result || result === "loading" || result.issues.length === 0) return null;
  const errors   = result.issues.filter(i => i.severity === "error");
  const warnings = result.issues.filter(i => i.severity === "warning");
  return (
    <div className="rounded-lg border border-gray-200 overflow-hidden text-[11px]">
      <div className="px-3 py-2 bg-gray-50 border-b border-gray-200 flex items-center gap-1.5">
        <span className="font-semibold text-gray-600 uppercase tracking-wider text-[10px]">Pre-flight validation</span>
        {errors.length > 0 && (
          <span className="ml-auto bg-red-100 text-red-700 rounded px-1.5 py-0.5 font-medium">
            {errors.length} error{errors.length > 1 ? "s" : ""}
          </span>
        )}
        {warnings.length > 0 && (
          <span className={`${errors.length ? "" : "ml-auto"} bg-amber-100 text-amber-700 rounded px-1.5 py-0.5 font-medium`}>
            {warnings.length} warning{warnings.length > 1 ? "s" : ""}
          </span>
        )}
      </div>
      <div className="divide-y divide-gray-100">
        {result.issues.map((issue, i) => (
          <div key={i} className={`px-3 py-2 ${issue.severity === "error" ? "bg-red-50" : "bg-amber-50"}`}>
            <div className="flex items-start gap-1.5">
              <span className={`mt-0.5 flex-shrink-0 ${issue.severity === "error" ? "text-red-500" : "text-amber-500"}`}>
                {issue.severity === "error" ? "✕" : "⚠"}
              </span>
              <div className="min-w-0">
                <span className={`font-semibold ${issue.severity === "error" ? "text-red-700" : "text-amber-700"}`}>
                  {issue.field}:{" "}
                </span>
                <span className={issue.severity === "error" ? "text-red-600" : "text-amber-600"}>
                  {issue.message}
                </span>
                {issue.unresolved.length > 0 && (
                  <div className="mt-1 space-y-0.5">
                    {issue.unresolved.map(name => (
                      <div key={name} className={`font-mono text-[10px] truncate ${issue.severity === "error" ? "text-red-500" : "text-amber-500"}`}>
                        → {name}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

type ValidationPopup = {
  section: string;
  message: string;
  onConfirm: () => void;
};
type BubbleAction = "create" | "update" | null;

interface BubbleRecord {
  found: boolean;
  id?: string;
  fields?: Record<string, string | string[]>;
  error?: string;
}

interface SectionState {
  status: SectionStatus;
  error?: string;
}

interface AgendaItemPreview {
  title: string;
  status?: string;  // "New" → create in Bubble; "Existing"/"Updated" → look up and link
  chronicle_topics?: string[];
  official_title?: string;
  reference_id?: string;
}

// A structured display field
interface FieldEntry {
  label: string;        // Bubble field name (what the sync writes to)
  agentField?: string;  // Agent output field key — resolved to its human-readable label at render time
  value?: string;       // plain text value
  topics?: string[];    // rendered as chips (or combobox selections when combobox is set)
  list?: string[];      // rendered as a numbered list
  changed?: boolean;    // highlight row amber for UPDATE
  readOnly?: boolean;   // no input shown
  // When set, renders a searchable dropdown of pre-existing Bubble values instead of free-text.
  combobox?: {
    type: BubbleOptionsType;
    mode: "single" | "multi";  // multi: edit `topics`; single: edit `value`
  };
}

// Fallback labels for internal/derived fields that aren't in the agent's output schema
// (e.g. doc extraction fields merged in, derived values).
const AGENT_FIELD_LABEL_FALLBACKS: Record<string, string> = {
  "alert_type (derived)":              "Alert Type (derived)",
  "alert_type (classifier)":           "Alert Type (classifier)",
  "doc_extraction.date_published":     "Document Publication Date",
  "doc_extraction.description":        "Document Description",
  "doc_extraction.document_type":      "Document Type",
  "transcript_s3_key":                 "Transcript File",
  "library_item_preliminary_title.title": "Library Item Preliminary Title",
  "agenda_item_title_chronicle_topics[].chronicle_topics": "Agenda Item Chronicle Topics",
  "agenda_item_title_chronicle_topics[].agenda_item_title": "Agenda Item Title",
  "agenda_item_title_official[].official_title":           "Agenda Item Official Title",
  "agenda_item_standardized_id[].standardized_id":         "Agenda Item Reference ID",
};

function resolveAgentFieldLabel(
  agentField: string | undefined,
  schemaLabels: Record<string, string> | null,
): string {
  if (!agentField || agentField === "—") return "—";
  // Try schema labels first (strip array/dot suffix for lookup)
  if (schemaLabels) {
    const base = agentField.replace(/\[.*?\]|\..*$/g, "");
    if (schemaLabels[base]) return schemaLabels[base];
    if (schemaLabels[agentField]) return schemaLabels[agentField];
  }
  // Fall back to known internal/derived labels
  if (AGENT_FIELD_LABEL_FALLBACKS[agentField]) return AGENT_FIELD_LABEL_FALLBACKS[agentField];
  // Final fallback: title-case the field name
  return agentField
    .replace(/\[.*?\]/g, "")
    .replace(/[._]/g, " ")
    .replace(/\b\w/g, c => c.toUpperCase());
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function safeStr(v: unknown): string {
  if (v == null) return "";
  if (Array.isArray(v)) return (v as unknown[]).filter(Boolean).join(", ");
  return String(v).trim();
}

const NA_SET = new Set(["n/a", "n/a.", "-", "none", ""]);
function notNA(s: string): boolean {
  return !NA_SET.has(s.toLowerCase().trim());
}

const STATUS_PREFIXES = /^(New|Existing|Updated|Revised):\s*/i;
function stripStatusPrefix(s: string): string {
  return s.replace(STATUS_PREFIXES, "").trim();
}

// Multi-select combobox values are stored in editState as "|"-joined strings so the
// edit-state map can stay Record<string, string>. These helpers convert both ways.
const MULTI_SEP = ""; // non-visible delimiter — won't collide with option names
function multiToEditValue(names: string[]): string { return names.join(MULTI_SEP); }
function multiFromEditValue(v: string | undefined): string[] | null {
  if (v == null) return null;
  return v === "" ? [] : v.split(MULTI_SEP);
}

function topicsFromFieldIds(fieldIds: unknown): string[] {
  if (!fieldIds || typeof fieldIds !== "object") return [];
  const fids = fieldIds as Record<string, unknown>;
  // Check new display-name key first, fall back to old underscore key
  const raw = fids["Topics - dt"] ?? fids["topics___dt_list_custom_newsreel_update"];
  if (!Array.isArray(raw)) return [];
  return (raw as unknown[]).map(t => String(t).trim()).filter(t => t && notNA(t));
}

function allLibraryItemTopics(
  lp: Record<string, unknown>,
  agendaItems: AgendaItemPreview[],
): string[] {
  const docTopics = topicsFromFieldIds(lp.field_ids);
  const agendaTopics: string[] = [];
  for (const item of agendaItems) {
    for (const t of item.chronicle_topics ?? []) {
      if (t && notNA(t) && !agendaTopics.includes(t)) agendaTopics.push(t);
    }
  }
  const seen = new Set(docTopics);
  const extra = agendaTopics.filter(t => !seen.has(t));
  return [...docTopics, ...extra];
}

// Enrich agenda item previews with live doc extraction data.
// Used for display only — the sync/patch paths use the un-enriched bubble_action data.
//
// Matching strategy:
//   1. Title match: doc extraction agenda_item_title (stripped) vs preview title (stripped).
//      Fails when the two agents use different phrasings for the same item.
//   2. Positional fallback: if title matching produces 0 hits, match by index order.
//      Doc rows sorted by agenda_number; row[i] → preview[i].
//
// Doc extraction rows use this structure (field name: agenda_item_bridgeway_title_chronicle_topic):
//   [{ agenda_item_title: "...", chronicle_topics: ["..."], status: "New|Existing" }, ...]
// Official titles: agenda_item_title_official: [{ official_title: "...", status: "..." }, ...]
// Reference IDs:   agenda_item_standardized_id: [{ standardized_id: "...", status: "..." }, ...]
function enrichFromDocExtraction(
  items: AgendaItemPreview[],
  docRows: Array<Record<string, unknown>>,
): AgendaItemPreview[] {
  if (!docRows.length) return items;

  type DocEntry = { chronicle_topics: string[]; official_title: string; reference_id: string };

  // Sort rows by agenda_number so positional matching is deterministic
  const sorted = [...docRows].sort(
    (a, b) => Number(a.agenda_number ?? 999) - Number(b.agenda_number ?? 999),
  );

  function extractEntries(rows: Array<Record<string, unknown>>): DocEntry[] {
    const entries: DocEntry[] = [];
    for (const dr of rows) {
      const bridgewayItems = (
        dr.agenda_item_bridgeway_title_chronicle_topic ??
        dr.agenda_item_title_chronicle_topic
      ) as Array<Record<string, unknown>> | undefined;
      const officialArr     = (dr.agenda_item_title_official    as Array<Record<string, unknown>> | undefined) ?? [];
      const standardizedArr = (dr.agenda_item_standardized_id  as Array<Record<string, unknown>> | undefined) ?? [];
      if (!Array.isArray(bridgewayItems)) continue;
      bridgewayItems.forEach((entry, i) => {
        const docTopics = Array.isArray(entry.chronicle_topics)
          ? (entry.chronicle_topics as unknown[]).map(String).filter(t => t && notNA(t))
          : [];
        const offTitle = String(officialArr[i]?.official_title ?? "").trim();
        const refId    = String(standardizedArr[i]?.standardized_id ?? "").trim();
        entries.push({ chronicle_topics: docTopics, official_title: offTitle, reference_id: refId });
      });
    }
    return entries;
  }

  function applyEntry(item: AgendaItemPreview, data: DocEntry): AgendaItemPreview {
    return {
      ...item,
      ...(data.chronicle_topics.length > 0                 ? { chronicle_topics: data.chronicle_topics } : {}),
      ...(data.official_title && notNA(data.official_title) ? { official_title: data.official_title }   : {}),
      ...(data.reference_id   && notNA(data.reference_id)   ? { reference_id: data.reference_id }       : {}),
    };
  }

  // Build title → entry map for title-based matching
  const byTitle = new Map<string, DocEntry>();
  const positional = extractEntries(sorted);
  for (const dr of sorted) {
    const bridgewayItems = (
      dr.agenda_item_bridgeway_title_chronicle_topic ??
      dr.agenda_item_title_chronicle_topic
    ) as Array<Record<string, unknown>> | undefined;
    const officialArr     = (dr.agenda_item_title_official    as Array<Record<string, unknown>> | undefined) ?? [];
    const standardizedArr = (dr.agenda_item_standardized_id  as Array<Record<string, unknown>> | undefined) ?? [];
    if (!Array.isArray(bridgewayItems)) continue;
    bridgewayItems.forEach((entry, i) => {
      const titleRaw = String(entry.agenda_item_title ?? "").trim();
      if (!titleRaw || NA_SET.has(titleRaw.toLowerCase())) return;
      const docTopics = Array.isArray(entry.chronicle_topics)
        ? (entry.chronicle_topics as unknown[]).map(String).filter(t => t && notNA(t))
        : [];
      const offTitle = String(officialArr[i]?.official_title ?? "").trim();
      const refId    = String(standardizedArr[i]?.standardized_id ?? "").trim();
      byTitle.set(titleRaw.toLowerCase(), { chronicle_topics: docTopics, official_title: offTitle, reference_id: refId });
    });
  }

  // Attempt title matching
  let titleMatchCount = 0;
  const titleMatched = items.map(item => {
    const key  = stripStatusPrefix(item.title).toLowerCase();
    const data = byTitle.get(key);
    if (!data) return null;
    titleMatchCount++;
    return applyEntry(item, data);
  });

  if (titleMatchCount > 0) {
    // At least one title matched — use title results (unmatched items keep original data)
    return titleMatched.map((enriched, i) => enriched ?? items[i]);
  }

  // Positional fallback — match ith preview to ith doc extraction entry
  if (!positional.length) return items;
  return items.map((item, idx) => {
    const data = positional[idx];
    return data ? applyEntry(item, data) : item;
  });
}

function cleanDate(raw: string): string {
  if (!raw) return "";
  // Strip trailing parenthetical annotations (e.g. "(HTML document link title)")
  const stripped = raw.replace(/\s*\(.*$/, "").trim();
  return stripped || raw;
}

// Normalize a datetime/date string to YYYY-MM-DD for display.
// Accepts ISO datetimes ("2026-06-11T14:00:00-04:00"), ISO dates, and
// "Month DD, YYYY" format. Returns "" on failure.
function toIsoDate(raw: string): string {
  if (!raw) return "";
  const s = cleanDate(raw);
  if (!s || /^n\/a\.?$/i.test(s)) return "";
  // Already YYYY-MM-DD-ish
  if (/^\d{4}-\d{2}-\d{2}/.test(s)) return s.slice(0, 10);
  // Month DD, YYYY
  const months: Record<string, string> = {
    january:"01", february:"02", march:"03", april:"04", may:"05", june:"06",
    july:"07", august:"08", september:"09", october:"10", november:"11", december:"12",
  };
  const m = s.match(/^([A-Z][a-z]+)\s+(\d{1,2}),?\s+(\d{4})/i);
  if (m) {
    const mon = months[m[1].toLowerCase()];
    if (mon) return `${m[3]}-${mon}-${m[2].padStart(2, "0")}`;
  }
  return "";
}

// Business rule: library item Date field is derived from the item's type:
//   Agenda & Materials                       → event_start_date_time (meeting date)
//   Proposed Guidance & Support Materials    → event_start_date_time (last comment date)
//   Existing Requirements & Guidance         → event_start_date_time (effective date)
//   Publication                              → doc_extraction.date_published
//   Meeting Transcript / Other               → event_start_date_time (meeting date) if present
// Fallback: whatever date was in the preview, else "".
function deriveLibraryItemDate(
  libPreview: Record<string, unknown>,
  row: Record<string, unknown>,
  docExtractionRow: Record<string, unknown> | undefined,
): string {
  const typeName = String(libPreview.type ?? "").toLowerCase();
  const eventDate = toIsoDate(
    String(row.event_start_date_time ?? row.event_start_date_time_et ?? ""),
  );
  const docDate = toIsoDate(String(docExtractionRow?.date_published ?? ""));
  const previewDate = toIsoDate(String((libPreview.fields as Record<string, unknown> | undefined)?.["Date"] ?? ""));

  const isAgenda       = typeName.includes("agenda");
  const isRfc          = typeName.includes("proposed guidance");
  const isEffective    = typeName.includes("existing requirements");
  const isPublication  = typeName.includes("publication");
  const isTranscriptLi = typeName === "meeting transcript" || /transcript/i.test(String(row.alert_type ?? ""));

  if (isAgenda || isRfc || isEffective || isTranscriptLi) return eventDate || docDate || previewDate;
  if (isPublication) return docDate || previewDate || eventDate;
  return previewDate || docDate || eventDate;
}

function deriveEventType(alertType: string): string {
  const lc = alertType.toLowerCase();
  if (lc.includes("request for comment")) return "RFC";
  if (lc.includes("effective date")) return "Effective Date";
  if (lc.includes("report") || lc.includes("resource")) return "Report";
  return "Meeting";
}

const CLASSIFIER_KEY_TO_LABEL: Record<string, string> = {
  "Title": "Title",
  "Start": "Start Date/Time",
  "End": "End Date/Time",
  "Groups": "Organizations",
  "Location": "Location URL",
  "Call-in": "Call-In",
  "Timezone": "Timezone",
  "Agenda": "Agenda",
  "Name": "Name",
  "URL": "URL",
  "File": "File",
  "Type": "Type",
  "Organizations": "Organizations",
  "Status": "Status",
  "Description": "Summary",
  "Date": "Date",
  "Doc Type": "Doc Type",
  "Topics": "Chronicle Topics",
};

function changedLabelsFromPreview(fieldsDict: Record<string, unknown>): Set<string> {
  return new Set(
    Object.keys(fieldsDict).map(k => CLASSIFIER_KEY_TO_LABEL[k] ?? k)
  );
}

/**
 * Build the full calendar item field list from preview data.
 * For UPDATE sections this is used as the "new values" layer;
 * non-changed fields will be overridden by the current Bubble record values.
 */
function buildEventFields(
  ep: Record<string, unknown>,
  lp: Record<string, unknown>,
  libAction: BubbleAction,
  alertType: string,
  changedLabels: Set<string>,
  agendaItems: AgendaItemPreview[],
): FieldEntry[] {
  const title     = safeStr(ep.title);
  const start     = safeStr(ep.start_datetime);
  const end       = safeStr(ep.end_datetime);
  const isFullDay = String(ep.is_full_day ?? "").toLowerCase() === "full day";
  const group     = Array.isArray(ep.group)
    ? (ep.group as string[]).filter(Boolean).join(", ")
    : safeStr(ep.group);
  const url       = safeStr(ep.url);
  const callIn    = safeStr(ep.call_in);
  const topics    = allLibraryItemTopics(ep, agendaItems);
  const eventType = deriveEventType(alertType);

  const mk = (label: string, agentField: string, value: string, opts?: Partial<FieldEntry>): FieldEntry => ({
    label, agentField, value,
    changed: changedLabels.size > 0 ? changedLabels.has(label) : undefined,
    ...opts,
  });

  const orgList = Array.isArray(ep.group) ? (ep.group as string[]).filter(Boolean) : (notNA(group) ? [group] : []);

  const entries: FieldEntry[] = [
    mk("Title",           "event_title",                       notNA(title) ? title : "—"),
    mk("Type",            "alert_type (derived)",              eventType, { combobox: { type: "calendaritemtype", mode: "single" } }),
    mk("Start Date/Time", "event_start_date_time",             notNA(start) ? start : "—"),
    mk("End Date/Time",   "event_end_date_time",               notNA(end)   ? end   : "—"),
    {
      label: "Organizations",
      agentField: "organization",
      topics: orgList,
      combobox: { type: "organization", mode: "multi" },
      changed: changedLabels.size > 0 ? changedLabels.has("Organizations") : undefined,
    },
    mk("Location",        "event_url",                         notNA(url)    ? url    : "—"),
    mk("Call-In",         "event_call_in_number_access_code",  notNA(callIn) ? callIn : "—"),
  ];
  if (isFullDay) entries.splice(3, 0, mk("Full Day", "event_is_full_day", "Yes", { readOnly: true }));

  // Chronicle Topics — searchable multi-select dropdown
  entries.push({
    label: "Chronicle Topics",
    agentField: "agenda_item_title_chronicle_topics[].chronicle_topics",
    topics,
    combobox: { type: "chronicletopic", mode: "multi" },
    changed: changedLabels.size > 0 ? changedLabels.has("Chronicle Topics") : undefined,
  });

  // Linked Agenda Items — always shown (agenda items linked to this event)
  entries.push({
    label: "Linked Agenda Items",
    agentField: "agenda_item_title_chronicle_topics[].agenda_item_title",
    ...(agendaItems.length > 0
      ? { list: agendaItems.map(item => stripStatusPrefix(item.title)) }
      : { value: "—" }),
    readOnly: true,
  });

  // Agenda — always shown when there's a library item being linked (Bubble field name)
  if (libAction) {
    const libTitle = safeStr(lp.title);
    entries.push(mk("Agenda", "library_item_preliminary_title.title", notNA(libTitle) ? libTitle : "—", { readOnly: true }));
  }

  return entries;
}

/**
 * Build the full library item field list from preview data.
 * Only includes fields that are actually written to Bubble.
 */
function buildLibFields(
  lp: Record<string, unknown>,
  agendaItems: AgendaItemPreview[],
  changedLabels: Set<string>,
  derivedDate: string,
): FieldEntry[] {
  const title    = safeStr(lp.title);
  const url      = safeStr(lp.url);
  const filename = safeStr(lp.filename);
  const type     = safeStr(lp.type);
  const group    = Array.isArray(lp.group)
    ? (lp.group as string[]).filter(Boolean).join(", ")
    : safeStr(lp.group);

  const topics = allLibraryItemTopics(lp, agendaItems);

  const fd = (lp.fields ?? {}) as Record<string, string>;
  const description = fd["Description"] ?? "";
  const date        = derivedDate || cleanDate(fd["Date"] ?? "");
  const docType     = fd["Doc Type"] ?? "";

  const mk = (label: string, agentField: string, value: string, opts?: Partial<FieldEntry>): FieldEntry => ({
    label, agentField, value,
    changed: changedLabels.size > 0 ? changedLabels.has(label) : undefined,
    ...opts,
  });

  const orgList = Array.isArray(lp.group) ? (lp.group as string[]).filter(Boolean) : (notNA(group) ? [group] : []);

  const entries: FieldEntry[] = [
    mk("Name",          "library_item_preliminary_title.title", notNA(title)    ? title    : "—"),
    mk("URL",           "library_item_url",                     notNA(url)      ? url      : "—"),
    mk("File Name",     "library_items_file_name",              notNA(filename) ? filename : "—"),
    mk("Type",          "alert_type (classifier)",              notNA(type)     ? type     : "—", { combobox: { type: "libraryitemtype", mode: "single" } }),
    {
      label: "Organizations",
      agentField: "organization",
      topics: orgList,
      combobox: { type: "organization", mode: "multi" },
      changed: changedLabels.size > 0 ? changedLabels.has("Organizations") : undefined,
    },
    mk("Date",          "doc_extraction.date_published",        notNA(date)     ? date     : "—"),
  ];

  // Chronicle Topics — searchable multi-select dropdown
  entries.push({
    label: "Chronicle Topics",
    agentField: "agenda_item_title_chronicle_topics[].chronicle_topics",
    topics,
    combobox: { type: "chronicletopic", mode: "multi" },
    changed: changedLabels.size > 0 ? changedLabels.has("Chronicle Topics") : undefined,
  });

  if (notNA(description)) entries.push(mk("Description", "doc_extraction.description", description));
  if (notNA(docType))     entries.push(mk("Doc Type",    "doc_extraction.document_type", docType, { readOnly: true }));

  // Linked Agenda Items — shown when items exist
  entries.push({
    label: "Linked Agenda Items",
    agentField: "agenda_item_title_chronicle_topics[].agenda_item_title",
    ...(agendaItems.length > 0
      ? { list: agendaItems.map(item => stripStatusPrefix(item.title)) }
      : { value: "—" }),
    readOnly: true,
  });

  return entries;
}

/**
 * For UPDATE sections: merge preview fields with current Bubble record.
 * Fields being changed (changedLabels) keep the new preview value, highlighted amber.
 * All other fields show the current Bubble value as the authoritative baseline.
 */
function mergeWithCurrentRecord(
  previewFields: FieldEntry[],
  currentRecord: BubbleRecord | null,
  isLoadingRecord: boolean,
): FieldEntry[] {
  if (isLoadingRecord || !currentRecord?.found || !currentRecord.fields) {
    return previewFields;
  }
  const current = currentRecord.fields;

  return previewFields.map(field => {
    // Changed fields keep the new value (from preview), highlighted amber
    if (field.changed) return field;

    // For non-changed fields, use the authoritative Bubble value
    const bubbleVal = current[field.label];
    if (bubbleVal === undefined) return field; // not in Bubble record, keep preview

    if (Array.isArray(bubbleVal)) {
      return {
        ...field,
        topics: field.topics !== undefined ? bubbleVal : undefined,
        list:   field.list   !== undefined ? bubbleVal : undefined,
        value:  field.topics === undefined && field.list === undefined
          ? (bubbleVal.length > 0 ? bubbleVal.join(", ") : "—")
          : undefined,
      };
    }
    return { ...field, value: bubbleVal || "—" };
  });
}

// ─── Field mapping for persisting edits back to bubble_action ─────────────────

type EditSpec = { previewKey?: string; fieldIdsKey?: string; isList?: boolean; isMulti?: boolean };

const EVENT_EDIT_MAP: Record<string, EditSpec> = {
  "Title":            { previewKey: "title",          fieldIdsKey: "title_text" },
  "Start Date/Time":  { previewKey: "start_datetime", fieldIdsKey: "date_date" },
  "End Date/Time":    { previewKey: "end_datetime",   fieldIdsKey: "length_end_time_date" },
  "Organizations":    { previewKey: "group",          fieldIdsKey: "orgs__list_custom_organization", isList: true, isMulti: true },
  "Location":         { previewKey: "url" },
  "Call-In":          { previewKey: "call_in",        fieldIdsKey: "phone_number_and_access_code_text" },
  "Type":             { previewKey: "type" },
  "Chronicle Topics": { fieldIdsKey: "topics___dt_list_custom_newsreel_update", isList: true, isMulti: true },
};

const LIB_EDIT_MAP: Record<string, EditSpec> = {
  "Name":             { previewKey: "title",    fieldIdsKey: "name_text" },
  "URL":              { previewKey: "url",      fieldIdsKey: "url_text" },
  "File Name":        { previewKey: "filename", fieldIdsKey: "file_name_text" },
  "Organizations":    { previewKey: "group",    fieldIdsKey: "organizations_list_custom_organization", isList: true, isMulti: true },
  "Date":             { fieldIdsKey: "date_date" },
  "Description":      { fieldIdsKey: "description_text" },
  "Type":             { previewKey: "type" },
  "Chronicle Topics": { fieldIdsKey: "topics___dt_list_custom_newsreel_update", isList: true, isMulti: true },
};

function applyEditsToPreview(
  preview: Record<string, unknown>,
  edits: Record<string, string>,
  editMap: Record<string, EditSpec>,
): Record<string, unknown> {
  const out = { ...preview };
  const outFieldIds = { ...((preview.field_ids as Record<string, unknown>) ?? {}) };
  const outFields   = { ...((preview.fields as Record<string, unknown>) ?? {}) };

  for (const [label, value] of Object.entries(edits)) {
    if (value == null) continue;
    const map = editMap[label];
    if (!map) continue;
    let coerced: unknown;
    let display: string;
    if (map.isMulti) {
      // Multi-select combobox: value is MULTI_SEP-joined
      const names = multiFromEditValue(value) ?? [];
      coerced = names;
      display = names.join(", ");
    } else if (map.isList) {
      coerced = value.split(",").map(s => s.trim()).filter(Boolean);
      display = value;
    } else {
      if (!value) continue;
      coerced = value;
      display = value;
    }
    if (map.previewKey)  out[map.previewKey] = coerced;
    if (map.fieldIdsKey) outFieldIds[map.fieldIdsKey] = coerced;
    outFields[label] = display;
  }
  out.field_ids = outFieldIds;
  out.fields    = outFields;
  return out;
}

// ─── Section card component ───────────────────────────────────────────────────

interface VectorStoreOption {
  value: string;         // internal identifier
  label: string;         // display name (e.g. "Newsreel Knowledge Base")
  onSelect: () => void;  // fires the ingest
  state: SectionState;   // per-type section state
  disabled?: boolean;
  disabledReason?: string;
}

interface SectionProps {
  title: string;         // button label
  subtitle?: string;     // actual content title shown prominently under the header bar
  objectType?: string;   // "Library Item" | "Calendar Event" | "Meeting Transcript" | "Library Document"
  system: "Bubble" | "Newsreel";
  action: BubbleAction;
  fields: FieldEntry[];
  editState: Record<string, string>;
  onEdit: (label: string, value: string) => void;
  sectionState: SectionState;
  onPublish?: () => void;              // optional — when omitted no primary Bubble button is shown
  currentRecord?: BubbleRecord | null;
  isLoadingRecord?: boolean;
  agendaItems?: AgendaItemPreview[];
  schemaLabels: Record<string, string> | null;
  bubbleOptions: BubbleOptionsMap;
  vectorStoreOptions?: VectorStoreOption[];  // when set, renders "Add to Library Vector Store ▼"
}

function FieldTable({
  fields,
  editState,
  onEdit,
  schemaLabels,
  bubbleOptions,
}: {
  fields: FieldEntry[];
  editState: Record<string, string>;
  onEdit: (label: string, value: string) => void;
  schemaLabels: Record<string, string> | null;
  bubbleOptions: BubbleOptionsMap;
}) {
  return (
    <table className="w-full table-fixed border-collapse">
      <colgroup>
        <col style={{ width: "30%" }} />
        <col style={{ width: "22%" }} />
        <col style={{ width: "48%" }} />
      </colgroup>
      <thead>
        <tr className="border-b-2 border-gray-200 bg-gray-50/60">
          <th className="py-2 px-2.5 text-left text-[9px] font-semibold text-gray-500 uppercase tracking-wider border-r border-gray-200">Agent Field</th>
          <th className="py-2 px-2.5 text-left text-[9px] font-semibold text-gray-500 uppercase tracking-wider border-r border-gray-200">Bubble Field</th>
          <th className="py-2 px-2.5 text-left text-[9px] font-semibold text-gray-500 uppercase tracking-wider">Value</th>
        </tr>
      </thead>
      <tbody>
        {fields.map((f, i) => {
          const isChanged = !!f.changed;
          const rowClass = `${isChanged ? "bg-amber-50" : ""} border-b border-gray-100 last:border-0`;

          return (
            <tr key={i} className={rowClass}>
              <td className="py-2 px-2.5 text-[11px] text-gray-700 align-top break-words leading-snug border-r border-gray-100">
                {resolveAgentFieldLabel(f.agentField, schemaLabels)}
              </td>
              <td
                className={`py-2 px-2.5 text-[11px] font-medium align-top break-words leading-snug border-r border-gray-100 ${isChanged ? "text-amber-700" : "text-gray-800"}`}
              >
                {f.label}
              </td>
              <td className="py-2 px-2.5 align-top">
                {f.combobox ? (() => {
                  const opts = bubbleOptions[f.combobox.type] ?? [];
                  const loading = bubbleOptions[f.combobox.type] === undefined;
                  if (f.combobox.mode === "multi") {
                    const edited = multiFromEditValue(editState[f.label]);
                    const values = edited !== null ? edited : (f.topics ?? []);
                    return (
                      <SearchableCombobox
                        mode="multi"
                        options={opts}
                        loading={loading}
                        values={values}
                        onChange={names => onEdit(f.label, multiToEditValue(names))}
                        placeholder="Select…"
                      />
                    );
                  }
                  // single
                  const value = editState[f.label] ?? f.value ?? "";
                  return (
                    <SearchableCombobox
                      mode="single"
                      options={opts}
                      loading={loading}
                      value={value}
                      onChange={name => onEdit(f.label, name)}
                      placeholder="Select…"
                    />
                  );
                })() : f.topics !== undefined ? (
                  f.topics.length > 0 ? (
                    <div className="flex flex-wrap gap-1 py-0.5">
                      {f.topics.map(t => (
                        <span key={t} className="text-[10px] bg-violet-100 text-violet-700 rounded px-1.5 py-0.5 border border-violet-200 leading-tight">{t}</span>
                      ))}
                    </div>
                  ) : (
                    <span className="text-[12px] text-gray-400">—</span>
                  )
                ) : f.list !== undefined ? (
                  f.list.length > 0 ? (
                    <div className="space-y-0.5">
                      {f.list.map((item, j) => (
                        <p key={j} className="text-[11px] text-gray-700">
                          <span className="text-gray-400 mr-1.5">{j + 1}.</span>{item}
                        </p>
                      ))}
                    </div>
                  ) : (
                    <span className="text-[12px] text-gray-400">—</span>
                  )
                ) : f.readOnly ? (
                  <span className="text-[12px] text-gray-800 break-all">{f.value || "—"}</span>
                ) : (
                  <input
                    type="text"
                    value={editState[f.label] ?? f.value ?? ""}
                    onChange={e => onEdit(f.label, e.target.value)}
                    className="w-full text-[12px] text-gray-800 bg-transparent border-b border-gray-200 hover:border-gray-400 focus:border-indigo-400 focus:outline-none px-0 py-0.5 transition-colors"
                  />
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function AddToVectorStoreButton({ options }: { options: VectorStoreOption[] }) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open]);

  const anyLoading = options.some(o => o.state.status === "loading");

  return (
    <div ref={rootRef} className="relative">
      <button
        onClick={() => !anyLoading && setOpen(v => !v)}
        disabled={anyLoading}
        className="text-[11px] font-medium text-white bg-purple-600 hover:bg-purple-700 disabled:opacity-50 rounded-md pl-3 pr-2.5 py-1.5 transition-colors whitespace-nowrap inline-flex items-center gap-1.5"
      >
        {anyLoading ? "Working…" : "Add to Library Vector Store"}
        <svg className={`w-3 h-3 transition-transform ${open ? "rotate-180" : ""}`} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
        </svg>
      </button>
      {open && (
        <div className="absolute right-0 bottom-full mb-1 w-64 bg-white border border-gray-200 rounded-md shadow-lg z-30 overflow-hidden">
          {options.map(opt => {
            const isDone     = opt.state.status === "done";
            const isLoading  = opt.state.status === "loading";
            const isError    = opt.state.status === "error";
            const isDisabled = opt.disabled || isDone || isLoading;
            return (
              <button
                key={opt.value}
                type="button"
                disabled={isDisabled}
                title={opt.disabledReason}
                onClick={() => { setOpen(false); opt.onSelect(); }}
                className={`w-full text-left px-3 py-2 flex items-center justify-between gap-2 border-b border-gray-100 last:border-0 ${isDisabled ? "opacity-60 cursor-not-allowed" : "hover:bg-gray-50"}`}
              >
                <span className="text-[12px] text-gray-800 flex-1">{opt.label}</span>
                {isDone     && <span className="text-[10px] text-green-600 font-semibold">✓ Added</span>}
                {isLoading  && <span className="text-[10px] text-gray-400 italic">Working…</span>}
                {isError    && <span className="text-[10px] text-red-500 font-medium" title={opt.state.error}>Failed</span>}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

function Section({
  title, subtitle, objectType, system, action, fields, editState, onEdit,
  sectionState, onPublish, currentRecord, isLoadingRecord, agendaItems, schemaLabels, bubbleOptions,
  vectorStoreOptions,
}: SectionProps) {
  const isDone    = sectionState.status === "done";
  const isLoading = sectionState.status === "loading";

  const borderColor = action === "create" ? "border-green-200"
    : action === "update" ? "border-blue-200"
    : "border-purple-200";
  const headerBg = action === "create" ? "bg-green-50"
    : action === "update" ? "bg-blue-50"
    : "bg-purple-50";
  const systemColor = system === "Bubble" ? "text-indigo-600" : "text-purple-600";

  const actionBadge = action === "create"
    ? <span className="text-[9px] font-bold uppercase tracking-wider bg-green-100 text-green-700 rounded px-1.5 py-0.5">Create</span>
    : action === "update"
    ? <span className="text-[9px] font-bold uppercase tracking-wider bg-blue-100 text-blue-700 rounded px-1.5 py-0.5">Update</span>
    : <span className="text-[9px] font-bold uppercase tracking-wider bg-purple-100 text-purple-700 rounded px-1.5 py-0.5">Publish</span>;

  const publishLabel = system === "Newsreel"
    ? "Publish to Newsreel"
    : action === "create" ? "Create in Bubble"
    : "Update in Bubble";

  const SectionHeader = ({ showSubtitle }: { showSubtitle?: boolean }) => (
    <div className={`${headerBg} px-4 pt-3 pb-2.5`}>
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 min-w-0">
          <span className={`text-[10px] font-bold uppercase tracking-wider shrink-0 ${systemColor}`}>{system}</span>
          {objectType && (
            <>
              <span className="text-[10px] text-gray-300 shrink-0">·</span>
              <span className="text-[10px] font-medium text-gray-500 uppercase tracking-wider shrink-0">{objectType}</span>
            </>
          )}
          <span className="ml-0.5 shrink-0">{actionBadge}</span>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {action === "update" && isLoadingRecord && (
            <span className="text-[10px] text-gray-400 italic">Loading…</span>
          )}
          {isDone ? (
            <span className="text-[11px] text-green-600 font-semibold">✓ Done</span>
          ) : sectionState.status === "error" ? (
            <span className="text-[11px] text-red-500 font-medium" title={sectionState.error}>✕ Failed</span>
          ) : null}
        </div>
      </div>
      {showSubtitle && subtitle && (
        <p className="text-[13px] font-semibold text-gray-800 mt-1.5 leading-snug line-clamp-2">{subtitle}</p>
      )}
    </div>
  );

  // For UPDATE: if loading finished and record not found → minimal card
  const recordNotFound = action === "update" && !isLoadingRecord && currentRecord != null && !currentRecord.found;

  if (recordNotFound) {
    return (
      <div className={`rounded-lg border ${borderColor} overflow-hidden`}>
        <SectionHeader showSubtitle />
        <div className="px-4 py-2.5 flex items-center justify-between gap-2">
          <p className="text-[12px] text-amber-700 font-medium">
            Record not found in Bubble — does not exist yet
          </p>
          <div className="flex items-center gap-2 shrink-0">
            {vectorStoreOptions && vectorStoreOptions.length > 0 && (
              <AddToVectorStoreButton options={vectorStoreOptions} />
            )}
            {!isDone && onPublish && (
              <button
                onClick={onPublish}
                disabled={isLoading}
                className="text-[11px] font-medium text-white bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 rounded-md px-3 py-1.5 transition-colors whitespace-nowrap"
              >
                {isLoading ? "Working…" : publishLabel}
              </button>
            )}
          </div>
        </div>
      </div>
    );
  }

  // For UPDATE: merge current Bubble record into the field list
  const displayFields = action === "update"
    ? mergeWithCurrentRecord(fields, currentRecord ?? null, isLoadingRecord ?? false)
    : fields;

  return (
    <div className={`rounded-lg border ${borderColor} overflow-hidden`}>
      {/* Header */}
      <SectionHeader showSubtitle />

      {/* Fields */}
      {displayFields.length > 0 && (
        <div className="px-4 py-3">
          <FieldTable
            fields={displayFields}
            editState={editState}
            onEdit={onEdit}
            schemaLabels={schemaLabels}
            bubbleOptions={bubbleOptions}
          />
        </div>
      )}

      {/* Agenda items created or linked alongside this library item */}
      {agendaItems && agendaItems.length > 0 && (() => {
        const newItems      = agendaItems.filter(a => !a.status || a.status.toLowerCase() === "new");
        const existingItems = agendaItems.filter(a => a.status && a.status.toLowerCase() !== "new");
        return (
          <div className="px-4 pb-3 border-t border-gray-100 pt-3 space-y-3">
            {newItems.length > 0 && (
              <div>
                <p className="text-[10px] font-semibold text-gray-500 uppercase tracking-wider mb-2">
                  Also creates {newItems.length} Agenda Item{newItems.length !== 1 ? "s" : ""}
                </p>
                <div className="space-y-3">
                  {newItems.map((item, i) => (
                    <div key={i} className={i > 0 ? "pt-3 border-t border-gray-100" : ""}>
                      <p className="text-[12px] font-semibold text-gray-800 mb-1.5">
                        <span className="text-gray-400 mr-1.5">{i + 1}.</span>{stripStatusPrefix(item.title)}
                      </p>
                      <div className="flex gap-2 text-[11px] py-0.5">
                        <span className="text-gray-400 w-28 flex-shrink-0">Official Title</span>
                        <span className="text-gray-600">{item.official_title ? stripStatusPrefix(item.official_title) : "—"}</span>
                      </div>
                      <div className="flex gap-2 text-[11px] py-0.5">
                        <span className="text-gray-400 w-28 flex-shrink-0">Ref ID</span>
                        <span className="text-gray-600 font-mono">{item.reference_id || "—"}</span>
                      </div>
                      <div className="flex gap-2 text-[11px] py-1">
                        <span className="text-gray-400 w-28 flex-shrink-0">Chronicle Topics</span>
                        <div className="flex flex-wrap gap-1">
                          {item.chronicle_topics && item.chronicle_topics.length > 0 ? (
                            item.chronicle_topics.map(t => (
                              <span key={t} className="text-[10px] bg-violet-100 text-violet-700 rounded px-1.5 py-0.5 border border-violet-200 leading-tight">{t}</span>
                            ))
                          ) : <span className="text-gray-400">—</span>}
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
            {existingItems.length > 0 && (
              <div>
                <p className="text-[10px] font-semibold text-gray-500 uppercase tracking-wider mb-1.5">
                  Also links {existingItems.length} existing Agenda Item{existingItems.length !== 1 ? "s" : ""}
                </p>
                <div className="space-y-0.5">
                  {existingItems.map((item, i) => (
                    <p key={i} className="text-[11px] text-gray-600">
                      <span className="text-gray-400 mr-1.5">{i + 1}.</span>{stripStatusPrefix(item.title)}
                    </p>
                  ))}
                </div>
              </div>
            )}
          </div>
        );
      })()}

      {/* Footer — primary Bubble publish button + optional vector store dropdown */}
      {((!isDone && onPublish) || (vectorStoreOptions && vectorStoreOptions.length > 0)) && (
        <div className="flex justify-end gap-2 px-4 py-2.5 border-t border-gray-100 bg-gray-50/50">
          {vectorStoreOptions && vectorStoreOptions.length > 0 && (
            <AddToVectorStoreButton options={vectorStoreOptions} />
          )}
          {!isDone && onPublish && (
            <button
              onClick={onPublish}
              disabled={isLoading}
              className="text-[11px] font-medium text-white bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed rounded-md px-3 py-1.5 transition-colors whitespace-nowrap"
            >
              {isLoading ? "Working…" : publishLabel}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

// ─── Main modal ───────────────────────────────────────────────────────────────

export function ContentGateModal({ row, onClose, onFieldPatched }: ContentGateModalProps) {
  const bubbleAction  = row.bubble_action as Record<string, unknown> | null | undefined;
  const eventAction   = (bubbleAction?.event ?? null) as BubbleAction;
  const libAction     = (bubbleAction?.library_item ?? null) as BubbleAction;
  const eventPreview  = (bubbleAction?.event_preview ?? {}) as Record<string, unknown>;
  const libPreview    = (bubbleAction?.library_item_preview ?? {}) as Record<string, unknown>;
  const alertType     = String(bubbleAction?.notes ?? row.alert_type ?? "");

  const agentCallId   = String(row.agent_call_id ?? "");
  const storedAgendaItems = ((bubbleAction?.agenda_item_previews ?? []) as AgendaItemPreview[]);

  // Live doc extraction rows for this alert's library item — fetched async on open.
  // When bubble_action.agenda_item_previews is empty (page_change_agent returned N/A from HTML),
  // doc extraction rows are used as the authoritative source of agenda items instead.
  const [docExtractionRows, setDocExtractionRows] = useState<Array<Record<string, unknown>>>([]);

  // Human-readable agent output field labels (from DynamoDB output_requested_values)
  const [schemaLabels, setSchemaLabels] = useState<Record<string, string> | null>(null);
  useEffect(() => {
    fetch("/api/schema", { cache: "no-store" })
      .then(r => r.json())
      .then((d: { labels?: Record<string, string> | null }) => setSchemaLabels(d.labels ?? null))
      .catch(() => {});
  }, []);

  // Live Bubble reference data for the searchable dropdowns
  const [bubbleOptions, setBubbleOptions] = useState<BubbleOptionsMap>({
    organization: undefined, chronicletopic: undefined,
    libraryitemtype: undefined, calendaritemtype: undefined,
  });
  useEffect(() => {
    const types: BubbleOptionsType[] = ["organization", "chronicletopic", "libraryitemtype", "calendaritemtype"];
    for (const t of types) {
      fetch(`/api/bubble/options?type=${t}`)
        .then(r => r.json())
        .then((d: { options?: BubbleOption[] }) => {
          setBubbleOptions(prev => ({ ...prev, [t]: d.options ?? [] }));
        })
        .catch(() => setBubbleOptions(prev => ({ ...prev, [t]: [] })));
    }
  }, []);

  // Synthesize AgendaItemPreview[] from doc extraction rows when stored previews are all empty/N/A.
  // This covers the common case where the page_change_agent couldn't read agenda items from HTML
  // but the document_agent extracted them from the PDF.
  // Synthesize agenda items from doc extraction rows.
  // Doc extraction (PDF-based) is always more accurate than page_change_agent HTML extraction,
  // which often captures the document title or file name rather than actual agenda items.
  const synthesizedAgendaItems = useMemo((): AgendaItemPreview[] => {
    if (!docExtractionRows.length) return [];
    const sorted = [...docExtractionRows].sort(
      (a, b) => Number(a.agenda_number ?? 999) - Number(b.agenda_number ?? 999),
    );
    const result: AgendaItemPreview[] = [];
    for (const dr of sorted) {
      const bridgewayItems = (
        dr.agenda_item_bridgeway_title_chronicle_topic ??
        dr.agenda_item_title_chronicle_topic
      ) as Array<Record<string, unknown>> | undefined;
      const officialArr     = (dr.agenda_item_title_official    as Array<Record<string, unknown>> | undefined) ?? [];
      const standardizedArr = (dr.agenda_item_standardized_id  as Array<Record<string, unknown>> | undefined) ?? [];
      if (!Array.isArray(bridgewayItems)) continue;
      bridgewayItems.forEach((entry, i) => {
        const title = String(entry.agenda_item_title ?? "").trim();
        if (!title || NA_SET.has(title.toLowerCase())) return;
        const topics = Array.isArray(entry.chronicle_topics)
          ? (entry.chronicle_topics as unknown[]).map(String).filter(t => t && notNA(t))
          : [];
        const offTitle = String(officialArr[i]?.official_title ?? "").trim();
        const refId    = String(standardizedArr[i]?.standardized_id ?? "").trim();
        result.push({
          title,
          status: String(entry.status ?? "New").trim(),
          chronicle_topics: topics,
          ...(offTitle && notNA(offTitle) ? { official_title: offTitle } : {}),
          ...(refId    && notNA(refId)    ? { reference_id:  refId }     : {}),
        });
      });
    }
    return result;
  }, [docExtractionRows]);

  // Prefer doc extraction synthesis (authoritative PDF data) over stored page_change_agent previews.
  // Fall back to stored only when no doc extraction rows are available for this document.
  const agendaItems     = synthesizedAgendaItems.length > 0 ? synthesizedAgendaItems : storedAgendaItems;
  const isAgendaSynthesized = synthesizedAgendaItems.length > 0;

  // Use agendaItems.length directly — bubble_action.agenda_items may be false when the
  // page_change_agent returned N/A for all agenda items, but doc extraction synthesized real ones.
  const hasAgenda      = agendaItems.length > 0;
  const newAgendaItems = agendaItems.filter(a => !a.status || a.status.toLowerCase() === "new");
  const existingAgendaItems = agendaItems.filter(a => a.status && a.status.toLowerCase() !== "new");
  // These must be declared before the derived activeNewAgendaItems that use them
  const [deletedNewIndices, setDeletedNewIndices] = useState<Set<number>>(new Set());
  // Per-item edit state: index → { Bubble field label → value }
  const [agendaItemEdits, setAgendaItemEdits] = useState<Record<number, Record<string, string>>>({});
  // Backwards compat: title shortcut (reads from the structured map)
  const agendaItemTitleEdits: Record<number, string> = useMemo(() => {
    const out: Record<number, string> = {};
    for (const [idx, m] of Object.entries(agendaItemEdits)) {
      if (m["Title"] !== undefined) out[Number(idx)] = m["Title"];
    }
    return out;
  }, [agendaItemEdits]);
  function updateAgendaItemField(idx: number, label: string, value: string) {
    setAgendaItemEdits(prev => ({ ...prev, [idx]: { ...(prev[idx] ?? {}), [label]: value } }));
  }
  // Apply per-field edits and deletions — these drive the UI and what gets patched to the API
  function applyAgendaEdits(item: AgendaItemPreview, i: number): AgendaItemPreview {
    const edits = agendaItemEdits[i] ?? {};
    const out: AgendaItemPreview = { ...item };
    if (edits["Title"] !== undefined) out.title = edits["Title"];
    if (edits["Official Title"] !== undefined) out.official_title = edits["Official Title"];
    if (edits["Reference ID"] !== undefined) out.reference_id = edits["Reference ID"];
    if (edits["Chronicle Topics"] !== undefined) {
      out.chronicle_topics = multiFromEditValue(edits["Chronicle Topics"]) ?? [];
    }
    return out;
  }
  const activeNewAgendaItems: AgendaItemPreview[] = newAgendaItems
    .map((item, i) => applyAgendaEdits(item, i))
    .filter((_, i) => !deletedNewIndices.has(i));
  const activeAgendaItems: AgendaItemPreview[] = [...activeNewAgendaItems, ...existingAgendaItems];
  const hasNewAgenda   = hasAgenda && activeNewAgendaItems.length > 0;

  // Enriched agenda items — chronicle_topics, official_title, reference_id overlaid
  // from live doc extraction rows. Used for rendering only; sync/patch still uses activeAgendaItems.
  // When items were synthesized from doc extraction, they're already enriched — skip double-enrichment.
  const enrichedNewAgendaItems = useMemo(
    () => isAgendaSynthesized ? newAgendaItems : enrichFromDocExtraction(newAgendaItems, docExtractionRows),
    [isAgendaSynthesized, newAgendaItems, docExtractionRows],
  );
  const displayedActiveNewAgenda: AgendaItemPreview[] = enrichedNewAgendaItems
    .map((item, i) => applyAgendaEdits(item, i))
    .filter((_, i) => !deletedNewIndices.has(i));
  const displayedAgendaItems: AgendaItemPreview[] = [
    ...displayedActiveNewAgenda,
    ...(isAgendaSynthesized ? existingAgendaItems : enrichFromDocExtraction(existingAgendaItems, docExtractionRows)),
  ];

  const transcriptKey = String((row.transcript_s3_key ?? row.manual_transcript_s3_key) ?? "");
  const hasTranscript = !!transcriptKey && row.alert_type === "New Meeting Transcript Available";
  const libItemUrl    = String(row.library_item_url ?? "");
  // Also trigger doc extraction fetch when library_item_preview has a URL (covers Meeting rows
  // where the alert's library_item_url is N/A but a sibling PDF was extracted in the same call).
  const lpUrl         = String((libPreview as Record<string, unknown>).url ?? "");
  const docFetchUrl   = [libItemUrl, lpUrl].find(u => u && u !== "N/A" && u.startsWith("http")) ?? "";
  const hasDocument   = !!docFetchUrl;

  const eventChangedLabels = useMemo(() =>
    eventAction === "update"
      ? changedLabelsFromPreview((eventPreview.fields ?? {}) as Record<string, unknown>)
      : new Set<string>(),
    [eventAction, eventPreview],
  );
  const libChangedLabels = useMemo(() =>
    libAction === "update"
      ? changedLabelsFromPreview((libPreview.fields ?? {}) as Record<string, unknown>)
      : new Set<string>(),
    [libAction, libPreview],
  );

  const eventFields = useMemo(() => {
    // Supplement stale/empty preview datetimes from live row fields (try both with and without _et suffix)
    const ep = { ...eventPreview } as Record<string, unknown>;
    const r = row as Record<string, unknown>;
    if (!notNA(safeStr(ep.start_datetime))) ep.start_datetime = safeStr(r.event_start_date_time ?? r.event_start_date_time_et);
    if (!notNA(safeStr(ep.end_datetime)))   ep.end_datetime   = safeStr(r.event_end_date_time   ?? r.event_end_date_time_et);
    return buildEventFields(ep, libPreview, libAction, alertType, eventChangedLabels, displayedAgendaItems);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [eventPreview, libPreview, libAction, alertType, eventChangedLabels, displayedAgendaItems]);
  // Date for the library item is derived from its Bubble type per project convention.
  // Meeting materials / RFC / Effective Date / Transcript → event start date.
  // Publication (reports) → doc extraction's publication date.
  const derivedDate = useMemo(
    () => libAction ? deriveLibraryItemDate(libPreview, row as unknown as Record<string, unknown>, docExtractionRows[0]) : "",
    [libAction, libPreview, row, docExtractionRows],
  );

  const libFields = useMemo(
    () => buildLibFields(libPreview, displayedAgendaItems, libChangedLabels, derivedDate),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [libPreview, displayedAgendaItems, libChangedLabels, derivedDate],
  );

  const [eventEdits, setEventEdits]               = useState<Record<string, string>>({});
  const [libEdits, setLibEdits]                   = useState<Record<string, string>>({});

  // Pre-flight validation — runs once on open for rows with Bubble sections
  const [validation, setValidation]               = useState<ValidationResult | null | "loading">(null);
  const hasBubbleSections = !!(eventAction || libAction || hasAgenda);
  useEffect(() => {
    if (!hasBubbleSections || !agentCallId) return;
    setValidation("loading");
    fetch(`/api/bubble/validate?agent_call_id=${encodeURIComponent(agentCallId)}`)
      .then(r => r.json() as Promise<ValidationResult>)
      .then(d => setValidation(d))
      .catch(() => setValidation(null));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentCallId]);

  // Live Bubble record state for UPDATE sections
  const [eventRecord, setEventRecord]               = useState<BubbleRecord | null>(null);
  const [libRecord, setLibRecord]                   = useState<BubbleRecord | null>(null);
  const [loadingEventRecord, setLoadingEventRecord] = useState(false);
  const [loadingLibRecord, setLoadingLibRecord]     = useState(false);

  useEffect(() => {
    if (eventAction !== "update" || !agentCallId) return;
    setLoadingEventRecord(true);
    fetch(`/api/bubble/record?agent_call_id=${encodeURIComponent(agentCallId)}&object_type=calendaritem`)
      .then(r => r.json() as Promise<BubbleRecord>)
      .then(d => setEventRecord(d))
      .catch(e => setEventRecord({ found: false, error: String(e) }))
      .finally(() => setLoadingEventRecord(false));
  }, [agentCallId, eventAction]);

  useEffect(() => {
    if (libAction !== "update" || !agentCallId) return;
    setLoadingLibRecord(true);
    fetch(`/api/bubble/record?agent_call_id=${encodeURIComponent(agentCallId)}&object_type=libraryitem`)
      .then(r => r.json() as Promise<BubbleRecord>)
      .then(d => setLibRecord(d))
      .catch(e => setLibRecord({ found: false, error: String(e) }))
      .finally(() => setLoadingLibRecord(false));
  }, [agentCallId, libAction]);

  const eventDone      = !!(row.bubble_event_id);
  const libDone        = !!(row.bubble_library_item_id);
  const transcriptDone = row.ingest_status === "approved";

  const agendaDone = !!(row.eidarix_agenda_item_ids as unknown[] | undefined)?.length;

  const [sections, setSections] = useState<Record<string, SectionState>>({
    event:        { status: eventDone  ? "done" : "idle" },
    library_item: { status: libDone   ? "done" : "idle" },
    agenda_items: { status: agendaDone ? "done" : "idle" },
    transcript:   { status: transcriptDone ? "done" : "idle" },
    document:     { status: "idle" },
  });

  useEffect(() => {
    if (!hasDocument || !agentCallId) return;
    fetch(`/api/document-extractions?agent_call_id=${encodeURIComponent(agentCallId)}`)
      .then(r => r.json())
      .then((d: { rows?: Array<Record<string, unknown>> }) => {
        const allRows = d.rows ?? [];
        // Filter to rows matching the effective library URL. When multiple PDFs share an
        // agent_call_id the URL discriminates which rows belong to this alert's document.
        const filtered = docFetchUrl
          ? allRows.filter(r => String(r.library_item_url ?? "") === docFetchUrl)
          : allRows;
        setDocExtractionRows(filtered);
        if (filtered.some(r => r.ingest_status === "approved")) {
          setSections(prev => ({ ...prev, document: { status: "done" } }));
        }
      })
      .catch(() => {});
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentCallId, hasDocument]);

  const [validationPopup, setValidationPopup] = useState<ValidationPopup | null>(null);

  function setSectionState(id: string, status: SectionStatus, error?: string) {
    setSections(prev => ({ ...prev, [id]: { status, error } }));
  }

  function dismissValidation() {
    if (!validationPopup) return;
    setSectionState(validationPopup.section, "idle");
    setValidationPopup(null);
  }

  async function patchBubbleAction(previewKey: string, updatedPreview: Record<string, unknown>) {
    const updated = { ...(bubbleAction ?? {}), [previewKey]: updatedPreview };
    await fetch("/api/alerts/patch-row", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ agent_call_id: agentCallId, fields: { bubble_action: updated } }),
    });
    onFieldPatched({ bubble_action: updated });
  }

  // Call /api/bubble/sync synchronously — Lambda invocation blocks until done, no polling needed.
  async function callSync(action: string, skipValidation: boolean): Promise<Record<string, unknown>> {
    const resp = await fetch("/api/bubble/sync", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ agent_call_id: agentCallId, action, ...(skipValidation && { skip_validation: true }) }),
    });
    const data = await resp.json() as Record<string, unknown>;
    if (!resp.ok) throw Object.assign(new Error(String(data.error ?? "Sync failed")), { isValidation: resp.status === 400 && !data.ok, data });
    if (!data.ok) throw new Error(String(data.error ?? "Sync failed"));
    return data;
  }

  async function handlePublishEvent(skipValidation = false) {
    if (libAction && sections.library_item.status !== "done") {
      setSectionState("event", "error",
        libAction === "create"
          ? "Create Library Item first — the event must link to it."
          : "Update Library Item first — the event must link to it."
      );
      return;
    }
    setSectionState("event", "loading");
    try {
      if (Object.keys(eventEdits).length > 0) {
        const updated = applyEditsToPreview(eventPreview, eventEdits, EVENT_EDIT_MAP);
        await patchBubbleAction("event_preview", updated);
      }
      let result: Record<string, unknown>;
      try {
        result = await callSync("event", skipValidation);
      } catch (err) {
        const e = err as Error & { isValidation?: boolean; data?: Record<string, unknown> };
        if (e.isValidation) {
          setSectionState("event", "idle");
          setValidationPopup({ section: "event", message: e.message, onConfirm: () => { setValidationPopup(null); void handlePublishEvent(true); } });
          return;
        }
        throw err;
      }
      if (!result.bubble_event_id) {
        throw new Error(String(result.error ?? "Event was not created/updated — no ID returned from Eidarix"));
      }
      setSectionState("event", "done");
      onFieldPatched({ bubble_sync_status: "syncing", bubble_event_id: result.bubble_event_id as string });
    } catch (err) {
      setSectionState("event", "error", err instanceof Error ? err.message : String(err));
    }
  }

  async function handleCreateAgendaItems(skipValidation = false) {
    setSectionState("agenda_items", "loading");
    try {
      // Patch stored bubble_action with the active item list.
      // Always patch when items were synthesized from doc extraction (previews were empty on the row).
      const needsPatch = isAgendaSynthesized || deletedNewIndices.size > 0 || Object.keys(agendaItemEdits).length > 0;
      if (needsPatch) {
        const patchedPreviews = [...activeNewAgendaItems, ...existingAgendaItems];
        const updated = {
          ...(bubbleAction ?? {}),
          agenda_item_previews: patchedPreviews as unknown[],
          // agenda_items may be false when page_change_agent returned N/A — set true so the
          // sync executor (which checks plan.agenda_items) processes the synthesized previews.
          ...(isAgendaSynthesized ? { agenda_items: true } : {}),
        };
        await fetch("/api/alerts/patch-row", {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ agent_call_id: agentCallId, fields: { bubble_action: updated } }),
        });
        onFieldPatched({ bubble_action: updated } as Partial<AlertRow>);
      }
      let result: Record<string, unknown>;
      try {
        result = await callSync("agenda_items", skipValidation);
      } catch (err) {
        const e = err as Error & { isValidation?: boolean };
        if (e.isValidation) {
          setSectionState("agenda_items", "idle");
          setValidationPopup({ section: "agenda_items", message: e.message, onConfirm: () => { setValidationPopup(null); void handleCreateAgendaItems(true); } });
          return;
        }
        throw err;
      }
      setSectionState("agenda_items", "done");
      const eidarixIds = (result.eidarix_agenda_item_ids as unknown[]) ?? [];
      onFieldPatched({ eidarix_agenda_item_ids: eidarixIds } as Partial<AlertRow>);
    } catch (err) {
      setSectionState("agenda_items", "error", err instanceof Error ? err.message : String(err));
    }
  }

  async function handlePublishLibItem(skipValidation = false) {
    if (hasNewAgenda && sections.agenda_items.status !== "done") {
      setSectionState("library_item", "error", "Create Agenda Items first — they must exist before the library item can link to them.");
      return;
    }
    setSectionState("library_item", "loading");
    try {
      // Inject the type-derived Date (meeting date / comment date / publication date) if the
      // user didn't type anything in the Date field. This ensures the correct date reaches
      // Bubble even when the agent-provided date was wrong for the given type.
      const effectiveEdits = { ...libEdits };
      if (effectiveEdits["Date"] === undefined && derivedDate) {
        effectiveEdits["Date"] = derivedDate;
      }
      if (Object.keys(effectiveEdits).length > 0) {
        const updated = applyEditsToPreview(libPreview, effectiveEdits, LIB_EDIT_MAP);
        await patchBubbleAction("library_item_preview", updated);
      }
      let result: Record<string, unknown>;
      try {
        result = await callSync("library_item", skipValidation);
      } catch (err) {
        const e = err as Error & { isValidation?: boolean };
        if (e.isValidation) {
          setSectionState("library_item", "idle");
          setValidationPopup({ section: "library_item", message: e.message, onConfirm: () => { setValidationPopup(null); void handlePublishLibItem(true); } });
          return;
        }
        throw err;
      }
      if (!result.bubble_library_item_id) {
        throw new Error(String(result.error ?? "Library item was not created/updated — no ID returned from Eidarix"));
      }
      setSectionState("library_item", "done");
      onFieldPatched({ bubble_sync_status: "syncing", bubble_library_item_id: result.bubble_library_item_id as string });
    } catch (err) {
      setSectionState("library_item", "error", err instanceof Error ? err.message : String(err));
    }
  }

  async function handlePublishTranscript() {
    setSectionState("transcript", "loading");
    try {
      const resp = await fetch("/api/ingest/approve-transcript", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent_call_id: agentCallId }),
      });
      const data = await resp.json() as Record<string, unknown>;
      if (!resp.ok) throw new Error(String(data.error ?? "Ingest failed"));
      setSectionState("transcript", "done");
      onFieldPatched({ ingest_status: "approved" });
    } catch (err) {
      setSectionState("transcript", "error", err instanceof Error ? err.message : String(err));
    }
  }

  async function handlePublishDocument() {
    setSectionState("document", "loading");
    try {
      const resp = await fetch("/api/ingest/approve-document", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent_call_id: agentCallId, library_item_url: libItemUrl }),
      });
      const data = await resp.json() as Record<string, unknown>;
      if (!resp.ok) throw new Error(String(data.error ?? "Ingest failed"));
      setSectionState("document", "done");
    } catch (err) {
      setSectionState("document", "error", err instanceof Error ? err.message : String(err));
    }
  }

  const libItemTitle = (() => {
    const raw = row.library_item_preliminary_title;
    if (!raw) return "";
    if (typeof raw === "object" && raw !== null) return String((raw as Record<string, unknown>).title ?? "");
    return String(raw);
  })();

  // Vector store destinations (dropdown shown on the Library Item card).
  // When a row has both a transcript AND a document URL, transcript ingest takes priority
  // (the transcript IS the authoritative text to vectorize).
  const libraryVectorStoreOptions = useMemo((): VectorStoreOption[] => {
    const opts: VectorStoreOption[] = [];
    if (hasTranscript) {
      opts.push({
        value: "newsreel",
        label: "Newsreel Knowledge Base",
        onSelect: () => void handlePublishTranscript(),
        state: sections.transcript,
      });
    } else if (hasDocument) {
      opts.push({
        value: "newsreel",
        label: "Newsreel Knowledge Base",
        onSelect: () => void handlePublishDocument(),
        state: sections.document,
      });
    }
    return opts;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hasTranscript, hasDocument, sections.transcript, sections.document]);

  const documentFields: FieldEntry[] = [
    { label: "Destination", agentField: "—", value: "Newsreel Knowledge Base", readOnly: true },
  ];
  if (libItemTitle && libItemTitle !== "N/A") documentFields.push({ label: "Title", agentField: "library_item_preliminary_title.title", value: libItemTitle, readOnly: true });
  if (libItemUrl) documentFields.push({ label: "URL", agentField: "library_item_url", value: libItemUrl, readOnly: true });

  const transcriptFields: FieldEntry[] = [
    { label: "Destination", agentField: "—", value: "Newsreel Knowledge Base", readOnly: true },
  ];
  if (transcriptKey) transcriptFields.push({ label: "File", agentField: "transcript_s3_key", value: transcriptKey.split("/").pop() ?? transcriptKey, readOnly: true });

  const hasAnySection = !!(eventAction || libAction || hasAgenda || hasTranscript || hasDocument);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={e => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div className="relative bg-white rounded-xl shadow-2xl w-full max-w-2xl max-h-[90vh] flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-200 shrink-0">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold text-gray-900">Content Gate</h2>
            {!!row.alert_title && (
              <p className="text-[11px] text-gray-500 mt-0.5 truncate">{String(row.alert_title)}</p>
            )}
          </div>
          <button onClick={onClose} className="ml-4 shrink-0 text-gray-400 hover:text-gray-600 transition-colors" aria-label="Close">
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* Sections */}
        <div className="overflow-y-auto flex-1 px-5 py-4 space-y-3">
          {!hasAnySection && (
            <p className="text-sm text-gray-400 text-center py-10">No publishable actions for this row.</p>
          )}

          {/* Pre-flight validation banner */}
          {hasBubbleSections && validation === "loading" && (
            <div className="text-[11px] text-gray-400 italic px-1">Checking field resolution…</div>
          )}
          {hasBubbleSections && validation !== "loading" && validation !== null && (
            <ValidationBanner result={validation} />
          )}

          {hasNewAgenda && (
            <div className="rounded-lg border border-green-200 overflow-hidden">
              {/* Header */}
              <div className="flex items-center justify-between px-4 py-2.5 bg-green-50">
                <div className="flex items-center gap-2">
                  <span className="text-[10px] font-bold uppercase tracking-wider text-indigo-600">Bubble</span>
                  <span className="text-[11px] text-gray-400">›</span>
                  <span className="text-[12px] font-semibold text-gray-700">
                    Create {activeNewAgendaItems.length} Agenda Item{activeNewAgendaItems.length !== 1 ? "s" : ""} in Bubble
                  </span>
                </div>
                {sections.agenda_items.status === "done" ? (
                  <span className="text-[11px] text-green-600 font-semibold">✓ Done</span>
                ) : sections.agenda_items.status === "error" ? (
                  <span className="text-[11px] text-red-500 font-medium">✕ Failed</span>
                ) : null}
              </div>
              {/* Item list — compact: title input + chronicle topics searchable combobox */}
              <div className="px-4 py-3 space-y-0">
                {newAgendaItems.map((item, i) => {
                  if (deletedNewIndices.has(i)) return null;
                  const isDone = sections.agenda_items.status === "done";
                  const activeIndex = newAgendaItems.slice(0, i + 1).filter((_, j) => !deletedNewIndices.has(j)).length;
                  const itemEdits = agendaItemEdits[i] ?? {};
                  const displayedTitle = itemEdits["Title"] ?? stripStatusPrefix(item.title);
                  // Topics: user-edited > enriched-from-doc-extraction > raw preview
                  const editedTopics = multiFromEditValue(itemEdits["Chronicle Topics"]);
                  const displayedTopics = editedTopics ?? (enrichedNewAgendaItems[i]?.chronicle_topics ?? item.chronicle_topics) ?? [];
                  return (
                    <div key={i} className={`py-2.5 ${i > 0 ? "border-t border-gray-100" : ""}`}>
                      <div className="flex items-start gap-2">
                        <span className="text-[11px] text-gray-400 pt-1.5 flex-shrink-0">{activeIndex}.</span>
                        <div className="flex-1 min-w-0 space-y-1.5">
                          {isDone ? (
                            <p className="text-[12px] font-semibold text-gray-800">{displayedTitle}</p>
                          ) : (
                            <input
                              type="text"
                              value={displayedTitle}
                              onChange={e => updateAgendaItemField(i, "Title", e.target.value)}
                              className="w-full text-[12px] font-semibold text-gray-800 bg-transparent border-b border-gray-200 hover:border-gray-400 focus:border-indigo-400 focus:outline-none pb-0.5 transition-colors"
                            />
                          )}
                          {isDone ? (
                            displayedTopics.length > 0 && (
                              <div className="flex flex-wrap gap-1">
                                {displayedTopics.map(t => (
                                  <span key={t} className="text-[10px] bg-violet-100 text-violet-700 rounded px-1.5 py-0.5 border border-violet-200 leading-tight">{t}</span>
                                ))}
                              </div>
                            )
                          ) : (
                            <SearchableCombobox
                              mode="multi"
                              options={bubbleOptions.chronicletopic ?? []}
                              loading={bubbleOptions.chronicletopic === undefined}
                              values={displayedTopics}
                              onChange={names => updateAgendaItemField(i, "Chronicle Topics", multiToEditValue(names))}
                              placeholder="Add chronicle topics…"
                            />
                          )}
                        </div>
                        {!isDone && (
                          <button
                            onClick={() => setDeletedNewIndices(prev => { const s = new Set(Array.from(prev)); s.add(i); return s; })}
                            className="flex-shrink-0 text-gray-300 hover:text-red-400 transition-colors text-[16px] leading-none pt-1"
                            title="Remove this agenda item"
                          >×</button>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
              {/* Error */}
              {sections.agenda_items.status === "error" && sections.agenda_items.error && (
                <div className="text-[11px] text-red-700 bg-red-50 border-t border-red-200 px-4 py-2 leading-relaxed">
                  {sections.agenda_items.error}
                </div>
              )}
              {/* Button */}
              {sections.agenda_items.status !== "done" && (
                <div className="flex justify-end px-4 pb-3">
                  <button
                    onClick={() => handleCreateAgendaItems()}
                    disabled={sections.agenda_items.status === "loading"}
                    className="text-[11px] font-medium text-white bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 rounded-md px-3 py-1.5 transition-colors"
                  >
                    {sections.agenda_items.status === "loading" ? "Working…" : `Create ${activeNewAgendaItems.length} Agenda Item${activeNewAgendaItems.length !== 1 ? "s" : ""}`}
                  </button>
                </div>
              )}
            </div>
          )}

          {libAction && (
            <div className="space-y-1.5">
              {sections.library_item.status === "error" && sections.library_item.error && (
                <div className="text-[11px] text-red-700 bg-red-50 border border-red-200 rounded-md px-3 py-2 leading-relaxed">
                  {sections.library_item.error}
                </div>
              )}
              <Section
                title={libAction === "create" ? "Create Library Item" : "Update Library Item"}
                subtitle={safeStr(libPreview.title) || libItemTitle || undefined}
                objectType="Library Item"
                system="Bubble"
                action={libAction}
                fields={libFields}
                editState={libEdits}
                onEdit={(label, value) => setLibEdits(prev => ({ ...prev, [label]: value }))}
                sectionState={sections.library_item}
                onPublish={() => handlePublishLibItem()}
                currentRecord={libRecord}
                isLoadingRecord={loadingLibRecord}
                schemaLabels={schemaLabels}
                bubbleOptions={bubbleOptions}
                vectorStoreOptions={libraryVectorStoreOptions}
              />
            </div>
          )}

          {eventAction && (
            <div className="space-y-1.5">
              {sections.event.status === "error" && sections.event.error && (
                <div className="text-[11px] text-red-700 bg-red-50 border border-red-200 rounded-md px-3 py-2 leading-relaxed">
                  {sections.event.error}
                </div>
              )}
              <Section
                title={eventAction === "create" ? "Create Event" : "Update Event"}
                subtitle={safeStr(eventPreview.title) || safeStr((row as Record<string, unknown>).event_title) || undefined}
                objectType="Calendar Event"
                system="Bubble"
                action={eventAction}
                fields={eventFields}
                editState={eventEdits}
                onEdit={(label, value) => setEventEdits(prev => ({ ...prev, [label]: value }))}
                sectionState={sections.event}
                onPublish={() => handlePublishEvent()}
                currentRecord={eventRecord}
                isLoadingRecord={loadingEventRecord}
                schemaLabels={schemaLabels}
                bubbleOptions={bubbleOptions}
              />
            </div>
          )}

          {/* Standalone vector-store section — only renders when there's no Library Item card to attach to */}
          {!libAction && hasTranscript && (
            <Section
              title="Publish Transcript"
              subtitle={transcriptKey.split("/").pop() || undefined}
              objectType="Meeting Transcript"
              system="Newsreel"
              action={null}
              fields={transcriptFields}
              editState={{}}
              onEdit={() => {}}
              sectionState={sections.transcript}
              schemaLabels={schemaLabels}
              bubbleOptions={bubbleOptions}
              vectorStoreOptions={libraryVectorStoreOptions}
            />
          )}

          {!libAction && !hasTranscript && hasDocument && (
            <Section
              title="Publish Document"
              subtitle={libItemTitle || undefined}
              objectType="Library Document"
              system="Newsreel"
              action={null}
              fields={documentFields}
              editState={{}}
              onEdit={() => {}}
              sectionState={sections.document}
              schemaLabels={schemaLabels}
              bubbleOptions={bubbleOptions}
              vectorStoreOptions={libraryVectorStoreOptions}
            />
          )}
        </div>

        {/* Footer */}
        <div className="flex justify-end px-5 py-3 border-t border-gray-100 shrink-0">
          <button
            onClick={onClose}
            className="text-sm text-gray-500 hover:text-gray-700 px-4 py-1.5 rounded-md border border-gray-200 hover:border-gray-300 transition-colors"
          >
            Close
          </button>
        </div>

        {/* Validation warning popup — shown when pre-ECS check fails */}
        {validationPopup && (
          <div className="absolute inset-0 bg-black/30 flex items-center justify-center z-10 rounded-xl">
            <div className="bg-white rounded-lg shadow-xl border border-gray-200 p-5 mx-6 max-w-sm w-full">
              <p className="text-[13px] font-semibold text-gray-800 mb-2">Cannot sync — data issue</p>
              <p className="text-[11px] text-red-700 bg-red-50 border border-red-200 rounded px-3 py-2 mb-4 leading-relaxed">
                {validationPopup.message}
              </p>
              <div className="flex gap-2 justify-end">
                <button
                  onClick={dismissValidation}
                  className="text-[12px] text-gray-600 hover:text-gray-800 px-3 py-1.5 rounded-md border border-gray-200 hover:border-gray-300 transition-colors"
                >
                  Close
                </button>
                <button
                  onClick={() => validationPopup.onConfirm()}
                  className="text-[12px] font-medium text-white bg-indigo-600 hover:bg-indigo-700 px-3 py-1.5 rounded-md transition-colors"
                >
                  Create Anyway
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
