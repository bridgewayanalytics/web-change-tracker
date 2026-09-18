# Production Dockerfile for web-change-tracker
# Uses official Playwright Python base with Chromium pre-installed for reliable ECS runs
# https://playwright.dev/python/docs/docker
#
# IMPORTANT: When bumping this version, also update playwright==X.Y.Z in requirements.txt
# to the matching version (same X.Y.Z). Dependabot opens grouped PRs that update both.
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code (use .dockerignore to exclude dev artifacts)
COPY . .
RUN chmod +x /app/scripts/entrypoint.sh

# Entrypoint: optionally fetch targets from S3, then run spike.py
ENTRYPOINT ["/app/scripts/entrypoint.sh"]
CMD ["python", "spike.py"]

# Document supported env vars for ECS task definitions
# State: STATE_BACKEND (local|dynamodb), STATE_TABLE (DynamoDB table; alias: DDB_TABLE)
# Storage: CHANGELOG_BUCKET, CHANGELOG_PREFIX (alias: S3_BUCKET → CHANGELOG_BUCKET)
# Targets: TARGETS_SOURCE (s3://bucket/key to fetch targets.json), TARGETS_FILE (default targets.json)
# Email: SEND_EMAIL, FROM_EMAIL, TO_EMAILS (alias: EMAIL_ENABLED, EMAIL_FROM, EMAIL_TO)
# AWS: AWS_REGION
ENV STATE_BACKEND=local \
    TARGETS_FILE=/app/targets.json \
    AWS_REGION=us-east-1
