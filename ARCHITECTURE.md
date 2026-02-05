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

- **State storage:** DynamoDB table (when `STATE_TABLE` is set) or local `state.json` for dev.
- **Change log:** S3 bucket (when `CHANGELOG_BUCKET` is set) for change events as JSON lines.
- **Target config:** `targets.json` must be available (baked into image/package, or fetched from S3).
- **Credentials:** IAM role for the Lambda or ECS task with DynamoDB, S3, and SES permissions.
- **Logging:** CloudWatch Logs for observability.

---

## Production Storage

**State backend:** Select via `STATE_BACKEND=local|dynamodb`. Default: `local` (state.json) for dev; `dynamodb` when `STATE_TABLE` is set.

**DynamoDB (state):** Set `STATE_TABLE` to use DynamoDB for latest per-target state. Table schema:

- Partition key: `target_id` (String)
- Attributes: `page_hash` (String), `extracted_json` (String, JSON-serialized)
- Billing: PAY_PER_REQUEST

**S3 (change log):** Set `CHANGELOG_BUCKET` to append change events as JSON lines. Path format:

- `CHANGELOG_PREFIX/YYYY/MM/DD/run-<epoch>.jsonl` (default prefix: `changelog/`)
- Each line is one change event (target + change or error)

**Run flow:** Load each target’s previous state from DynamoDB → scrape and extract → compute diffs (added/removed per resource type + hash changes) → save updated state back to DynamoDB → append change events to S3 at end of run.

---

## Environment Variables (for AWS runs)

| Variable | Purpose |
|----------|---------|
| `STATE_BACKEND` | `local` (state.json) or `dynamodb`; default: `local` if no STATE_TABLE, else `dynamodb` |
| `STATE_TABLE` | DynamoDB table name for per-target state (required when STATE_BACKEND=dynamodb) |
| `CHANGELOG_BUCKET` | S3 bucket for change events (JSONL append at end of run) |
| `CHANGELOG_PREFIX` | S3 key prefix (default: `changelog/`); path: `PREFIX/YYYY/MM/DD/run-<ts>.jsonl` |
| `AWS_REGION` | AWS region for DynamoDB, S3, SES |
| `TARGETS_FILE` | Path to targets JSON (default: `targets.json`); CLI `--targets-file` overrides |
| `TARGET_IDS` | Comma-separated target IDs to process; omit to process all; CLI `--target-ids` overrides |
| `TARGETS_S3_URI` | Optional: S3 URI to fetch targets.json instead of bundled file |
| `USE_PLAYWRIGHT` | `true` or `false`; set `false` in Lambda if not packaged |
| **Hardening** | |
| `MAX_RETRIES` | Fetch retries (default: 3) |
| `BACKOFF_SECONDS` | Initial backoff in seconds; doubles each retry (default: 2) |
| `DELAY_BETWEEN_PAGES` | Seconds to wait between targets (default: 1) |
| **Email (optional)** | |
| `SEND_EMAIL` | `true` or `false`; enable SES email when changes detected |
| `DRY_RUN` | `true` or `false`; if true, print subject/body only, do not send |
| `SES_REGION` | AWS region for SES (default: `us-east-1`) |
| `FROM_EMAIL` | Verified SES sender address |
| `TO_EMAILS` | Comma-separated recipient addresses |

---

## IAM Policy Snippets

**DynamoDB (state table):**

```json
{
  "Effect": "Allow",
  "Action": ["dynamodb:GetItem", "dynamodb:PutItem"],
  "Resource": "arn:aws:dynamodb:REGION:ACCOUNT:table/STATE_TABLE_NAME"
}
```

**S3 (change log bucket):**

```json
{
  "Effect": "Allow",
  "Action": ["s3:PutObject", "s3:AbortMultipartUpload"],
  "Resource": "arn:aws:s3:::CHANGELOG_BUCKET_NAME/CHANGELOG_PREFIX/*"
},
{
  "Effect": "Allow",
  "Action": ["s3:ListBucket"],
  "Resource": "arn:aws:s3:::CHANGELOG_BUCKET_NAME",
  "Condition": {
    "StringLike": { "s3:prefix": ["CHANGELOG_PREFIX/*"] }
  }
}
```

**SES (email):**

```json
{
  "Effect": "Allow",
  "Action": ["ses:SendEmail"],
  "Resource": "*"
}
```

