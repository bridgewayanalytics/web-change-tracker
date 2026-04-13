"""DynamoDB-backed state store. Per-target load/save via STATE_TABLE."""

import json
import os
from typing import Any


def _get_client():
    import boto3
    region = os.environ.get("AWS_REGION", "us-east-1")
    return boto3.client("dynamodb", region_name=region)


def load_target_state(target_id: str) -> dict | None:
    """
    Load the latest state for a single target from DynamoDB.
    Returns None if the target has no stored state.
    """
    table_name = os.environ.get("STATE_TABLE", "").strip()
    if not table_name:
        raise ValueError("STATE_TABLE env var required for DynamoDB state backend")

    client = _get_client()
    resp = client.get_item(
        TableName=table_name,
        Key={"target_id": {"S": target_id}},
    )

    item = resp.get("Item")
    if not item:
        return None

    page_hash = item.get("page_hash", {}).get("S", "")
    extracted_json = item.get("extracted_json", {}).get("S", "{}")
    try:
        extracted = json.loads(extracted_json)
    except json.JSONDecodeError:
        extracted = {}

    result: dict = {"page_hash": page_hash, "extracted": extracted}
    content_html = item.get("content_html", {}).get("S")
    if content_html is not None:
        result["content_html"] = content_html
    return result


def save_target_state(target_id: str, state: dict[str, Any]) -> None:
    """Save the latest state for a single target to DynamoDB."""
    table_name = os.environ.get("STATE_TABLE", "").strip()
    if not table_name:
        raise ValueError("STATE_TABLE env var required for DynamoDB state backend")

    page_hash = state.get("page_hash", "")
    extracted = state.get("extracted", {})
    extracted_json = json.dumps(extracted)
    content_html = state.get("content_html")

    item: dict = {
        "target_id": {"S": target_id},
        "page_hash": {"S": page_hash},
        "extracted_json": {"S": extracted_json},
    }
    if content_html is not None:
        item["content_html"] = {"S": content_html}

    client = _get_client()
    client.put_item(TableName=table_name, Item=item)
