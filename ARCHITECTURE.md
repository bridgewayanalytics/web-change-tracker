# Running web-change-tracker Every 6 Hours on AWS

This document describes how to run the change-detection pipeline on a schedule (e.g., every 6 hours) in AWS. Two options are available: **Lambda** (simpler, but Playwright may be heavy to package) and **ECS Fargate** (recommended if Playwright is required).

---

## High-Level Flow

```
Every 6 hours:
  EventBridge (schedule) → triggers → Lambda OR ECS Fargate
       ↓
  Runner executes spike.py: fetch pages → extract → diff → save state → (optional) send email
       ↓
  State stored in S3 or DynamoDB; report output to CloudWatch / SES
```

---

## Option 1: EventBridge → Lambda

**Use when:** You can package Playwright/Chromium in a Lambda layer or deployment package (≈50–150 MB compressed), or you rely on the `requests` fallback for simple pages.

### AWS Components

| Component | Purpose |
|-----------|---------|
| **EventBridge (CloudWatch Events)** | Scheduled rule: `rate(6 hours)` or cron `0 */6 * * ? *` (every 6 hours) |
| **Lambda function** | Runs the Python pipeline (`spike.py`); triggered by EventBridge |
| **Lambda deployment package** | Python code + dependencies (Playwright optional); or use a Lambda Layer for Playwright |
| **S3 bucket** | Stores state file (if using S3StateStore); optional: downloaded PDFs |
| **IAM role** | Grants Lambda permission to read/write S3, invoke SES (when email is added) |
| **CloudWatch Logs** | Log group for Lambda; logs all run output |

### How It Works

1. EventBridge fires every 6 hours.
2. EventBridge invokes the Lambda function.
3. Lambda runs the pipeline: loads `targets.json`, fetches each URL, extracts, diffs, saves state to S3.
4. Lambda writes logs to CloudWatch.

### Pros and Cons

| Pros | Cons |
|------|------|
| No servers to manage | Playwright + Chromium can exceed Lambda size limits (~250 MB unpacked) |
| Pay per invocation | Cold starts may add latency |
| Simple to set up | Need to package or layer Chromium for JS-rendered pages |

---

## Option 2: EventBridge → ECS Fargate (Recommended)

**Use when:** Playwright and Chromium are required for JavaScript-rendered pages. Fargate has no size limit for the container image.

### AWS Components

| Component | Purpose |
|-----------|---------|
| **EventBridge (CloudWatch Events)** | Scheduled rule: `rate(6 hours)` or cron `0 */6 * * ? *` |
| **ECS Cluster** | Logical grouping for Fargate tasks |
| **ECS Task Definition** | Defines the container image (Python + Playwright/Chromium), CPU, memory, env vars |
| **ECR repository** | Stores the Docker image used by the task |
| **ECS Task** | Runs the container; executes `python spike.py` and exits |
| **EventBridge target** | Invokes ECS RunTask API when the schedule fires |
| **S3 bucket** | Stores state file (S3StateStore); optional: downloaded PDFs |
| **IAM role** | Grants the task permission to read/write S3, SES |
| **CloudWatch Logs** | Log group for the ECS task; logs all run output |
| **VPC** | Fargate runs in a VPC; ensure outbound HTTPS for scraping |

### How It Works

1. EventBridge fires every 6 hours.
2. EventBridge calls the ECS RunTask API.
3. ECS Fargate starts a task using your container image.
4. The task runs `python spike.py`; fetches pages, extracts, diffs, saves state to S3.
5. Task exits; logs are in CloudWatch.

### Pros and Cons

| Pros | Cons |
|------|------|
| No size limit for Playwright/Chromium | Slightly more setup (Docker, ECR, ECS) |
| Full control over runtime environment | Minimal per-task cost (a few cents per run) |
| Easy to add more CPU/memory if needed | Need to maintain a Docker image |

---

## Summary: Which Option?

| If you need… | Choose |
|--------------|--------|
| Simple pages only (no JS rendering) | **Lambda** — use `requests` fallback |
| JavaScript-rendered pages (Playwright) | **ECS Fargate** |
| Fastest path to production | **ECS Fargate** — avoids Lambda packaging pain |

---

## Shared Requirements (Both Options)

- **State storage:** S3 bucket with a key like `web-change-tracker/state.json`, or DynamoDB table.
- **Target config:** `targets.json` must be available (baked into image/package, or fetched from S3).
- **Credentials:** IAM role for the Lambda or ECS task with S3 (and SES, when added) permissions.
- **Logging:** CloudWatch Logs for observability.

---

## Environment Variables (for AWS runs)

| Variable | Purpose |
|----------|---------|
| `STATE_S3_BUCKET` | S3 bucket name for state (when S3StateStore is implemented) |
| `STATE_S3_KEY` | Object key for state file (e.g. `web-change-tracker/state.json`) |
| `TARGETS_S3_URI` | Optional: S3 URI to fetch targets.json instead of bundled file |
| `USE_PLAYWRIGHT` | `true` or `false`; set `false` in Lambda if not packaged |
| **Email (optional)** | |
| `SEND_EMAIL` | `true` or `false`; enable SES email when changes detected |
| `DRY_RUN` | `true` or `false`; if true, print subject/body only, do not send |
| `SES_REGION` | AWS region for SES (default: `us-east-1`) |
| `FROM_EMAIL` | Verified SES sender address |
| `TO_EMAILS` | Comma-separated recipient addresses |
