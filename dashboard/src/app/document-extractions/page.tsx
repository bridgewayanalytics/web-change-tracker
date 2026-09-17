"use client";

import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { DocExtractionsTable, type DocExtractionRow } from "./DocExtractionsTable";
import type { DocScoreReport, FieldStat } from "../api/eval/documents/report/route";

interface PageOption {
  id: string;
  name: string;
}

function scoreColor(pct: number): string {
  if (pct >= 90) return "text-green-700";
  if (pct >= 70) return "text-amber-600";
  return "text-red-600";
}

function scoreBg(pct: number): string {
  if (pct >= 90) return "bg-green-500";
  if (pct >= 70) return "bg-amber-400";
  return "bg-red-500";
}

function relativeTime(iso: string): string {
  const diffMs = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diffMs / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

function FieldBar({ stat }: { stat: FieldStat }) {
  const pct = stat.accuracy_pct;
  return (
    <tr className="border-t border-gray-100">
      <td className="py-1 pr-4 text-xs text-gray-700 font-mono whitespace-nowrap">{stat.field}</td>
      <td className="py-1 pr-4 w-48">
        <div className="flex items-center gap-2">
          <div className="flex-1 h-1.5 bg-gray-100 rounded-full overflow-hidden">
            <div
              className={`h-full rounded-full ${scoreBg(pct)}`}
              style={{ width: `${pct}%` }}
            />
          </div>
          <span className={`text-xs font-semibold tabular-nums ${scoreColor(pct)}`}>
            {pct.toFixed(1)}%
          </span>
        </div>
      </td>
      <td className="py-1 text-xs text-gray-500 tabular-nums whitespace-nowrap">
        {stat.correct}✓ {stat.partially_correct > 0 ? `${stat.partially_correct}~ ` : ""}{stat.incorrect}✗ / {stat.total}
      </td>
    </tr>
  );
}

function ScoreBanner({ report }: { report: DocScoreReport }) {
  const [expanded, setExpanded] = useState(false);
  const { overall, by_field, generated_at, total_rows } = report;
  const pct = overall.accuracy_pct;

  return (
    <div className="mb-4 rounded-lg border border-gray-200 bg-white shadow-sm overflow-hidden">
      {/* Always-visible summary row */}
      <div className="flex items-center gap-4 px-4 py-3">
        <span className={`text-2xl font-bold tabular-nums ${scoreColor(pct)}`}>
          {pct.toFixed(1)}%
        </span>
        <div className="flex flex-col">
          <span className="text-sm font-medium text-gray-800">Doc Extraction QA Accuracy</span>
          <span className="text-xs text-gray-500">
            {overall.correct.toLocaleString()} correct / {overall.total_scores.toLocaleString()} scores · {total_rows.toLocaleString()} rows · {by_field.length} fields
            {overall.partially_correct > 0 && (
              <> · <span className="text-amber-600">{overall.partially_correct.toLocaleString()} partial</span></>
            )}
          </span>
        </div>
        <div className="ml-auto flex items-center gap-3">
          <span className="text-xs text-gray-400">updated {relativeTime(generated_at)}</span>
          <button
            onClick={() => setExpanded((e) => !e)}
            className="text-xs text-indigo-600 hover:text-indigo-800 font-medium"
          >
            {expanded ? "Hide by field ▲" : "By field ▼"}
          </button>
        </div>
      </div>

      {/* Collapsible by-field table */}
      {expanded && (
        <div className="border-t border-gray-100 px-4 py-3 overflow-x-auto">
          <table className="text-sm w-full">
            <thead>
              <tr className="text-xs text-gray-400 uppercase tracking-wide">
                <th className="text-left pb-1 pr-4 font-medium">Field</th>
                <th className="text-left pb-1 pr-4 font-medium w-48">Accuracy</th>
                <th className="text-left pb-1 font-medium">Breakdown</th>
              </tr>
            </thead>
            <tbody>
              {by_field.map((stat) => (
                <FieldBar key={stat.field} stat={stat} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

interface ApiResponse {
  rows: DocExtractionRow[];
  total: number;
  error: string | null;
}

const labelClass = "block text-xs text-gray-500 mb-1";
const inputClass =
  "w-full rounded border border-gray-300 px-3 py-1.5 text-sm disabled:opacity-50 disabled:cursor-not-allowed h-[34px]";

/** Extract org name string(s) from a row — handles all field naming variations and value shapes. */
function extractOrgName(val: unknown): string {
  if (!val) return "";
  if (typeof val === "string") return val.trim();
  if (typeof val === "object") {
    const obj = val as Record<string, unknown>;
    // {name, status} shape used by organization_author
    const s = String(obj.name ?? obj.title ?? obj.value ?? "").trim();
    return s;
  }
  return String(val).trim();
}

function getOrgValue(row: DocExtractionRow): string[] {
  // organization_author is the current field name; organization_or_publisher is legacy
  const val = row.organization_author ?? row.organization_or_publisher ?? row.organization;
  if (!val) return [];
  const items = Array.isArray(val) ? val : [val];
  return items.map(extractOrgName).filter((s) => s && s !== "N/A");
}

function PageContent() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const targetId = searchParams.get("targetId") ?? "";
  const startDate = searchParams.get("startDate") ?? "";
  const endDate = searchParams.get("endDate") ?? "";
  const q = searchParams.get("q") ?? "";

  const [data, setData] = useState<ApiResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [pages, setPages] = useState<PageOption[]>([]);
  const [schemaVersion] = useState(0);
  const [scoreReport, setScoreReport] = useState<DocScoreReport | null>(null);

  // Client-side filters
  const [filterOrg, setFilterOrg] = useState("");
  const [filterDocType, setFilterDocType] = useState("");
  const [qaFilter, setQaFilter] = useState<"" | "__qa__" | "__qa_imperfect__">("");

  useEffect(() => {
    fetch("/api/pages")
      .then((r) => r.json())
      .then((res: unknown) => {
        if (Array.isArray(res)) setPages(res as PageOption[]);
      })
      .catch(() => setPages([]));
  }, []);

  useEffect(() => {
    fetch("/api/eval/documents/report", { cache: "no-store" })
      .then((r) => r.json())
      .then((res: { report: DocScoreReport | null }) => {
        if (res.report) setScoreReport(res.report);
      })
      .catch(() => {});
  }, []);

  const updateUrl = useCallback(
    (updates: Record<string, string>) => {
      const params = new URLSearchParams();
      const merged = { targetId, startDate, endDate, q, ...updates };
      for (const [k, v] of Object.entries(merged)) {
        if (v) params.set(k, v);
      }
      router.replace(`/document-extractions?${params.toString()}`, { scroll: false });
    },
    [targetId, startDate, endDate, q, router]
  );

  const fetchData = useCallback(() => {
    setLoading(true);
    const params = new URLSearchParams();
    if (targetId) params.set("targetId", targetId);
    if (startDate) params.set("startDate", startDate);
    if (endDate) params.set("endDate", endDate);
    if (q) params.set("q", q);
    fetch(`/api/document-extractions?${params.toString()}`, { cache: "no-store" })
      .then((r) => r.json())
      .then((res: ApiResponse) => setData(res))
      .finally(() => setLoading(false));
  }, [targetId, startDate, endDate, q]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const allRows = data?.rows ?? [];

  // Derive unique org/doctype options from all API rows (before client-side filter)
  const orgOptions = useMemo(() => {
    const set = new Set<string>();
    for (const row of allRows) {
      for (const o of getOrgValue(row)) set.add(o);
    }
    return Array.from(set).sort();
  }, [allRows]);

  const docTypeOptions = useMemo(() => {
    const set = new Set<string>();
    for (const row of allRows) {
      const dt = String(row.document_type ?? "").trim();
      if (dt && dt !== "N/A") set.add(dt);
    }
    return Array.from(set).sort();
  }, [allRows]);

  // Apply client-side org/doctype filters
  const rows = useMemo(() => {
    let r = allRows;
    if (filterOrg) {
      r = r.filter((row) => getOrgValue(row).some((o) => o.toLowerCase().includes(filterOrg.toLowerCase())));
    }
    if (filterDocType) {
      r = r.filter((row) => String(row.document_type ?? "") === filterDocType);
    }
    return r;
  }, [allRows, filterOrg, filterDocType]);

  return (
    <main className="min-h-screen px-4 py-6 w-full">
      {/* Filter Bar */}
      <div className="mb-6">
        <div className="flex flex-wrap items-end gap-4">
          {/* Page */}
          <div className="flex-1 min-w-[160px] max-w-[280px]">
            <label className={labelClass}>Page</label>
            <select
              value={targetId}
              onChange={(e) => updateUrl({ targetId: e.target.value })}
              className={inputClass}
            >
              <option value="">All pages</option>
              {pages.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </div>

          {/* Organization */}
          <div className="flex-1 min-w-[150px] max-w-[240px]">
            <label className={labelClass}>Organization</label>
            <select
              value={filterOrg}
              onChange={(e) => setFilterOrg(e.target.value)}
              className={inputClass}
            >
              <option value="">All organizations</option>
              {orgOptions.map((o) => (
                <option key={o} value={o}>{o}</option>
              ))}
            </select>
          </div>

          {/* Document Type */}
          <div className="flex-1 min-w-[150px] max-w-[220px]">
            <label className={labelClass}>Document Type</label>
            <select
              value={filterDocType}
              onChange={(e) => setFilterDocType(e.target.value)}
              className={inputClass}
            >
              <option value="">All types</option>
              {docTypeOptions.map((dt) => (
                <option key={dt} value={dt}>{dt}</option>
              ))}
            </select>
          </div>

          {/* Start Date */}
          <div className="w-[140px] shrink-0">
            <label className={labelClass}>From</label>
            <input
              type="date"
              value={startDate}
              onChange={(e) => updateUrl({ startDate: e.target.value })}
              className={inputClass}
            />
          </div>

          {/* End Date */}
          <div className="w-[140px] shrink-0">
            <label className={labelClass}>To</label>
            <input
              type="date"
              value={endDate}
              onChange={(e) => updateUrl({ endDate: e.target.value })}
              className={inputClass}
            />
          </div>

          {/* QA Filter */}
          <div className="flex-1 min-w-[150px] max-w-[220px]">
            <label className={labelClass}>QA</label>
            <select
              value={qaFilter}
              onChange={(e) => setQaFilter(e.target.value as "" | "__qa__" | "__qa_imperfect__")}
              className={inputClass}
            >
              <option value="">All rows</option>
              <option value="__qa__">Has QA score</option>
              <option value="__qa_imperfect__">Imperfect QA only</option>
            </select>
          </div>

          {/* Search */}
          <div className="flex-[2] min-w-[180px] max-w-[320px]">
            <label className={labelClass}>Search</label>
            <input
              type="text"
              value={q}
              onChange={(e) => updateUrl({ q: e.target.value })}
              placeholder="Search all fields..."
              className={inputClass}
            />
          </div>
        </div>
      </div>

      {/* Score Report Banner */}
      {scoreReport && <ScoreBanner report={scoreReport} />}

      {/* Status */}
      {loading && !data && (
        <p className="text-gray-600 mb-4">Loading...</p>
      )}

      {data?.error && (
        <div className="rounded-lg bg-red-50 border border-red-200 p-4 text-red-800 mb-4">
          {data.error}
        </div>
      )}

      {!loading && data && !data.error && allRows.length === 0 && (
        <p className="text-gray-600">No document extractions found.</p>
      )}

      {allRows.length > 0 && (
        <>
          <DocExtractionsTable
            rows={rows}
            onAccepted={fetchData}
            schemaVersion={schemaVersion}
            hasQaScoreFilter={qaFilter === "__qa__"}
            hasImperfectQaFilter={qaFilter === "__qa_imperfect__"}
          />
        </>
      )}
    </main>
  );
}

export default function DocumentExtractionsPage() {
  return (
    <Suspense
      fallback={
        <main className="min-h-screen p-8">
          <p className="text-gray-600">Loading...</p>
        </main>
      }
    >
      <PageContent />
    </Suspense>
  );
}
