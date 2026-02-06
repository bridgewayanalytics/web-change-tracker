"""Local state store (state.json). Per-target load/save for dev."""

import json
import os
from pathlib import Path
from typing import Any


def _get_path() -> Path:
    path = os.environ.get("STATE_FILE", "").strip()
    if path:
        return Path(path)
    return Path(__file__).resolve().parent.parent / "state.json"


def _load_full() -> dict[str, Any]:
    path = _get_path()
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save_full(data: dict[str, Any]) -> None:
    path = _get_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _migrate_state(s: dict | None) -> dict | None:
    """Migrate old extracted format to new extracted[resource_type] with {title, url} items."""
    if not s:
        return s
    if "pdf_links" in s and "extracted" not in s:
        s = dict(s)
        s["extracted"] = {"docs": [{"title": u.split("/")[-1] or u[:60], "url": u} for u in s.get("pdf_links", [])]}
        del s["pdf_links"]
    return s


def load_target_state(target_id: str) -> dict | None:
    """
    Load the latest state for a single target from state.json.
    Returns None if the target has no stored state.
    """
    raw = _load_full()

    # Legacy format: { page_hash, extracted } for "default"
    if "page_hash" in raw and "targets" not in raw:
        if target_id == "default":
            return _migrate_state({
                "page_hash": raw["page_hash"],
                "extracted": raw.get("extracted", {}),
            })
        return None

    if "targets" not in raw:
        return None

    s = raw["targets"].get(target_id)
    return _migrate_state(s)


def save_target_state(target_id: str, state: dict[str, Any]) -> None:
    """Save the latest state for a single target to state.json."""
    raw = _load_full()

    # Normalize to targets structure
    if "page_hash" in raw and "targets" not in raw:
        raw = {"targets": {"default": {"page_hash": raw["page_hash"], "extracted": raw.get("extracted", {})}}}
    if "targets" not in raw:
        raw["targets"] = {}

    raw["targets"][target_id] = state
    _save_full(raw)
