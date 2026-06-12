"""
Bubble Data API client. Single client for reading/writing Bubble app data.

Config: BUBBLE_API_URL, BUBBLE_API_KEY (optional BUBBLE_APP_VERSION, default live).
- Production (ECS): Injected via ECS task definition secrets (valueFrom SSM). Never log these.
- Local dev: Set in .env (python-dotenv loads from cwd). .env is for local dev only and must not be committed.
"""

import json
import logging
import os
import time
from typing import Any, Generator
from urllib.parse import urlparse

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests

log = logging.getLogger(__name__)

# Default app version for API path (Bubble: "live" or "version-test")
DEFAULT_APP_VERSION = "live"

# Per-process Bubble HTTP request metrics (approx. per-run for CLI/ECS tasks)
_REQUEST_COUNT: int = 0
_REQUEST_FAILURES: int = 0


def get_bubble_request_stats() -> dict[str, int]:
    """Return aggregate Bubble HTTP request stats for this process."""
    return {
        "total": _REQUEST_COUNT,
        "failures": _REQUEST_FAILURES,
        "successes": max(0, _REQUEST_COUNT - _REQUEST_FAILURES),
    }


class BubbleAPIError(Exception):
    """Raised when the Bubble Data API returns an error. No secrets in message."""

    def __init__(self, message: str, status_code: int | None = None, response_snippet: str = ""):
        self.status_code = status_code
        self.response_snippet = response_snippet[:500] if response_snippet else ""
        super().__init__(message)

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.status_code is not None:
            parts.append(f" (HTTP {self.status_code})")
        if self.response_snippet:
            parts.append(f" Response: {self.response_snippet}")
        return "".join(parts)


def _safe_snippet(obj: Any) -> str:
    """Produce a short, safe string from a response (no secrets). Never log BUBBLE_API_URL/BUBBLE_API_KEY."""
    if obj is None:
        return ""
    try:
        if isinstance(obj, dict):
            # Omit keys that might look like tokens
            safe = {k: v for k, v in obj.items() if str(k).lower() not in ("token", "authorization", "api_key", "key")}
            return json.dumps(safe)[:400]
        return str(obj)[:400]
    except Exception:
        return str(type(obj).__name__)


