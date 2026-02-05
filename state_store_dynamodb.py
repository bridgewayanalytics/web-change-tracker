"""DynamoDB-backed state store. Table name via env STATE_TABLE."""

import json
import os
from typing import Any

from state_store import StateStore


class DynamoDBStateStore(StateStore):
    """
    Stores latest state per target_id in DynamoDB.
    Table schema: PK=target_id (String), page_hash (String), extracted_json (String).
    """

    def __init__(self, table_name: str | None = None, region: str | None = None):
        self.table_name = table_name or os.environ.get("STATE_TABLE", "")
        self.region = region or os.environ.get("AWS_REGION", "us-east-1")

    def load_state(self) -> dict[str, Any]:
        if not self.table_name:
            raise ValueError("STATE_TABLE env var required for DynamoDBStateStore")
        import boto3

        client = boto3.client("dynamodb", region_name=self.region)
        resp = client.scan(TableName=self.table_name)
        targets: dict[str, Any] = {}
        for item in resp.get("Items", []):
            target_id = item.get("target_id", {}).get("S", "")
            if not target_id:
                continue
            page_hash = item.get("page_hash", {}).get("S", "")
            extracted_json = item.get("extracted_json", {}).get("S", "{}")
            try:
                extracted = json.loads(extracted_json)
            except json.JSONDecodeError:
                extracted = {}
            targets[target_id] = {"page_hash": page_hash, "extracted": extracted}
        while "LastEvaluatedKey" in resp:
            resp = client.scan(TableName=self.table_name, ExclusiveStartKey=resp["LastEvaluatedKey"])
            for item in resp.get("Items", []):
                target_id = item.get("target_id", {}).get("S", "")
                if not target_id:
                    continue
                page_hash = item.get("page_hash", {}).get("S", "")
                extracted_json = item.get("extracted_json", {}).get("S", "{}")
                try:
                    extracted = json.loads(extracted_json)
                except json.JSONDecodeError:
                    extracted = {}
                targets[target_id] = {"page_hash": page_hash, "extracted": extracted}
        return {"targets": targets}

    def save_state(self, state: dict[str, Any]) -> None:
        if not self.table_name:
            raise ValueError("STATE_TABLE env var required for DynamoDBStateStore")
        import boto3

        client = boto3.client("dynamodb", region_name=self.region)
        targets = state.get("targets", {})
        for target_id, data in targets.items():
            page_hash = data.get("page_hash", "")
            extracted = data.get("extracted", {})
            extracted_json = json.dumps(extracted)
            client.put_item(
                TableName=self.table_name,
                Item={
                    "target_id": {"S": target_id},
                    "page_hash": {"S": page_hash},
                    "extracted_json": {"S": extracted_json},
                },
            )
