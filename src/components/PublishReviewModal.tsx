"use client";

import React, { useState, useEffect } from "react";

interface ChunkData {
  agenda_item_title?: string;
  text?: string;
  [key: string]: unknown;
}

interface PublishReviewModalProps {
  row: Record<string, unknown>;
  type: "transcript" | "document";
  onConfirm: () => Promise<void>;
  onReject?: () => Promise<void>;
  onClose: () => void;
}

function ExpandSection({
  title,
  defaultOpen = false,
  badge,
  children,
}: {
  title: string;
  defaultOpen?: boolean;
  badge?: string;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="border border-gray-200 rounded overflow-hidden">
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center justify-between px-3 py-2 text-xs font-medium text-gray-700 hover:bg-gray-50 bg-white"
      >
        <span className="flex items-center gap-2">
          {title}
          {badge && (
            <span className="text-[10px] font-normal text-gray-400 bg-gray-100 rounded-full px-1.5 py-0.5">
              {badge}
            </span>
          )}
        </span>
        <svg
          className={`h-3.5 w-3.5 text-gray-400 transition-transform ${open ? "rotate-180" : ""}`}
          viewBox="0 0 20 20"
          fill="currentColor"
        >
          <path fillRule="evenodd" d="M5.293 7.293a1 1 0 011.414 0L10 10.586l3.293-3.293a1 1 0 111.414 1.414l-4 4a1 1 0 01-1.414 0l-4-4a1 1 0 010-1.414z" clipRule="evenodd" />
        </svg>
      </button>
      {open && <div className="px-3 pb-3 pt-2 border-t border-gray-100 bg-white">{children}</div>}
    </div>
  );
}

function KV({ label, value, mono = false, link = false }: { label: string; value: string; mono?: boolean; link?: boolean }) {
  if (!value || value === "—") return null;
  return (
    <div className="flex gap-2 text-xs">
      <span className="text-gray-400 w-24 shrink-0">{label}</span>
      {link && (value.startsWith("http") || value.startsWith("s3")) ? (
        <a
          href={value.startsWith("http") ? value : `/api/presigned-url?key=${encodeURIComponent(value)}`}
          target="_blank"
          rel="noopener noreferrer"
          className="text-blue-600 hover:underline break-all"
        >
          {value.length > 60 ? value.slice(0, 57) + "…" : value}
        </a>
      ) : (
        <span className={`text-gray-800 break-all ${mono ? "font-mono text-[11px]" : ""}`}>{value}</span>
      )}
    </div>
  );
}

