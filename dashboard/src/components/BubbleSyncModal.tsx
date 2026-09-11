"use client";

import { useEffect, useState } from "react";

// ── Types ──────────────────────────────────────────────────────────────────

interface EventPreview {
  title?: string;
  start_datetime?: string;
  end_datetime?: string;
  group?: string[];
  url?: string;
  call_in?: string;
  match_key?: string;
  match_search?: Record<string, string>;
  what_changes?: string[];
  fields?: Record<string, string>;
  field_ids?: Record<string, unknown>;
}

interface LibraryItemPreview {
  title?: string;
  url?: string;
  filename?: string;
  type?: string;
  group?: string[];
  match_search?: Record<string, string>;
  what_changes?: string[];
  fields?: Record<string, string>;
  field_ids?: Record<string, unknown>;
}

interface AgendaItemPreview {
  title: string;
  chronicle_topics?: string[];
  official_title?: string;
  reference_id?: string;
}

interface BubbleAction {
  event?: "create" | "update" | null;
  library_item?: "create" | "update" | null;
  agenda_items?: boolean;
  event_preview?: EventPreview;
  library_item_preview?: LibraryItemPreview;
  agenda_item_previews?: AgendaItemPreview[];
  notes?: string;
}

type RecordStatus = "idle" | "loading" | "found" | "not_found" | "error";

interface RecordState {
  status: RecordStatus;
  fields: Record<string, string>;
  id?: string;
}

interface Props {
  agentCallId: string;
  bubbleAction: BubbleAction;
  onClose: () => void;
  onSynced: () => void;
}

// ── Sub-components ─────────────────────────────────────────────────────────

function Badge({ action }: { action: "create" | "update" }) {
  return (
    <span className={`text-[10px] font-bold px-2 py-0.5 rounded border ${
      action === "create"
        ? "bg-green-100 text-green-700 border-green-300"
        : "bg-blue-100 text-blue-700 border-blue-300"
    }`}>
      {action.toUpperCase()}
    </span>
  );
}

function FieldRow({ label, value }: { label: string; value: string }) {
  const trimmed = value?.trim();
  if (!trimmed || trimmed === "N/A" || trimmed === "N/A." || trimmed === "-") return null;
  return (
    <div className="flex gap-3 text-xs py-1.5 border-b border-black/5 last:border-0">
      <span className="text-gray-400 w-32 flex-shrink-0">{label}</span>
      <span className="text-gray-800 break-all leading-relaxed">{trimmed}</span>
    </div>
  );
}

function TopicChips({ topics }: { topics: string[] }) {
  if (!topics.length) return null;
  return (
    <div className="flex flex-wrap gap-1">
      {topics.map(t => (
        <span key={t} className="text-[10px] bg-violet-100 text-violet-700 rounded px-1.5 py-0.5 border border-violet-200 leading-tight">
          {t}
        </span>
      ))}
    </div>
  );
}

/** Renders fields dict with Topics as chips. */
function FieldGrid({ fields }: { fields: Record<string, string> }) {
  const entries = Object.entries(fields).filter(([, v]) => {
    const t = v?.trim();
    return t && t !== "N/A" && t !== "N/A." && t !== "-";
  });
  if (!entries.length) return null;
  return (
    <div>
      {entries.map(([label, value]) => {
        if (label === "Topics") {
          return (
            <div key={label} className="flex gap-3 text-xs py-1.5 border-b border-black/5 last:border-0">
              <span className="text-gray-400 w-32 flex-shrink-0">{label}</span>
              <TopicChips topics={value.split(", ").filter(Boolean)} />
            </div>
          );
        }
        return <FieldRow key={label} label={label} value={value} />;
      })}
    </div>
  );
}

/** Legacy fallback fields for old rows lacking the `fields` dict. */
function legacyEventFields(ep: EventPreview): Record<string, string> {
  const out: Record<string, string> = {};
  if (ep.title) out["Title"] = ep.title;
  if (ep.start_datetime) out["Start"] = ep.start_datetime;
  if (ep.end_datetime && ep.end_datetime !== ep.start_datetime) out["End"] = ep.end_datetime;
  if (ep.group?.length) out["Groups"] = ep.group.join(", ");
  if (ep.url) out["Location"] = ep.url;
  if (ep.call_in) out["Call-in"] = ep.call_in;
  return out;
}

