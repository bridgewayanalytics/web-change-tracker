"use client";

import { Suspense, useCallback, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { AlertsTable, type AlertRow } from "./AlertsTable";

interface PageOption {
  id: string;
  name: string;
}

interface ApiResponse {
  rows: AlertRow[];
  total: number;
  error: string | null;
}

const labelClass = "block text-xs text-gray-500 mb-1";
const inputClass =
  "w-full rounded border border-gray-300 px-3 py-1.5 text-sm disabled:opacity-50 disabled:cursor-not-allowed h-[34px]";

function AlertsPageContent() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const targetId = searchParams.get("targetId") || "";
  const alertType = searchParams.get("alertType") || "";
  const startDate = searchParams.get("startDate") || "";
  const endDate = searchParams.get("endDate") || "";
  const q = searchParams.get("q") || "";
  const [data, setData] = useState<ApiResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [pages, setPages] = useState<PageOption[]>([]);
  const [alertTypes, setAlertTypes] = useState<string[]>([]);
  const [schemaVersion] = useState(0);
  const [qaFilter, setQaFilter] = useState<"" | "__qa__" | "__qa_imperfect__">("");

  useEffect(() => {
    fetch("/api/pages")
      .then((r) => r.json())
      .then((res: unknown) => {
        if (Array.isArray(res)) setPages(res as PageOption[]);
      })
      .catch(() => setPages([]));
  }, []);

  const updateUrl = useCallback(
    (updates: Record<string, string>) => {
      const params = new URLSearchParams();
      const merged = { targetId, alertType, startDate, endDate, q, ...updates };
      for (const [k, v] of Object.entries(merged)) {
        if (v) params.set(k, v);
      }
      router.replace(`/alerts?${params.toString()}`, { scroll: false });
    },
    [targetId, alertType, startDate, endDate, q, router]
  );

  const fetchData = useCallback(() => {
    setLoading(true);
    const params = new URLSearchParams();
    if (targetId) params.set("targetId", targetId);
    if (alertType) params.set("alertType", alertType);
    if (startDate) params.set("startDate", startDate);
    if (endDate) params.set("endDate", endDate);
    if (q) params.set("q", q);
    fetch(`/api/alerts?${params.toString()}`, { cache: "no-store" })
      .then((r) => r.json())
      .then((res: ApiResponse) => {
        setData(res);
        if (res.rows?.length) {
          const seen = new Set<string>();
          const types: string[] = [];
          res.rows.forEach((r) => { const t = r.alert_type as string; if (t && !seen.has(t)) { seen.add(t); types.push(t); } });
          types.sort();
          setAlertTypes(types);
        }
      })
      .finally(() => setLoading(false));
  }, [targetId, alertType, startDate, endDate, q]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const rows = data?.rows ?? [];

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

          {/* Alert Type */}
          <div className="flex-1 min-w-[150px] max-w-[240px]">
            <label className={labelClass}>Alert Type</label>
            <select
              value={alertType}
              onChange={(e) => updateUrl({ alertType: e.target.value })}
              className={inputClass}
            >
              <option value="">All types</option>
              {alertTypes.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
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
              placeholder="Search title, description, alert ID..."
              className={inputClass}
            />
          </div>
        </div>
      </div>

      {/* Status */}
      {loading && !data && (
        <p className="text-gray-600 mb-4">Loading alerts...</p>
      )}

      {data?.error && (
        <div className="rounded-lg bg-red-50 border border-red-200 p-4 text-red-800 mb-4">
          {data.error}
        </div>
      )}

      {!loading && data && !data.error && rows.length === 0 && (
        <p className="text-gray-600">No alerts found.</p>
      )}

      {rows.length > 0 && (
        <>
          <p className="text-sm text-gray-500 mb-2">
            {rows.length} alert{rows.length !== 1 ? "s" : ""}
          </p>
          <AlertsTable
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

export default function AlertsPage() {
  return (
    <Suspense
      fallback={
        <main className="min-h-screen p-8">
          <p className="text-gray-600">Loading...</p>
        </main>
      }
    >
      <AlertsPageContent />
    </Suspense>
  );
}
