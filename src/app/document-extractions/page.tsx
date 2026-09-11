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
  const [hasQaScoreFilter, setHasQaScoreFilter] = useState(false);
  const [hasImperfectQaFilter, setHasImperfectQaFilter] = useState(false);

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

  const clearFilters = useCallback(() => {
    setFilterOrg("");
    setFilterDocType("");
    setHasQaScoreFilter(false);
    setHasImperfectQaFilter(false);
    router.replace("/document-extractions", { scroll: false });
  }, [router]);

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

  const activeFilterCount = [filterOrg, filterDocType, hasQaScoreFilter, hasImperfectQaFilter].filter(Boolean).length;

  return (
    <main className="min-h-screen px-4 py-6 w-full">
      {/* Filter Bar */}
      <div className="space-y-2 mb-6">
        <div className="flex flex-wrap items-end gap-4">
          {/* Page */}
          <div className="flex-[2] min-w-[180px] max-w-[280px]">
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
          <div className="flex-[2] min-w-[160px] max-w-[260px]">
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
          <div className="flex-[2] min-w-[160px] max-w-[220px]">
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

          {/* Search */}
          <div className="flex-[2] min-w-[160px] max-w-[280px]">
            <label className={labelClass}>Search</label>
            <input
              type="text"
              value={q}
              onChange={(e) => updateUrl({ q: e.target.value })}
              placeholder="Search all fields..."
              className={inputClass}
            />
          </div>

          {/* Apply Filters */}
          <div className="shrink-0">
            <label className={labelClass}>&nbsp;</label>
            <button
              type="button"
              onClick={fetchData}
              disabled={loading}
              className="h-[34px] rounded bg-blue-600 px-4 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Apply Filters
            </button>
          </div>

          {/* Clear Filters */}
          <div className="shrink-0">
            <label className={labelClass}>&nbsp;</label>
            <button
              type="button"
              onClick={clearFilters}
              className="h-[34px] rounded border border-gray-300 px-4 text-sm font-medium text-gray-700 hover:bg-gray-50"
            >
              Clear{activeFilterCount > 0 ? ` (${activeFilterCount})` : ""}
            </button>
          </div>
        </div>

        {/* QA Filter Toggles */}
        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-500 mr-1">QA:</span>
          <button
            type="button"
            onClick={() => {
              const next = !hasQaScoreFilter;
              setHasQaScoreFilter(next);
              if (next) setHasImperfectQaFilter(false);
            }}
            className={`h-[26px] rounded px-3 text-xs font-medium border transition-colors ${
              hasQaScoreFilter
                ? "bg-violet-600 text-white border-violet-600"
                : "bg-white text-gray-600 border-gray-300 hover:bg-violet-50 hover:border-violet-300"
            }`}
          >
            Has QA Score
          </button>
          <button
            type="button"
            onClick={() => {
              const next = !hasImperfectQaFilter;
              setHasImperfectQaFilter(next);
              if (next) setHasQaScoreFilter(false);
            }}
            className={`h-[26px] rounded px-3 text-xs font-medium border transition-colors ${
              hasImperfectQaFilter
                ? "bg-amber-500 text-white border-amber-500"
                : "bg-white text-gray-600 border-gray-300 hover:bg-amber-50 hover:border-amber-300"
            }`}
          >
            Imperfect QA Only
          </button>
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
            hasQaScoreFilter={hasQaScoreFilter}
            hasImperfectQaFilter={hasImperfectQaFilter}
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