function legacyLibFields(lp: LibraryItemPreview): Record<string, string> {
  const out: Record<string, string> = {};
  if (lp.title) out["Name"] = lp.title;
  if (lp.type) out["Type"] = lp.type;
  if (lp.url) out["URL"] = lp.url;
  if (lp.filename) out["File"] = lp.filename;
  if (lp.group?.length) out["Organizations"] = lp.group.join(", ");
  return out;
}

function hasFields(fields?: Record<string, string>): boolean {
  return !!fields && Object.keys(fields).length > 0;
}

function MatchCriteria({ matchSearch, matchKey }: { matchSearch?: Record<string, string>; matchKey?: string }) {
  const parts = matchSearch
    ? Object.entries(matchSearch).filter(([, v]) => v).map(([k, v]) => `${k}: ${v}`)
    : matchKey
    ? [matchKey]
    : [];
  if (!parts.length) return null;
  return (
    <div className="flex items-baseline gap-2 mb-3">
      <span className="text-[10px] font-semibold text-gray-400 uppercase tracking-wide flex-shrink-0">Finding by</span>
      <code className="text-xs bg-gray-100 text-gray-700 px-2 py-0.5 rounded break-all">{parts.join("  ·  ")}</code>
    </div>
  );
}

function ExistingRecord({ state }: { state: RecordState }) {
  return (
    <div className="bg-white/60 border border-gray-200 rounded-lg p-3 mb-3">
      <p className="text-[10px] font-semibold text-gray-400 uppercase tracking-wide mb-2">Existing record in Bubble</p>
      {state.status === "loading" && (
        <p className="text-xs text-gray-400 animate-pulse">Searching Bubble...</p>
      )}
      {state.status === "not_found" && (
        <p className="text-xs text-amber-600">No matching record found — this update step may be skipped</p>
      )}
      {state.status === "error" && (
        <p className="text-xs text-red-500">Could not reach Bubble to check existing record</p>
      )}
      {state.status === "found" && (() => {
        const entries = Object.entries(state.fields).slice(0, 10);
        return entries.length ? (
          <div>
            {entries.map(([k, v]) => (
              <div key={k} className="flex gap-2 text-xs py-0.5">
                <span className="text-gray-400 w-28 flex-shrink-0">{k}</span>
                <span className="text-gray-600 break-all">{v.length > 120 ? v.slice(0, 120) + "…" : v}</span>
              </div>
            ))}
            {Object.keys(state.fields).length > 10 && (
              <p className="text-[10px] text-gray-400 mt-1">+{Object.keys(state.fields).length - 10} more fields</p>
            )}
          </div>
        ) : (
          <p className="text-xs text-gray-400">Record found (Bubble ID: {state.id})</p>
        );
      })()}
    </div>
  );
}

// ── Main Modal ─────────────────────────────────────────────────────────────

