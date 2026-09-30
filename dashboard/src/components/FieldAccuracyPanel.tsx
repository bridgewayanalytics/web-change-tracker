"use client";

import { useEffect, useState } from "react";

interface FieldStat {
  field: string;
  correct: number;
  partially_correct: number;
  incorrect: number;
  total: number;
  accuracy_pct: number;
}

interface ScoreReport {
  generated_at: string;
  triggered_by_run: string;
  total_rows: number;
  overall: {
    correct: number;
    partially_correct: number;
    incorrect: number;
    total_scores: number;
    accuracy_pct: number;
  };
  by_field: FieldStat[];
}

interface Props {
  endpoint: string;
  refreshTrigger?: number;
}

function accuracyColor(pct: number): string {
  if (pct >= 80) return "text-green-700";
  if (pct >= 60) return "text-yellow-700";
  return "text-red-600";
}

function AccuracyBar({ pct }: { pct: number }) {
  const color = pct >= 80 ? "bg-green-500" : pct >= 60 ? "bg-yellow-400" : "bg-red-400";
  return (
    <div className="w-full bg-gray-100 rounded-full h-1.5 overflow-hidden">
      <div className={`h-1.5 rounded-full ${color}`} style={{ width: `${pct}%` }} />
    </div>
  );
}

export function FieldAccuracyPanel({ endpoint, refreshTrigger = 0 }: Props) {
  const [current, setCurrent] = useState<ScoreReport | null>(null);
  const [prev, setPrev] = useState<ScoreReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    setLoading(true);
    fetch(`${endpoint}?_t=${Date.now()}`, { cache: "no-store" })
      .then((r) => {
        if (!r.ok) throw new Error("no report");
        return r.json() as Promise<{ current: ScoreReport; prev: ScoreReport | null }>;
      })
      .then((d) => {
        setCurrent(d.current ?? null);
        setPrev(d.prev ?? null);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [endpoint, refreshTrigger]);

  if (loading) return null;

  if (!current) {
    return (
      <div className="mb-5 rounded-lg border border-gray-200 bg-white px-4 py-3">
        <div className="flex items-center gap-3">
          <span className="text-sm font-medium text-gray-700">Field Accuracy</span>
          <span className="text-xs text-gray-400">No evaluation data yet</span>
        </div>
      </div>
    );
  }

  // Build prev field lookup for delta display
  const prevByField: Record<string, number> = {};
  if (prev) {
    for (const f of prev.by_field) prevByField[f.field] = f.accuracy_pct;
  }

  const overall = current.overall;
  const generatedDate = new Date(current.generated_at).toLocaleDateString("en-US", {
    month: "short", day: "numeric", year: "numeric",
  });

  return (
    <div className="mb-5 rounded-lg border border-gray-200 bg-white overflow-hidden">
      <button
        className="w-full flex items-center justify-between px-4 py-3 text-left hover:bg-gray-50 transition-colors"
        onClick={() => setExpanded((v) => !v)}
        type="button"
      >
        <div className="flex items-center gap-3">
          <span className="text-sm font-medium text-gray-700">Field Accuracy</span>
          <span className={`text-sm font-semibold ${accuracyColor(overall.accuracy_pct)}`}>
            {overall.accuracy_pct}%
          </span>
          <span className="text-xs text-gray-400">
            {current.total_rows} row{current.total_rows !== 1 ? "s" : ""} evaluated
          </span>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-400">{generatedDate}</span>
          <svg
            className={`w-4 h-4 text-gray-400 transition-transform ${expanded ? "rotate-180" : ""}`}
            fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}
          >
            <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
          </svg>
        </div>
      </button>

      {expanded && (
        <div className="border-t border-gray-100 px-4 py-3">
          <div className="flex items-center gap-6 mb-4 pb-3 border-b border-gray-100">
            <div className="text-center">
              <div className={`text-2xl font-bold ${accuracyColor(overall.accuracy_pct)}`}>
                {overall.accuracy_pct}%
              </div>
              <div className="text-xs text-gray-500 mt-0.5">Overall</div>
            </div>
            <div className="flex gap-4 text-sm">
              <div className="text-center">
                <div className="font-semibold text-green-700">{overall.correct}</div>
                <div className="text-xs text-gray-500">Correct</div>
              </div>
              <div className="text-center">
                <div className="font-semibold text-yellow-700">{overall.partially_correct}</div>
                <div className="text-xs text-gray-500">Partial</div>
              </div>
              <div className="text-center">
                <div className="font-semibold text-red-600">{overall.incorrect}</div>
                <div className="text-xs text-gray-500">Incorrect</div>
              </div>
              <div className="text-center">
                <div className="font-semibold text-gray-600">{overall.total_scores}</div>
                <div className="text-xs text-gray-500">Total scores</div>
              </div>
            </div>
          </div>

          {current.by_field.length === 0 ? (
            <p className="text-sm text-gray-500">No field data yet.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-gray-500 border-b border-gray-100">
                    <th className="text-left pb-1.5 pr-4 font-medium w-1/3">Field</th>
                    <th className="text-right pb-1.5 px-2 font-medium">Correct</th>
                    <th className="text-right pb-1.5 px-2 font-medium">Partial</th>
                    <th className="text-right pb-1.5 px-2 font-medium">Incorrect</th>
                    <th className="text-right pb-1.5 px-2 font-medium">Total</th>
                    <th className="text-right pb-1.5 pl-2 font-medium w-32">Accuracy</th>
                  </tr>
                </thead>
                <tbody>
                  {current.by_field.map((f) => {
                    const prevPct = prevByField[f.field] ?? null;
                    const delta = prevPct !== null ? f.accuracy_pct - prevPct : null;
                    const showDelta = delta !== null && Math.abs(delta) > 1;
                    return (
                      <tr key={f.field} className="border-b border-gray-50 last:border-0">
                        <td className="py-1.5 pr-4 font-mono text-gray-700 truncate max-w-[200px]" title={f.field}>
                          {f.field}
                        </td>
                        <td className="py-1.5 px-2 text-right text-green-700">{f.correct}</td>
                        <td className="py-1.5 px-2 text-right text-yellow-700">{f.partially_correct}</td>
                        <td className="py-1.5 px-2 text-right text-red-600">{f.incorrect}</td>
                        <td className="py-1.5 px-2 text-right text-gray-500">{f.total}</td>
                        <td className="py-1.5 pl-2">
                          <div className="flex items-center gap-1.5 justify-end">
                            <AccuracyBar pct={f.accuracy_pct} />
                            <span className={`font-semibold tabular-nums w-10 text-right shrink-0 ${accuracyColor(f.accuracy_pct)}`}>
                              {f.accuracy_pct}%
                            </span>
                            {showDelta ? (
                              <span className={`text-[10px] font-semibold tabular-nums w-9 text-right shrink-0 ${delta > 0 ? "text-green-600" : "text-red-500"}`}>
                                {delta > 0 ? "+" : ""}{delta.toFixed(1)}%
                              </span>
                            ) : (
                              <span className="w-9 shrink-0" />
                            )}
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
