"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";

// ─── Types ────────────────────────────────────────────────────────────────────

export type DocExtractionRow = Record<string, unknown>;

interface FieldScore {
  score: "Correct" | "Partially Correct" | "Incorrect";
  reasoning: string;
}

interface DocEvalResult {
  agent_call_id: string;
  eval_row_key?: string;
  eval_scores: Record<string, FieldScore | { total_fields?: number; correct?: number; partially_correct?: number; incorrect?: number; pattern?: string }>;
  eval_timestamp: string;
  eval_run_id: string;
}

interface ConfigData {
  hash: string;
  model: string;
  updated_at: string;
}

interface RerunResult {
  run_id: string;
  target_id: string;
  rerun_timestamp?: string;
  agent_call_id?: string;
  original_rows?: DocExtractionRow[];
  rerun_rows?: DocExtractionRow[];
}

interface ActiveTask {
  taskId: string;
  run_id: string;
  target_id: string;
  library_item_url: string;
  agent_call_id: string;
  hasExistingQa?: boolean;
}

// ─── Legacy storage key map ───────────────────────────────────────────────────
// Maps current field registry keys → old storage keys used before normalization.
// Rows in S3 were written under these old keys; registry prior_keys doesn't
// capture them because they predate the registry or used different naming conventions.

const DOC_LEGACY_STORAGE_KEYS: Record<string, string[]> = {
  data_extraction_date_time:                          ["data_extraction_datetime"],
  organization_author:                                ["organization_or_publisher"],
  agenda_item_title_chronicle_topic:                  ["agenda_item_bridgeway_title_chronicle_topic", "agenda_items", "agenda_item_title", "agenda_item_title_chronicle_topics"],
  agenda_item_title_official:                         ["agenda_items_official"],
  agenda_item_standardized_id:                        ["agenda_items_standardized_id"],
  agenda_item_official_id:                            ["agenda_items_official_id"],
  meeting_date_or_last_comment_date:                  ["meeting_or_last_comment_date"],
  is_the_document_relevant_for_a_future_newsreel_article: ["newsreel_relevance", "is_newsreel_relevant", "is_the_document_relevant_for_a_newsreel_article"],
  web_page_url:                                       ["source_url"],
};

/** Old storage key names — excluded from column derivation to avoid duplicate columns. */
const DOC_SUPERSEDED_COLS = new Set(Object.values(DOC_LEGACY_STORAGE_KEYS).flat());

function resolveDocCell(row: DocExtractionRow, col: string, priorKeys?: Record<string, string[]>): unknown {
  if (col in row) return row[col];
  // Walk registry rename history (recent renames)
  const prior = priorKeys?.[col];
  if (prior) {
    for (const k of prior) {
      if (k in row) return row[k];
    }
  }
  // Fall back to known pre-normalization storage keys
  const legacy = DOC_LEGACY_STORAGE_KEYS[col];
  if (legacy) {
    for (const k of legacy) {
      if (k in row) return row[k];
    }
  }
  return undefined;
}

// ─── QA result lookup ────────────────────────────────────────────────────────

const _NA = new Set(["n/a", "n/a.", "-", ""]);

function _extractStdId(val: unknown): string {
  if (Array.isArray(val) && val.length > 0) {
    const first = val[0];
    return String(typeof first === "object" && first !== null ? (first as Record<string, unknown>).standardized_id ?? "" : first).trim();
  }
  return String(val ?? "").trim();
}

function _extractAgendaTitle(val: unknown): string {
  if (Array.isArray(val) && val.length > 0) {
    const first = val[0];
    return String(typeof first === "object" && first !== null ? (first as Record<string, unknown>).agenda_item_title ?? "" : first).trim();
  }
  return String(val ?? "").trim();
}

function _extractOfficialTitle(val: unknown): string {
  if (Array.isArray(val) && val.length > 0) {
    const first = val[0];
    return String(typeof first === "object" && first !== null ? (first as Record<string, unknown>).official_title ?? "" : first).trim();
  }
  return String(val ?? "").trim();
}

function getEvalForRow(row: DocExtractionRow, evalResults: Record<string, DocEvalResult>): DocEvalResult | undefined {
  const callId = String(row.agent_call_id ?? "");
  if (!callId) return undefined;
  // Mirror backend make_doc_eval_row_key: std_id → title → official_title → number → bare id
  const stdId = _extractStdId(row.agenda_item_standardized_id ?? row.agenda_items_standardized_id);
  if (stdId && !_NA.has(stdId.toLowerCase())) {
    const r = evalResults[`${callId}|${stdId}`];
    if (r) return r;
  }
  const title = _extractAgendaTitle(row.agenda_item_title_chronicle_topic ?? row.agenda_item_bridgeway_title_chronicle_topic ?? row.agenda_item_title ?? row.agenda_items);
  if (title && !_NA.has(title.toLowerCase())) {
    const r = evalResults[`${callId}|${title}`];
    if (r) return r;
  }
  const official = _extractOfficialTitle(row.agenda_item_title_official);
  if (official && !_NA.has(official.toLowerCase())) {
    const r = evalResults[`${callId}|${official}`];
    if (r) return r;
  }
  const number = String(row.number ?? "").trim();
  if (number && !_NA.has(number.toLowerCase()) && number !== "0") {
    const r = evalResults[`${callId}|item_${number}`];
    if (r) return r;
  }
  // Backward compat: old eval keys used library_item_url
  const libUrl = String(row.library_item_url ?? "");
  if (libUrl && !_NA.has(libUrl.toLowerCase())) {
    const r = evalResults[`${callId}|${libUrl}`];
    if (r) return r;
  }
  return evalResults[callId];
}

// ─── QA scoring ───────────────────────────────────────────────────────────────

const _DOC_EVAL_METADATA_KEYS = new Set([
  "run_id", "run_timestamp", "target_id", "source_url", "agent_call_id",
  "eval_run_id", "eval_timestamp", "eval_scores", "eval_row_key",
  "extraction_source", "ingest_status",
  "library_item_title", "library_item_url", "library_item_file_name",
]);

