"use client";

import React, { useEffect, useState, useMemo } from "react";
import type { AlertRow } from "@/app/alerts/AlertsTable";

interface ContentGateModalProps {
  row: AlertRow;
  onClose: () => void;
  onFieldPatched: (fields: Partial<AlertRow>) => void;
}

type SectionStatus = "idle" | "loading" | "done" | "error";

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
  label: string;
  value?: string;     // plain text value
  topics?: string[];  // rendered as chips
  list?: string[];    // rendered as a numbered list
  changed?: boolean;  // highlight row amber for UPDATE
  readOnly?: boolean; // no input shown
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

function topicsFromFieldIds(fieldIds: unknown): string[] {
  if (!fieldIds || typeof fieldIds !== "object") return [];
  const raw = (fieldIds as Record<string, unknown>)["topics___dt_list_custom_newsreel_update"];
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
  const topics    = topicsFromFieldIds(ep.field_ids);
  const eventType = deriveEventType(alertType);

  const mk = (label: string, value: string, opts?: Partial<FieldEntry>): FieldEntry => ({
    label, value,
    changed: changedLabels.size > 0 ? changedLabels.has(label) : undefined,
    ...opts,
  });

  const entries: FieldEntry[] = [
    mk("Title",           notNA(title) ? title : "—"),
    mk("Type",            eventType,             { readOnly: true }),
    mk("Start Date/Time", notNA(start) ? start : "—"),
    mk("End Date/Time",   notNA(end)   ? end   : "—"),
    mk("Full Day",        isFullDay ? "Yes" : "No", { readOnly: true }),
    mk("Organizations",   group || "—"),
    mk("Location URL",    notNA(url)    ? url    : "—"),
    mk("Call-In",         notNA(callIn) ? callIn : "—"),
    mk("Timezone",        "America/New_York",    { readOnly: true }),
  ];

  // Chronicle Topics — always shown
  entries.push({
    label: "Chronicle Topics",
    ...(topics.length > 0 ? { topics } : { value: "—" }),
    readOnly: true,
    changed: changedLabels.size > 0 ? changedLabels.has("Chronicle Topics") : undefined,
  });

  // Linked Agenda Items — always shown (agenda items linked to this event)
  entries.push({
    label: "Linked Agenda Items",
    ...(agendaItems.length > 0
      ? { list: agendaItems.map(item => stripStatusPrefix(item.title)) }
      : { value: "—" }),
    readOnly: true,
  });

  // Agenda — always shown when there's a library item being linked (Bubble field name)
  if (libAction) {
    const libTitle = safeStr(lp.title);
    entries.push(mk("Agenda", notNA(libTitle) ? libTitle : "—", { readOnly: true }));
  }

  return entries;
}

/**
 * Build the full library item field list from preview data.
 */
