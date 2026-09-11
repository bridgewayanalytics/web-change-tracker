"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";
import { ContentGateModal } from "@/components/ContentGateModal";

// ─── Page labels ─────────────────────────────────────────────────────────────

const PAGE_LABELS: Record<string, string> = {
  "naic.newsroom": "NAIC Newsroom",
  "naic.resource_center": "NAIC Resource Center",
  "naic.capital_markets_bureau": "Capital Markets Bureau",
  "naic.e.index": "E Committee Index",
  "naic.e.statutory_accounting_principles_wg": "Statutory Accounting Principles Working Group",
  "naic.e.blanks_wg": "Blanks Working Group",
  "naic.e.capital_adequacy_tf": "Capital Adequacy Task Force",
  "naic.e.health_rbc_wg": "Health Risk-Based Capital Working Group",
  "naic.e.life_rbc_wg": "Life Risk-Based Capital Working Group",
  "naic.ae.generator_economic_scenarios_sg": "Generator Economic Scenarios Subgroup",
  "naic.e.property_casualty_rbc_wg": "Property & Casualty Risk-Based Capital Working Group",
  "naic.e.rbc_investment_risk_evaluation_wg": "Risk-Based Capital Investment Risk Evaluation Working Group",
  "naic.e.receivership_insolvency_tf": "Receivership & Insolvency Task Force",
  "naic.e.financial_stability_tf": "Financial Stability Task Force",
  "naic.e.macroprudential_wg": "Macroprudential Working Group",
  "naic.e.group_capital_calculation_wg": "Group Capital Calculation Working Group",
  "naic.e.invested_assets_tf": "Invested Assets Task Force",
  "naic.e.credit_rating_provider_wg": "Credit Rating Provider Working Group",
  "naic.e.investment_analysis_wg": "Investment Analysis Working Group",
  "naic.e.investment_designation_analysis_wg": "Investment Designation Analysis Working Group",
  "naic.e.reinsurance.index": "Reinsurance (E) Index",
  "naic.e.reinsurance_financial_analysis_wg": "Reinsurance Financial Analysis Working Group",
  "naic.e.valuation_analysis_wg": "Valuation Analysis Working Group",
  "naic.ex.index": "EX Committee Index",
  "naic.ex.climate_resiliency_tf": "Climate Resiliency Task Force",
  "naic.ex.rbc_model_governance_tf": "RBC Model Governance Task Force",
  "naic.a.index": "A Committee Index",
  "naic.a.latf": "Life Actuarial Task Force",
  "naic.a.latf.vm22sg": "VM-22 Subgroup (LATF)",
};

// ─── Types ────────────────────────────────────────────────────────────────────

export type AlertRow = Record<string, unknown>;

interface ConfigData {
  hash: string;
  model: string;
  updated_at: string;
}

interface RerunResult {
  run_id: string;
  target_id: string;
  rerun_timestamp?: string;
  config_hash?: string;
  agent_call_id?: string;
  original_rows?: AlertRow[];
  rerun_rows?: AlertRow[];
  error?: string;
  // fallback single-object schema
  original?: AlertRow;
  rerun?: AlertRow;
}

interface ActiveTask {
  taskId: string;
  row: AlertRow;
}

// ─── Column config ────────────────────────────────────────────────────────────

// Pipeline metadata and raw array columns — never shown as table columns.
const METADATA_COLS = new Set([
  "run_id", "run_timestamp", "target_id", "source_url", "config_hash",
  "events", "agenda_items", "agent_call_id",
  // ingest gate fields — displayed in dedicated columns, not as data columns
  "recording_s3_key", "transcript_s3_key", "transcript_chunks_s3_key",
  "manual_transcript_s3_key", "ingest_status",
  // bubble sync fields
  "bubble_action", "bubble_sync_status",
]);

/**
 * Field aliases: maps current schema field name → the old field name it replaced.
 *
 * When a field is renamed in the Bubble admin (which updates output_json_schema
 * in DynamoDB), old rows in S3 still store the previous key. Adding an alias here
 * makes old rows display under the new column header without any data migration —
 * a rename is a display change, not a data change.
 *
 * To add a new alias when a field is renamed: { newName: oldName }.
 */
const LEGACY_ALIASES: Record<string, string> = {
  // ── current stable IDs → previous stable IDs (backend field rename, May 2026) ─
  // The new sync backend derives IDs via to_field_id(label). Several IDs changed.
  // These chains let old rows display correctly under the new column names.
  alert_date_time:                                        "alert_datetime_et",
  event_start_date_time:                                  "event_start_datetime_et",
  event_end_date_time:                                    "event_end_datetime_et",
  event_call_in_number_access_code:                       "event_call_in_number_and_access_code",
  agenda_item_title_chronicle_topics:                     "agenda_items",
  is_the_alert_relevant_for_an_art_newsreel_article:      "art_newsreel_relevance",

  // ── previous stable IDs → label-as-key era ───────────────────────────────────
  alert_type:                              "Alert Type1",
  alert_title:                             "Alert Title",
  alert_description:                       "Alert Description",
  alert_url:                               "Alert URL",
  organization:                            "Organization",
  alert_datetime_et:                       "Alert Date & Time (ET)",
  event_title:                             "Event Title",
  event_start_datetime_et:                 "Event Start Date & Time (ET)",
  event_end_datetime_et:                   "Event End Date & Time (ET)",
  event_duration:                          "Event Duration",
  event_is_full_day:                       "Event is Full Day",
  event_url:                               "Event URL",
  event_call_in_number_and_access_code:    "Event Call-In Number & Access Code",
  agenda_items:                            "agenda_item_title_and_chronicle_topics",
  agenda_item_title_and_chronicle_topics:  "Agenda Item Title & Chronicle Topics",
  library_item_preliminary_title:          "Library Item Preliminary Title",
  library_item_url:                        "Library Item URL",
  library_items_file_name:                 "Library Items File Name",
  art_newsreel_relevance:                  "is_alert_relevant_for_art_newsreel",
  is_alert_relevant_for_art_newsreel:      "Is the Alert Relevant for an ART Newsreel article?",

  // ── intermediate snake_case era → very old snake_case ────────────────────────
  event_start_datetime:                    "event_start_date_time",
  event_end_datetime:                      "event_end_date_time",
  event_call_in_access_code:               "event_call_in_number_access_code",

  // ── agenda sub-type fields: stable ID → old intermediate → label-as-key ─────
  agenda_item_title_official:              "agenda_item_official_title",
  agenda_item_official_title:              "Agenda Item Title - Official",
  agenda_item_standardized_id:             "Agenda Item - Standardized ID",
  agenda_item_official_id:                 "Agenda Item - Official ID",

  // ── label-as-key era → very old snake_case ───────────────────────────────────
  "Alert Type1":                           "alert_type",
  "Alert Title":                           "alert_title",
  "Alert Description":                     "alert_description",
  "Alert URL":                             "alert_url",
  "Organization":                          "organization",
  "Alert Date & Time (ET)":                "alert_date_time",
  "Event Title":                           "event_title",
  "Event Start Date & Time (ET)":          "event_start_datetime",
  "Event End Date & Time (ET)":            "event_end_datetime",
  "Event Duration":                        "event_duration",
  "Event is Full Day":                     "event_is_full_day",
  "Event URL":                             "event_url",
  "Event Call-In Number & Access Code":    "event_call_in_access_code",
  "Is the Alert Relevant for an ART Newsreel article?": "is_relevant_for_art_newsreel",
  "Library Item Preliminary Title":        "library_item_preliminary_title",
  "Library Item URL":                      "library_item_url",
  "Library Items File Name":               "library_item_file_name",
  "Agenda Item Title & Chronicle Topics":  "agenda_item_title",
};

// Extra old field names to hide (merged or removed, not simple renames).
const EXTRA_SUPERSEDED = new Set([
  "agenda_item_chronicle_topics",
  "agenda_item_is_existing",
  "agenda_item_official_title",
  "candidate_agenda_items",
  "candidate_chronicles",
  "event_timezone",
]);

/** Build the superseded set: all old keys that are replaced by current registry keys. */
function buildSupersededCols(priorKeys: Record<string, string[]> = {}): Set<string> {
  return new Set([
    ...Object.values(LEGACY_ALIASES),
    ...Object.values(priorKeys).flat(),
    ...Array.from(EXTRA_SUPERSEDED),
  ]);
}

/**
 * Resolve a cell value. Tries:
 * 1. row[col] — current key
 * 2. prior_keys from field registry — rename history maintained by Bubble backend
 * 3. LEGACY_ALIASES chain — fallback for rows predating the registry
 */
function resolveCell(row: AlertRow, col: string, priorKeys: Record<string, string[]> = {}): unknown {
  if (col in row) return row[col];
  for (const k of (priorKeys[col] ?? [])) {
    if (k in row) return row[k];
  }
  // Fallback: walk static LEGACY_ALIASES chain for very old rows
  const visited = new Set<string>([col]);
  let key: string | undefined = LEGACY_ALIASES[col];
  while (key !== undefined && !visited.has(key)) {
    if (key in row) return row[key];
    visited.add(key);
    key = LEGACY_ALIASES[key];
  }
  return undefined;
}