export function PublishReviewModal({ row, type, onConfirm, onReject, onClose }: PublishReviewModalProps) {
  const [chunks, setChunks] = useState<ChunkData[] | null>(null);
  const [chunksLoading, setChunksLoading] = useState(false);
  const [chunksError, setChunksError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const chunksKey = type === "transcript" ? (row.transcript_chunks_s3_key as string | undefined) : undefined;
  const hasChunks = !!chunksKey;

  useEffect(() => {
    if (!chunksKey) return;
    setChunksLoading(true);
    fetch(`/api/chunks-data?key=${encodeURIComponent(chunksKey)}`)
      .then((r) => r.json())
      .then((d: { chunks?: ChunkData[]; error?: string }) => {
        if (d.error) setChunksError(d.error);
        else setChunks(d.chunks ?? []);
      })
      .catch((e: unknown) => setChunksError(String(e)))
      .finally(() => setChunksLoading(false));
  }, [chunksKey]);

  async function handleConfirm() {
    setConfirming(true);
    setError(null);
    try {
      await onConfirm();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed");
      setConfirming(false);
    }
  }

  async function handleReject() {
    if (!onReject) return;
    setRejecting(true);
    setError(null);
    try {
      await onReject();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Reject failed");
      setRejecting(false);
    }
  }

  const displayTitle =
    type === "transcript"
      ? ((row.event_title as string | undefined) ?? (row.alert_title as string | undefined) ?? "Alert")
      : ((row.library_item_title as string | undefined) ?? (row.library_item_file_name as string | undefined) ?? "Document");

  const org = row.organization;
  const orgStr = Array.isArray(org) ? (org as string[]).join(", ") : typeof org === "string" ? org : "";

  const agendaGroups = chunks
    ? (() => {
        const map = new Map<string, ChunkData[]>();
        for (const c of chunks) {
          const k = c.agenda_item_title ?? "(no title)";
          if (!map.has(k)) map.set(k, []);
          map.get(k)!.push(c);
        }
        return Array.from(map.entries());
      })()
    : [];

  // File/URL for doc type
  const fileUrl = String(row.library_item_url ?? row.manual_doc_s3_key ?? "");
  const fileName = String(row.library_item_file_name ?? row.library_item_title ?? "");

  const canConfirm = type === "document" || hasChunks;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" onClick={onClose}>
      <div
        className="bg-white rounded-lg shadow-xl w-full max-w-lg mx-4 flex flex-col max-h-[85vh]"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-start justify-between px-5 pt-5 pb-4 border-b border-gray-100">
          <div>
            <h2 className="text-base font-semibold text-gray-900">Publish to Knowledge Base</h2>
            <p className="text-xs text-gray-500 mt-1 line-clamp-2">{displayTitle}</p>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 ml-4 mt-0.5 shrink-0">
            <svg className="h-4 w-4" viewBox="0 0 20 20" fill="currentColor">
              <path fillRule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clipRule="evenodd" />
            </svg>
          </button>
        </div>

        {/* No-chunks warning (transcript only) */}
        {type === "transcript" && !hasChunks && (
          <div className="mx-5 mt-4 bg-red-50 border border-red-200 rounded p-3 flex gap-2">
            <svg className="h-4 w-4 text-red-500 shrink-0 mt-0.5" viewBox="0 0 20 20" fill="currentColor">
              <path fillRule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clipRule="evenodd" />
            </svg>
            <div>
              <p className="text-xs font-semibold text-red-700">No transcript chunks file attached</p>
              <p className="text-xs text-red-600 mt-0.5">Upload a transcript first — the pipeline will chunk it automatically before you can publish.</p>
            </div>
          </div>
        )}

        {/* Scrollable body */}
        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-2">
          {/* Content section */}
          {type === "transcript" ? (
            hasChunks && (
              <ExpandSection
                title="Transcript Chunks"
                badge={chunks ? `${chunks.length} chunks · ${agendaGroups.length} sections` : chunksLoading ? "loading…" : undefined}
                defaultOpen
              >
                {chunksLoading ? (
                  <div className="flex items-center gap-2 text-xs text-gray-500 py-2">
                    <svg className="animate-spin h-3.5 w-3.5" viewBox="0 0 24 24" fill="none">
                      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
                    </svg>
                    Loading chunks…
                  </div>
                ) : chunksError ? (
                  <p className="text-xs text-red-500 py-1">{chunksError}</p>
                ) : agendaGroups.length === 0 ? (
                  <p className="text-xs text-gray-400 py-1">No chunks found.</p>
                ) : (
                  <div className="space-y-2 mt-1">
                    {agendaGroups.map(([agendaTitle, agendaChunks]) => (
                      <div key={agendaTitle} className="bg-gray-50 border border-gray-100 rounded p-2.5">
                        <p className="text-[11px] font-semibold text-gray-700 mb-1">{agendaTitle}</p>
                        <p className="text-[11px] text-gray-500 leading-relaxed line-clamp-3">
                          {agendaChunks[0]?.text?.slice(0, 220)}
                          {(agendaChunks[0]?.text?.length ?? 0) > 220 ? "…" : ""}
                        </p>
                        {agendaChunks.length > 1 && (
                          <p className="text-[10px] text-gray-400 mt-1">+{agendaChunks.length - 1} more chunk(s)</p>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </ExpandSection>
            )
          ) : (
            <ExpandSection title="Document" defaultOpen>
              <div className="space-y-1.5 mt-1">
                {fileName && <KV label="Title" value={fileName} />}
                {fileUrl && <KV label="File / URL" value={fileUrl} link />}
              </div>
            </ExpandSection>
          )}

          {/* Metadata */}
          <ExpandSection title="Metadata">
            <div className="space-y-1.5 mt-1">
              {orgStr && <KV label="Organization" value={orgStr} />}
              <KV label="Date" value={String((row.alert_date_time as string | undefined) ?? (row.run_timestamp as string | undefined) ?? "")} />
              <KV label="Run ID" value={String(row.run_id ?? "")} mono />
              <KV label="Call ID" value={String(row.agent_call_id ?? "").slice(-8)} mono />
            </div>
          </ExpandSection>

          {/* Destination */}
          <ExpandSection title="Destination">
            <div className="space-y-1.5 mt-1">
              <KV label="Namespace" value="newsreel-generation:ART" mono />
              <KV label="Knowledge base" value="ART Newsreel Generation" />
              <KV
                label="Content"
                value={type === "transcript" ? `${chunks?.length ?? "?"} transcript chunk(s)` : (fileName || fileUrl)}
              />
            </div>
          </ExpandSection>
        </div>

        {/* Footer */}
        <div className="px-5 py-4 border-t border-gray-100">
          {error && <p className="text-xs text-red-600 mb-3 bg-red-50 rounded p-2">{error}</p>}
          <div className="flex items-center justify-between">
            {/* Reject on the left */}
            {onReject ? (
              <button
                onClick={handleReject}
                disabled={rejecting || confirming}
                className="text-xs text-gray-400 hover:text-red-600 hover:underline disabled:opacity-50"
              >
                {rejecting ? "Rejecting…" : "Reject"}
              </button>
            ) : <span />}

            {/* Cancel + Confirm on the right */}
            <div className="flex gap-3">
              <button
                onClick={onClose}
                disabled={confirming || rejecting}
                className="px-4 py-1.5 text-sm text-gray-600 hover:text-gray-900 border border-gray-300 rounded disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                onClick={handleConfirm}
                disabled={confirming || rejecting || !canConfirm}
                className="px-4 py-1.5 text-sm font-medium text-white bg-blue-600 hover:bg-blue-700 rounded disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
                title={!canConfirm ? "Upload and chunk a transcript first" : undefined}
              >
                {confirming && (
                  <svg className="animate-spin h-3.5 w-3.5" viewBox="0 0 24 24" fill="none">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
                  </svg>
                )}
                {confirming ? "Publishing…" : "Confirm Publish"}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