function buildLibFields(
  lp: Record<string, unknown>,
  agendaItems: AgendaItemPreview[],
  hasAgenda: boolean,
  changedLabels: Set<string>,
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
  const date        = fd["Date"] ?? "";
  const docType     = fd["Doc Type"] ?? "";

  const mk = (label: string, value: string, opts?: Partial<FieldEntry>): FieldEntry => ({
    label, value,
    changed: changedLabels.size > 0 ? changedLabels.has(label) : undefined,
    ...opts,
  });

  const entries: FieldEntry[] = [
    mk("Name",          notNA(title)    ? title    : "—"),
    mk("URL",           notNA(url)      ? url      : "—"),
    mk("File",          notNA(filename) ? filename : "—"),
    mk("Type",          notNA(type)     ? type     : "—", { readOnly: true }),
    mk("Organizations", group || "—"),
    mk("Date",          notNA(date)     ? date     : "—"),
    mk("Date Display",  "Full date",                { readOnly: true }),
    mk("Status",        "Active",                   { readOnly: true }),
  ];

  // Chronicle Topics — always shown
  entries.push({
    label: "Chronicle Topics",
    ...(topics.length > 0 ? { topics } : { value: "—" }),
    readOnly: true,
    changed: changedLabels.size > 0 ? changedLabels.has("Chronicle Topics") : undefined,
  });

  if (notNA(description)) entries.push(mk("Summary", description));
  if (notNA(docType))     entries.push(mk("Doc Type", docType, { readOnly: true }));

  // Linked Agenda Items — always shown
  entries.push({
    label: "Linked Agenda Items",
    ...(hasAgenda && agendaItems.length > 0
      ? { list: agendaItems.map(item => item.title) }
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

const EVENT_EDIT_MAP: Record<string, { previewKey: string; fieldIdsKey?: string; isList?: boolean }> = {
  "Title":           { previewKey: "title",          fieldIdsKey: "title_text" },
  "Start Date/Time": { previewKey: "start_datetime", fieldIdsKey: "date_date" },
  "End Date/Time":   { previewKey: "end_datetime",   fieldIdsKey: "length_end_time_date" },
  "Organizations":   { previewKey: "group",          fieldIdsKey: "orgs__list_custom_organization", isList: true },
  "Location URL":    { previewKey: "url" },
  "Call-In":         { previewKey: "call_in",        fieldIdsKey: "phone_number_and_access_code_text" },
};

const LIB_EDIT_MAP: Record<string, { previewKey?: string; fieldIdsKey?: string; isList?: boolean }> = {
  "Name":          { previewKey: "title",    fieldIdsKey: "name_text" },
  "URL":           { previewKey: "url",      fieldIdsKey: "url_text" },
  "File":          { previewKey: "filename", fieldIdsKey: "file_name_text" },
  "Organizations": { previewKey: "group",    fieldIdsKey: "organizations_list_custom_organization", isList: true },
  "Date":          { fieldIdsKey: "date_date" },
  "Summary":       { fieldIdsKey: "description_text" },
};

function applyEditsToPreview(
  preview: Record<string, unknown>,
  edits: Record<string, string>,
  editMap: Record<string, { previewKey?: string; fieldIdsKey?: string; isList?: boolean }>,
): Record<string, unknown> {
  const out = { ...preview };
  const outFieldIds = { ...((preview.field_ids as Record<string, unknown>) ?? {}) };
  const outFields   = { ...((preview.fields as Record<string, unknown>) ?? {}) };

  for (const [label, value] of Object.entries(edits)) {
    if (!value) continue;
    const map = editMap[label];
    if (!map) continue;
    const coerced: unknown = map.isList
      ? value.split(",").map(s => s.trim()).filter(Boolean)
      : value;
    if (map.previewKey)  out[map.previewKey] = coerced;
    if (map.fieldIdsKey) outFieldIds[map.fieldIdsKey] = coerced;
    outFields[label] = value;
  }
  out.field_ids = outFieldIds;
  out.fields    = outFields;
  return out;
}

// ─── Section card component ───────────────────────────────────────────────────

interface SectionProps {
  title: string;
  system: "Bubble" | "Newsreel";
  action: BubbleAction;
  fields: FieldEntry[];
  editState: Record<string, string>;
  onEdit: (label: string, value: string) => void;
  sectionState: SectionState;
  onPublish: () => void;
  currentRecord?: BubbleRecord | null;
  isLoadingRecord?: boolean;
  agendaItems?: AgendaItemPreview[];
}

function FieldTable({
  fields,
  editState,
  onEdit,
}: {
  fields: FieldEntry[];
  editState: Record<string, string>;
  onEdit: (label: string, value: string) => void;
}) {
  return (
    <table className="w-full">
      <tbody>
        {fields.map((f, i) => {
          const isChanged = !!f.changed;
          const rowClass = `${isChanged ? "bg-amber-50" : ""} border-b border-gray-50 last:border-0`;

          return (
            <tr key={i} className={rowClass}>
              <td
                className={`py-1.5 pr-3 text-[11px] font-medium whitespace-nowrap align-top ${isChanged ? "text-amber-700" : "text-gray-400"}`}
                style={{ width: "32%" }}
              >
                {f.label}
              </td>
              <td className="py-1.5">
                {f.topics !== undefined ? (
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

function Section({
  title, system, action, fields, editState, onEdit,
  sectionState, onPublish, currentRecord, isLoadingRecord, agendaItems,
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

  // Button label = action-specific title for Bubble sections
  const publishLabel = system === "Bubble" ? title
    : title === "Publish Transcript" ? "Publish Transcript"
    : "Publish to Newsreel";

  // For UPDATE: if loading finished and record not found → minimal card
  const recordNotFound = action === "update" && !isLoadingRecord && currentRecord != null && !currentRecord.found;

  if (recordNotFound) {
    return (
      <div className={`rounded-lg border ${borderColor} overflow-hidden`}>
        <div className={`flex items-center justify-between px-4 py-2.5 ${headerBg}`}>
          <div className="flex items-center gap-2">
            <span className={`text-[10px] font-bold uppercase tracking-wider ${systemColor}`}>{system}</span>
            <span className="text-[11px] text-gray-400">›</span>
            <span className="text-[12px] font-semibold text-gray-700">{title}</span>
          </div>
        </div>
        <div className="px-4 py-2.5 flex items-center justify-between">
          <p className="text-[12px] text-amber-700 font-medium">
            Record not found in Bubble — does not exist yet
          </p>
          {!isDone && (
            <button
              onClick={onPublish}
              disabled={isLoading}
              className="ml-4 text-[11px] font-medium text-white bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 rounded-md px-3 py-1.5 transition-colors whitespace-nowrap"
            >
              {isLoading ? "Working…" : publishLabel}
            </button>
          )}
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
      <div className={`flex items-center justify-between px-4 py-2.5 ${headerBg}`}>
        <div className="flex items-center gap-2">
          <span className={`text-[10px] font-bold uppercase tracking-wider ${systemColor}`}>{system}</span>
          <span className="text-[11px] text-gray-400">›</span>
          <span className="text-[12px] font-semibold text-gray-700">{title}</span>
        </div>
        <div className="flex items-center gap-2">
          {action === "update" && isLoadingRecord && (
            <span className="text-[10px] text-gray-400 italic">Loading current record…</span>
          )}
          {isDone ? (
            <span className="text-[11px] text-green-600 font-semibold">✓ Done</span>
          ) : sectionState.status === "error" ? (
            <span className="text-[11px] text-red-500 font-medium" title={sectionState.error}>✕ Failed</span>
          ) : null}
        </div>
      </div>

      {/* Fields */}
      {displayFields.length > 0 && (
        <div className="px-4 py-3">
          <FieldTable fields={displayFields} editState={editState} onEdit={onEdit} />
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

      {/* Footer */}
      {!isDone && (
        <div className="flex justify-end px-4 py-2.5 border-t border-gray-100 bg-gray-50/50">
          <button
            onClick={onPublish}
            disabled={isLoading}
            className="text-[11px] font-medium text-white bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed rounded-md px-3 py-1.5 transition-colors whitespace-nowrap"
          >
            {isLoading ? "Working…" : publishLabel}
          </button>
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
  const agendaItems    = ((bubbleAction?.agenda_item_previews ?? []) as AgendaItemPreview[]);
  const hasAgenda      = !!bubbleAction?.agenda_items && agendaItems.length > 0;
  const newAgendaItems = agendaItems.filter(a => !a.status || a.status.toLowerCase() === "new");
  const existingAgendaItems = agendaItems.filter(a => a.status && a.status.toLowerCase() !== "new");
  // These must be declared before the derived activeNewAgendaItems that use them
  const [deletedNewIndices, setDeletedNewIndices] = useState<Set<number>>(new Set());
  const [agendaItemTitleEdits, setAgendaItemTitleEdits] = useState<Record<number, string>>({});
  // Apply title edits and deletions — these drive the UI and what gets patched to the API
  const activeNewAgendaItems: AgendaItemPreview[] = newAgendaItems
    .map((item, i) => agendaItemTitleEdits[i] !== undefined ? { ...item, title: agendaItemTitleEdits[i] } : item)
    .filter((_, i) => !deletedNewIndices.has(i));
  const activeAgendaItems: AgendaItemPreview[] = [...activeNewAgendaItems, ...existingAgendaItems];
  const hasNewAgenda   = hasAgenda && activeNewAgendaItems.length > 0;
  const transcriptKey = String((row.transcript_s3_key ?? row.manual_transcript_s3_key) ?? "");
  const hasTranscript = !!transcriptKey && row.alert_type === "New Meeting Transcript Available";
  const libItemUrl    = String(row.library_item_url ?? "");
  const hasDocument   = !!(libItemUrl && libItemUrl !== "N/A" && libItemUrl.startsWith("http"));

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

  const eventFields = useMemo(
    () => buildEventFields(eventPreview, libPreview, libAction, alertType, eventChangedLabels, activeAgendaItems),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [eventPreview, libPreview, libAction, alertType, eventChangedLabels, activeAgendaItems],
  );
  const libFields = useMemo(
    () => buildLibFields(libPreview, activeAgendaItems, hasAgenda, libChangedLabels),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [libPreview, activeAgendaItems, hasAgenda, libChangedLabels],
  );

  const [eventEdits, setEventEdits]               = useState<Record<string, string>>({});
  const [libEdits, setLibEdits]                   = useState<Record<string, string>>({});

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
        if (d.rows?.[0]?.ingest_status === "approved") {
          setSections(prev => ({ ...prev, document: { status: "done" } }));
        }
      })
      .catch(() => {});
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
      // Patch stored bubble_action with the active item list (respects deletions + title edits)
      if (deletedNewIndices.size > 0 || Object.keys(agendaItemTitleEdits).length > 0) {
        const patchedPreviews = [...activeNewAgendaItems, ...existingAgendaItems];
        const updated = { ...(bubbleAction ?? {}), agenda_item_previews: patchedPreviews as unknown[] };
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
      if (Object.keys(libEdits).length > 0) {
        const updated = applyEditsToPreview(libPreview, libEdits, LIB_EDIT_MAP);
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

  const documentFields: FieldEntry[] = [];
  if (libItemTitle && libItemTitle !== "N/A") documentFields.push({ label: "Title", value: libItemTitle, readOnly: true });
  if (libItemUrl) documentFields.push({ label: "URL", value: libItemUrl, readOnly: true });

  const transcriptFields: FieldEntry[] = [];
  if (transcriptKey) transcriptFields.push({ label: "File", value: transcriptKey.split("/").pop() ?? transcriptKey, readOnly: true });

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

          {hasNewAgenda && (
            <div className="rounded-lg border border-green-200 overflow-hidden">
              {/* Header */}
              <div className="flex items-center justify-between px-4 py-2.5 bg-green-50">
                <div className="flex items-center gap-2">
                  <span className="text-[10px] font-bold uppercase tracking-wider text-indigo-600">Bubble</span>
                  <span className="text-[11px] text-gray-400">›</span>
                  <span className="text-[12px] font-semibold text-gray-700">
                    Create {activeNewAgendaItems.length} Agenda Item{activeNewAgendaItems.length !== 1 ? "s" : ""}
                  </span>
                </div>
                {sections.agenda_items.status === "done" ? (
                  <span className="text-[11px] text-green-600 font-semibold">✓ Done</span>
                ) : sections.agenda_items.status === "error" ? (
                  <span className="text-[11px] text-red-500 font-medium">✕ Failed</span>
                ) : null}
              </div>
              {/* Item list — each item is editable + deletable */}
              <div className="px-4 py-3 space-y-0">
                {newAgendaItems.map((item, i) => {
                  if (deletedNewIndices.has(i)) return null;
                  const displayedTitle = agendaItemTitleEdits[i] ?? stripStatusPrefix(item.title);
                  const isDone = sections.agenda_items.status === "done";
                  return (
                    <div key={i} className={`py-2.5 ${i > 0 ? "border-t border-gray-100" : ""}`}>
                      <div className="flex items-start gap-2">
                        <span className="text-[11px] text-gray-400 pt-1.5 flex-shrink-0">{newAgendaItems.slice(0, i + 1).filter((_, j) => !deletedNewIndices.has(j)).length}.</span>
                        <div className="flex-1 min-w-0">
                          {isDone ? (
                            <p className="text-[12px] font-semibold text-gray-800">{displayedTitle}</p>
                          ) : (
                            <input
                              type="text"
                              value={displayedTitle}
                              onChange={e => setAgendaItemTitleEdits(prev => ({ ...prev, [i]: e.target.value }))}
                              className="w-full text-[12px] font-semibold text-gray-800 bg-transparent border-b border-gray-200 hover:border-gray-400 focus:border-indigo-400 focus:outline-none pb-0.5 transition-colors"
                            />
                          )}
                          {(item.chronicle_topics ?? []).length > 0 && (
                            <div className="flex flex-wrap gap-1 mt-1.5">
                              {(item.chronicle_topics ?? []).map(t => (
                                <span key={t} className="text-[10px] bg-violet-100 text-violet-700 rounded px-1.5 py-0.5 border border-violet-200 leading-tight">{t}</span>
                              ))}
                            </div>
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
                system="Bubble"
                action={libAction}
                fields={libFields}
                editState={libEdits}
                onEdit={(label, value) => setLibEdits(prev => ({ ...prev, [label]: value }))}
                sectionState={sections.library_item}
                onPublish={() => handlePublishLibItem()}
                currentRecord={libRecord}
                isLoadingRecord={loadingLibRecord}
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
                system="Bubble"
                action={eventAction}
                fields={eventFields}
                editState={eventEdits}
                onEdit={(label, value) => setEventEdits(prev => ({ ...prev, [label]: value }))}
                sectionState={sections.event}
                onPublish={() => handlePublishEvent()}
                currentRecord={eventRecord}
                isLoadingRecord={loadingEventRecord}
              />
            </div>
          )}

          {hasTranscript && (
            <Section
              title="Publish Transcript"
              system="Newsreel"
              action={null}
              fields={transcriptFields}
              editState={{}}
              onEdit={() => {}}
              sectionState={sections.transcript}
              onPublish={handlePublishTranscript}
            />
          )}

          {hasDocument && (
            <Section
              title="Publish Document"
              system="Newsreel"
              action={null}
              fields={documentFields}
              editState={{}}
              onEdit={() => {}}
              sectionState={sections.document}
              onPublish={handlePublishDocument}
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