const COL_MAX_WIDTHS: Record<string, number> = {
  alert_type: 140,
  alert_title: 180,
  alert_description: 220,
  alert_url: 120,
  organization: 130,
  alert_date_time: 130,
  alert_datetime_et: 130,
  is_alert_relevant_for_art_newsreel: 110,
  is_relevant_for_art_newsreel: 110,
  event_title: 160,
  event_start_date_time: 130,
  event_start_datetime_et: 130,
  event_end_date_time: 130,
  event_end_datetime_et: 130,
  event_timezone: 100,
  event_is_full_day: 90,
  event_url: 100,
  event_call_in_number_access_code: 160,
  event_call_in_number_and_access_code: 160,
  event_duration: 100,
  library_item_preliminary_title: 160,
  library_item_url: 120,
  library_items_file_name: 130,
  agenda_item_title_chronicle_topics: 200,
  agenda_item_title_and_chronicle_topics: 200,
  agenda_item_title_official: 180,
  agenda_item_standardized_id: 120,
  agenda_item_official_id: 110,
  is_the_alert_relevant_for_an_art_newsreel_article: 110,
};

/**
 * Derive the visible column list.
 *
 * Schema columns (from DynamoDB output_json_schema.required) define the set
 * and order. Superseded old field names are hidden. Any remaining extra data
 * fields (e.g. event_timezone, doc extraction fields) are appended alphabetically.
 */

export function deriveColumns(rows: AlertRow[], schemaColumns: string[] | null, superseded: Set<string>): string[] {
  const dataKeys = new Set<string>();
  rows.forEach((r) =>
    Object.keys(r).forEach((k) => {
      if (!METADATA_COLS.has(k) && !superseded.has(k)) dataKeys.add(k);
    })
  );

  if (schemaColumns && schemaColumns.length > 0) {
    // Only show schema columns — never include stale columns from old rows
    return [...schemaColumns];
  }

  return Array.from(dataKeys).sort();
}

function colLabel(key: string): string {
  return key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

// ─── Cell renderers ───────────────────────────────────────────────────────────

function isEmpty(val: unknown): boolean {
  if (val === null || val === undefined) return true;
  if (typeof val === "string") return val.trim() === "";
  return false;
}

function formatDate(iso: unknown): string {
  if (!iso || typeof iso !== "string") return "";
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "America/New_York" });
  } catch { return String(iso); }
}

function formatTimestamp(iso: unknown): string {
  if (!iso || typeof iso !== "string") return "";
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    return d.toLocaleString("en-US", {
      month: "short", day: "numeric", year: "numeric",
      hour: "numeric", minute: "2-digit", timeZone: "America/New_York", timeZoneName: "short",
    });
  } catch { return String(iso); }
}

function alertTypeBadge(type: string) {
  let color = "bg-gray-100 text-gray-700";
  if (type.includes("Meeting")) color = "bg-blue-100 text-blue-800";
  if (type.includes("Report") || type.includes("Resource")) color = "bg-green-100 text-green-800";
  if (type.includes("Agenda") || type.includes("Materials")) color = "bg-orange-100 text-orange-800";
  if (type.includes("not relevant") || type.includes("carrousel")) color = "bg-gray-100 text-gray-400";
  if (type === "No Meaningful Change") color = "bg-gray-50 text-gray-400";
  return (
    <span className={`inline-block px-2 py-0.5 rounded text-[11px] font-medium ${color}`}>
      {type}
    </span>
  );
}

function TagList({ value }: { value: string }) {
  return (
    <div className="flex flex-wrap gap-1">
      {value.split(",").map((t, i) => (
        <span key={i} className="inline-block bg-purple-50 text-purple-700 text-[11px] px-1.5 py-0.5 rounded">
          {t.trim()}
        </span>
      ))}
    </div>
  );
}

function ExpandableText({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false);
  const isLong = text.length > 150;
  return (
    <div>
      <div className={`text-xs text-gray-700 ${!expanded && isLong ? "line-clamp-2" : ""}`}>{text}</div>
      {isLong && (
        <button onClick={() => setExpanded(!expanded)} className="text-[11px] text-blue-600 hover:underline mt-0.5">
          {expanded ? "Show less" : "Show more"}
        </button>
      )}
    </div>
  );
}

/** Extract the primary display value from a status-pattern object. */
function extractMainValue(obj: Record<string, unknown>): unknown {
  // "Library Item Title" is the key used in the label-as-key schema era
  // "details" is the key used by is_the_alert_relevant_for_an_art_newsreel_article
  return obj.title ?? obj["Library Item Title"] ?? obj.title_official ?? obj.official_title ?? obj.name
    ?? obj.agenda_item_title ?? obj.details ?? obj.reference ?? obj.explanation_or_reference
    ?? obj.standardized_id ?? obj.official_id ?? obj.new_duration ?? null;
}