function computeDocQaScore(
  evalScores: DocEvalResult["eval_scores"],
): { correct: number; partial: number; incorrect: number; total: number } {
  let correct = 0, partial = 0, incorrect = 0, total = 0;
  for (const [key, val] of Object.entries(evalScores)) {
    if (key === "overall_summary" || _DOC_EVAL_METADATA_KEYS.has(key)) continue;
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

function QaScoreCell({ evalScores, col, colLabel: label, priorKeys }: { evalScores: DocEvalResult["eval_scores"]; col: string; colLabel?: string; priorKeys?: Record<string, string[]> }) {
  const trimmedLabel = label?.trim();
  // Try col, trimmed label, registry rename history, then legacy pre-normalization keys
  const candidates = [col, ...(trimmedLabel ? [trimmedLabel] : []), ...(priorKeys?.[col] ?? []), ...(DOC_LEGACY_STORAGE_KEYS[col] ?? [])];
  let entry = candidates
    .map(k => evalScores[k])
    .find((v): v is FieldScore => !!v && typeof v === "object" && "score" in v);
  // Case-insensitive fallback: handles label mismatches like "Document description" vs "Document Description"
  if (!entry) {
    const lowerCandidates = new Set(candidates.map(k => k.toLowerCase()));
    for (const [key, val] of Object.entries(evalScores)) {
      if (lowerCandidates.has(key.toLowerCase()) && typeof val === "object" && val !== null && "score" in val) {
        entry = val as FieldScore;
        break;
      }
    }
  }
  if (!entry) return <span className="text-gray-300 text-xs">—</span>;
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

function DocQaScoreRow({
  evalResult,
  columns,
  stickyLefts,
  callId,
  libUrl,
  onRerun,
  isRunning,
  schemaLabels,
  priorKeys,
}: {
  evalResult: DocEvalResult;
  columns: string[];
  stickyLefts: number[];
  callId: string;
  libUrl?: string;
  onRerun: (callId: string, libUrl?: string) => void;
  isRunning: boolean;
  schemaLabels?: Record<string, string> | null;
  priorKeys?: Record<string, string[]>;
}) {
  const { correct, total } = computeDocQaScore(evalResult.eval_scores);
  const _evalDt = evalResult.eval_timestamp ? new Date(evalResult.eval_timestamp) : null;
  const evalDateStr = _evalDt && !isNaN(_evalDt.getTime())
    ? _evalDt.toLocaleDateString("en-US", { month: "short", day: "numeric" }) + " " +
      _evalDt.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })
    : "";

  return (
    <tr className="border-t border-violet-100">
      <td
        className={`${cellClass} bg-violet-50 z-[5]`}
        style={{ width: ROW_NUM_COL_WIDTH, minWidth: ROW_NUM_COL_WIDTH, position: "sticky", left: 0 }}
      />
      <td
        className={`${cellClass} bg-violet-50 z-[5]`}
        style={{ width: ACTIONS_COL_WIDTH, minWidth: ACTIONS_COL_WIDTH, position: "sticky", left: ROW_NUM_COL_WIDTH }}
      >
        <div className="flex flex-col gap-1">
          <div className="text-[11px] text-violet-700 font-semibold">
            QA &nbsp;<span className="font-normal text-gray-600">{correct}/{total}</span>
          </div>
          {evalDateStr && <div className="text-[10px] text-gray-500">Evaluated: {evalDateStr}</div>}
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
              onClick={() => onRerun(callId, libUrl)}
              className="text-[11px] text-gray-400 hover:text-gray-600 hover:underline text-left"
            >
              Re-run QA
            </button>
          )}
        </div>
      </td>
      <td
        className={`${cellClass} bg-violet-50 z-[5]`}
        style={{ width: RUN_ID_COL_WIDTH, minWidth: RUN_ID_COL_WIDTH, position: "sticky", left: ROW_NUM_COL_WIDTH + ACTIONS_COL_WIDTH }}
      />
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
            <QaScoreCell evalScores={evalResult.eval_scores} col={col} colLabel={schemaLabels?.[col]} priorKeys={priorKeys} />
          </td>
        );
      })}
    </tr>
  );
}

// ─── Column config ────────────────────────────────────────────────────────────

// Pipeline metadata — never shown as table columns
const METADATA_COLS = new Set([
  "run_id", "run_timestamp", "target_id", "source_url", "agent_call_id",
  "ingest_status",
]);

// Identity columns pinned first (before schema columns)
export const DOC_IDENTITY_COLS = [
  "library_item_title",
  "library_item_url",
  "library_item_file_name",
  "source_url",
];

const COL_MAX_WIDTHS: Record<string, number> = {
  library_item_title: 240,
  library_item_url: 200,
  library_item_file_name: 200,
  source_url: 200,
  number: 80,
  organization_or_publisher: 180,
  document_type: 160,
  document_title: 240,
  updated_or_new_document: 160,
  date_published: 140,
  meeting_or_last_comment_date: 160,
  existing_or_new_agenda_item: 160,
  agenda_item_title: 220,
  agenda_item_title_official: 220,
  agenda_item_official_id: 140,
  agenda_item_standardized_id: 160,
  chronicle_topics: 240,
};

/**
 * Derive the visible column list.
 * Identity cols are pinned first, then schema cols, then any extras alphabetically.
 * METADATA_COLS are never shown.
 */