export function BubbleSyncModal({ agentCallId, bubbleAction, onClose, onSynced }: Props) {
  const [syncing, setSyncing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [existingCal, setExistingCal] = useState<RecordState>({ status: "idle", fields: {} });
  const [existingLib, setExistingLib] = useState<RecordState>({ status: "idle", fields: {} });

  const ep = bubbleAction.event_preview ?? {};
  const lp = bubbleAction.library_item_preview ?? {};
  const agendaItems = bubbleAction.agenda_item_previews ?? [];

  const hasEvent = !!bubbleAction.event;
  const hasLib = !!bubbleAction.library_item;
  const hasAgenda = !!bubbleAction.agenda_items && agendaItems.length > 0;
  const hasAny = hasEvent || hasLib || hasAgenda;

  // Fetch existing records for UPDATE operations
  useEffect(() => {
    if (bubbleAction.event === "update") {
      setExistingCal({ status: "loading", fields: {} });
      fetch(`/api/bubble/record?agent_call_id=${encodeURIComponent(agentCallId)}&object_type=calendaritem`)
        .then(r => r.json())
        .then((d: { found?: boolean; fields?: Record<string, string>; id?: string; error?: string }) => {
          if (d.error) setExistingCal({ status: "error", fields: {} });
          else if (d.found) setExistingCal({ status: "found", fields: d.fields ?? {}, id: d.id });
          else setExistingCal({ status: "not_found", fields: {} });
        })
        .catch(() => setExistingCal({ status: "error", fields: {} }));
    }
    if (bubbleAction.library_item === "update") {
      setExistingLib({ status: "loading", fields: {} });
      fetch(`/api/bubble/record?agent_call_id=${encodeURIComponent(agentCallId)}&object_type=libraryitem`)
        .then(r => r.json())
        .then((d: { found?: boolean; fields?: Record<string, string>; id?: string; error?: string }) => {
          if (d.error) setExistingLib({ status: "error", fields: {} });
          else if (d.found) setExistingLib({ status: "found", fields: d.fields ?? {}, id: d.id });
          else setExistingLib({ status: "not_found", fields: {} });
        })
        .catch(() => setExistingLib({ status: "error", fields: {} }));
    }
  }, [agentCallId, bubbleAction.event, bubbleAction.library_item]);

  async function handleConfirm() {
    setSyncing(true);
    setError(null);
    try {
      const res = await fetch("/api/bubble/sync", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent_call_id: agentCallId }),
      });
      const data = await res.json() as { ok?: boolean; error?: string };
      if (!res.ok || !data.ok) throw new Error(data.error ?? "Sync failed");
      onSynced();
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSyncing(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <div className="bg-white rounded-xl shadow-2xl w-full max-w-2xl flex flex-col max-h-[90vh]">

        {/* Header */}
        <div className="px-6 py-4 border-b border-gray-100 flex items-start justify-between flex-shrink-0">
          <div>
            <h2 className="text-base font-semibold text-gray-900">Sync to Bubble</h2>
            {bubbleAction.notes && (
              <p className="text-xs text-gray-500 mt-0.5">{bubbleAction.notes}</p>
            )}
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 text-2xl leading-none ml-4">×</button>
        </div>

        {/* Body */}
        <div className="px-6 py-5 overflow-y-auto flex-1 space-y-4">

          {/* ── Calendar Event ─────────────────────────────────────── */}
          {hasEvent && bubbleAction.event && (
            <div className={`rounded-lg border p-4 ${
              bubbleAction.event === "create" ? "border-green-200 bg-green-50" : "border-blue-200 bg-blue-50"
            }`}>
              <div className="flex items-center gap-2 mb-4">
                <Badge action={bubbleAction.event} />
                <span className="text-sm font-semibold text-gray-800">Calendar Event</span>
              </div>

              {bubbleAction.event === "create" && (
                <FieldGrid fields={hasFields(ep.fields) ? ep.fields! : legacyEventFields(ep)} />
              )}

              {bubbleAction.event === "update" && (
                <>
                  <MatchCriteria matchSearch={ep.match_search} matchKey={ep.match_key} />
                  <ExistingRecord state={existingCal} />
                  {hasFields(ep.fields) ? (
                    <>
                      <p className="text-[10px] font-semibold text-gray-400 uppercase tracking-wide mb-2">
                        {ep.fields && Object.keys(ep.fields).length === 1 && ep.fields["Agenda"]
                          ? "Change being made"
                          : "Fields being updated"}
                      </p>
                      <FieldGrid fields={ep.fields!} />
                    </>
                  ) : ep.what_changes?.length ? (
                    <div>
                      <p className="text-[10px] font-semibold text-gray-400 uppercase tracking-wide mb-1">Changes</p>
                      {ep.what_changes.map((c, i) => (
                        <div key={i} className="flex items-start gap-1.5 text-xs text-gray-700 py-0.5">
                          <span className="text-gray-400">→</span>
                          <span>{c}</span>
                        </div>
                      ))}
                    </div>
                  ) : null}
                </>
              )}
            </div>
          )}

          {/* ── Library Item ─────────────────────────────────────────── */}
          {hasLib && bubbleAction.library_item && (
            <div className={`rounded-lg border p-4 ${
              bubbleAction.library_item === "create" ? "border-green-200 bg-green-50" : "border-blue-200 bg-blue-50"
            }`}>
              <div className="flex items-center gap-2 mb-4">
                <Badge action={bubbleAction.library_item} />
                <span className="text-sm font-semibold text-gray-800">Library Item</span>
              </div>

              {bubbleAction.library_item === "create" && (
                <FieldGrid fields={hasFields(lp.fields) ? lp.fields! : legacyLibFields(lp)} />
              )}

              {bubbleAction.library_item === "update" && (
                <>
                  <MatchCriteria matchSearch={lp.match_search} matchKey={lp.title} />
                  <ExistingRecord state={existingLib} />
                  {hasFields(lp.fields) ? (
                    <>
                      <p className="text-[10px] font-semibold text-gray-400 uppercase tracking-wide mb-2">Fields being updated</p>
                      <FieldGrid fields={lp.fields!} />
                    </>
                  ) : lp.what_changes?.length ? (
                    <div>
                      <p className="text-[10px] font-semibold text-gray-400 uppercase tracking-wide mb-1">Changes</p>
                      {lp.what_changes.map((c, i) => (
                        <div key={i} className="flex items-start gap-1.5 text-xs text-gray-700 py-0.5">
                          <span className="text-gray-400">→</span>
                          <span>{c}</span>
                        </div>
                      ))}
                    </div>
                  ) : null}
                </>
              )}
            </div>
          )}

          {/* ── Agenda Items ──────────────────────────────────────────── */}
          {hasAgenda && (
            <div className="rounded-lg border border-green-200 bg-green-50 p-4">
              <div className="flex items-center gap-2 mb-4">
                <Badge action="create" />
                <span className="text-sm font-semibold text-gray-800">
                  {agendaItems.length} Agenda Item{agendaItems.length !== 1 ? "s" : ""}
                </span>
              </div>
              <div className="space-y-4">
                {agendaItems.map((item, i) => (
                  <div key={i} className={i > 0 ? "pt-4 border-t border-green-200" : ""}>
                    <p className="text-xs font-semibold text-gray-800 mb-2">
                      <span className="text-gray-400 mr-1.5">{i + 1}.</span>
                      {item.title}
                    </p>
                    {item.official_title && (
                      <div className="flex gap-3 text-xs py-0.5">
                        <span className="text-gray-400 w-20 flex-shrink-0">Official</span>
                        <span className="text-gray-700">{item.official_title}</span>
                      </div>
                    )}
                    {item.reference_id && (
                      <div className="flex gap-3 text-xs py-0.5">
                        <span className="text-gray-400 w-20 flex-shrink-0">Ref ID</span>
                        <span className="text-gray-700 font-mono">{item.reference_id}</span>
                      </div>
                    )}
                    {item.chronicle_topics && item.chronicle_topics.length > 0 && (
                      <div className="flex gap-3 text-xs py-1">
                        <span className="text-gray-400 w-20 flex-shrink-0">Topics</span>
                        <TopicChips topics={item.chronicle_topics} />
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}

          {!hasAny && (
            <p className="text-sm text-gray-500 py-8 text-center">No applicable Bubble actions for this alert.</p>
          )}
        </div>

        {/* Footer */}
        <div className="px-6 py-4 border-t border-gray-100 flex items-center justify-between gap-3 flex-shrink-0">
          {error ? (
            <p className="text-xs text-red-600 flex-1">{error}</p>
          ) : (
            <p className="text-xs text-gray-400 flex-1">Sync runs via ECS — row status updates when complete.</p>
          )}
          <div className="flex gap-2 flex-shrink-0">
            <button
              onClick={onClose}
              disabled={syncing}
              className="px-4 py-1.5 text-sm text-gray-600 border border-gray-300 rounded-lg hover:bg-gray-50 disabled:opacity-50"
            >
              Cancel
            </button>
            <button
              onClick={handleConfirm}
              disabled={syncing || !hasAny}
              className="px-4 py-1.5 text-sm font-medium text-white bg-indigo-600 rounded-lg hover:bg-indigo-700 disabled:opacity-50 flex items-center gap-2"
            >
              {syncing && (
                <svg className="animate-spin h-3 w-3" viewBox="0 0 24 24" fill="none">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
                </svg>
              )}
              Confirm Sync
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
