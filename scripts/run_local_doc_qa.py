"""One-off local QA runner for a list of agent_call_ids.

Loads DB creds from Secrets Manager and OpenAI/ChatKit keys from SSM in-process,
sets PGVECTOR_ENABLED, then invokes eval.run_doc_eval.run().

Usage:
    AWS_PROFILE=bridgeway python3 scripts/run_local_doc_qa.py /tmp/missing_acids.txt
"""
import json
import os
import sys


def main():
    if len(sys.argv) < 2:
        print("Usage: run_local_doc_qa.py <path-to-csv-of-agent-call-ids>", file=sys.stderr)
        sys.exit(2)

    csv_path = sys.argv[1]
    with open(csv_path) as f:
        acids = [x.strip() for x in f.read().split(",") if x.strip()]
    print(f"Loaded {len(acids)} agent_call_ids")

    import boto3

    sm = boto3.client("secretsmanager", region_name="us-east-1")
    db = json.loads(sm.get_secret_value(SecretId="bridgeway/database")["SecretString"])
    os.environ["DATABASE_IP"] = db["DATABASE_IP"]
    os.environ["DATABASE_NAME"] = db["DATABASE_NAME"]
    os.environ["DATABASE_USERNAME_CHATKIT"] = db["DATABASE_USERNAME"]
    os.environ["DATABASE_PASSWORD_CHATKIT"] = db["DATABASE_PASSWORD"]
    os.environ.setdefault("DATABASE_PORT", "5432")
    os.environ["PGVECTOR_ENABLED"] = "true"

    ssm = boto3.client("ssm", region_name="us-east-1")
    for env_key, path, decrypt in [
        ("OPENAI_API_KEY", "/web-change-tracker/prod/openai_api_key", True),
        ("CHATKIT_INTERNAL_API_KEY", "/web-change-tracker/prod/chatkit_internal_api_key", True),
        ("BUBBLE_API_URL", "/web-change-tracker/prod/bubble_api_url", False),
        ("BUBBLE_API_KEY", "/web-change-tracker/prod/bubble_api_key", True),
    ]:
        kwargs = {"Name": path}
        if decrypt:
            kwargs["WithDecryption"] = True
        value = ssm.get_parameter(**kwargs)["Parameter"]["Value"]
        os.environ[env_key] = value

    os.environ.setdefault("CHATKIT_CONFIG_TABLE", "chatkit_production_config")
    os.environ.setdefault("CHANGELOG_BUCKET", "web-change-tracker-prod-artifacts-815039343351")
    os.environ.setdefault("BUBBLE_ARTIFACT_BUCKET", "web-change-tracker-prod-artifacts-815039343351")

    print("Secrets loaded in-process. Starting eval...")
    from eval.run_doc_eval import run as run_doc_eval
    run_doc_eval(agent_call_ids=acids)
    print("Done.")


if __name__ == "__main__":
    main()
