"""
Bubble API healthcheck for LIVE mode. Called at startup when bubble_enrich_enabled and bubble_mode=LIVE.
Verifies BUBBLE_API_URL/BUBBLE_API_KEY are set and one cheap API call succeeds.
Never logs credential values.
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)

# Type name for healthcheck (Bubble Data API: "Tree" -> path "tree")
HEALTHCHECK_TYPE = "Tree"
HEALTHCHECK_LIMIT = 5


def bubble_healthcheck() -> tuple[bool, dict[str, Any]]:
    """
    Run a single cheap Bubble API call to verify LIVE mode can reach the API.
    - Ensures BUBBLE_API_URL and BUBBLE_API_KEY are present (logs only that they exist, not values).
    - Calls list/search for Tree (or search Tree node for "NAIC") with small limit.
    - Returns (ok: bool, detail: dict). detail has endpoint, status, result_count; on failure, error and optionally status.
    """
    detail: dict[str, Any] = {}
    url = (os.environ.get("BUBBLE_API_URL") or "").strip()
    key = (os.environ.get("BUBBLE_API_KEY") or "").strip()
    if not url or not key:
        log.warning(
            "Bubble healthcheck skipped: BUBBLE_API_URL or BUBBLE_API_KEY not set (values not logged)"
        )
        detail["error"] = "BUBBLE_API_URL or BUBBLE_API_KEY not set"
        return False, detail
    log.info("BUBBLE_API_URL and BUBBLE_API_KEY are set (values not logged)")

    try:
        from bubble.client import get_client
        client = get_client(use_cache=False)
        # Cheap call: list Trees (small limit)
        out = client.search(HEALTHCHECK_TYPE, limit=HEALTHCHECK_LIMIT)
        results = out.get("results") or []
        count = len(results)
        status = 200
        # Path only (no host) to avoid logging sensitive URL
        endpoint = "GET /obj/tree"
        log.info(
            "Bubble healthcheck: endpoint=%s, HTTP status=%s, result_count=%s",
            endpoint, status, count,
        )
        detail["endpoint"] = endpoint
        detail["status"] = status
        detail["result_count"] = count
        return True, detail
    except Exception as e:
        status_code = getattr(e, "status_code", None)
        log.error(
            "Bubble healthcheck failed: %s (HTTP %s)",
            e,
            status_code if status_code is not None else "N/A",
        )
        detail["error"] = str(e)
        if status_code is not None:
            detail["status"] = status_code
        return False, detail
