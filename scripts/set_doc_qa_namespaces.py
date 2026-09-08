"""
One-time script to set pgvector_namespaces for document-extraction-qa-agent.

The DynamoDB config currently has pgvector_namespaces = [] (empty list), which
bypasses the fallback and leaves the agent with zero knowledge bases to search.
This script sets it to the same namespace list used by the web tracking QA agent.

Usage:
  python3 scripts/set_doc_qa_namespaces.py           # show current value, apply fix
  python3 scripts/set_doc_qa_namespaces.py --dry-run # show current value only
"""

import argparse
import json
import os
import sys

CHAT_ID = "document-extraction-qa-agent"
TABLE = os.environ.get("CHATKIT_CONFIG_TABLE", "chatkit_production_config")
REGION = os.environ.get("AWS_REGION", "us-east-1")

NAMESPACES = [
    "bubble-data",
    "art-chronicles",
    "art-newsreels",
    "naic-guidelines",
    "naic-proceedings",
    "international-guidelines",
    "ratings-agencies",
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    import boto3
    client = boto3.client("dynamodb", region_name=REGION)

    key = {"config_key": {"S": f"chat:{CHAT_ID}"}}

    resp = client.get_item(TableName=TABLE, Key=key)
    item = resp.get("Item", {})
    if not item:
        print(f"ERROR: no item found for chat:{CHAT_ID} in {TABLE}", file=sys.stderr)
        sys.exit(1)

    current_ns = item.get("pgvector_namespaces", {})
    print(f"Current pgvector_namespaces: {json.dumps(current_ns, indent=2)}")
    print(f"Will set to: {json.dumps(NAMESPACES, indent=2)}")

    if args.dry_run:
        print("Dry run — no changes made.")
        return

    client.update_item(
        TableName=TABLE,
        Key=key,
        UpdateExpression="SET pgvector_namespaces = :ns",
        ExpressionAttributeValues={
            ":ns": {"L": [{"S": ns} for ns in NAMESPACES]},
        },
    )
    print(f"Updated pgvector_namespaces for chat:{CHAT_ID}")


if __name__ == "__main__":
    main()
