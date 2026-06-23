"""
Pull 13 representative alert rows from alerts_table.jsonl, upload as a JSON
sample file to S3, and generate a 7-day presigned URL.

Usage:
    AWS_PROFILE=bridgeway python scripts/share_sample_rows.py
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

BUCKET = os.environ.get("CHANGELOG_BUCKET") or "web-change-tracker-prod-artifacts-815039343351"
ALERTS_KEY = "alerts/alerts_table.jsonl"
SAMPLE_KEY = "alerts/sample_rows.json"
SAMPLE_COUNT = 13
EXPIRY_SECONDS = 7 * 24 * 3600  # 7 days


def s3_client():
    import boto3
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def main():
    client = s3_client()

    print(f"Loading {ALERTS_KEY} from s3://{BUCKET} ...")
    body = client.get_object(Bucket=BUCKET, Key=ALERTS_KEY)["Body"].read().decode("utf-8")
    rows = []
    for line in body.splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    print(f"Total rows: {len(rows)}")

    # Take the most recent rows that have bubble_action set (richest structure),
    # fall back to any rows if not enough.
    rich = [r for r in rows if r.get("bubble_action")]
    sample = rich[-SAMPLE_COUNT:] if len(rich) >= SAMPLE_COUNT else rows[-SAMPLE_COUNT:]
    sample = list(reversed(sample))  # newest first
    print(f"Sample size: {len(sample)}")

    payload = json.dumps(sample, indent=2, ensure_ascii=False).encode("utf-8")
    client.put_object(
        Bucket=BUCKET,
        Key=SAMPLE_KEY,
        Body=payload,
        ContentType="application/json",
    )
    print(f"Uploaded to s3://{BUCKET}/{SAMPLE_KEY}")

    url = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET, "Key": SAMPLE_KEY},
        ExpiresIn=EXPIRY_SECONDS,
    )
    print(f"\nPresigned URL (expires in 7 days):\n{url}\n")


if __name__ == "__main__":
    main()
