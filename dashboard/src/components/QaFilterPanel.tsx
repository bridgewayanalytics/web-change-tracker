"use client";

import { useEffect, useRef, useState } from "react";

export type ScoreCondition = "Incorrect" | "Partially Correct" | "Correct" | "not_correct";

export interface EvalEntry {
  agent_call_id: string;
  eval_row_key?: string;
  eval_scores: Record<string, { score: string }>;
  eval_timestamp: string;
  run_timestamp?: string;
}

interface Condition {
  id: string;
  field: string;
  score: ScoreCondition;
}

interface StoredState {
  conditions: Condition[];
  logic: "AND" | "OR";
  reEvalCount: number;
  bulkTaskId: string | null;
  bulkRunning: boolean;
  beforeXofY: { x: number; y: number; evalRowKeys: string[] } | null;
}

interface Props {
  mode: "alerts" | "documents";
  onFilteredCallIdsChange: (ids: string[] | null) => void;
  onEvalComplete: () => void;
  onFilterActive: (active: boolean) => void;
}

const SCORE_CONDITION_LABELS: Record<ScoreCondition, string> = {
  "Incorrect": "Incorrect",
  "Partially Correct": "Partially Correct",
  "Correct": "Correct",
  "not_correct": "is not Correct",
};

function lsKey(mode: string) { return `qa_filter_state_${mode}`; }

function readStored(mode: string): Partial<StoredState> {
  if (typeof window === "undefined") return {};
  try {
    const raw = localStorage.getItem(lsKey(mode));
    return raw ? (JSON.parse(raw) as Partial<StoredState>) : {};
  } catch { return {}; }
}

function writeStored(mode: string, state: StoredState) {
  if (typeof window === "undefined") return;
  try { localStorage.setItem(lsKey(mode), JSON.stringify(state)); } catch {}
}

function clearStored(mode: string) {
  if (typeof window === "undefined") return;
  try { localStorage.removeItem(lsKey(mode)); } catch {}
}

function conditionMatches(entry: EvalEntry, condition: Condition): boolean {
  const fieldScore = entry.eval_scores[condition.field]?.score;
  if (!fieldScore) return false;
  if (condition.score === "not_correct") {
    return fieldScore === "Incorrect" || fieldScore === "Partially Correct";
  }
  return fieldScore === condition.score;
}

function entryMatchesConditions(entry: EvalEntry, conditions: Condition[], logic: "AND" | "OR"): boolean {
  if (conditions.length === 0) return false;
  if (!entry.eval_scores || Object.keys(entry.eval_scores).length === 0) return false;
  if (logic === "AND") return conditions.every((c) => conditionMatches(entry, c));
  return conditions.some((c) => conditionMatches(entry, c));
}

let _conditionCounter = 0;
function nextConditionId() { return `cond-${++_conditionCounter}`; }

