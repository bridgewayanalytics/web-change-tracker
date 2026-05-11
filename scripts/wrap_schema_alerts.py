"""
Wrap the existing flat output_json_schema for chat:web-tracking-agent in an
`alerts` array wrapper so the agent can produce multiple alerts per page change.

Before:
  { "type": "object", "properties": { ...21 fields... }, "required": [...], "additionalProperties": false }

After:
  { "type": "object", "properties": { "alerts": { "type": "array", "items": { <original schema> } } },
    "required": ["alerts"], "additionalProperties": false }

Usage:
    python scripts/wrap_schema_alerts.py [--dry-run]

Requires: AWS credentials with access to the chatkit_production_config DynamoDB table.
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

CHAT_ID = "web-tracking-agent"
TABLE = os.environ.get("CHATKIT_CONFIG_TABLE", "chatkit_production_config")


def main():
    parser = argparse.ArgumentParser(description="Wrap output_json_schema in alerts array")
    parser.add_argument("--dry-run", action="store_true", help="Print new schema without writing")
    args = parser.parse_args()

    import boto3

    region = os.environ.get("AWS_REGION", "us-east-1")
    client = boto3.client("dynamodb", region_name=region)
    config_key = f"chat:{CHAT_ID}"

    # 1. Read current schema
    resp = client.get_item(
        TableName=TABLE,
        Key={"config_key": {"S": config_key}},
    )
    item = resp.get("Item")
    if not item:
        log.error("No item found for config_key='%s' in table '%s'", config_key, TABLE)
        sys.exit(1)

    schema_attr = item.get("output_json_schema")
    if not schema_attr or "M" not in schema_attr:
        log.error("output_json_schema not found or not a Map type")
        sys.exit(1)

    # Deserialize the current schema to inspect it
    from config.chatkit_config import _deserialize_value
    current_schema = _deserialize_value(schema_attr)

    # Guard: don't wrap if already wrapped
    if (
        isinstance(current_schema, dict)
        and set(current_schema.get("properties", {}).keys()) == {"alerts"}
    ):
        log.info("Schema is already wrapped in an alerts array — nothing to do.")
        sys.exit(0)

    log.info("Current schema has %d top-level properties", len(current_schema.get("properties", {})))

    # 2. Build wrapped schema
    wrapped = {
        "type": "object",
        "properties": {
            "alerts": {
                "type": "array",
                "items": current_schema,
            }
        },
        "required": ["alerts"],
        "additionalProperties": False,
    }

    print("\n=== New wrapped schema ===")
    print(json.dumps(wrapped, indent=2)[:2000], "..." if len(json.dumps(wrapped)) > 2000 else "")

    if args.dry_run:
        log.info("Dry run — not writing to DynamoDB.")
        return

    # 3. Write back to DynamoDB using the raw attribute map
    # We need to serialize the wrapped schema to DynamoDB typed format
    serialized = _serialize_value(wrapped)

    client.update_item(
        TableName=TABLE,
        Key={"config_key": {"S": config_key}},
        UpdateExpression="SET output_json_schema = :s",
        ExpressionAttributeValues={":s": serialized},
    )
    log.info("Updated output_json_schema for '%s' in DynamoDB.", config_key)


def _serialize_value(val) -> dict:
    """Convert a Python value to DynamoDB typed attribute map."""
    if val is None:
        return {"NULL": True}
    if isinstance(val, bool):
        return {"BOOL": val}
    if isinstance(val, str):
        return {"S": val}
    if isinstance(val, (int, float)):
        return {"N": str(val)}
    if isinstance(val, list):
        return {"L": [_serialize_value(i) for i in val]}
    if isinstance(val, dict):
        return {"M": {k: _serialize_value(v) for k, v in val.items()}}
    return {"S": str(val)}


if __name__ == "__main__":
    main()
