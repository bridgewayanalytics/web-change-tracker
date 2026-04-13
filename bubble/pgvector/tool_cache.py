"""Per-day, per-chat tool result cache.

Recency-tool results (latest newsreel, recent newsreels list) are identical
for every user on a given calendar day (Eastern Time).  Caching them avoids
redundant DB round-trips within and across conversations.

Cache keys are ``(chat_id, tool_name, params_hash, eastern_date)``.
The entire cache is invalidated daily by including the date in the key.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

_tool_result_cache: dict[str, Any] = {}


def _eastern_today() -> str:
    return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")


def _cache_key(chat_id: str, tool_name: str, params: dict, today: Optional[str] = None) -> str:
    params_hash = hashlib.md5(json.dumps(params, sort_keys=True).encode()).hexdigest()
    return f"{chat_id}:{tool_name}:{params_hash}:{today or _eastern_today()}"


def get_cached(chat_id: str, tool_name: str, params: dict) -> Optional[Any]:
    """Return a cached tool result, or ``None`` on miss."""
    key = _cache_key(chat_id, tool_name, params)
    return _tool_result_cache.get(key)


def set_cached(chat_id: str, tool_name: str, params: dict, result: Any) -> None:
    """Store a tool result in the day-scoped cache."""
    key = _cache_key(chat_id, tool_name, params)
    _tool_result_cache[key] = result


def clear_cache() -> None:
    """Drop all entries (useful for tests or manual invalidation)."""
    _tool_result_cache.clear()