class BubbleClient:
    """
    Bubble Data API client: get, search, list_all.
    Optional in-memory cache for read-heavy usage within a run.
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        app_version: str | None = None,
        use_cache: bool = False,
    ):
        base_url = (base_url or os.environ.get("BUBBLE_API_URL", "")).strip()
        api_key = (api_key or os.environ.get("BUBBLE_API_KEY", "")).strip()
        app_version = (app_version or os.environ.get("BUBBLE_APP_VERSION", DEFAULT_APP_VERSION)).strip()

        if not base_url:
            raise ValueError("BUBBLE_API_URL must be set (e.g. https://myapp.bubbleapps.io)")
        if not api_key:
            raise ValueError("BUBBLE_API_KEY must be set")

        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._app_version = app_version or DEFAULT_APP_VERSION
        self._use_cache = use_cache
        self._cache: dict[str, Any] = {}

        # Build object API base: .../version/api/1.1/obj (Bubble Data API path).
        # BUBBLE_API_URL can be: (1) app root https://example.com, (2) with version https://example.com/live,
        # or (3) full obj base https://example.com/live/api/1.1/obj. We avoid duplicating the version segment.
        base = self._base_url.rstrip("/")
        if base.endswith("/obj"):
            self._obj_base = base
        elif "/api/1.1" in self._base_url or "/api/1.0" in self._base_url:
            self._obj_base = f"{base}/obj"
        elif base.endswith("/live") or base.endswith("/version-test"):
            # URL already has version; append only /api/1.1/obj
            self._obj_base = f"{base}/api/1.1/obj"
        else:
            self._obj_base = f"{base}/{self._app_version}/api/1.1/obj"

    @property
    def base_url(self) -> str:
        """Base API URL (read-only)."""
        return self._base_url

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Low-level HTTP request wrapper with structured logging and per-run metrics.
        Logs method, endpoint path (no secrets), status code, and duration.
        """
        global _REQUEST_COUNT, _REQUEST_FAILURES

        url = f"{self._obj_base}/{path}" if not path.startswith("http") else path
        params = params or {}
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        # Derive a safe endpoint path for logs (no base URL or query secrets)
        if path.startswith("http"):
            parsed = urlparse(path)
            endpoint = parsed.path or "/"
        else:
            # Bubble object API paths are always under /obj
            endpoint = f"/obj/{path}"

        start = time.monotonic()
        try:
            resp = requests.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=30,
            )
            duration = time.monotonic() - start
        except requests.RequestException as e:
            duration = time.monotonic() - start
            _REQUEST_COUNT += 1
            _REQUEST_FAILURES += 1
            log.error(
                "Bubble API request failed: method=%s endpoint=%s duration=%.3fs error=%s",
                method,
                endpoint,
                duration,
                e,
            )
            raise BubbleAPIError(
                f"Bubble API request failed: {e}",
                response_snippet=str(e),
            ) from e

        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            data = {}

        if resp.status_code >= 400:
            _REQUEST_COUNT += 1
            _REQUEST_FAILURES += 1
            snippet = _safe_snippet(data or resp.text[:200])
            log.error(
                "Bubble API error: method=%s endpoint=%s status=%s duration=%.3fs snippet=%s",
                method,
                endpoint,
                resp.status_code,
                duration,
                snippet,
            )
            raise BubbleAPIError(
                f"Bubble API error: {resp.reason or 'Unknown'}",
                status_code=resp.status_code,
                response_snippet=_safe_snippet(data or resp.text[:500]),
            )

        _REQUEST_COUNT += 1
        log.info(
            "Bubble API request: method=%s endpoint=%s status=%s duration=%.3fs",
            method,
            endpoint,
            resp.status_code,
            duration,
        )
        return data

    def _cache_key(self, op: str, type_name: str, extra: str = "") -> str:
        return f"{op}:{type_name}:{extra}"

    def _type_path(self, type_name: str) -> str:
        """Normalize type name for Bubble Data API URL: lowercase, spaces removed."""
        s = (type_name or "").strip().lower()
        return "".join(s.split())

    def get(self, type_name: str, id: str) -> dict[str, Any]:
        """
        Fetch a single thing by type and id.
        Raises BubbleAPIError on HTTP error or missing record.
        """
        cache_key = self._cache_key("get", type_name, id)
        if self._use_cache and cache_key in self._cache:
            return self._cache[cache_key]

        path = f"{self._type_path(type_name)}/{id}"
        data = self._request("GET", path)

        # Bubble may return { "response": { ... } } or direct object
        result = data.get("response", data)
        if isinstance(result, dict) and "_id" not in result and "id" not in result:
            # Might be wrapped
            result = data

        if self._use_cache:
            self._cache[cache_key] = result
        return result

    def search(
        self,
        type_name: str,
        constraints: list[dict] | None = None,
        limit: int = 100,
        cursor: int | str | None = None,
    ) -> dict[str, Any]:
        """
        Search for things with Bubble-style constraints.
        Returns dict with keys: results (list), count, remaining, cursor.
        """
        constraints = constraints or []
        params: dict[str, str] = {
            "limit": str(min(max(1, limit), 100)),
        }
        if constraints:
            params["constraints"] = json.dumps(constraints)
        if cursor is not None:
            params["cursor"] = str(cursor)

        path = self._type_path(type_name)
        data = self._request("GET", path, params=params)

        response = data.get("response", data)
        if not isinstance(response, dict):
            raise BubbleAPIError(
                "Bubble API returned unexpected search response",
                response_snippet=_safe_snippet(data),
            )
        results = response.get("results", [])
        return {
            "results": results,
            "count": response.get("count", len(results)),
            "remaining": response.get("remaining", 0),
            "cursor": response.get("cursor", cursor),
        }

    def list_all(
        self,
        type_name: str,
        constraints: list[dict] | None = None,
        page_size: int = 100,
    ) -> Generator[dict[str, Any], None, None]:
        """
        Yield all things of type_name matching constraints, paginating until exhausted.
        """
        cursor: int | None = 0
        while True:
            out = self.search(
                type_name,
                constraints=constraints,
                limit=page_size,
                cursor=cursor,
            )
            for item in out["results"]:
                yield item
            remaining = out.get("remaining", 0)
            if remaining <= 0:
                break
            # Next page: cursor is typically current cursor + count
            count = out.get("count", len(out["results"]))
            if count == 0:
                break
            if isinstance(cursor, int):
                cursor = cursor + count
            else:
                cursor = out.get("cursor", cursor)
            if cursor is None:
                break

    # ------------------------------------------------------------------
    # Write operations — scoped to an explicit allowlist of types/fields
    # ------------------------------------------------------------------

    # Only these (type_name, operation) pairs are permitted for writes.
    # Keeps the blast radius small while the system is read-heavy.
    _WRITE_ALLOWLIST: set[tuple[str, str]] = {
        ("Alert", "create"),
        ("Calendar Item", "patch_alerts"),
        ("calendaritem", "create"),
        ("calendaritem", "sync"),
        ("libraryitem", "create"),
        ("libraryitem", "sync"),
    }

    def _assert_write_allowed(self, type_name: str, operation: str) -> None:
        if (type_name, operation) not in self._WRITE_ALLOWLIST:
            raise BubbleAPIError(
                f"Write not allowed: type={type_name!r} operation={operation!r}. "
                f"Allowed: {self._WRITE_ALLOWLIST}"
            )

    def create(self, type_name: str, fields: dict[str, Any]) -> str:
        """
        Create a new thing of *type_name* with the given fields.
        Returns the Bubble ``_id`` of the created object.

        Guarded by ``_WRITE_ALLOWLIST``.
        """
        self._assert_write_allowed(type_name, "create")
        path = self._type_path(type_name)
        data = self._request("POST", path, json_body=fields)
        # Bubble returns {"id": "<new_id>", "status": "success"} on creation.
        obj_id = data.get("id") or ""
        if not obj_id:
            raise BubbleAPIError(
                "Bubble create did not return an id",
                response_snippet=_safe_snippet(data),
            )
        return obj_id

    def patch(self, type_name: str, id: str, fields: dict[str, Any], *, scope: str) -> None:
        """
        Update (PATCH) specific fields on an existing thing.

        *scope* is a logical label checked against ``_WRITE_ALLOWLIST``
        (e.g. ``"patch_alerts"``).  This prevents accidental broad writes.
        """
        self._assert_write_allowed(type_name, scope)
        path = f"{self._type_path(type_name)}/{id}"
        self._request("PATCH", path, json_body=fields)

    def clear_cache(self) -> None:
        """Clear the in-memory read cache."""
        self._cache.clear()


def get_client(
    base_url: str | None = None,
    api_key: str | None = None,
    app_version: str | None = None,
    use_cache: bool | None = None,
) -> BubbleClient:
    """
    Create a BubbleClient from env (BUBBLE_API_URL, BUBBLE_API_KEY, etc.).
    use_cache: if None, reads BUBBLE_USE_CACHE (1/true/yes) for read-heavy runs.
    """
    if use_cache is None:
        use_cache = os.environ.get("BUBBLE_USE_CACHE", "").strip().lower() in ("1", "true", "yes")
    return BubbleClient(
        base_url=base_url,
        api_key=api_key,
        app_version=app_version,
        use_cache=use_cache,
    )
