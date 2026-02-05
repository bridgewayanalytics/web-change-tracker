"""Storage abstraction for change-detection state. Implementations: LocalStateStore, S3StateStore (stub)."""

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class StateStore(ABC):
    """Interface for loading and saving full state dict."""

    @abstractmethod
    def load_state(self) -> dict[str, Any]:
        """Load the full state. Returns empty dict if no state exists."""
        ...

    @abstractmethod
    def save_state(self, state: dict[str, Any]) -> None:
        """Persist the full state."""
        ...


class LocalStateStore(StateStore):
    """Uses a local JSON file (state.json)."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else Path(__file__).parent / "state.json"

    def load_state(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def save_state(self, state: dict[str, Any]) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)


class S3StateStore(StateStore):
    """S3-backed state store. Stub — no AWS wiring yet."""

    def __init__(self, bucket: str | None = None, key: str | None = None):
        # TODO: bucket — S3 bucket name (e.g. from env STATE_S3_BUCKET)
        # TODO: key — object key for state file (e.g. from env STATE_S3_KEY or default "web-change-tracker/state.json")
        # TODO: boto3 client, region from env AWS_REGION
        self._bucket = bucket or ""
        self._key = key or ""

    def load_state(self) -> dict[str, Any]:
        # TODO: boto3 get_object(Bucket=..., Key=...); parse JSON; return {} on NoSuchKey
        raise NotImplementedError("S3StateStore not yet implemented")

    def save_state(self, state: dict[str, Any]) -> None:
        # TODO: boto3 put_object(Bucket=..., Key=..., Body=json.dumps(state))
        raise NotImplementedError("S3StateStore not yet implemented")