/** Render a single status-pattern object as inline text with optional chronicle topic tags. */
function StatusObjectItem({ obj }: { obj: Record<string, unknown> }) {
  const status = obj.status;
  const main = extractMainValue(obj);
  const isNA = status === "N/A" || status === "No";
  const topics = Array.isArray(obj.chronicle_topics)
    ? (obj.chronicle_topics as string[]).filter((t) => t && t !== "N/A")
    : [];

  const mainStr = String(main ?? "");
  const showMain = main !== null && main !== undefined && mainStr !== "" && mainStr !== "N/A" && mainStr !== "false";

  return (
    <div className={`text-xs ${isNA ? "text-gray-400" : "text-gray-900"}`}>
      {showMain ? `${mainStr} (${String(status)})` : String(status ?? "")}
      {topics.length > 0 && (
        <div className="flex flex-wrap gap-0.5 mt-0.5">
          {topics.map((t, i) => (
            <span key={i} className="inline-block bg-purple-50 text-purple-700 text-[10px] px-1 py-0 rounded">
              {t}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

/** Render an array of status-pattern objects as a structured vertical list. */
function ObjectArrayList({ items, col }: { items: Record<string, unknown>[]; col: string }) {
  const [expanded, setExpanded] = useState(false);
  const COLLAPSE_THRESHOLD = 3;
  const hasStatus = items.some((it) => it.status !== undefined);

  if (!hasStatus) {
    const text = JSON.stringify(items, null, 2);
    return <ExpandableText text={text} />;
  }

  const visible = expanded ? items : items.slice(0, COLLAPSE_THRESHOLD);
  return (
    <div className="space-y-0.5">
      {visible.map((item, i) => (
        <div key={i} className={i > 0 ? "border-t border-gray-100 pt-0.5" : ""}>
          <StatusObjectItem obj={item} />
        </div>
      ))}
      {items.length > COLLAPSE_THRESHOLD && (
        <button
          onClick={() => setExpanded(!expanded)}
          className="text-[10px] text-blue-600 hover:underline"
        >
          {expanded ? "Show less" : `+${items.length - COLLAPSE_THRESHOLD} more`}
        </button>
      )}
    </div>
  );
}

function CellValue({ col, value }: { col: string; value: unknown }) {
  if (isEmpty(value)) return <span className="text-gray-400">—</span>;
  if (col === "target_id" && typeof value === "string")
    return <span className="text-xs text-gray-900">{PAGE_LABELS[value] || value}</span>;
  if (col.startsWith("alert_type") && typeof value === "string") return alertTypeBadge(value);
  if (typeof value === "boolean")
    return <span className="text-xs text-gray-900">{value ? "Yes" : "No"}</span>;
  if (col.endsWith("_url") && typeof value === "string" && value.startsWith("http")) {
    const urls = value.split(";").map((u) => u.trim()).filter((u) => u.startsWith("http"));
    if (urls.length <= 1)
      return (
        <a href={value} target="_blank" rel="noopener noreferrer" className="text-xs text-blue-600 hover:underline break-all">
          {value}
        </a>
      );
    return (
      <div className="flex flex-col gap-0.5">
        {urls.map((u, i) => (
          <a key={i} href={u} target="_blank" rel="noopener noreferrer" className="text-xs text-blue-600 hover:underline break-all">
            {u}
          </a>
        ))}
      </div>
    );
  }
  // Arrays — render as comma-separated tags or structured list
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="text-gray-400">—</span>;
    // Array of objects (e.g. agenda_item_title_and_chronicle_topics array)
    if (typeof value[0] === "object" && value[0] !== null) {
      return <ObjectArrayList items={value as Record<string, unknown>[]} col={col} />;
    }
    const str = value.filter(Boolean).join(", ");
    return col.includes("topic") || col.includes("chronicle")
      ? <TagList value={str} />
      : <span className="text-xs text-gray-900">{str}</span>;
  }
  // Objects (e.g. {status, title}, {status, reference}, {relevance, explanation_or_reference})
  if (typeof value === "object" && value !== null) {
    const obj = value as Record<string, unknown>;
    const status = obj.status ?? obj.relevance;  // relevance used in label-as-key era schema
    const main = extractMainValue(obj);
    if (status !== undefined) {
      const isNA = status === "N/A" || status === "No";
      // Only show main value if it's meaningful (not "N/A", "false", empty, etc.)
      const mainStr = String(main ?? "");
      const isLikelyFieldRef = /^[a-z][a-z_]+:\s*(true|false|null)/i.test(mainStr);
      const showMain = main !== null && main !== undefined && mainStr !== "" && mainStr !== "N/A" && mainStr !== "false" && !isLikelyFieldRef;
      return (
        <div className={`text-xs ${isNA ? "text-gray-400" : "text-gray-900"}`}>
          {showMain ? `${mainStr} (${String(status)})` : String(status)}
          {Array.isArray(obj.chronicle_topics) && (obj.chronicle_topics as string[]).filter((t) => t && t !== "N/A").length > 0 && (
            <div className="flex flex-wrap gap-0.5 mt-0.5">
              {(obj.chronicle_topics as string[]).filter((t) => t && t !== "N/A").map((t, i) => (
                <span key={i} className="inline-block bg-purple-50 text-purple-700 text-[10px] px-1 py-0 rounded">
                  {t}
                </span>
              ))}
            </div>
          )}
        </div>
      );
    }
    return <ExpandableText text={JSON.stringify(value)} />;
  }
  if (
    typeof value === "string" && value.includes(",") &&
    (col.includes("topic") || col.includes("chronicle") || col === "candidate_agenda_items")
  ) return <TagList value={value} />;
  if (typeof value === "string" && value.length > 150) return <ExpandableText text={value} />;
  return <span className="text-xs text-gray-900">{String(value)}</span>;
}

// ─── Rerun modal ──────────────────────────────────────────────────────────────

function RerunModal({
  row,
  config,
  configLoading,
  configError,
  onConfirm,
  onClose,
}: {
  row: AlertRow;
  config: ConfigData | null;
  configLoading: boolean;
  configError: string | null;
  onConfirm: () => void;
  onClose: () => void;
}) {
  const rowHash = typeof row.config_hash === "string" ? row.config_hash : null;
  const hashMatch = config && rowHash ? config.hash === rowHash : false;
  const runTimestamp = typeof row.run_timestamp === "string" ? row.run_timestamp : "—";
  const pageLabel = typeof row.target_id === "string" ? (PAGE_LABELS[row.target_id] ?? row.target_id) : "—";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="bg-white rounded-lg shadow-xl w-full max-w-md mx-4 p-6">
        <h2 className="text-base font-semibold text-gray-900 mb-1">Re-evaluate Alert</h2>
        <p className="text-xs text-gray-500 mb-4">
          Re-runs the page change agent on stored HTML snapshots using the current DynamoDB config.
        </p>

        <div className="text-xs text-gray-700 space-y-1 mb-4 bg-gray-50 rounded p-3">
          <div><span className="font-medium">Page:</span> {pageLabel}</div>
          <div><span className="font-medium">Run ID:</span> {String(row.run_id ?? "—")}</div>
          <div><span className="font-medium">Original run:</span> {runTimestamp}</div>
        </div>

        {/* Config status */}
        <div className="mb-5">
          {configLoading && (
            <div className="flex items-center gap-2 text-xs text-gray-500">
              <svg className="animate-spin h-3 w-3" viewBox="0 0 24 24" fill="none">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
              </svg>
              Checking current config...
            </div>
          )}
          {configError && (
            <div className="text-xs text-red-600 bg-red-50 rounded p-2">
              Could not fetch config: {configError}
            </div>
          )}
          {config && !configLoading && (
            <div className="text-xs space-y-2">
              <div className="text-gray-600">
                Current config: <span className="font-medium">{config.model}</span>
                {config.updated_at && (
                  <span className="text-gray-400 ml-1">
                    (updated {new Date(config.updated_at).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })})
                  </span>
                )}
              </div>
              {!rowHash ? (
                <div className="bg-gray-50 border border-gray-200 rounded p-2 text-gray-600">
                  No config hash stored for this alert — hash comparison unavailable.
                </div>
              ) : hashMatch ? (
                <div className="bg-yellow-50 border border-yellow-200 rounded p-2 text-yellow-800">
                  ⚠️ Config unchanged since this alert was generated. Re-running may produce the same result.
                </div>
              ) : (
                <div className="bg-green-50 border border-green-200 rounded p-2 text-green-800">
                  ✓ Config has changed — re-running will use updated instructions.
                </div>
              )}
            </div>
          )}
        </div>

        <div className="flex justify-end gap-3">
          <button
            onClick={onClose}
            className="px-4 py-1.5 text-sm text-gray-600 hover:text-gray-900 border border-gray-300 rounded"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            disabled={configLoading}
            className="px-4 py-1.5 text-sm font-medium text-white bg-blue-600 hover:bg-blue-700 rounded disabled:opacity-50 disabled:cursor-not-allowed"
          >
            Run Re-evaluation
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── Rerun result group ──────────────────────────────────────────────────────
//
// Rendered once per (run_id, target_id) group after the last original row.
// Shows ALL rerun rows as a group with a single Accept/Discard action.
// For diff highlighting, each rerun row is compared against the best-matching
// original row (fewest field differences).

/**
 * Find the original row that best matches a given rerun row (fewest changed fields).
 * Used for diff highlighting when row count or order may differ between original and rerun.
 */
function bestMatchOriginal(rerunRow: AlertRow, originals: AlertRow[], columns: string[], priorKeys: Record<string, string[]> = {}): AlertRow {
  if (originals.length === 0) return {};
  if (originals.length === 1) return originals[0];
  let bestIdx = 0;
  let bestDiffs = Infinity;
  for (let oi = 0; oi < originals.length; oi++) {
    let diffs = 0;
    for (const col of columns) {
      const ov = resolveCell(originals[oi], col, priorKeys);
      const rv = resolveCell(rerunRow, col, priorKeys);
      const a = ov === null || ov === undefined ? "" : String(ov);
      const b = rv === null || rv === undefined ? "" : String(rv);
      if (a !== b) diffs++;
    }
    if (diffs < bestDiffs) { bestDiffs = diffs; bestIdx = oi; }
  }
  return originals[bestIdx];
}

function RerunResultGroup({
  result,
  columns,
  priorKeys = {},
  onAccept,
  onDiscard,
}: {
  result: RerunResult;
  columns: string[];
  priorKeys?: Record<string, string[]>;
  onAccept: () => void;
  onDiscard: () => void;
}) {
  const [accepting, setAccepting] = useState(false);
  const [discarding, setDiscarding] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  // Normalise to arrays (API can return either array or single-object schema)
  const originals: AlertRow[] = result.original_rows ?? (result.original ? [result.original] : []);
  const reruns: AlertRow[] = result.rerun_rows ?? (result.rerun ? [result.rerun] : []);

  // Cumulative left offsets for sticky columns (mirrors main table logic)
  const stickyLefts = useMemo(() => {
    let acc = ROW_NUM_COL_WIDTH + ACTIONS_COL_WIDTH + RUN_ID_COL_WIDTH;
    return columns.map((col) => {
      const left = acc;
      acc += COL_MAX_WIDTHS[col] ?? 160;
      return left;
    });
  }, [columns]);

  // Count total changed fields across all rerun rows
  const totalChangedCount = useMemo(() => {
    const changed = new Set<string>();
    for (const rerunRow of reruns) {
      const best = bestMatchOriginal(rerunRow, originals, columns, priorKeys);
      for (const col of columns) {
        const ov = resolveCell(best, col, priorKeys);
        const rv = resolveCell(rerunRow, col, priorKeys);
        const a = ov === null || ov === undefined ? "" : String(ov);
        const b = rv === null || rv === undefined ? "" : String(rv);
        if (a !== b) changed.add(col);
      }
    }
    return changed.size;
  }, [originals, reruns, columns]);

  async function handleAccept() {
    setAccepting(true);
    setActionError(null);
    try {
      const res = await fetch("/api/rerun/accept", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ run_id: result.run_id, target_id: result.target_id }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error ?? "Accept failed");
      onAccept();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
      setAccepting(false);
    }
  }

  async function handleDiscard() {
    setDiscarding(true);
    setActionError(null);
    try {
      const res = await fetch("/api/rerun/discard", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ run_id: result.run_id, target_id: result.target_id }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error ?? "Discard failed");
      onDiscard();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
      setDiscarding(false);
    }
  }

  const rowCountChanged = originals.length !== reruns.length;

  // Empty rerun — agent produced no results (e.g. schema error or "No Meaningful Change").
  // Show a single row with Discard so the user can dismiss it.
  if (reruns.length === 0) {
    return (
      <tr className="bg-amber-50 border-t-2 border-b-2 border-amber-300">
        <td className={`${cellClass} bg-amber-50 z-[5]`} style={{ width: ROW_NUM_COL_WIDTH, minWidth: ROW_NUM_COL_WIDTH, position: "sticky", left: 0 }} />
        <td
          className={`${cellClass} border-l-4 border-amber-400 bg-amber-50 z-[5]`}
          style={{ width: ACTIONS_COL_WIDTH, minWidth: ACTIONS_COL_WIDTH, position: "sticky", left: ROW_NUM_COL_WIDTH }}
          colSpan={1}
        >
          <div className="flex flex-col gap-1.5">
            <span className="text-[10px] font-bold text-amber-700 uppercase tracking-wide">
              Re-run result
            </span>
            <span className="text-[10px] text-amber-600">0 rows — agent found no changes</span>
            {result.error && (
              <span className="text-[10px] text-gray-500 italic">{result.error}</span>
            )}
            {result.rerun_timestamp && (
              <span className="text-[10px] text-gray-400">{formatTimestamp(result.rerun_timestamp)}</span>
            )}
            {actionError && <span className="text-[10px] text-red-600">{actionError}</span>}
            <button
              onClick={handleDiscard}
              disabled={discarding}
              className="text-[11px] font-medium text-gray-600 hover:text-gray-900 border border-gray-300 bg-white rounded px-2 py-0.5 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {discarding ? "Discarding…" : "Discard"}
            </button>
          </div>
        </td>
        <td className={`${cellClass} bg-amber-50`} colSpan={columns.length + 1} />
      </tr>
    );
  }

  return (
    <>
      {reruns.map((rerunRow, ri) => {
        const best = bestMatchOriginal(rerunRow, originals, columns, priorKeys);
        const isFirst = ri === 0;

        function isChanged(col: string): boolean {
          const ov = resolveCell(best, col, priorKeys);
          const rv = resolveCell(rerunRow, col, priorKeys);
          const a = ov === null || ov === undefined ? "" : String(ov);
          const b = rv === null || rv === undefined ? "" : String(rv);
          return a !== b;
        }

        return (
          <tr key={ri} className={`bg-amber-50 ${isFirst ? "border-t-2" : ""} ${ri === reruns.length - 1 ? "border-b-2" : ""} border-amber-300`}>
            {/* Row number cell — empty for rerun rows */}
            <td className={`${cellClass} bg-amber-50 z-[5]`} style={{ width: ROW_NUM_COL_WIDTH, minWidth: ROW_NUM_COL_WIDTH, position: "sticky", left: 0 }} />
            {/* Actions cell — only first row shows buttons, others show row indicator */}
            <td
              className={`${cellClass} border-l-4 border-amber-400 bg-amber-50 z-[5]`}
              style={{ width: ACTIONS_COL_WIDTH, minWidth: ACTIONS_COL_WIDTH, position: "sticky", left: ROW_NUM_COL_WIDTH }}
            >
              {isFirst ? (
                <div className="flex flex-col gap-1.5">
                  <span className="text-[10px] font-bold text-amber-700 uppercase tracking-wide">
                    Re-run result
                  </span>
                  {rowCountChanged ? (
                    <span className="text-[10px] text-amber-600">
                      {originals.length} → {reruns.length} row{reruns.length !== 1 ? "s" : ""}
                    </span>
                  ) : null}
                  <span className="text-[10px] text-amber-600">
                    {totalChangedCount} field{totalChangedCount !== 1 ? "s" : ""} changed
                  </span>
                  {result.rerun_timestamp && (
                    <span className="text-[10px] text-gray-400">{formatTimestamp(result.rerun_timestamp)}</span>
                  )}
                  {actionError && (
                    <span className="text-[10px] text-red-600">{actionError}</span>
                  )}
                  <button
                    onClick={handleAccept}
                    disabled={accepting || discarding}
                    className="text-[11px] font-medium text-white bg-blue-600 hover:bg-blue-700 rounded px-2 py-0.5 disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    {accepting ? "Accepting…" : reruns.length > 1 ? `Accept all ${reruns.length}` : "Accept"}
                  </button>
                  <button
                    onClick={handleDiscard}
                    disabled={accepting || discarding}
                    className="text-[11px] font-medium text-gray-600 hover:text-gray-900 border border-gray-300 bg-white rounded px-2 py-0.5 disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    {discarding ? "Discarding…" : "Discard"}
                  </button>
                </div>
              ) : (
                <span className="text-[10px] text-amber-500">row {ri + 1}/{reruns.length}</span>
              )}
            </td>

            {/* Agent Call ID cell — sticky */}
            <td className={`${cellClass} bg-amber-50 z-[5]`} style={{ width: RUN_ID_COL_WIDTH, minWidth: RUN_ID_COL_WIDTH, position: "sticky", left: ROW_NUM_COL_WIDTH + ACTIONS_COL_WIDTH }}>
              <span className="text-[11px] text-gray-500 font-mono">{(String(result.agent_call_id ?? (rerunRow.agent_call_id as string) ?? "")).slice(-8) || "—"}</span>
            </td>

            {/* Data cells — show rerun values; highlight cells that differ from best-match original */}
            {columns.map((col, i) => {
              const sticky = i < STICKY_DATA_COLS;
              const changed = isChanged(col);
              return (
                <td
                  key={col}
                  className={`${cellClass} ${changed ? "bg-amber-100" : sticky ? "bg-amber-50" : ""}${sticky ? " z-[5]" : ""}`}
                  style={{
                    width: COL_MAX_WIDTHS[col] ?? 160,
                    maxWidth: COL_MAX_WIDTHS[col] ?? 160,
                    ...(sticky ? { position: "sticky", left: stickyLefts[i] } : {}),
                    ...(i === STICKY_DATA_COLS - 1 ? STICKY_DIVIDER : {}),
                  }}
                  title={changed ? `Was: ${(() => { const v = resolveCell(best, col, priorKeys); return v === null || v === undefined ? "—" : String(v); })()}` : undefined}
                >
                  <CellValue col={col} value={resolveCell(rerunRow, col, priorKeys)} />
                </td>
              );
            })}

            {/* No supplementary content on rerun result rows */}
            <td className={cellClass} style={{ width: SNAPSHOT_COL_WIDTH, minWidth: SNAPSHOT_COL_WIDTH }} />
            <td className={cellClass} style={{ width: SNAPSHOT_COL_WIDTH, minWidth: SNAPSHOT_COL_WIDTH }} />
            <td className={cellClass} style={{ width: RECORDING_COL_WIDTH, minWidth: RECORDING_COL_WIDTH }} />
            <td className={cellClass} style={{ width: TRANSCRIPT_COL_WIDTH, minWidth: TRANSCRIPT_COL_WIDTH }} />
            <td className={cellClass} style={{ width: CHUNKS_COL_WIDTH, minWidth: CHUNKS_COL_WIDTH }} />
          </tr>
        );
      })}
    </>
  );
}

// ─── Scroll sync ──────────────────────────────────────────────────────────────

function useSyncScroll() {
  const topRef = useRef<HTMLDivElement>(null);
  const tableRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const top = topRef.current;
    const table = tableRef.current;
    if (!top || !table) return;
    const onTopScroll = () => { table.scrollLeft = top.scrollLeft; };
    const onTableScroll = () => { top.scrollLeft = table.scrollLeft; };
    top.addEventListener("scroll", onTopScroll);
    table.addEventListener("scroll", onTableScroll);
    return () => {
      top.removeEventListener("scroll", onTopScroll);
      table.removeEventListener("scroll", onTableScroll);
    };
  }, []);

  return { topRef, tableRef };
}

// ─── Snapshot helpers ─────────────────────────────────────────────────────────

const SNAPSHOT_COL_WIDTH = 110;

function snapshotKey(row: AlertRow, file: "before.html" | "after.html"): string | null {
  const runId = String(row.run_id ?? "");
  const targetId = String(row.target_id ?? "");
  const ts = String(row.run_timestamp ?? "");
  if (!runId || !targetId || !ts) return null;
  const d = new Date(ts);
  if (isNaN(d.getTime())) return null;
  const yyyy = d.getUTCFullYear();
  const mm = String(d.getUTCMonth() + 1).padStart(2, "0");
  const dd = String(d.getUTCDate()).padStart(2, "0");
  return `pages/${targetId}/${yyyy}/${mm}/${dd}/${runId}/${file}`;
}

// ─── Main table ───────────────────────────────────────────────────────────────

const thClass = "px-3 py-2 text-xs font-semibold text-gray-700 border border-gray-300 bg-gray-50 sticky top-0 z-10";
const cellClass = "px-3 py-2 text-sm border border-gray-300 align-top break-words";

const ROW_NUM_COL_WIDTH = 44;
const ACTIONS_COL_WIDTH = 120;
const RUN_ID_COL_WIDTH = 100;
const RECORDING_COL_WIDTH = 90;
const TRANSCRIPT_COL_WIDTH = 110;
const CHUNKS_COL_WIDTH = 90;
const STICKY_DIVIDER: React.CSSProperties = { boxShadow: "4px 0 8px -2px rgba(0,0,0,0.3)" };
// Number of data columns to freeze (after the Actions + Run ID columns).
const STICKY_DATA_COLS = 4;

// ─── QA types ─────────────────────────────────────────────────────────────────

interface FieldScore {
  score: "Correct" | "Partially Correct" | "Incorrect";
  reasoning: string;
}

interface EvalResult {
  agent_call_id: string;
  eval_row_key?: string;
  eval_scores: Record<string, FieldScore | { total_fields?: number; correct?: number; partially_correct?: number; incorrect?: number; pattern?: string }>;
  eval_timestamp: string;
  eval_run_id: string;
}

function lookupEvalResult(row: AlertRow, evalResults: Record<string, EvalResult>): EvalResult | undefined {
  const callId = String(row.agent_call_id ?? "");
  if (!callId) return undefined;
  const libUrl = String(row.library_item_url ?? "").trim();
  const plain = evalResults[callId];
  const composite = (libUrl && libUrl.toLowerCase() !== "n/a")
    ? evalResults[`${callId}|${libUrl}`]
    : undefined;
  if (plain && composite) {
    // Prefer whichever was evaluated more recently (re-evaluated solo rows win over stale sibling results)
    return (plain.eval_timestamp ?? "") >= (composite.eval_timestamp ?? "") ? plain : composite;
  }
  return plain ?? composite;
}

const _EVAL_METADATA_KEYS = new Set([
  "run_id", "run_timestamp", "target_id", "source_url", "config_hash",
  "agent_call_id", "eval_run_id", "eval_timestamp", "eval_scores",
  "eval_row_key", "recording_s3_key", "transcript_s3_key",
  "extraction_source", "ingest_status", "bubble_sync_status",
  "bubble_sync_error", "eidarix_agenda_item_ids", "bubble_library_item_id",
  "bubble_event_id", "last_rerun_at",
]);

function computeQaScore(evalScores: EvalResult["eval_scores"]): { correct: number; partial: number; incorrect: number; total: number } {
  let correct = 0, partial = 0, incorrect = 0, total = 0;
  for (const [key, val] of Object.entries(evalScores)) {
    if (key === "overall_summary" || _EVAL_METADATA_KEYS.has(key)) continue;
    if (typeof val === "object" && val !== null && "score" in val) {
      total++;
      const s = (val as FieldScore).score;
      if (s === "Correct") correct++;
      else if (s === "Partially Correct") partial++;
      else incorrect++;
    }
  }
  return { correct, partial, incorrect, total };
}

const SCORE_TEXT: Record<string, string> = {
  "Correct": "✓",
  "Partially Correct": "~",
  "Incorrect": "✗",
};
const SCORE_TEXT_COLOR: Record<string, string> = {
  "Correct": "text-green-600",
  "Partially Correct": "text-amber-500",
  "Incorrect": "text-red-500",
};

function QaScoreCell({ evalScores, col }: { evalScores: EvalResult["eval_scores"]; col: string }) {
  const entry = evalScores[col] as FieldScore | undefined;
  if (!entry || !("score" in entry)) return <span className="text-gray-300 text-xs">—</span>;
  const symbol = SCORE_TEXT[entry.score] ?? "?";
  const color = SCORE_TEXT_COLOR[entry.score] ?? "text-gray-500";
  return (
    <div className="flex flex-col gap-0.5">
      <span className={`text-xs font-medium ${color}`}>{symbol} {entry.score}</span>
      {entry.reasoning && (
        <span className="text-[10px] text-gray-400 leading-snug">{entry.reasoning}</span>
      )}
    </div>
  );
}

function QaScoreRow({
  evalResult,
  columns,
  stickyLefts,
  callId,
  onRerun,
  isRunning,
}: {
  evalResult: EvalResult;
  columns: string[];
  stickyLefts: number[];
  callId: string;
  onRerun: (callId: string) => void;
  isRunning: boolean;
}) {
  const { correct, total } = computeQaScore(evalResult.eval_scores);
  const _evalDt = evalResult.eval_timestamp ? new Date(evalResult.eval_timestamp) : null;
  const evalDateStr = _evalDt && !isNaN(_evalDt.getTime())
    ? _evalDt.toLocaleDateString("en-US", { month: "short", day: "numeric" }) + " " +
      _evalDt.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })
    : "";

  return (
    <tr className="border-t border-violet-100">
      {/* Row number cell — empty for sub-rows */}
      <td className={`${cellClass} bg-violet-50 z-[5]`} style={{ width: ROW_NUM_COL_WIDTH, minWidth: ROW_NUM_COL_WIDTH, position: "sticky", left: 0 }} />
      {/* Actions cell */}
      <td
        className={`${cellClass} bg-violet-50 z-[5]`}
        style={{ width: ACTIONS_COL_WIDTH, minWidth: ACTIONS_COL_WIDTH, position: "sticky", left: ROW_NUM_COL_WIDTH }}
      >
        <div className="flex flex-col gap-1">
          <div className="text-[11px] text-violet-700 font-semibold">
            QA &nbsp;<span className="font-normal text-gray-600">{correct}/{total}</span>
          </div>
          {evalDateStr && <div className="text-[10px] text-gray-500">{evalDateStr}</div>}
          {isRunning ? (
            <div className="flex items-center gap-1 text-[11px] text-gray-400">
              <svg className="animate-spin h-3 w-3 shrink-0" viewBox="0 0 24 24" fill="none">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
              </svg>
              Running…
            </div>
          ) : (
            <button
              onClick={() => onRerun(callId)}
              className="text-[11px] text-gray-400 hover:text-gray-600 hover:underline text-left"
            >
              Re-run QA
            </button>
          )}
        </div>
      </td>
      {/* Alert ID cell */}
      <td
        className={`${cellClass} bg-violet-50 z-[5]`}
        style={{ width: RUN_ID_COL_WIDTH, minWidth: RUN_ID_COL_WIDTH, position: "sticky", left: ROW_NUM_COL_WIDTH + ACTIONS_COL_WIDTH }}
      />
      {/* Score per data column */}
      {columns.map((col, ci) => {
        const sticky = ci < STICKY_DATA_COLS;
        return (
          <td
            key={col}
            className={`${cellClass} bg-violet-50${sticky ? " z-[5]" : ""}`}
            style={{
              width: COL_MAX_WIDTHS[col] ?? 160,
              maxWidth: COL_MAX_WIDTHS[col] ?? 160,
              ...(sticky ? { position: "sticky", left: stickyLefts[ci], backgroundColor: "rgb(245 243 255)" } : {}),
              ...(ci === STICKY_DATA_COLS - 1 ? STICKY_DIVIDER : {}),
            }}
          >
            <QaScoreCell evalScores={evalResult.eval_scores} col={col} />
          </td>
        );
      })}
      {/* Fixed right columns — empty */}
      <td className={`${cellClass} bg-violet-50`} style={{ width: SNAPSHOT_COL_WIDTH }} />
      <td className={`${cellClass} bg-violet-50`} style={{ width: SNAPSHOT_COL_WIDTH }} />
      <td className={`${cellClass} bg-violet-50`} style={{ width: RECORDING_COL_WIDTH }} />
      <td className={`${cellClass} bg-violet-50`} style={{ width: TRANSCRIPT_COL_WIDTH }} />
      <td className={`${cellClass} bg-violet-50`} style={{ width: CHUNKS_COL_WIDTH }} />
    </tr>
  );
}

