"use client";

import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { DocExtractionsTable, type DocExtractionRow } from "./DocExtractionsTable";

interface PageOption {
  id: string;
  name: string;
}

interface ApiResponse {
  rows: DocExtractionRow[];
  total: number;
  error: string | null;
}

const labelClass = "block text-xs text-gray-500 mb-1";
const inputClass =
  "w-full rounded border border-gray-300 px-3 py-1.5 text-sm disabled:opacity-50 disabled:cursor-not-allowed h-[34px]";

function getOrgValue(row: DocExtractionRow): string[] {
  const val = row.organization_or_publisher ?? row.organization;
  if (!val) return [];
  if (Array.isArray(val)) return val.map(String).filter(Boolean);
  const s = String(val).trim();
  return s && s !== "N/A" ? [s] : [];
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
          <p className="text-sm text-gray-500 mb-2">
            {rows.length}{rows.length !== allRows.length ? ` of ${allRows.length}` : ""} row{allRows.length !== 1 ? "s" : ""}
          </p>
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