export function QaFilterPanel({ mode, onFilteredCallIdsChange, onEvalComplete, onFilterActive }: Props) {
  // Initialise all persisted state from localStorage so a refresh restores the session
  const [evalResults, setEvalResults] = useState<Record<string, EvalEntry>>({});
  const [schemaFields, setSchemaFields] = useState<string[]>([]);
  const [conditions, setConditions] = useState<Condition[]>(() => readStored(mode).conditions ?? []);
  const [logic, setLogic] = useState<"AND" | "OR">(() => readStored(mode).logic ?? "OR");
  const [reEvalCount, setReEvalCount] = useState<number>(() => readStored(mode).reEvalCount ?? 10);
  const [bulkRunning, setBulkRunning] = useState<boolean>(() => readStored(mode).bulkRunning ?? false);
  const [bulkTaskId, setBulkTaskId] = useState<string | null>(() => readStored(mode).bulkTaskId ?? null);
  const [refetchVersion, setRefetchVersion] = useState(0);
  const [beforeXofY, setBeforeXofY] = useState<{ x: number; y: number; evalRowKeys: string[] } | null>(() => {
    const stored = readStored(mode).beforeXofY as { x: number; y: number; evalRowKeys?: string[]; callIds?: string[] } | null | undefined;
    if (!stored?.evalRowKeys) return null;
    return { x: stored.x, y: stored.y, evalRowKeys: stored.evalRowKeys };
  });

  const evalEndpoint = mode === "alerts" ? "/api/eval/alerts" : "/api/eval/documents";
  const schemaEndpoint = mode === "alerts" ? "/api/schema" : "/api/doc-schema";

  // Persist relevant state to localStorage whenever it changes
  useEffect(() => {
    writeStored(mode, { conditions, logic, reEvalCount, bulkRunning, bulkTaskId, beforeXofY });
  }, [mode, conditions, logic, reEvalCount, bulkRunning, bulkTaskId, beforeXofY]);

  // Notify parent on mount if filter was active when the page was last left
  const onFilterActiveRef = useRef(onFilterActive);
  onFilterActiveRef.current = onFilterActive;
  useEffect(() => {
    if (conditions.length > 0) onFilterActiveRef.current(true);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Fetch schema fields
  useEffect(() => {
    fetch(schemaEndpoint, { cache: "no-store" })
      .then((r) => r.json())
      .then((d: { columns?: string[] | null }) => {
        if (Array.isArray(d.columns) && d.columns.length > 0) setSchemaFields(d.columns);
      })
      .catch(() => {});
  }, [schemaEndpoint]);

  // Fetch eval results on mount and after re-eval completes
  useEffect(() => {
    fetch(`${evalEndpoint}?_t=${Date.now()}`, { cache: "no-store" })
      .then((r) => r.json())
      .then((d: { results?: Record<string, EvalEntry> }) => {
        if (d.results) setEvalResults(d.results);
      })
      .catch(() => {});
  }, [evalEndpoint, refetchVersion]);

  // Reset before-snapshot when conditions or logic change (skip initial mount — snapshot was restored)
  const isInitialMount = useRef(true);
  useEffect(() => {
    if (isInitialMount.current) { isInitialMount.current = false; return; }
    setBeforeXofY(null);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(conditions), logic]);

  const matchingEntries = conditions.length === 0
    ? []
    : Object.values(evalResults).filter((e) => entryMatchesConditions(e, conditions, logic));
  const matchingCallIds = Array.from(new Set(matchingEntries.map((e) => e.agent_call_id).filter(Boolean)));
  // Y is row-level (each eval entry = one table row)
  const Y = matchingEntries.length;

  const matchingEvalRowKeys = conditions.length > 0
    ? matchingEntries.map((e) => e.eval_row_key ?? e.agent_call_id).filter(Boolean)
    : null;

  // X = how many of the given eval row keys now have ALL condition fields = "Correct"
  function computeEntryX(evalRowKeys: string[]): number {
    return evalRowKeys.filter((key) => {
      const entry = evalResults[key];
      if (!entry) return false;
      return conditions.every((c) => entry.eval_scores[c.field]?.score === "Correct");
    }).length;
  }

  const currentX = conditions.length > 0 ? computeEntryX(matchingEvalRowKeys ?? []) : 0;
  const afterX = beforeXofY ? computeEntryX(beforeXofY.evalRowKeys) : null;

  const matchingKeysStr = matchingEvalRowKeys ? matchingEvalRowKeys.join(",") : "~~none~~";
  const matchingKeysRef = useRef<string[] | null>(null);
  matchingKeysRef.current = matchingEvalRowKeys;
  const onFilteredCallIdsChangeRef = useRef(onFilteredCallIdsChange);
  onFilteredCallIdsChangeRef.current = onFilteredCallIdsChange;
  useEffect(() => {
    onFilteredCallIdsChangeRef.current(matchingKeysRef.current);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [matchingKeysStr]);

  // Poll bulk task until complete — resumes automatically if page was refreshed mid-run
  useEffect(() => {
    if (!bulkTaskId || !bulkRunning) return;
    const taskRoute = mode === "alerts"
      ? `/api/eval/alerts/${bulkTaskId}`
      : `/api/eval/documents/${bulkTaskId}`;
    const interval = setInterval(async () => {
      try {
        const res = await fetch(taskRoute);
        const data = await res.json() as { status: string; error?: string };
        if (data.status === "running") return;
        setBulkRunning(false);
        setBulkTaskId(null);
        if (data.status === "complete" || data.status === "unknown") {
          setRefetchVersion((v) => v + 1);
          onEvalComplete();
        } else {
          alert(`Bulk QA evaluation failed: ${data.error ?? "Unknown error"}`);
        }
      } catch { /* keep polling */ }
    }, 5000);
    return () => clearInterval(interval);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bulkTaskId, bulkRunning, mode]);

  function addCondition() {
    const field = schemaFields[0] ?? "";
    const isFirst = conditions.length === 0;
    setConditions((prev) => [...prev, { id: nextConditionId(), field, score: "not_correct" }]);
    if (isFirst) onFilterActive(true);
  }

  function removeCondition(id: string) {
    const next = conditions.filter((c) => c.id !== id);
    setConditions(next);
    if (next.length === 0) {
      clearStored(mode);
      onFilteredCallIdsChange(null);
      onFilterActive(false);
    }
  }

  function updateConditionField(id: string, field: string) {
    setConditions((prev) => prev.map((c) => c.id === id ? { ...c, field } : c));
  }

  function updateConditionScore(id: string, score: ScoreCondition) {
    setConditions((prev) => prev.map((c) => c.id === id ? { ...c, score } : c));
  }

  function clearAll() {
    clearStored(mode);
    setConditions([]);
    setBeforeXofY(null);
    setBulkRunning(false);
    setBulkTaskId(null);
    onFilteredCallIdsChange(null);
    onFilterActive(false);
  }

  async function runBulkEval() {
    if (Y === 0 || bulkRunning) return;

    const sorted = [...matchingEntries].sort((a, b) =>
      (b.eval_timestamp ?? "").localeCompare(a.eval_timestamp ?? "")
    );
    const topEntries = sorted.slice(0, reEvalCount);
    const uniqueIds = Array.from(new Set(topEntries.map((e) => e.agent_call_id).filter(Boolean)));

    const snapshotEvalRowKeys = [...(matchingEvalRowKeys ?? [])];
    setBeforeXofY({ x: currentX, y: Y, evalRowKeys: snapshotEvalRowKeys });

    // Use bulk-reeval endpoint for alerts mode: re-runs the actual pipeline agent
    // (page_change_agent with current DynamoDB schema) then auto-accepts + runs QA eval.
    // For documents mode, QA-only re-eval is sufficient (pipeline rerun not yet wired).
    const endpoint = mode === "alerts" ? "/api/rerun/bulk-qa" : evalEndpoint;

    try {
      const res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent_call_ids: uniqueIds }),
      });
      const data = await res.json() as { taskId?: string; error?: string };
      if (!res.ok || !data.taskId) throw new Error(data.error ?? "Failed to start bulk eval");
      setBulkTaskId(data.taskId);
      setBulkRunning(true);
    } catch (err) {
      setBeforeXofY(null);
      alert(`Failed to start bulk eval: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  const hasEvalData = Object.keys(evalResults).length > 0;

  // Compact — card with add button when no conditions
  if (conditions.length === 0) {
    return (
      <div className="rounded-lg border border-gray-200 bg-white px-4 py-3 mb-5 flex items-center justify-between">
        <span className="text-sm font-medium text-gray-700">QA Filter</span>
        <button
          onClick={addCondition}
          className="text-sm text-blue-600 hover:text-blue-800 flex items-center gap-1"
          type="button"
        >
          <span className="text-base leading-none">+</span> Add condition
        </button>
      </div>
    );
  }

  const statText = beforeXofY
    ? `${beforeXofY.x}/${beforeXofY.y} → ${afterX ?? currentX}/${beforeXofY.y} correct`
    : `${currentX}/${Y} correct`;

  const fieldOptions = schemaFields.length > 0
    ? schemaFields
    : Array.from(new Set(
        Object.values(evalResults)
          .flatMap((e) => Object.keys(e.eval_scores))
          .filter((f) => f !== "overall_summary")
      )).sort();

  return (
    <div className="rounded-lg border border-gray-200 bg-white px-4 py-3 mb-5">
      {/* Header */}
      <div className="flex items-center gap-3 mb-3">
        <span className="text-sm font-medium text-gray-700">QA Filter</span>
        <button
          onClick={addCondition}
          className="text-sm text-blue-600 hover:text-blue-800 flex items-center gap-1"
          type="button"
        >
          <span className="text-base leading-none">+</span> Add condition
        </button>
        {conditions.length > 1 && (
          <div className="flex rounded overflow-hidden border border-gray-200 text-xs">
            <button
              onClick={() => setLogic("AND")}
              className={`px-2 py-0.5 ${logic === "AND" ? "bg-blue-600 text-white" : "text-gray-500 hover:bg-gray-50"}`}
              type="button"
            >
              AND
            </button>
            <button
              onClick={() => setLogic("OR")}
              className={`px-2 py-0.5 ${logic === "OR" ? "bg-blue-600 text-white" : "text-gray-500 hover:bg-gray-50"}`}
              type="button"
            >
              OR
            </button>
          </div>
        )}
        <button
          onClick={clearAll}
          className="text-xs text-gray-400 hover:text-gray-600 ml-auto"
          type="button"
        >
          Clear
        </button>
      </div>

      {/* Condition rows */}
      <div className="space-y-2 mb-3">
        {conditions.map((cond) => (
          <div key={cond.id} className="flex items-center gap-2">
            <select
              value={cond.field}
              onChange={(e) => updateConditionField(cond.id, e.target.value)}
              className="rounded border border-gray-300 px-2 py-1 text-xs text-gray-700 min-w-0 flex-1 max-w-[280px]"
            >
              {fieldOptions.map((f) => (
                <option key={f} value={f}>{f}</option>
              ))}
              {cond.field && !fieldOptions.includes(cond.field) && (
                <option value={cond.field}>{cond.field}</option>
              )}
            </select>
            <span className="text-xs text-gray-400 shrink-0">is</span>
            <select
              value={cond.score}
              onChange={(e) => updateConditionScore(cond.id, e.target.value as ScoreCondition)}
              className="rounded border border-gray-300 px-2 py-1 text-xs text-gray-700 shrink-0"
            >
              {(Object.keys(SCORE_CONDITION_LABELS) as ScoreCondition[]).map((s) => (
                <option key={s} value={s}>{SCORE_CONDITION_LABELS[s]}</option>
              ))}
            </select>
            <button
              onClick={() => removeCondition(cond.id)}
              className="text-gray-400 hover:text-gray-600 shrink-0 text-base leading-none"
              type="button"
              title="Remove condition"
            >
              ×
            </button>
          </div>
        ))}
      </div>

      {/* Stat + re-evaluate */}
      {hasEvalData && (
        <div className="flex items-center gap-3 flex-wrap border-t border-gray-100 pt-3">
          <span className="text-xs text-gray-500">
            <span className="font-medium text-gray-700">{Y}</span> row{Y !== 1 ? "s" : ""} matched
            {" · "}
            <span className="font-medium text-gray-700">{statText}</span>
          </span>
          <span className="text-gray-200">·</span>
          <span className="text-xs text-gray-500 flex items-center gap-1.5">
            Re-evaluate
            <input
              type="number"
              min={1}
              max={Y || 1}
              value={reEvalCount}
              onChange={(e) => setReEvalCount(Math.max(1, parseInt(e.target.value, 10) || 1))}
              className="w-14 rounded border border-gray-300 px-1.5 py-0.5 text-xs text-center"
            />
            rows
          </span>
          <button
            onClick={runBulkEval}
            disabled={bulkRunning || Y === 0}
            className="flex items-center gap-1.5 text-xs font-medium text-white bg-violet-600 hover:bg-violet-700 disabled:opacity-50 disabled:cursor-not-allowed rounded px-2.5 py-1"
            type="button"
          >
            {bulkRunning ? (
              <>
                <svg className="animate-spin h-3 w-3" viewBox="0 0 24 24" fill="none">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
                </svg>
                Running…
              </>
            ) : (
              <>Run ▶</>
            )}
          </button>
        </div>
      )}
    </div>
  );
}