// ─────────────────────────────────────────────────────────────────────────────

interface AlertsTableProps {
  rows: AlertRow[];
  onAccepted?: () => void;
  /** Bump this to trigger a re-fetch of the column schema from DynamoDB. */
  schemaVersion?: number;
  /** If true, only show rows that have a QA score. */
  hasQaScoreFilter?: boolean;
  /** If true, only show rows with a QA score that is not perfect (correct < total). */
  hasImperfectQaFilter?: boolean;
}

export function AlertsTable({ rows, onAccepted, schemaVersion = 0, hasQaScoreFilter = false, hasImperfectQaFilter = false }: AlertsTableProps) {
  const [schemaColumns, setSchemaColumns] = useState<string[] | null>(null);
  const [schemaLabels, setSchemaLabels] = useState<Record<string, string> | null>(null);
  const [priorKeys, setPriorKeys] = useState<Record<string, string[]>>({});

  // Fetch column order + labels from DynamoDB via /api/schema
  useEffect(() => {
    fetch("/api/schema", { cache: "no-store" })
      .then((r) => r.json())
      .then((d: { columns: string[] | null; labels: Record<string, string> | null; priorKeys?: Record<string, string[]> | null }) => {
        setSchemaColumns(d.columns);
        setSchemaLabels(d.labels);
        setPriorKeys(d.priorKeys ?? {});
      })
      .catch(() => {});
  }, [schemaVersion]);

  const superseded = useMemo(() => buildSupersededCols(priorKeys), [priorKeys]);
  const columns = deriveColumns(rows, schemaColumns, superseded);
  const tableWidth = ROW_NUM_COL_WIDTH + ACTIONS_COL_WIDTH + RUN_ID_COL_WIDTH + columns.reduce((sum, col) => sum + (COL_MAX_WIDTHS[col] ?? 160), 0) + SNAPSHOT_COL_WIDTH * 2 + RECORDING_COL_WIDTH + TRANSCRIPT_COL_WIDTH + CHUNKS_COL_WIDTH;
  const topRef = useRef<HTMLDivElement>(null);
  const tableRef = useRef<HTMLDivElement>(null);

  // Cumulative left offsets for sticky data columns (offset from left edge of table).
  // Starts after Row Num + Actions + Run ID columns.
  const stickyLefts = useMemo(() => {
    let acc = ROW_NUM_COL_WIDTH + ACTIONS_COL_WIDTH + RUN_ID_COL_WIDTH;
    return columns.map((col) => {
      const left = acc;
      acc += COL_MAX_WIDTHS[col] ?? 160;
      return left;
    });
  }, [columns]);

  // ── Content gate modal state ──────────────────────────────────────────────
  const [contentGateRow, setContentGateRow] = useState<AlertRow | null>(null);

  // ── Pagination ─────────────────────────────────────────────────────────────
  const PAGE_SIZE = 50;
  const [page, setPage] = useState(0);

  // ── Re-evaluate state ──────────────────────────────────────────────────────
  const [modalRow, setModalRow] = useState<AlertRow | null>(null);
  const [config, setConfig] = useState<ConfigData | null>(null);
  const [configLoading, setConfigLoading] = useState(false);
  const [configError, setConfigError] = useState<string | null>(null);

  // rowKey -> { taskId, row } — tracks in-flight ECS tasks
  const [activeTasks, setActiveTasks] = useState<Map<string, ActiveTask>>(new Map());

  // rowKey -> RerunResult — completed reruns awaiting Accept/Discard.
  // Multiple entries can coexist; each renders an inline result row below its original.
  const [completedReruns, setCompletedReruns] = useState<Map<string, RerunResult>>(new Map());

  // ── QA evaluation state ───────────────────────────────────────────────────
  const [evalResults, setEvalResults] = useState<Record<string, EvalResult>>({});
  const [evalVersion, setEvalVersion] = useState(0);
  // agent_call_id → ECS taskId (in-flight QA tasks)
  const [qaRunning, setQaRunning] = useState<Map<string, string>>(new Map());
  // (QA score rows are always visible — no expand/collapse state needed)

  // Fetch eval results on mount and after each completed QA run
  useEffect(() => {
    fetch("/api/eval/alerts")
      .then((r) => r.json())
      .then((d: { results: Record<string, EvalResult> }) => {
        if (d.results) setEvalResults(d.results);
      })
      .catch(() => {});
  }, [evalVersion]);

  // Poll in-flight QA tasks every 5s
  useEffect(() => {
    if (qaRunning.size === 0) return;
    const interval = setInterval(async () => {
      for (const [callId, taskId] of Array.from(qaRunning.entries())) {
        try {
          const res = await fetch(`/api/eval/alerts/${taskId}`);
          const data = await res.json() as { status: string; error?: string };
          if (data.status === "running") continue;
          setQaRunning((prev) => {
            const next = new Map(prev);
            next.delete(callId);
            return next;
          });
          if (data.status === "complete") {
            setEvalVersion((v) => v + 1);
          } else if (data.status === "failed" || data.status === "error") {
            alert(`QA evaluation failed: ${data.error ?? "Unknown error"}`);
          } else {
            // "unknown": task not found in ECS (completed and purged, or transient).
            // Refresh results — if the task completed, scores will appear.
            setEvalVersion((v) => v + 1);
          }
        } catch { /* keep polling */ }
      }
    }, 5000);
    return () => clearInterval(interval);
  }, [qaRunning]);

  async function startQa(callId: string) {
    try {
      const res = await fetch("/api/eval/alerts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent_call_id: callId }),
      });
      const data = await res.json() as { taskId?: string; error?: string };
      if (!res.ok || !data.taskId) throw new Error(data.error ?? "Failed to start QA");
      setQaRunning((prev) => new Map(prev).set(callId, data.taskId!));
    } catch (err) {
      alert(`Failed to start QA: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  // ── Manual transcript upload state ────────────────────────────────────────
  // agent_call_id -> "uploading" | "chunking" | "ready" | "error:<msg>"
  const [uploadStates, setUploadStates] = useState<Map<string, string>>(new Map());
  // agent_call_id -> partial row overrides (for real-time chunk key updates)
  const [rowOverrides, setRowOverrides] = useState<Map<string, Partial<AlertRow>>>(new Map());

  function setUploadState(callId: string, state: string) {
    setUploadStates((prev) => new Map(prev).set(callId, state));
  }

  async function handleTranscriptUpload(row: AlertRow, file: File) {
    const callId = String(row.agent_call_id ?? "");
    if (!callId) return;
    setUploadState(callId, "uploading");
    try {
      const urlResp = await fetch("/api/ingest/upload-transcript-url", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent_call_id: callId, filename: file.name }),
      });
      const { upload_url, s3_key, error: urlErr } = await urlResp.json() as Record<string, string>;
      if (urlErr) throw new Error(urlErr);

      await fetch(upload_url, { method: "PUT", body: file, headers: { "Content-Type": "text/plain" } });

      setRowOverrides((prev) => new Map(prev).set(callId, {
        manual_transcript_s3_key: s3_key,
        ingest_status: "pending",
      }));
      setUploadState(callId, "ready");
    } catch (err) {
      setUploadState(callId, `error:${err instanceof Error ? err.message : String(err)}`);
    }
  }

  // ── Persist QA running state to localStorage across navigation ───────────
  const QA_LS_KEY = "wct_qaRunning";

  // On mount, restore any in-flight QA tasks from localStorage
  useEffect(() => {
    try {
      const stored = localStorage.getItem(QA_LS_KEY);
      if (!stored) return;
      const parsed = JSON.parse(stored) as Record<string, string>;
      if (Object.keys(parsed).length === 0) return;
      setQaRunning((prev) => {
        const next = new Map(prev);
        for (const [callId, taskId] of Object.entries(parsed)) {
          if (!next.has(callId)) next.set(callId, taskId);
        }
        return next;
      });
    } catch { /* ignore */ }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Sync qaRunning → localStorage whenever it changes
  useEffect(() => {
    try {
      if (qaRunning.size === 0) {
        localStorage.removeItem(QA_LS_KEY);
      } else {
        const obj: Record<string, string> = {};
        for (const [k, v] of Array.from(qaRunning.entries())) obj[k] = v;
        localStorage.setItem(QA_LS_KEY, JSON.stringify(obj));
      }
    } catch { /* ignore storage errors */ }
  }, [qaRunning]);

  // ── Persist active reruns to localStorage across navigation ──────────────
  const LS_KEY = "wct_activeReruns";

  // On mount, restore any in-flight tasks from localStorage
  useEffect(() => {
    try {
      const stored = localStorage.getItem(LS_KEY);
      if (!stored) return;
      const parsed = JSON.parse(stored) as Record<string, { taskId: string; row: AlertRow }>;
      if (Object.keys(parsed).length === 0) return;
      setActiveTasks((prev) => {
        const next = new Map(prev);
        for (const [rowKey, { taskId, row }] of Object.entries(parsed)) {
          if (!next.has(rowKey)) {
            next.set(rowKey, { taskId, row });
          }
        }
        return next;
      });
    } catch {
      // ignore parse errors
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Sync activeTasks → localStorage whenever it changes
  useEffect(() => {
    try {
      if (activeTasks.size === 0) {
        localStorage.removeItem(LS_KEY);
      } else {
        const obj: Record<string, { taskId: string; row: AlertRow }> = {};
        for (const [k, v] of Array.from(activeTasks.entries())) {
          obj[k] = v;
        }
        localStorage.setItem(LS_KEY, JSON.stringify(obj));
      }
    } catch {
      // ignore storage errors (e.g. private browsing quota)
    }
  }, [activeTasks]);

  // Reruns are now auto-accepted — clear any stale manual-review entries from localStorage
  useEffect(() => {
    try { localStorage.removeItem("wct_completedReruns"); } catch { /* ignore */ }
  }, []);

  // ── Open modal and fetch config ────────────────────────────────────────────
  function openModal(row: AlertRow) {
    setModalRow(row);
    setConfig(null);
    setConfigError(null);
    setConfigLoading(true);
    fetch("/api/config")
      .then((r) => r.json())
      .then((d) => {
        if (d.error) throw new Error(d.error);
        setConfig(d as ConfigData);
      })
      .catch((e) => setConfigError(e.message))
      .finally(() => setConfigLoading(false));
  }

  // ── Trigger rerun ─────────────────────────────────────────────────────────
  async function triggerRerun(row: AlertRow) {
    setModalRow(null);
    const run_id = String(row.run_id ?? "");
    const target_id = String(row.target_id ?? "");
    const rowKey = `${run_id}::${target_id}`;

    try {
      const res = await fetch("/api/rerun", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ run_id, target_id, agent_call_id: String(row.agent_call_id ?? "") }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error ?? "Rerun failed to start");
      const taskId = (data.taskArn as string).split("/").pop()!;
      setActiveTasks((prev) => {
        const next = new Map(prev);
        next.set(rowKey, { taskId, row });
        return next;
      });
    } catch (err) {
      alert(`Failed to start re-evaluation: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  // ── Poll active tasks every 5s ─────────────────────────────────────────────
  useEffect(() => {
    if (activeTasks.size === 0) return;

    const interval = setInterval(async () => {
      for (const [rowKey, { taskId, row }] of Array.from(activeTasks.entries())) {
        const run_id = String(row.run_id ?? "");
        const target_id = String(row.target_id ?? "");
        try {
          const res = await fetch(
            `/api/rerun/${taskId}?run_id=${encodeURIComponent(run_id)}&target_id=${encodeURIComponent(target_id)}`
          );
          const data = await res.json();
          if (data.status === "running") continue;
          // Terminal state — remove from active
          setActiveTasks((prev) => {
            const next = new Map(prev);
            next.delete(rowKey);
            return next;
          });
          if (data.status === "complete" && data.result) {
            // Auto-accept: immediately accept the rerun result without manual review
            const result = data.result as RerunResult;
            try {
              const acceptRes = await fetch("/api/rerun/accept", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ run_id, target_id }),
              });
              if (acceptRes.ok) {
                onAccepted?.();
                // Auto-trigger QA on the accepted row
                const callId = String(row.agent_call_id ?? "");
                if (callId) startQa(callId);
              } else {
                // Accept failed — fall back to showing the result for manual review
                setCompletedReruns((prev) => {
                  const next = new Map(prev);
                  next.set(rowKey, result);
                  return next;
                });
              }
            } catch {
              // Network error on accept — fall back to manual review
              setCompletedReruns((prev) => {
                const next = new Map(prev);
                next.set(rowKey, result);
                return next;
              });
            }
          } else if (data.status === "failed" || data.status === "error") {
            alert(`Re-evaluation failed: ${data.error ?? "Unknown error"}`);
          } else if (data.status === "complete" && !data.result) {
            alert("Re-evaluation completed but no result was found in S3.");
          }
        } catch {
          // network error — keep polling
        }
      }
    }, 5000);

    return () => clearInterval(interval);
  }, [activeTasks]);

  const displayRows = hasImperfectQaFilter
    ? rows.filter((r) => {
        const ev = lookupEvalResult(r, evalResults);
        if (!ev) return false;
        const { correct, total } = computeQaScore(ev.eval_scores);
        return correct < total;
      })
    : hasQaScoreFilter
    ? rows.filter((r) => !!lookupEvalResult(r, evalResults))
    : rows;

  const totalPages = Math.ceil(displayRows.length / PAGE_SIZE);
  const pagedRows = displayRows.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);

  // Reset to page 0 when filters or row count changes
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { if (page !== 0) setPage(0); }, [displayRows.length, hasImperfectQaFilter, hasQaScoreFilter]);

  // Stable row index map: row object → index in full unfiltered rows
  const alertRowIndexMap = useMemo(() => new Map(rows.map((r, i) => [r, i])), [rows]);

  if (displayRows.length === 0) {
    if (hasImperfectQaFilter) {
      return <p className="text-sm text-gray-500 mt-2">No rows with imperfect QA scores found.</p>;
    }
    if (hasQaScoreFilter) {
      return <p className="text-sm text-gray-500 mt-2">No rows with QA scores found.</p>;
    }
    return null;
  }

  return (
    <>
      {/* Pagination controls — above the table so they're always visible */}
      {totalPages > 1 && (
        <div className="flex items-center gap-3 mb-2 text-sm text-gray-600">
          <span>{displayRows.length} of {rows.length} rows · Page {page + 1} / {totalPages}</span>
          <button
            className="px-2 py-0.5 rounded border border-gray-300 disabled:opacity-40"
            onClick={() => setPage((p) => Math.max(0, p - 1))}
            disabled={page === 0}
          >← Prev</button>
          <button
            className="px-2 py-0.5 rounded border border-gray-300 disabled:opacity-40"
            onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
            disabled={page >= totalPages - 1}
          >Next →</button>
        </div>
      )}
      <div className="rounded border border-gray-300">
        {/* Top scrollbar — synced with table */}
        <div ref={topRef} className="overflow-x-scroll table-scroll sticky top-0 z-30 bg-white border-b border-gray-200" onScroll={() => { if (topRef.current && tableRef.current) tableRef.current.scrollLeft = topRef.current.scrollLeft; }}>
          <div style={{ width: tableWidth, height: 1 }} />
        </div>
        {/* Table scroll container */}
        <div ref={tableRef} className="overflow-auto table-scroll hide-bottom-scrollbar" style={{ maxHeight: "calc(100vh - 200px)" }} onScroll={() => { if (topRef.current && tableRef.current) topRef.current.scrollLeft = tableRef.current.scrollLeft; }}>
          <table className="w-full text-left border-collapse" style={{ width: tableWidth }}>
            <thead>
              <tr>
                <th className={`${thClass} z-30`} style={{ width: ROW_NUM_COL_WIDTH, minWidth: ROW_NUM_COL_WIDTH, position: "sticky", left: 0, textAlign: "center" }}>
                  #
                </th>
                <th className={`${thClass} z-30`} style={{ width: ACTIONS_COL_WIDTH, minWidth: ACTIONS_COL_WIDTH, position: "sticky", left: ROW_NUM_COL_WIDTH }}>
                  Content Gate
                </th>
                <th className={`${thClass} z-20`} style={{ width: RUN_ID_COL_WIDTH, minWidth: RUN_ID_COL_WIDTH, position: "sticky", left: ROW_NUM_COL_WIDTH + ACTIONS_COL_WIDTH }}>
                  Alert ID
                </th>
                {columns.map((col, i) => {
                  const sticky = i < STICKY_DATA_COLS;
                  return (
                    <th
                      key={col}
                      className={`${thClass}${sticky ? " z-20" : ""}`}
                      style={{
                        width: COL_MAX_WIDTHS[col] ?? 160,
                        maxWidth: COL_MAX_WIDTHS[col] ?? 160,
                        ...(sticky ? { position: "sticky", left: stickyLefts[i] } : {}),
                        ...(i === STICKY_DATA_COLS - 1 ? STICKY_DIVIDER : {}),
                      }}
                    >
                      {(schemaLabels && schemaLabels[col]) ? schemaLabels[col] : colLabel(col)}
                    </th>
                  );
                })}
                <th className={thClass} style={{ width: SNAPSHOT_COL_WIDTH, minWidth: SNAPSHOT_COL_WIDTH }}>Before HTML</th>
                <th className={thClass} style={{ width: SNAPSHOT_COL_WIDTH, minWidth: SNAPSHOT_COL_WIDTH }}>After HTML</th>
                <th className={thClass} style={{ width: RECORDING_COL_WIDTH, minWidth: RECORDING_COL_WIDTH }}>Recording</th>
                <th className={thClass} style={{ width: TRANSCRIPT_COL_WIDTH, minWidth: TRANSCRIPT_COL_WIDTH }}>Transcript</th>
                <th className={thClass} style={{ width: CHUNKS_COL_WIDTH, minWidth: CHUNKS_COL_WIDTH }}>Chunks</th>
              </tr>
            </thead>
            <tbody>
              {(() => {
                // Track which recording_s3_keys have already been rendered as primary.
                // Reset per render so order is stable. Plain const — NOT state.
                const seenRecordings = new Set<string>();
                return pagedRows.map((row, i) => {
                const rowNum = rows.length - (alertRowIndexMap.get(row) ?? (page * PAGE_SIZE + i));
                const run_id = String(row.run_id ?? "");
                const target_id = String(row.target_id ?? "");
                const rowKey = `${run_id}::${target_id}`;
                const isRunning = activeTasks.has(rowKey);
                const canRerun = !!(run_id && target_id);
                const rerunResult = completedReruns.get(rowKey);

                // Determine if this is the first/last row in its (run_id, target_id) group
                const prevRowKey = i > 0 ? `${String(pagedRows[i - 1].run_id ?? "")}::${String(pagedRows[i - 1].target_id ?? "")}` : null;
                const nextRowKey = i < pagedRows.length - 1 ? `${String(pagedRows[i + 1].run_id ?? "")}::${String(pagedRows[i + 1].target_id ?? "")}` : null;
                const isFirstInGroup = prevRowKey !== rowKey;
                const isLastInGroup = nextRowKey !== rowKey;

                // Merge real-time row overrides (e.g. transcript_chunks_s3_key after upload)
                const callId = String(row.agent_call_id ?? "");
                const overrides = rowOverrides.get(callId) ?? {};
                const displayRow = { ...row, ...overrides };

                // ── Recording deduplication ────────────────────────────────────
                // If multiple rows share the same recording_s3_key, only the first
                // one encountered ("primary") shows Upload/Publish controls.
                // Subsequent rows ("secondary") show a compact indicator instead.
                const recKey = typeof displayRow.recording_s3_key === "string" && displayRow.recording_s3_key
                  ? displayRow.recording_s3_key
                  : null;
                const isSecondaryRecording = recKey !== null && seenRecordings.has(recKey);
                if (recKey && !seenRecordings.has(recKey)) {
                  seenRecordings.add(recKey);
                }

                return (
                  <React.Fragment key={i}>
                    {/* ── Original alert row ── */}
                    <tr className="hover:bg-gray-50">
                      {/* Row number cell */}
                      <td className={`${cellClass} bg-white z-[5]`} style={{ width: ROW_NUM_COL_WIDTH, minWidth: ROW_NUM_COL_WIDTH, position: "sticky", left: 0, textAlign: "center" }}>
                        <span className="text-[11px] text-gray-400 font-mono">{rowNum}</span>
                      </td>
                      {/* Actions cell — Re-evaluate + ingest gate, first row of group */}
                      <td className={`${cellClass} bg-white z-[5]`} style={{ width: ACTIONS_COL_WIDTH, minWidth: ACTIONS_COL_WIDTH, position: "sticky", left: ROW_NUM_COL_WIDTH }}>
                        <div className="flex flex-col gap-2">
                          {canRerun && (
                            isRunning ? (
                              <div className="flex items-center gap-1.5 text-xs text-gray-500">
                                <svg className="animate-spin h-3.5 w-3.5 shrink-0" viewBox="0 0 24 24" fill="none">
                                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
                                </svg>
                                Running…
                              </div>
                            ) : (
                              <button
                                onClick={() => openModal(row)}
                                className="text-[11px] font-medium text-blue-600 hover:text-blue-800 hover:underline whitespace-nowrap"
                              >
                                Re-evaluate
                              </button>
                            )
                          )}
                          {(() => {
                            const ts = String(row.last_rerun_at ?? "");
                            if (!ts) return null;
                            const dt = new Date(ts);
                            if (isNaN(dt.getTime())) return null;
                            const label = dt.toLocaleDateString("en-US", { month: "short", day: "numeric" }) + " " +
                              dt.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
                            return <div className="text-[10px] text-gray-400">Evaluated: {label}</div>;
                          })()}
                          {(() => {
                            const callId = String(row.agent_call_id ?? "");
                            const overrideData = rowOverrides.get(callId) ?? {};
                            const effectiveRow = { ...row, ...overrideData };

                            // Never show content gate for non-applicable alert types
                            const alertType = String(effectiveRow.alert_type ?? "");
                            const NON_APPLICABLE = new Set([
                              "No Meaningful Change",
                              "Alert not relevant - the change was limited to carrousel or reordering of content",
                              "Alert not relevant - the change was limited to removal of content",
                              "Alert not relevant",
                            ]);
                            if (NON_APPLICABLE.has(alertType)) return null;

                            const bubbleAction = effectiveRow.bubble_action as Record<string, unknown> | null | undefined;
                            const eventAction = bubbleAction?.event;
                            const libAction = bubbleAction?.library_item;
                            const transcriptKey = effectiveRow.transcript_s3_key || effectiveRow.manual_transcript_s3_key;
                            const libUrl = String(effectiveRow.library_item_url ?? "");
                            const hasDocument = !!(libUrl && libUrl !== "N/A" && libUrl.startsWith("http"));
                            // Also show content gate when bubble_sync_status is set (rows synced before bubble_action was stamped)
                            const hasSyncRecord = !!effectiveRow.bubble_sync_status;

                            const hasAnyAction = !!(bubbleAction || transcriptKey || hasSyncRecord);
                            if (!hasAnyAction) return null;

                            // "Complete" = all applicable Bubble IDs actually stamped + transcript approved
                            // For rows with no bubble_action but sync_status=synced (old stub rows), treat as complete
                            const eventDone = !eventAction || !!effectiveRow.bubble_event_id;
                            const libDone = !libAction || !!effectiveRow.bubble_library_item_id;
                            const transcriptDone = !transcriptKey || effectiveRow.ingest_status === "approved";
                            const isComplete = (eventDone && libDone && transcriptDone) ||
                              (!bubbleAction && !transcriptKey && effectiveRow.bubble_sync_status === "synced");

                            if (isComplete) {
                              return (
                                <button
                                  onClick={() => setContentGateRow(effectiveRow as AlertRow)}
                                  className="text-[11px] font-medium text-green-600 hover:text-green-800 hover:underline whitespace-nowrap"
                                >
                                  ✓ Complete
                                </button>
                              );
                            }

                            const isSyncing = effectiveRow.bubble_sync_status === "syncing";
                            if (isSyncing) return <span className="text-[11px] text-blue-500 whitespace-nowrap">⟳ Syncing…</span>;

                            return (
                              <button
                                onClick={() => setContentGateRow(effectiveRow as AlertRow)}
                                className="text-[11px] font-medium text-indigo-600 hover:text-indigo-800 hover:underline whitespace-nowrap"
                              >
                                Content Gate
                              </button>
                            );
                          })()}
                        </div>
                      </td>
                      {/* Agent Call ID — always sticky */}
                      <td className={`${cellClass} bg-white z-[5]`} style={{ width: RUN_ID_COL_WIDTH, minWidth: RUN_ID_COL_WIDTH, position: "sticky", left: ROW_NUM_COL_WIDTH + ACTIONS_COL_WIDTH }}>
                        <span className="text-[11px] text-gray-500 font-mono">{(String(row.agent_call_id ?? "")).slice(-8) || "—"}</span>
                      </td>
                      {columns.map((col, ci) => {
                        const sticky = ci < STICKY_DATA_COLS;
                        return (
                          <td
                            key={col}
                            className={`${cellClass}${sticky ? " bg-white z-[5]" : ""}`}
                            style={{
                              width: COL_MAX_WIDTHS[col] ?? 160,
                              maxWidth: COL_MAX_WIDTHS[col] ?? 160,
                              ...(sticky ? { position: "sticky", left: stickyLefts[ci] } : {}),
                              ...(ci === STICKY_DATA_COLS - 1 ? STICKY_DIVIDER : {}),
                            }}
                          >
                            <CellValue col={col} value={resolveCell(row, col, priorKeys)} />
                          </td>
                        );
                      })}
                      {(["before.html", "after.html"] as const).map((file) => {
                        const key = snapshotKey(row, file);
                        return (
                          <td key={file} className={cellClass} style={{ width: SNAPSHOT_COL_WIDTH, minWidth: SNAPSHOT_COL_WIDTH }}>
                            {key ? (
                              <a
                                href={`/api/snapshot?key=${encodeURIComponent(key)}`}
                                className="text-xs text-blue-600 hover:underline"
                              >
                                Download
                              </a>
                            ) : null}
                          </td>
                        );
                      })}

                      {/* Recording */}
                      <td className={cellClass} style={{ width: RECORDING_COL_WIDTH, minWidth: RECORDING_COL_WIDTH }}>
                        {row.recording_s3_key ? (
                          <a
                            href={`/api/presigned-url?key=${encodeURIComponent(String(row.recording_s3_key))}`}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="text-xs text-blue-600 hover:underline"
                          >
                            Download
                          </a>
                        ) : <span className="text-gray-300 text-xs">—</span>}
                      </td>

                      {/* Transcript */}
                      <td className={cellClass} style={{ width: TRANSCRIPT_COL_WIDTH, minWidth: TRANSCRIPT_COL_WIDTH }}>
                        {isSecondaryRecording ? (
                          // Secondary row sharing the same recording — show compact indicator + read-only view link
                          <div className="flex flex-col gap-1">
                            <span className="text-[11px] text-gray-400" title="Upload controls are on the first row for this meeting">↑ Same meeting</span>
                            {!!(displayRow.transcript_s3_key || displayRow.manual_transcript_s3_key) && (
                              <a
                                href={`/api/presigned-url?key=${encodeURIComponent(String(displayRow.transcript_s3_key || displayRow.manual_transcript_s3_key))}`}
                                target="_blank"
                                rel="noopener noreferrer"
                                className="text-xs text-blue-600 hover:underline"
                              >
                                View
                              </a>
                            )}
                          </div>
                        ) : (
                          (() => {
                            const upState = uploadStates.get(callId);
                            const transcriptKey = displayRow.transcript_s3_key || displayRow.manual_transcript_s3_key;
                            const isProcessing = upState === "uploading";
                            return (
                              <div className="flex flex-col gap-1">
                                {transcriptKey ? (
                                  <a
                                    href={`/api/presigned-url?key=${encodeURIComponent(String(transcriptKey))}`}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    className="text-xs text-blue-600 hover:underline"
                                  >
                                    View
                                  </a>
                                ) : null}
                                {upState === "uploading" ? (
                                  <span className="text-[11px] text-gray-500">Uploading…</span>
                                ) : upState === "ready" ? (
                                  <span className="text-[11px] text-green-600">✓ Ready</span>
                                ) : upState?.startsWith("error:") ? (
                                  <span className="text-[10px] text-red-500">{upState.slice(6)}</span>
                                ) : !isProcessing ? (
                                  <label className="text-[11px] text-gray-400 hover:text-blue-600 cursor-pointer">
                                    {transcriptKey ? "Replace" : "Upload"}
                                    <input
                                      type="file"
                                      accept=".txt,text/plain"
                                      className="hidden"
                                      onChange={(e) => {
                                        const f = e.target.files?.[0];
                                        if (f) handleTranscriptUpload(row, f);
                                        e.target.value = "";
                                      }}
                                    />
                                  </label>
                                ) : null}
                              </div>
                            );
                          })()
                        )}
                      </td>

                      {/* Chunks — human-readable view of transcript_chunks_s3_key */}
                      <td className={cellClass} style={{ width: CHUNKS_COL_WIDTH, minWidth: CHUNKS_COL_WIDTH }}>
                        {displayRow.transcript_chunks_s3_key ? (
                          <a
                            href={`/api/chunks?key=${encodeURIComponent(String(displayRow.transcript_chunks_s3_key))}`}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="text-xs text-blue-600 hover:underline"
                          >
                            View
                          </a>
                        ) : uploadStates.get(callId) === "chunking" ? (
                          <svg className="animate-spin h-3.5 w-3.5 text-gray-400" viewBox="0 0 24 24" fill="none">
                            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
                          </svg>
                        ) : (
                          <span className="text-gray-300 text-xs">—</span>
                        )}
                      </td>
                    </tr>

                    {/* Re-run results are auto-accepted — no manual review group needed */}

                    {/* ── QA score row — shown below rerun results ── */}
                    {lookupEvalResult(row, evalResults) && (
                      <QaScoreRow
                        evalResult={lookupEvalResult(row, evalResults)!}
                        columns={columns}
                        stickyLefts={stickyLefts}
                        callId={String(row.agent_call_id ?? "")}
                        onRerun={startQa}
                        isRunning={qaRunning.has(String(row.agent_call_id ?? ""))}
                      />
                    )}
                  </React.Fragment>
                );
                }); // end rows.map
              })()} {/* end seenRecordings IIFE */}
            </tbody>
          </table>
        </div>
      </div>

      {/* Confirmation modal */}
      {modalRow && (
        <RerunModal
          row={modalRow}
          config={config}
          configLoading={configLoading}
          configError={configError}
          onConfirm={() => triggerRerun(modalRow)}
          onClose={() => setModalRow(null)}
        />
      )}

      {/* Content gate modal */}
      {contentGateRow && (
        <ContentGateModal
          row={contentGateRow}
          onClose={() => setContentGateRow(null)}
          onFieldPatched={(fields) => {
            const callId = String(contentGateRow.agent_call_id ?? "");
            setRowOverrides((prev) => new Map(prev).set(callId, { ...(prev.get(callId) ?? {}), ...fields }));
            // Update the modal's own row reference so re-renders reflect changes
            setContentGateRow((prev) => prev ? { ...prev, ...fields } : null);
          }}
        />
      )}

    </>
  );
}
