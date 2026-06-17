"""
Dynamic org tree — fetches the Bubble organization hierarchy at runtime.

Formats as dash-depth text for injection into agent context (same format as
the static prompts/org_tree.txt it replaces).

Caches for 30 minutes per process so a full pipeline run (20+ targets)
makes a single Bubble API call. Falls back to prompts/org_tree.txt if
Bubble API credentials are absent or the call fails.
"""

import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

_CACHE_TTL = 30 * 60  # 30 minutes
_cache: tuple[str, float] | None = None  # (text, fetched_at)

_HEADER = """\
# Organization Tree
# Source: Bubble (live) — fetched at pipeline start
# Format: dash depth = Level (- = Level 1, -- = Level 2, ...)
#
# Instructions for the agent:
# - Use this tree to assign the Organization field in your output.
# - Match to the most specific (deepest) applicable organization.
# - Use the full Name exactly as written (not the short name).\
"""


def _static_fallback() -> str:
    path = Path(__file__).resolve().parent.parent / "prompts" / "org_tree.txt"
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _build_tree_text(orgs: list[dict]) -> str:
    """DFS traversal of the org hierarchy → dash-depth text."""
    id_to_org = {o["_id"]: o for o in orgs if o.get("_id")}

    # Build parent → [children] map
    children: dict[str | None, list[dict]] = {}
    for org in orgs:
        parent_id = org.get("Parent") or None
        children.setdefault(parent_id, []).append(org)
    for sibs in children.values():
        sibs.sort(key=lambda o: int(o.get("Order") or 0))

    lines = [_HEADER, ""]

    def _visit(org_id: str, level: int) -> None:
        org = id_to_org.get(org_id)
        if not org:
            return
        name = str(org.get("Name") or "").strip()
        short = str(org.get("Short Name") or "").strip()
        if not name:
            return
        prefix = "-" * level
        line = f"{prefix} {name} ({short})" if short and short != name else f"{prefix} {name}"
        lines.append(line)
        for child in children.get(org_id, []):
            child_id = child.get("_id")
            if child_id:
                _visit(child_id, level + 1)

    roots = sorted(
        [o for o in orgs if not o.get("Parent")],
        key=lambda o: int(o.get("Order") or 0),
    )
    for root in roots:
        root_id = root.get("_id")
        if root_id:
            _visit(root_id, 1)

    return "\n".join(lines)


def _fetch_from_bubble() -> str:
    try:
        from bubble.bridgemind import get_client, TYPE_ORGANIZATION, SPACE_CONSTRAINT
        client = get_client()
        orgs = list(client.list_all(TYPE_ORGANIZATION, constraints=SPACE_CONSTRAINT))
    except Exception as exc:
        log.warning("org_tree: Bubble API fetch failed (%s) — will use static fallback", exc)
        return ""

    if not orgs:
        log.warning("org_tree: Bubble returned 0 organizations — will use static fallback")
        return ""

    text = _build_tree_text(orgs)
    log.info("org_tree: fetched %d orgs from Bubble", len(orgs))
    return text


def get_org_tree() -> str:
    """
    Return the org hierarchy as dash-depth text.

    Uses a 30-minute in-process cache so repeated calls within a pipeline run
    don't hit the Bubble API more than once. Falls back to prompts/org_tree.txt
    if Bubble is unavailable.
    """
    global _cache

    now = time.monotonic()
    if _cache is not None:
        text, ts = _cache
        if now - ts < _CACHE_TTL:
            return text

    text = _fetch_from_bubble()
    if not text:
        text = _static_fallback()
        if text:
            log.info("org_tree: using static prompts/org_tree.txt fallback")

    _cache = (text, now)
    return text
