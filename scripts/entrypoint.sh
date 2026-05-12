#!/usr/bin/env bash
# Entrypoint for web-change-tracker Docker image.
# - Optionally downloads targets.json from S3 when TARGETS_SOURCE starts with s3://
# - Maps common env aliases (DDB_TABLE, S3_BUCKET, EMAIL_*) to app vars
# - Executes python spike.py with any passed args

set -e

# Map env aliases to app-native vars (if native var not already set)
[[ -n "${DDB_TABLE:-}" && -z "${STATE_TABLE:-}" ]] && export STATE_TABLE="$DDB_TABLE"
[[ -n "${S3_BUCKET:-}" && -z "${CHANGELOG_BUCKET:-}" ]] && export CHANGELOG_BUCKET="$S3_BUCKET"
[[ -n "${EMAIL_FROM:-}" ]] && export FROM_EMAIL="$EMAIL_FROM"
[[ -n "${EMAIL_TO:-}" ]] && export TO_EMAILS="$EMAIL_TO"

# Download targets.json from S3 if TARGETS_SOURCE is an s3:// URI
if [[ -n "${TARGETS_SOURCE:-}" && "${TARGETS_SOURCE}" == s3://* ]]; then
  echo "Fetching targets from ${TARGETS_SOURCE}..."
  python -c "
import os, re, boto3
from pathlib import Path
uri = os.environ.get('TARGETS_SOURCE', '')
m = re.match(r's3://([^/]+)/(.+)', uri)
if m:
    bucket, key = m.group(1), m.group(2)
    Path('/app/targets.json').write_bytes(boto3.client('s3').get_object(Bucket=bucket, Key=key)['Body'].read())
"
  export TARGETS_FILE=/app/targets.json
fi

# Run spike.py: if CMD/args look like a full command (e.g. "python spike.py"), exec as-is;
# otherwise treat args as spike.py arguments (e.g. docker run image --verbose)
if [[ $# -eq 0 ]]; then
  exec python spike.py
elif [[ "$1" == python ]]; then
  # Full python command (e.g. "python spike.py", "python scripts/backfill_document_extractions.py")
  exec "$@"
else
  exec python spike.py "$@"
fi