export function deriveColumns(rows: DocExtractionRow[], schemaColumns: string[] | null): string[] {
  const dataKeys = new Set<string>();
  rows.forEach((r) =>
    Object.keys(r).forEach((k) => {
      if (!METADATA_COLS.has(k) && !DOC_SUPERSEDED_COLS.has(k)) dataKeys.add(k);
    })
  );

  const identitySet = new Set(DOC_IDENTITY_COLS);

  if (schemaColumns && schemaColumns.length > 0) {
    // Only show schema columns — never include stale columns from old rows
    return [...schemaColumns];
  }

  // No schema: identity cols first, then remaining data keys sorted
  const identity = DOC_IDENTITY_COLS.filter((c) => dataKeys.has(c));
  const extras = Array.from(dataKeys).filter((k) => !identitySet.has(k)).sort();
  return [...identity, ...extras];
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

/** Extract the primary display value from an object with known field patterns. */
function extractMainValue(obj: Record<string, unknown>): unknown {
  return obj.title ?? obj.title_official ?? obj.official_title ?? obj.name
    ?? obj.agenda_item_title ?? obj.reference ?? obj.explanation_or_reference
    ?? obj.standardized_id ?? obj.official_id ?? null;
}

/** Render an array of objects as a structured vertical list instead of raw JSON. */
function ObjectArrayList({ items, col }: { items: Record<string, unknown>[]; col: string }) {
  const [expanded, setExpanded] = useState(false);
  const visible = expanded ? items : items.slice(0, 3);
  const hasMore = items.length > 3;

  return (
    <div className="space-y-1">
      {visible.map((item, idx) => {
        const status = item.status;
        const main = extractMainValue(item);
        const isNA = status === "N/A" || status === "No";
        // Find chronicle_topics or similar tag arrays nested in the item
        const tagArrays = Object.entries(item).filter(
          ([k, v]) => Array.isArray(v) && v.length > 0 && typeof v[0] === "string"
            && (k.includes("topic") || k.includes("chronicle"))
        );
        const extraFields = Object.entries(item).filter(
          ([k, v]) => k !== "status" && v !== main && typeof v === "string" && v !== "N/A"
            && k !== Object.keys(item).find((key) => item[key] === main)
            && !k.includes("topic") && !k.includes("chronicle")
        );

        return (
          <div key={idx} className={`${idx > 0 ? "border-t border-gray-200 pt-1" : ""}`}>
            <span className={`text-xs ${isNA ? "text-gray-400" : "text-gray-900"}`}>
              {main ? String(main) : ""}
              {status !== undefined ? ` (${status})` : ""}
            </span>
            {extraFields.map(([k, v]) => (
              <div key={k} className="text-[10px] text-gray-500 ml-2">{String(v)}</div>
            ))}
            {tagArrays.map(([k, v]) => (
              <div key={k} className="mt-0.5">
                <TagList value={(v as string[]).join(", ")} />
              </div>
            ))}
          </div>
        );
      })}
      {hasMore && (
        <button onClick={() => setExpanded(!expanded)} className="text-[11px] text-blue-600 hover:underline">
          {expanded ? "Show less" : `+${items.length - 3} more`}
        </button>
      )}
    </div>
  );
}

function CellValue({ col, value }: { col: string; value: unknown }) {
  if (isEmpty(value)) return <span className="text-gray-400">—</span>;
  if (typeof value === "boolean")
    return <span className="text-xs text-gray-900">{value ? "Yes" : "No"}</span>;
  if ((col.endsWith("_url") || col.includes("_url_")) && typeof value === "string" && value.startsWith("http") && value !== "N/A") {
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
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="text-gray-400">—</span>;
    if (typeof value[0] === "object" && value[0] !== null)
      return <ObjectArrayList items={value as Record<string, unknown>[]} col={col} />;
    const str = value.filter(Boolean).join(", ");
    return col.includes("topic") || col.includes("chronicle")
      ? <TagList value={str} />
      : <span className="text-xs text-gray-900">{str}</span>;
  }
  if (typeof value === "object" && value !== null) {
    const obj = value as Record<string, unknown>;
    const status = obj.status;
    const main = extractMainValue(obj);
    // organization_author uses {name, status} where status="Listed"/"NEW ORGANIZATION" is
    // internal org-tree metadata, not display content — show name only.
    if (col === "organization_author" || col === "organization_or_publisher") {
      return <span className="text-xs text-gray-900">{String(main ?? JSON.stringify(value))}</span>;
    }
    if (status !== undefined) {
      const isNA = status === "N/A" || status === "No";
      return (
        <span className={`text-xs ${isNA ? "text-gray-400" : "text-gray-900"}`}>
          {main ? `${main} (${status})` : String(status)}
        </span>
      );
    }
    return <ExpandableText text={JSON.stringify(value)} />;
  }
  if (typeof value === "string" && value.includes(",") && (col.includes("topic") || col.includes("chronicle")))
    return <TagList value={value} />;
  if (typeof value === "string" && value.length > 150)
    return <ExpandableText text={value} />;
  return <span className="text-xs text-gray-900">{String(value)}</span>;
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

// ─── Rerun result group ──────────────────────────────────────────────────────

/**
 * Find the original row that best matches a given rerun row (fewest changed fields).
 */
function bestMatchOriginal(rerunRow: DocExtractionRow, originals: DocExtractionRow[], columns: string[]): DocExtractionRow {
  if (originals.length === 0) return {};
  if (originals.length === 1) return originals[0];
  let bestIdx = 0;
  let bestDiffs = Infinity;
  for (let oi = 0; oi < originals.length; oi++) {
    let diffs = 0;
    for (const col of columns) {
      const ov = resolveDocCell(originals[oi], col);
      const rv = resolveDocCell(rerunRow, col);
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
  stickyLefts,
  onAccept,
  onDiscard,
}: {
  result: RerunResult;
  columns: string[];
  stickyLefts: number[];
  onAccept: () => void;
  onDiscard: () => void;
}) {
  const [accepting, setAccepting] = useState(false);
  const [discarding, setDiscarding] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const originals: DocExtractionRow[] = result.original_rows ?? [];
  const reruns: DocExtractionRow[] = result.rerun_rows ?? [];

  const IMMUTABLE_COLS_SET = new Set(["data_extraction_datetime"]);
  const totalChangedCount = useMemo(() => {
    const changed = new Set<string>();
    for (const rerunRow of reruns) {
      const best = bestMatchOriginal(rerunRow, originals, columns);
      for (const col of columns) {
        if (IMMUTABLE_COLS_SET.has(col)) continue;
        const a = resolveDocCell(best, col);
        const b = resolveDocCell(rerunRow, col);
        const aStr = a === null || a === undefined ? "" : String(a);
        const bStr = b === null || b === undefined ? "" : String(b);
        if (aStr !== bStr) changed.add(col);
      }
    }
    return changed.size;
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [originals, reruns, columns]);

  async function handleAccept() {
    setAccepting(true);
    setActionError(null);
    try {
      // Extract library_item_url from the first rerun row for per-document accept
      const firstRerun = reruns[0] as Record<string, unknown> | undefined;
      const libUrl = firstRerun?.library_item_url ? String(firstRerun.library_item_url) : undefined;
      const res = await fetch("/api/doc-rerun/accept", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ run_id: result.run_id, target_id: result.target_id, ...(libUrl ? { library_item_url: libUrl } : {}) }),
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
      const res = await fetch("/api/doc-rerun/discard", {
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

  if (reruns.length === 0) {
    return (
      <tr className="bg-amber-50 border-t-2 border-b-2 border-amber-300">
        <td className={`${cellClass} bg-amber-50 z-[5]`} style={{ width: ROW_NUM_COL_WIDTH, minWidth: ROW_NUM_COL_WIDTH, position: "sticky", left: 0 }} />
        <td
          className={`${cellClass} border-l-4 border-amber-400 bg-amber-50 z-[5]`}
          style={{ width: ACTIONS_COL_WIDTH, minWidth: ACTIONS_COL_WIDTH, position: "sticky", left: ROW_NUM_COL_WIDTH }}
        >
          <div className="flex flex-col gap-1.5">
            <span className="text-[10px] font-bold text-amber-700 uppercase tracking-wide">Re-run result</span>
            <span className="text-[10px] text-amber-600">0 rows — agent found no output</span>
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
        <td className={`${cellClass} bg-amber-50`} colSpan={columns.length + 2} />
      </tr>
    );
  }

  return (
    <>
      {reruns.map((rerunRow, ri) => {
        const best = bestMatchOriginal(rerunRow, originals, columns);
        const isFirst = ri === 0;

        // Fields that must never change on re-evaluation — always show original value, never highlight as changed
        const IMMUTABLE_COLS = new Set(["data_extraction_datetime"]);

        function isChanged(col: string): boolean {
          if (IMMUTABLE_COLS.has(col)) return false;
          const a = resolveDocCell(best, col);
          const b = resolveDocCell(rerunRow, col);
          return (a === null || a === undefined ? "" : String(a)) !== (b === null || b === undefined ? "" : String(b));
        }

        function resolveCell(row: DocExtractionRow, col: string): unknown {
          return IMMUTABLE_COLS.has(col) ? resolveDocCell(best, col) : resolveDocCell(row, col);
        }

        return (
          <tr key={ri} className={`bg-amber-50 ${isFirst ? "border-t-2" : ""} ${ri === reruns.length - 1 ? "border-b-2" : ""} border-amber-300`}>
            <td
              className={`${cellClass} bg-amber-50 z-[5]`}
              style={{ width: ROW_NUM_COL_WIDTH, minWidth: ROW_NUM_COL_WIDTH, position: "sticky", left: 0 }}
            />
            <td
              className={`${cellClass} border-l-4 border-amber-400 bg-amber-50 z-[5]`}
              style={{ width: ACTIONS_COL_WIDTH, minWidth: ACTIONS_COL_WIDTH, position: "sticky", left: ROW_NUM_COL_WIDTH }}
            >
              {isFirst ? (
                <div className="flex flex-col gap-1.5">
                  <span className="text-[10px] font-bold text-amber-700 uppercase tracking-wide">Re-run result</span>
                  {rowCountChanged ? (
                    <span className="text-[10px] text-amber-600">
                      {originals.length} → {reruns.length} row{reruns.length !== 1 ? "s" : ""}
                    </span>
                  ) : null}
                  <span className="text-[10px] text-amber-600">{totalChangedCount} field{totalChangedCount !== 1 ? "s" : ""} changed</span>
                  {result.rerun_timestamp && (
                    <span className="text-[10px] text-gray-400">{formatTimestamp(result.rerun_timestamp)}</span>
                  )}
                  {actionError && <span className="text-[10px] text-red-600">{actionError}</span>}
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
            <td className={`${cellClass} bg-amber-50 z-[5]`} style={{ width: RUN_ID_COL_WIDTH, minWidth: RUN_ID_COL_WIDTH, position: "sticky", left: ROW_NUM_COL_WIDTH + ACTIONS_COL_WIDTH }}>
              <span className="text-[11px] text-gray-500 font-mono">{(String(result.agent_call_id ?? (rerunRow.agent_call_id as string) ?? "")).slice(-8) || "—"}</span>
            </td>
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
                  title={changed ? `Was: ${resolveDocCell(best, col) === null || resolveDocCell(best, col) === undefined ? "—" : String(resolveDocCell(best, col))}` : undefined}
                >
                  <CellValue col={col} value={resolveCell(rerunRow, col)} />
                </td>
              );
            })}
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

// ─── Add Document panel ───────────────────────────────────────────────────────

function AddDocumentPanel({
  onAdded,
  onPending,
}: {
  onAdded: () => void;
  onPending: (callId: string, filename: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<"url" | "file">("url");
  const [url, setUrl] = useState("");
  const [filename, setFilename] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [status, setStatus] = useState<"idle" | "uploading" | "saving" | "done" | "error">("idle");
  const [errorMsg, setErrorMsg] = useState("");

  function reset() {
    setUrl(""); setFilename(""); setFile(null); setStatus("idle"); setErrorMsg("");
  }

  async function handleSubmit() {
    const name = mode === "file" ? (file?.name ?? "") : (filename.trim() || url.split("/").pop() || url);
    if (!name) { setErrorMsg("Please enter a filename"); return; }
    if (mode === "url" && !url.trim()) { setErrorMsg("Please enter a URL"); return; }
    if (mode === "file" && !file) { setErrorMsg("Please choose a file"); return; }

    setStatus("uploading");
    setErrorMsg("");
    try {
      let s3_key: string | undefined;

      if (mode === "file" && file) {
        const urlResp = await fetch("/api/ingest/upload-document-url", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ filename: file.name }),
        });
        const { upload_url, s3_key: key, error } = await urlResp.json() as Record<string, string>;
        if (error) throw new Error(error);
        await fetch(upload_url, { method: "PUT", body: file, headers: { "Content-Type": "application/pdf" } });
        s3_key = key;
      }

      setStatus("saving");
      const addResp = await fetch("/api/ingest/add-document", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: name, url: mode === "url" ? url.trim() : undefined, s3_key }),
      });
      const { error, agent_call_id } = await addResp.json() as { error?: string; agent_call_id?: string };
      if (error) throw new Error(error);

      setStatus("done");
      // Register as pending so the table shows a shimmer row while ECS processes it.
      // Close the panel immediately — don't wait for a full refresh (onAdded) since
      // the row will appear as a shimmer until the agent finishes.
      setTimeout(() => {
        setOpen(false);
        reset();
        if (agent_call_id) {
          onPending(agent_call_id, name);
        } else {
          // Fallback: no call ID returned, just refresh immediately
          onAdded();
        }
      }, 800);
    } catch (err) {
      setStatus("error");
      setErrorMsg(err instanceof Error ? err.message : String(err));
    }
  }

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="text-[12px] font-medium text-blue-600 hover:text-blue-800 border border-blue-200 rounded px-3 py-1 bg-white hover:bg-blue-50"
      >
        + Add Document
      </button>
    );
  }

  return (
    <div className="bg-white border border-gray-200 rounded-lg p-4 shadow-sm w-full max-w-lg">
      <div className="flex items-center justify-between mb-3">
        <span className="text-sm font-semibold text-gray-800">Add Document</span>
        <button onClick={() => { setOpen(false); reset(); }} className="text-gray-400 hover:text-gray-600 text-lg leading-none">×</button>
      </div>

      {/* Mode toggle */}
      <div className="flex gap-0 mb-3 border border-gray-200 rounded overflow-hidden w-fit text-[12px]">
        <button
          onClick={() => setMode("url")}
          className={`px-3 py-1 ${mode === "url" ? "bg-blue-600 text-white" : "text-gray-600 hover:bg-gray-50"}`}
        >
          URL
        </button>
        <button
          onClick={() => setMode("file")}
          className={`px-3 py-1 ${mode === "file" ? "bg-blue-600 text-white" : "text-gray-600 hover:bg-gray-50"}`}
        >
          File Upload
        </button>
      </div>

      {mode === "url" ? (
        <div className="space-y-2">
          <input
            type="url"
            placeholder="https://..."
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            className="w-full border border-gray-300 rounded px-2 py-1.5 text-xs focus:outline-none focus:border-blue-400"
          />
          <input
            type="text"
            placeholder="Display name (optional)"
            value={filename}
            onChange={(e) => setFilename(e.target.value)}
            className="w-full border border-gray-300 rounded px-2 py-1.5 text-xs focus:outline-none focus:border-blue-400"
          />
        </div>
      ) : (
        <label className="block border-2 border-dashed border-gray-200 rounded p-4 text-center cursor-pointer hover:border-blue-300">
          <input
            type="file"
            accept=".pdf,application/pdf"
            className="hidden"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
          {file
            ? <span className="text-xs text-gray-800">{file.name}</span>
            : <span className="text-xs text-gray-400">Click to choose a PDF</span>
          }
        </label>
      )}

      {errorMsg && <p className="text-[11px] text-red-500 mt-2">{errorMsg}</p>}

      <div className="flex justify-end gap-2 mt-3">
        <button onClick={() => { setOpen(false); reset(); }} className="text-xs text-gray-500 hover:text-gray-700 px-3 py-1">
          Cancel
        </button>
        <button
          onClick={handleSubmit}
          disabled={status === "uploading" || status === "saving" || status === "done"}
          className="text-xs font-medium text-white bg-blue-600 hover:bg-blue-700 rounded px-3 py-1 disabled:opacity-50"
        >
          {status === "uploading" ? "Uploading…" : status === "saving" ? "Saving…" : status === "done" ? "Added ✓" : "Add"}
        </button>
      </div>
    </div>
  );
}

// ─── Main table ───────────────────────────────────────────────────────────────

const thClass = "px-3 py-2 text-xs font-semibold text-gray-700 border border-gray-300 bg-gray-50 sticky top-0 z-10";
const cellClass = "px-3 py-2 text-sm border border-gray-300 align-top break-words";

const ROW_NUM_COL_WIDTH = 44;
const ACTIONS_COL_WIDTH = 140;
const RUN_ID_COL_WIDTH = 130;
const STICKY_DIVIDER: React.CSSProperties = { boxShadow: "4px 0 8px -2px rgba(0,0,0,0.3)" };
// Number of data columns to freeze after the Row # + Actions + Run ID columns
const STICKY_DATA_COLS = 2;

interface DocExtractionsTableProps {
  rows: DocExtractionRow[];
  onAccepted?: () => void;
  schemaVersion?: number;
  hasQaScoreFilter?: boolean;
  hasImperfectQaFilter?: boolean;
}

export function DocExtractionsTable({ rows, onAccepted, schemaVersion = 0, hasQaScoreFilter = false, hasImperfectQaFilter = false }: DocExtractionsTableProps) {
  const [schemaColumns, setSchemaColumns] = useState<string[] | null>(null);
  const [schemaLabels, setSchemaLabels] = useState<Record<string, string> | null>(null);
  const [priorKeys, setPriorKeys] = useState<Record<string, string[]>>({});

  // ── Pending rows — keyed by agent_call_id, shown as shimmer until ECS task completes ──
  const [pendingRows, setPendingRows] = useState<Map<string, { filename: string; submitted_at: string }>>(new Map());

  useEffect(() => {
    fetch("/api/doc-schema", { cache: "no-store" })
      .then((r) => r.json())
      .then((d: { columns: string[] | null; labels: Record<string, string> | null; priorKeys?: Record<string, string[]> }) => {
        setSchemaColumns(d.columns);
        setSchemaLabels(d.labels);
        setPriorKeys(d.priorKeys ?? {});
      })
      .catch(() => {});
  }, [schemaVersion]);

  const columns = deriveColumns(rows, schemaColumns);
  const tableWidth = ROW_NUM_COL_WIDTH + ACTIONS_COL_WIDTH + RUN_ID_COL_WIDTH + columns.reduce((sum, col) => sum + (COL_MAX_WIDTHS[col] ?? 160), 0);
  const topRef = useRef<HTMLDivElement>(null);
  const tableRef = useRef<HTMLDivElement>(null);

  // Cumulative left offsets for sticky data columns (starts after Row # + Actions + Run ID)
  const stickyLefts = useMemo(() => {
    let acc = ROW_NUM_COL_WIDTH + ACTIONS_COL_WIDTH + RUN_ID_COL_WIDTH;
    return columns.map((col) => {
      const left = acc;
      acc += COL_MAX_WIDTHS[col] ?? 160;
      return left;
    });
  }, [columns]);


  // ── Re-evaluate state ──────────────────────────────────────────────────────
  const [modalRow, setModalRow] = useState<DocExtractionRow | null>(null);
  const [config, setConfig] = useState<ConfigData | null>(null);
  const [configLoading, setConfigLoading] = useState(false);
  const [configError, setConfigError] = useState<string | null>(null);
  const [activeTasks, setActiveTasks] = useState<Map<string, ActiveTask>>(new Map());
  // Pending QA trigger: set by the polling handler after auto-accept; consumed by a useEffect
  const [pendingQaTrigger, setPendingQaTrigger] = useState<{ callId: string; libUrl: string } | null>(null);
  const [page, setPage] = useState(0);
  const PAGE_SIZE = 50;

  // ── QA evaluation state ───────────────────────────────────────────────────
  const [evalResults, setEvalResults] = useState<Record<string, DocEvalResult>>({});
  const [evalVersion, setEvalVersion] = useState(0);
  const [qaRunning, setQaRunning] = useState<Map<string, string>>(new Map());

  // Fetch eval results on mount and after each completed QA run
  useEffect(() => {
    fetch("/api/eval/documents", { cache: "no-store" })
      .then((r) => r.json())
      .then((d: { results: Record<string, DocEvalResult> }) => {
        if (d.results) setEvalResults(d.results);
      })
      .catch(() => {});
  }, [evalVersion]);

  // Trigger QA after auto-accept — runs in a clean React effect context, not inside setInterval
  useEffect(() => {
    if (!pendingQaTrigger) return;
    setPendingQaTrigger(null);
    startQa(pendingQaTrigger.callId, pendingQaTrigger.libUrl || undefined);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingQaTrigger]);

  // Poll in-flight QA tasks every 5s
  useEffect(() => {
    if (qaRunning.size === 0) return;
    const interval = setInterval(async () => {
      for (const [qaKey, taskId] of Array.from(qaRunning.entries())) {
        try {
          const res = await fetch(`/api/eval/documents/${taskId}`);
          const data = await res.json() as { status: string; error?: string };
          if (data.status === "running") continue;
          setQaRunning((prev) => {
            const next = new Map(prev);
            next.delete(qaKey);
            return next;
          });
          if (data.status === "complete") {
            setEvalVersion((v) => v + 1);
          } else if (data.status === "failed" || data.status === "error") {
            alert(`QA evaluation failed: ${data.error ?? "Unknown error"}`);
          } else {
            setEvalVersion((v) => v + 1);
          }
        } catch { /* keep polling */ }
      }
    }, 5000);
    return () => clearInterval(interval);
  }, [qaRunning]);

  async function startQa(callId: string, libraryItemUrl?: string) {
    const qaKey = libraryItemUrl ? `${callId}::${libraryItemUrl}` : callId;
    try {
      const res = await fetch("/api/eval/documents", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent_call_id: callId, library_item_url: libraryItemUrl || undefined }),
      });
      const data = await res.json() as { taskId?: string; error?: string };
      if (!res.ok || !data.taskId) throw new Error(data.error ?? "Failed to start QA");
      setQaRunning((prev) => new Map(prev).set(qaKey, data.taskId!));
    } catch (err) {
      alert(`Failed to start QA: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  // ── Persist QA running state to localStorage ──────────────────────────────
  const QA_LS_KEY = "doc_qaRunning";

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

  // ── Persist active rerun tasks to localStorage ────────────────────────────
  const ACTIVE_LS_KEY = "doc_activeReruns";

  useEffect(() => {
    try {
      const stored = localStorage.getItem(ACTIVE_LS_KEY);
      if (!stored) return;
      const parsed = JSON.parse(stored) as Record<string, unknown>;
      if (Object.keys(parsed).length === 0) return;
      const valid: Record<string, ActiveTask> = {};
      for (const [rowKey, val] of Object.entries(parsed)) {
        // Only accept new-format entries: { taskId, run_id, target_id, library_item_url }
        // Old-format entries had { taskId, row } — silently drop them
        if (typeof val === "object" && val !== null && "taskId" in val && "run_id" in val && "target_id" in val) {
          const t = val as ActiveTask;
          // Backfill agent_call_id for tasks stored before it was added to ActiveTask
          if (!t.agent_call_id) t.agent_call_id = rowKey.split("::")[0] ?? "";
          valid[rowKey] = t;
        }
      }
      if (Object.keys(valid).length === 0) return;
      setActiveTasks((prev) => {
        const next = new Map(prev);
        for (const [rowKey, val] of Object.entries(valid)) {
          if (!next.has(rowKey)) next.set(rowKey, val);
        }
        return next;
      });
    } catch { /* ignore */ }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    try {
      if (activeTasks.size === 0) {
        localStorage.removeItem(ACTIVE_LS_KEY);
      } else {
        const obj: Record<string, ActiveTask> = {};
        for (const [k, v] of Array.from(activeTasks.entries())) obj[k] = v;
        localStorage.setItem(ACTIVE_LS_KEY, JSON.stringify(obj));
      }
    } catch { /* ignore storage errors */ }
  }, [activeTasks]);

  // Clear any stale completed reruns from localStorage on mount (auto-accept is now immediate)
  useEffect(() => {
    try { localStorage.removeItem("doc_completedReruns"); } catch { /* ignore */ }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Poll for newly-submitted document row ─────────────────────────────────
  function pollForDocRow(callId: string, attempt = 0) {
    if (attempt > 20) {
      // ~2 min timeout — give up and refresh so user can see what exists
      setPendingRows((prev) => {
        const next = new Map(prev);
        next.delete(callId);
        return next;
      });
      onAccepted?.();
      return;
    }
    setTimeout(async () => {
      try {
        const r = await fetch(`/api/document-extractions/row-status?agent_call_id=${encodeURIComponent(callId)}`);
        const d = await r.json() as { found?: boolean; agent_call_id?: string };
        if (d.found) {
          setPendingRows((prev) => {
            const next = new Map(prev);
            next.delete(callId);
            return next;
          });
          onAccepted?.();
        } else {
          pollForDocRow(callId, attempt + 1);
        }
      } catch {
        pollForDocRow(callId, attempt + 1);
      }
    }, 6000);
  }

  function openModal(row: DocExtractionRow) {
    setModalRow(row);
    setConfig(null);
    setConfigError(null);
    setConfigLoading(true);
    fetch("/api/doc-config")
      .then((r) => r.json())
      .then((d: { hash?: string; model?: string; updated_at?: string; error?: string }) => {
        if (d.error) throw new Error(d.error);
        setConfig(d as ConfigData);
      })
      .catch((e: Error) => setConfigError(e.message))
      .finally(() => setConfigLoading(false));
  }

  /** Build per-document rerun key: agent_call_id::library_item_url */
  function docRerunKey(row: DocExtractionRow): string {
    return `${String(row.agent_call_id ?? "")}::${String(row.library_item_url ?? "")}`;
  }

  async function triggerRerun(row: DocExtractionRow) {
    setModalRow(null);
    const run_id = String(row.run_id ?? "");
    const target_id = String(row.target_id ?? "");
    const library_item_url = String(row.library_item_url ?? "");
    const agent_call_id = String(row.agent_call_id ?? "");
    const rowKey = docRerunKey(row);
    const hasExistingQa = !!getEvalForRow(row, evalResults);

    try {
      const res = await fetch("/api/doc-rerun", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ run_id, target_id, library_item_url }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error ?? "Rerun failed to start");
      const taskId = (data.taskArn as string).split("/").pop()!;
      setActiveTasks((prev) => {
        const next = new Map(prev);
        next.set(rowKey, { taskId, run_id, target_id, library_item_url, agent_call_id, hasExistingQa });
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
      for (const [rowKey, { taskId, run_id, target_id, library_item_url, agent_call_id, hasExistingQa }] of Array.from(activeTasks.entries())) {
        try {
          const res = await fetch(
            `/api/doc-rerun/${taskId}?run_id=${encodeURIComponent(run_id)}&target_id=${encodeURIComponent(target_id)}`
          );
          const data = await res.json();
          if (data.status === "running") continue;
          setActiveTasks((prev) => {
            const next = new Map(prev);
            next.delete(rowKey);
            return next;
          });
          if (data.status === "complete" && data.result) {
            try {
              const acceptRes = await fetch("/api/doc-rerun/accept", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ run_id, target_id, library_item_url }),
              });
              if (acceptRes.ok) {
                onAccepted?.();
                // Only auto-trigger QA if QA already existed for this row when the rerun was started
                if (agent_call_id && hasExistingQa) setPendingQaTrigger({ callId: agent_call_id, libUrl: library_item_url });
              } else {
                console.error("doc-rerun auto-accept failed", await acceptRes.text().catch(() => ""));
              }
            } catch (e) {
              console.error("doc-rerun auto-accept error", e);
            }
          } else if (data.status === "failed" || data.status === "error") {
            alert(`Re-evaluation failed: ${data.error ?? "Unknown error"}`);
          } else if (data.status === "complete" && !data.result) {
            alert("Re-evaluation completed but no document extraction result was found in S3.");
          }
        } catch {
          // network error — keep polling
        }
      }
    }, 5000);

    return () => clearInterval(interval);
  }, [activeTasks]);

  // Stable row number index: position in full unfiltered rows (for stable, descending numbering)
  const rowIndexMap = useMemo(() => new Map(rows.map((r, i) => [r, i])), [rows]);

  // Apply QA filters
  const displayedRows = hasImperfectQaFilter
    ? rows.filter((r) => {
        const ev = getEvalForRow(r, evalResults);
        if (!ev) return false;
        const { correct, total } = computeDocQaScore(ev.eval_scores);
        return correct < total;
      })
    : hasQaScoreFilter
    ? rows.filter((r) => !!getEvalForRow(r, evalResults))
    : rows;

  const totalPages = Math.ceil(displayedRows.length / PAGE_SIZE);
  const pagedRows = displayedRows.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);

  // Reset to page 0 when filters or row count changes
  const prevFilterKey = React.useRef("");
  const filterKey = `${hasImperfectQaFilter}|${hasQaScoreFilter}|${displayedRows.length}`;
  if (filterKey !== prevFilterKey.current) {
    prevFilterKey.current = filterKey;
    if (page !== 0) setPage(0);
  }

  if (rows.length === 0) return null;

  if (hasImperfectQaFilter && displayedRows.length === 0) {
    return <p className="text-sm text-gray-500 mt-2">No rows with imperfect QA scores found.</p>;
  }
  if (hasQaScoreFilter && displayedRows.length === 0) {
    return <p className="text-sm text-gray-500 mt-2">No rows with QA scores found.</p>;
  }

  return (
    <>
      {/* Add Document panel */}
      <div className="mb-3">
        <AddDocumentPanel
          onAdded={() => onAccepted?.()}
          onPending={(callId, filename) => {
            setPendingRows((prev) => new Map(prev).set(callId, { filename, submitted_at: new Date().toISOString() }));
            pollForDocRow(callId);
          }}
        />
      </div>

      {/* Pagination controls — above the table so they're always visible */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between mb-2 px-1">
          <span className="text-xs text-gray-500">
            {displayedRows.length} of {rows.length} rows · Page {page + 1} / {totalPages}
          </span>
          <div className="flex gap-2">
            <button
              onClick={() => setPage((p) => Math.max(0, p - 1))}
              disabled={page === 0}
              className="px-3 py-1 text-xs rounded border border-gray-300 disabled:opacity-40 hover:bg-gray-50"
            >
              ← Prev
            </button>
            <button
              onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
              disabled={page >= totalPages - 1}
              className="px-3 py-1 text-xs rounded border border-gray-300 disabled:opacity-40 hover:bg-gray-50"
            >
              Next →
            </button>
          </div>
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
                  Actions
                </th>
                <th className={`${thClass} z-20`} style={{ width: RUN_ID_COL_WIDTH, minWidth: RUN_ID_COL_WIDTH, position: "sticky", left: ROW_NUM_COL_WIDTH + ACTIONS_COL_WIDTH }}>
                  Extraction ID
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
              </tr>
            </thead>
            <tbody>
              {/* ── Pending shimmer rows — shown above data rows while ECS processes new docs ── */}
              {Array.from(pendingRows.entries()).map(([callId, { filename }]) => (
                <tr key={`pending-${callId}`} className="bg-blue-50">
                  <td
                    className={`${cellClass} bg-blue-50 z-[5]`}
                    style={{ width: ROW_NUM_COL_WIDTH, minWidth: ROW_NUM_COL_WIDTH, position: "sticky", left: 0 }}
                  />
                  <td
                    className={`${cellClass} bg-blue-50 z-[5]`}
                    style={{ width: ACTIONS_COL_WIDTH, minWidth: ACTIONS_COL_WIDTH, position: "sticky", left: ROW_NUM_COL_WIDTH }}
                  >
                    <div className="flex items-center gap-1.5 text-xs text-blue-500">
                      <svg className="animate-spin h-3.5 w-3.5 shrink-0" viewBox="0 0 24 24" fill="none">
                        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                        <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
                      </svg>
                      Processing…
                    </div>
                  </td>
                  <td
                    className={`${cellClass} bg-blue-50 z-[5]`}
                    style={{ width: RUN_ID_COL_WIDTH, minWidth: RUN_ID_COL_WIDTH, position: "sticky", left: ROW_NUM_COL_WIDTH + ACTIONS_COL_WIDTH }}
                  >
                    <span className="text-[11px] text-blue-400 font-mono">{callId.slice(-8)}</span>
                  </td>
                  <td
                    className={`${cellClass} bg-blue-50`}
                    colSpan={columns.length}
                  >
                    <span className="text-xs text-blue-600 font-medium">Processing: {filename}</span>
                    <span className="text-[11px] text-blue-400 ml-2">Agent is extracting document metadata — this may take 30–60s</span>
                  </td>
                </tr>
              ))}
              {(() => {
            const seenCallIdsInPage = new Set<string>();
            return pagedRows.map((row, pageIndex) => {
                const i = page * PAGE_SIZE + pageIndex;
                const rowNum = rows.length - (rowIndexMap.get(row) ?? i);
                const run_id = String(row.run_id ?? "");
                const target_id = String(row.target_id ?? "");
                const perDocKey = docRerunKey(row);
                const isRunning = activeTasks.has(perDocKey);
                const libUrl = String(row.library_item_url ?? "");
                const canRerun = !!(run_id && target_id && libUrl && libUrl !== "N/A");
                const callId = String(row.agent_call_id ?? "");
                const evalResult = getEvalForRow(row, evalResults);
                const qaKey = libUrl ? `${callId}::${libUrl}` : callId;
                const isQaRunning = qaRunning.has(qaKey);
                const isFirstInCallGroup = !seenCallIdsInPage.has(callId);
                if (callId) seenCallIdsInPage.add(callId);

                return (
                  <React.Fragment key={i}>
                    <tr className="hover:bg-gray-50">
                      {/* Row number */}
                      <td className={`${cellClass} bg-white z-[5]`} style={{ width: ROW_NUM_COL_WIDTH, minWidth: ROW_NUM_COL_WIDTH, position: "sticky", left: 0, textAlign: "center" }}>
                        <span className="text-xs text-gray-400">{rowNum}</span>
                      </td>
                      {/* Actions cell — Re-evaluate + QA */}
                      <td className={`${cellClass} bg-white z-[5]`} style={{ width: ACTIONS_COL_WIDTH, minWidth: ACTIONS_COL_WIDTH, position: "sticky", left: ROW_NUM_COL_WIDTH }}>
                        <div className="flex flex-col gap-2">
                          {row.last_rerun_at ? (
                            <div className="text-[10px] text-gray-400">
                              <span className="text-gray-300">Re-eval: </span>{formatTimestamp(row.last_rerun_at)}
                            </div>
                          ) : row.run_timestamp ? (
                            <div className="text-[10px] text-gray-400">{formatTimestamp(row.run_timestamp)}</div>
                          ) : null}
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
                          {callId && evalResult && (
                            isQaRunning ? (
                              <div className="flex items-center gap-1 text-[11px] text-gray-400">
                                <svg className="animate-spin h-3 w-3 shrink-0" viewBox="0 0 24 24" fill="none">
                                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
                                </svg>
                                QA Running…
                              </div>
                            ) : (
                              <button
                                onClick={() => startQa(callId, libUrl || undefined)}
                                className="text-[11px] text-violet-600 hover:text-violet-800 hover:underline whitespace-nowrap"
                              >
                                Re-run QA
                              </button>
                            )
                          )}
                        </div>
                      </td>
                      {/* Agent Call ID */}
                      <td className={`${cellClass} bg-white z-[5]`} style={{ width: RUN_ID_COL_WIDTH, minWidth: RUN_ID_COL_WIDTH, position: "sticky", left: ROW_NUM_COL_WIDTH + ACTIONS_COL_WIDTH }}>
                        {callId
                          ? <span className="text-[11px] text-gray-500 font-mono">{callId.slice(-8)}</span>
                          : null
                        }
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
                            <CellValue col={col} value={resolveDocCell(row, col, priorKeys)} />
                          </td>
                        );
                      })}

                    </tr>

                    {/* Violet QA score row — shown below document row */}
                    {evalResult && (
                      <DocQaScoreRow
                        evalResult={evalResult}
                        columns={columns}
                        stickyLefts={stickyLefts}
                        callId={callId}
                        libUrl={libUrl || undefined}
                        onRerun={startQa}
                        isRunning={isQaRunning}
                        schemaLabels={schemaLabels}
                        priorKeys={priorKeys}
                      />
                    )}
                  </React.Fragment>
                );
              });
            })()}
            </tbody>
          </table>
        </div>
      </div>


      {/* Re-evaluate confirmation modal */}
      {modalRow && (() => {
        const rowHash = typeof modalRow.config_hash === "string" ? modalRow.config_hash : null;
        const hashMatch = config && rowHash ? config.hash === rowHash : false;
        return (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
            <div className="bg-white rounded-lg shadow-xl w-full max-w-md mx-4 p-6">
              <h2 className="text-base font-semibold text-gray-900 mb-1">Re-evaluate Document Extraction</h2>
              <p className="text-xs text-gray-500 mb-4">
                Re-runs the document data extraction agent on this document using the current DynamoDB config.
              </p>
              <div className="text-xs text-gray-700 space-y-1 mb-4 bg-gray-50 rounded p-3">
                <div><span className="font-medium">Document:</span> {String(modalRow.library_item_title ?? "—")}</div>
                <div><span className="font-medium">Run ID:</span> {String(modalRow.run_id ?? "—")}</div>
                <div><span className="font-medium">Original run:</span> {formatTimestamp(modalRow.run_timestamp)}</div>
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
                        No config hash stored for this extraction — hash comparison unavailable.
                      </div>
                    ) : hashMatch ? (
                      <div className="bg-yellow-50 border border-yellow-200 rounded p-2 text-yellow-800">
                        ⚠️ Config unchanged since this extraction was generated. Re-running may produce the same result.
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
                  onClick={() => setModalRow(null)}
                  className="px-4 py-1.5 text-sm text-gray-600 hover:text-gray-900 border border-gray-300 rounded"
                >
                  Cancel
                </button>
                <button
                  onClick={() => triggerRerun(modalRow)}
                  disabled={configLoading}
                  className="px-4 py-1.5 text-sm font-medium text-white bg-blue-600 hover:bg-blue-700 rounded disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  Run Re-evaluation
                </button>
              </div>
            </div>
          </div>
        );
      })()}

    </>
  );
}
