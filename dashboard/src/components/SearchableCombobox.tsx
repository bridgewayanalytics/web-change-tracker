"use client";

import { useEffect, useMemo, useRef, useState } from "react";

export type BubbleOption = { id: string; name: string };

interface BaseProps {
  options: BubbleOption[];
  loading?: boolean;
  placeholder?: string;
  disabled?: boolean;
}

interface SingleProps extends BaseProps {
  mode: "single";
  value: string;              // the selected option name (or "")
  onChange: (name: string) => void;
}

interface MultiProps extends BaseProps {
  mode: "multi";
  values: string[];           // selected option names
  onChange: (names: string[]) => void;
}

type Props = SingleProps | MultiProps;

const DROPDOWN_MAX_HEIGHT = 256;

export function SearchableCombobox(props: Props) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [openUpward, setOpenUpward] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Close on outside click
  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open]);

  // Decide upward vs downward based on viewport space when opening
  useEffect(() => {
    if (!open || !triggerRef.current) return;
    const rect = triggerRef.current.getBoundingClientRect();
    const spaceBelow = window.innerHeight - rect.bottom;
    const spaceAbove = rect.top;
    // Open upward only if there's not enough space below AND there's more space above
    setOpenUpward(spaceBelow < DROPDOWN_MAX_HEIGHT && spaceAbove > spaceBelow);
  }, [open]);

  // Focus input when opened
  useEffect(() => {
    if (open) setTimeout(() => inputRef.current?.focus(), 0);
  }, [open]);

  const filtered = useMemo(() => {
    if (!query.trim()) return props.options;
    const q = query.toLowerCase();
    return props.options.filter(o => o.name.toLowerCase().includes(q));
  }, [props.options, query]);

  const selectedNames = props.mode === "multi" ? props.values : (props.value ? [props.value] : []);
  const selectedSet = new Set(selectedNames);

  function toggle(name: string) {
    if (props.mode === "multi") {
      const next = selectedSet.has(name)
        ? props.values.filter(v => v !== name)
        : [...props.values, name];
      props.onChange(next);
    } else {
      props.onChange(name);
      setOpen(false);
      setQuery("");
    }
  }

  function removeOne(name: string) {
    if (props.mode === "multi") {
      props.onChange(props.values.filter(v => v !== name));
    } else {
      props.onChange("");
    }
  }

  const placeholder = props.placeholder ?? "Select…";
  const isEmpty = selectedNames.length === 0;

  return (
    <div ref={rootRef} className="relative w-full">
      {/* Trigger / selected chips */}
      <div
        ref={triggerRef}
        onClick={() => !props.disabled && setOpen(true)}
        className={`min-h-[28px] w-full border border-gray-200 rounded px-2 py-1 bg-white cursor-text transition-colors ${open ? "border-indigo-400 ring-1 ring-indigo-100" : "hover:border-gray-400"} ${props.disabled ? "opacity-50 cursor-not-allowed" : ""}`}
      >
        {isEmpty ? (
          <span className="text-[12px] text-gray-400">{props.loading ? "Loading…" : placeholder}</span>
        ) : (
          <div className="flex flex-wrap gap-1">
            {selectedNames.map(name => (
              <span key={name} className="inline-flex items-center gap-1 text-[11px] bg-violet-100 text-violet-700 rounded px-1.5 py-0.5 border border-violet-200 leading-tight">
                {name}
                {!props.disabled && (
                  <button
                    type="button"
                    onClick={e => { e.stopPropagation(); removeOne(name); }}
                    className="text-violet-500 hover:text-violet-800 leading-none"
                    aria-label={`Remove ${name}`}
                  >×</button>
                )}
              </span>
            ))}
          </div>
        )}
      </div>

      {/* Dropdown panel */}
      {open && (
        <div className={`absolute z-20 w-full bg-white border border-gray-200 rounded-md shadow-lg max-h-64 overflow-hidden flex flex-col ${openUpward ? "bottom-full mb-1" : "top-full mt-1"}`}>
          <div className="p-2 border-b border-gray-100 bg-gray-50/60">
            <input
              ref={inputRef}
              type="text"
              value={query}
              onChange={e => setQuery(e.target.value)}
              placeholder="Search…"
              className="w-full text-[12px] px-2 py-1 border border-gray-200 rounded focus:border-indigo-400 focus:outline-none focus:ring-1 focus:ring-indigo-100"
              onKeyDown={e => { if (e.key === "Escape") setOpen(false); }}
            />
          </div>
          <div className="overflow-y-auto flex-1">
            {props.loading ? (
              <div className="px-3 py-3 text-[11px] text-gray-400 italic">Loading options…</div>
            ) : filtered.length === 0 ? (
              <div className="px-3 py-3 text-[11px] text-gray-400 italic">
                {props.options.length === 0 ? "No options available" : "No matches"}
              </div>
            ) : (
              filtered.map(opt => {
                const isSelected = selectedSet.has(opt.name);
                return (
                  <button
                    key={opt.id}
                    type="button"
                    onClick={() => toggle(opt.name)}
                    className={`w-full text-left text-[12px] px-3 py-1.5 flex items-center gap-2 ${isSelected ? "bg-violet-50 text-violet-900" : "hover:bg-gray-50 text-gray-700"}`}
                  >
                    {props.mode === "multi" && (
                      <span className={`inline-block w-3.5 h-3.5 rounded border flex items-center justify-center text-[9px] ${isSelected ? "bg-violet-600 border-violet-600 text-white" : "border-gray-300"}`}>
                        {isSelected ? "✓" : ""}
                      </span>
                    )}
                    <span className="flex-1 break-words leading-snug">{opt.name}</span>
                  </button>
                );
              })
            )}
          </div>
          {props.mode === "multi" && selectedNames.length > 0 && (
            <div className="px-3 py-1.5 border-t border-gray-100 bg-gray-50/60 text-[10px] text-gray-500">
              {selectedNames.length} selected
            </div>
          )}
        </div>
      )}
    </div>
  );
}