---

## Terraform MVP Deployment

The `infra/` directory contains Terraform to deploy the scheduled ECS Fargate task.

### Deployed Flow

```
EventBridge (rate 6 hours) → RunTask → ECS Fargate task
       ↓
  Container: python spike.py
       ↓
  Load state from DynamoDB → fetch pages → extract → diff → save state to DynamoDB
       ↓
  Append change events to S3 (changelog/*.jsonl)
       ↓
  (Optional) Send email via SES when changes detected
       ↓
  Logs to CloudWatch
```

### Resources Created

| Resource | Purpose |
|----------|---------|
| **ECR repository** | Docker image for the app |
| **ECS cluster** | Fargate cluster |
| **ECS task definition** | Fargate task (execution role + task role) |
| **CloudWatch log group** | 14-day retention for task logs |
| **EventBridge rule** | Schedule (default: every 6 hours) |
| **DynamoDB table** | Optional; state per target_id |
| **S3 bucket** | Optional; changelog JSON lines |
| **Security group** | Optional; outbound HTTPS if none provided |

### Required Inputs

| Variable | Description |
|----------|-------------|
| `vpc_id` | VPC where the task runs (private subnets with NAT for outbound) |
| `private_subnet_ids` | Private subnet IDs for the task |

### Optional Inputs

| Variable | Description |
|----------|-------------|
| `security_group_ids` | Existing SGs; if empty, one is created with outbound allowed |
| `create_dynamodb_table` | Create state table (default: true) |
| `create_changelog_bucket` | Create S3 changelog bucket (default: true) |
| `existing_state_table_name` | Use existing DynamoDB table |
| `existing_changelog_bucket_name` | Use existing S3 bucket |
| `changelog_prefix` | S3 key prefix for changelog objects (default: changelog/) |
| `enable_ses` | Enable SES IAM + email env vars |
| `from_email`, `to_emails` | Email config (when enable_ses = true) |
| `schedule_expression` | e.g. `rate(6 hours)` or `cron(0 */6 * * ? *)` |
| `task_cpu`, `task_memory_mb` | Fargate sizing |

### Task Env Vars (from Terraform)

Non-secrets are passed via the task definition:

| Env Var | Source |
|---------|--------|
| `STATE_TABLE` | From Terraform (DynamoDB table name) |
| `CHANGELOG_BUCKET` | From Terraform (S3 bucket name) |
| `CHANGELOG_PREFIX` | From Terraform (default: changelog/) |
| `TARGETS_FILE` | Optional; defaults to `targets.json` |
| `TARGET_IDS` | Optional; comma-separated subset for partial runs (e.g. Step Functions payload) |
| `USE_PLAYWRIGHT` | `var.use_playwright` (default 1) |
| `MAX_RETRIES`, `BACKOFF_SECONDS`, `DELAY_BETWEEN_PAGES` | Terraform vars |
| `AWS_REGION` | `var.aws_region` |
| `SEND_EMAIL`, `FROM_EMAIL`, `TO_EMAILS`, `SES_REGION` | When `enable_ses = true` |

### Deploy Steps

1. **Configure networking:** Create `infra/terraform.tfvars` from `terraform.tfvars.example`; set `vpc_id` and `private_subnet_ids`.
2. **Apply Terraform:** `cd infra && terraform init && terraform plan && terraform apply`
3. **Build and push image:** `aws ecr get-login-password --region REGION | docker login --username AWS --password-stdin ECR_URL` then `docker build -t ECR_URL:latest . && docker push ECR_URL:latest`
4. **Trigger a run:** EventBridge will run on schedule, or run manually: `aws ecs run-task --cluster CLUSTER --task-definition TASK_DEF --launch-type FARGATE --network-configuration ...`

### Sample Production Run (env vars)

```bash
# Production run with DynamoDB + S3 changelog
export STATE_BACKEND=dynamodb
export STATE_TABLE=web-change-tracker-state
export CHANGELOG_BUCKET=my-app-changelog-123456789012
export CHANGELOG_PREFIX=changelog/
export AWS_REGION=us-east-1

python spike.py
```

Output: state per target in DynamoDB; change events (if any) appended to `s3://BUCKET/changelog/YYYY/MM/DD/run-<epoch>.jsonl`.
