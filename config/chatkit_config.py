"""
Fetch chat agent configuration from DynamoDB chatkit_production_config table.

Table: chatkit_production_config
Partition key: config_key  (format: "chat:{chat_id}")

Usage:
    from config.chatkit_config import get_chat_config
    cfg = get_chat_config("web-tracking-agent")
    instructions = cfg.get("instructions", "")
    model = cfg.get("model", "gpt-5")
    reasoning_effort = cfg.get("reasoning_effort", "medium")
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)

CHATKIT_CONFIG_TABLE = os.environ.get(
    "CHATKIT_CONFIG_TABLE", "chatkit_production_config"
)

# Module-level cache: chat_id -> flattened config dict
_cache: dict[str, dict[str, Any]] = {}


def get_chat_config(chat_id: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """
    Fetch and cache chat agent config from DynamoDB.

    Args:
        chat_id: e.g. "web-tracking-agent" or "document-data-extraction"
        force_refresh: bypass in-process cache

    Returns:
        Flattened config dict. Empty dict if item not found or fetch fails.
    """
    if not force_refresh and chat_id in _cache:
        return _cache[chat_id]

    try:
        config = _fetch(chat_id)
        _cache[chat_id] = config
        log.info(
            "chatkit_config: loaded config for '%s' (model=%s, reasoning_effort=%s)",
            chat_id,
            config.get("model", "—"),
            config.get("reasoning_effort", "—"),
        )
        return config
    except Exception as e:
        log.warning("chatkit_config: failed to load config for '%s': %s", chat_id, e)
        return {}


def _fetch(chat_id: str) -> dict[str, Any]:
    import boto3

    region = os.environ.get("AWS_REGION", "us-east-1")
    client = boto3.client("dynamodb", region_name=region)

    resp = client.get_item(
        TableName=CHATKIT_CONFIG_TABLE,
        Key={"config_key": {"S": f"chat:{chat_id}"}},
    )

    item = resp.get("Item")
    if not item:
        log.warning("chatkit_config: no item found for chat_id='%s'", chat_id)
        return {}

    return _deserialize(item)


def _deserialize(item: dict) -> dict[str, Any]:
    """Flatten DynamoDB typed attribute map to a plain Python dict."""
    result: dict[str, Any] = {}
    for key, typed_val in item.items():
        result[key] = _deserialize_value(typed_val)
    return result


def _deserialize_value(typed_val: dict) -> Any:
    if "S" in typed_val:
        return typed_val["S"]
    if "N" in typed_val:
        v = typed_val["N"]
        return int(v) if "." not in v else float(v)
    if "BOOL" in typed_val:
        return typed_val["BOOL"]
    if "NULL" in typed_val:
        return None
    if "L" in typed_val:
        return [_deserialize_value(i) for i in typed_val["L"]]
    if "M" in typed_val:
        return {k: _deserialize_value(v) for k, v in typed_val["M"].items()}
    if "SS" in typed_val:
        return list(typed_val["SS"])
    if "NS" in typed_val:
        return [float(n) for n in typed_val["NS"]]
    return typed_val
