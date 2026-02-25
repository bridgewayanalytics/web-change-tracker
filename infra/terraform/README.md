# Web Change Tracker – Terraform Infrastructure

MVP architecture: EventBridge Scheduler runs an ECS Fargate task every 6 hours. The task reads targets from S3, loads/saves state in DynamoDB, writes reports to S3, and sends email via SES.

## Prerequisites

- [Terraform](https://www.terraform.io/downloads) >= 1.0
- AWS CLI configured with credentials
- Docker image built and pushed to ECR (see below)

## Resources Created

| Resource | Purpose |
|----------|---------|
| ECR repository | Docker image for the app |
| S3 bucket | Artifacts (targets.json, changelog, reports); versioning enabled |
| DynamoDB table | State storage (keyed by `target_id`), PAY_PER_REQUEST |
| CloudWatch log group | Task logs |
| ECS cluster + task definition | Fargate task with Playwright |
| Security group | Outbound internet for web scraping |
| EventBridge Scheduler | Runs ECS task on schedule (default: rate(6 hours)) |

**Networking:** Uses the default VPC and its subnets via data sources. No manual VPC/subnet setup required. Tasks run in public subnets with `assign_public_ip = true` for outbound internet access.

## Apply Instructions

### 1. Create `terraform.tfvars`

```hcl
project_name = "web-change-tracker"
region       = "us-east-1"
environment  = "prod"

# Required: SES verified sender and recipients
email_from = "notifications@yourdomain.com"
email_to   = "alerts@yourdomain.com"

# Optional overrides
# image_tag           = "latest"
# schedule_expression = "rate(6 hours)"
# enable_scheduler    = true
# cpu                 = 512
# memory              = 1024
```

**Email:** Ensure `email_from` is verified in SES (sandbox or production). Recipients must be verified in SES sandbox.

**OpenAI (optional):** For AI enrichment, create SSM parameters before or after apply. No secrets are stored in Terraform.

```bash
aws ssm put-parameter --name "/web-change-tracker/prod/openai_api_key" \
  --value "sk-proj-..." --type "SecureString" --region us-east-1
aws ssm put-parameter --name "/web-change-tracker/prod/openai_model" \
  --value "gpt-5" --type "String" --region us-east-1
aws ssm put-parameter --name "/web-change-tracker/prod/openai_reasoning_effort" \
  --value "medium" --type "String" --region us-east-1
```

The ECS task role has `ssm:GetParameter` and `kms:Decrypt` (for `alias/aws/ssm`) scoped to these parameters.

**Bubble API (required for Bubble enrichment):** Stored in SSM; injected into the container via ECS task definition `secrets` (valueFrom). The task **execution** role can read them. Create before or after apply. Use **SecureString** for the API key. Local dev: use `.env` only (never commit; not used in ECS).

**BUBBLE_API_URL** must point to the Bubble Data API. The client accepts:
- **App root:** `https://your-app.bubbleapps.io` → requests go to `.../live/api/1.1/obj/tree` etc.
- **With version:** `https://your-app.bubbleapps.io/live` → same (no duplicate version).
- **Full obj base (recommended):** Set this to the **exact root URL shown in Bubble’s API settings** (Settings → API). Examples:
  - **Custom domain:** often `https://your-domain.com/api/1.1/obj` (no `/live`).
  - **Bubbleapps.io:** often `https://your-app.bubbleapps.io/live/api/1.1/obj`.

After updating SSM, **start a new ECS task** (secrets are read when the task starts). To test with curl, fetch the value and call the tree endpoint:

```bash
BUBBLE_API_URL=$(aws ssm get-parameter --region us-east-1 --name "/web-change-tracker/prod/bubble_api_url" --query "Parameter.Value" --output text)
BUBBLE_API_KEY=$(aws ssm get-parameter --with-decryption --region us-east-1 --name "/web-change-tracker/prod/bubble_api_key" --query "Parameter.Value" --output text)
echo "Base URL: $BUBBLE_API_URL"
curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $BUBBLE_API_KEY" "$BUBBLE_API_URL/tree?limit=1"
# Expect 200. If 404, try the other base (with or without /live) per Bubble’s API settings.
```

```bash
# Use the exact root URL from Bubble: Settings → API (Data API). Custom domains often use /api/1.1/obj with no /live.
REGION=us-east-1
# If your API settings show .../live/api/1.1/obj use that; if they show .../api/1.1/obj (no live) use that:
aws ssm put-parameter --name "/web-change-tracker/prod/bubble_api_url" \
  --value "https://your-app.bubbleapps.io/api/1.1/obj" \
  --type "String" \
  --region "$REGION"
# Or for custom domain without /live: https://art.bridgewayanalytics.com/api/1.1/obj
aws ssm put-parameter --name "/web-change-tracker/prod/bubble_api_key" \
  --value "your-bubble-data-api-key" \
  --type "SecureString" \
  --region "$REGION"
```

To update the API key later without redeploying:

```bash
aws ssm put-parameter --name "/web-change-tracker/prod/bubble_api_key" \
  --value "new-key" --type "SecureString" --overwrite --region us-east-1
```

**Bubble tree names:** The ECS task sets `BUBBLE_ORGANIZATION_TREE`, `BUBBLE_NAIC_GROUP_TREE`, and `BUBBLE_TYPE1_TREE` so they match the **exact Tree "Name"** in your Bubble app (e.g. `Organization`, `Resources Types`). If your app uses different names, override them in `main.tf` or via Terraform variables.

**Validate all Bubble API endpoints** (trees, tree nodes, calendar items, resources) from the repo root:

```bash
./scripts/validate_bubble_api.sh
```
Uses `BUBBLE_API_URL` and `BUBBLE_API_KEY` from env or SSM. Override tree names with `BUBBLE_ORGANIZATION_TREE` / `BUBBLE_TYPE1_TREE` if needed.

### 2. Initialize and Plan

```bash
cd infra/terraform
terraform init
terraform plan
```

### 3. Apply

Deploy with the current git commit as the image tag (recommended):

```bash
export TF_VAR_image_tag=$(git rev-parse --short HEAD)
terraform apply
```

Or use default `latest`:

```bash
terraform apply
```

(Vars are in `terraform.tfvars`; copy from `terraform.tfvars.example` and fill in `email_from`/`email_to` if needed.)

### 4. Verify Schedule

**AWS Console:** EventBridge → Schedulers → Schedules → `{project_name}-{environment}-schedule` (e.g. `web-change-tracker-prod-schedule`)

**CLI:**
```bash
aws scheduler get-schedule \
  --name $(terraform -chdir=infra/terraform output -raw scheduler_name) \
  --group-name default \
  --region $(terraform -chdir=infra/terraform output -raw region)
```

### 5. Run Task Manually (Run Now)

**AWS Console:** EventBridge → Schedulers → Schedules → select schedule → **Invoke** (or ECS → Clusters → `web-change-tracker-prod` → Run task)

**CLI:**
```bash
REGION=$(terraform -chdir=infra/terraform output -raw region)
CLUSTER=$(terraform -chdir=infra/terraform output -raw ecs_cluster_name)
TASK_DEF=$(terraform -chdir=infra/terraform output -raw task_definition_arn)
SUBNETS=$(terraform -chdir=infra/terraform output -json subnet_ids | jq -r 'join(",")')
SG=$(terraform -chdir=infra/terraform output -raw security_group_id)

aws ecs run-task --region $REGION --cluster $CLUSTER --task-definition $TASK_DEF \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=ENABLED}"
```

### 6. Logs

CloudWatch log group: `/ecs/{project_name}-{environment}` (e.g. `/ecs/web-change-tracker-prod`)

```bash
terraform -chdir=infra/terraform output -raw cloudwatch_log_group
```

**AWS Console:** CloudWatch → Log groups → `/ecs/web-change-tracker-prod`

### 7. Deploy script (build, push, apply)

From the repo root, use the deploy script to build the image, push to ECR, and run `terraform apply` with the new image tag:

```bash
./scripts/deploy.sh
# Optional: run one ECS task after apply (no ECS service; new task definition is used on next run)
./scripts/deploy.sh --run-task
# Custom tag
./scripts/deploy.sh --tag v1.2.3
```

### 8. Smoke test (run one task, tail logs, verify state/S3)

Run one ECS task on-demand, wait for it to stop, print CloudWatch logs, and verify DynamoDB table and S3 bucket (PASS/FAIL):

```bash
./scripts/smoke_test.sh
```

### 9. Build and Push Docker Image (manual)

After `terraform apply`, push the app image to ECR. Use the same tag you deploy with:

```bash
# From project root
export TF_VAR_image_tag=$(git rev-parse --short HEAD)
AWS_REGION=$(terraform -chdir=infra/terraform output -raw region 2>/dev/null || echo "us-east-1")
ECR_URL=$(terraform -chdir=infra/terraform output -raw ecr_repository_url)

aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$ECR_URL"
docker build -t web-change-tracker .
docker tag web-change-tracker:latest "$ECR_URL:$TF_VAR_image_tag"
docker push "$ECR_URL:$TF_VAR_image_tag"
terraform -chdir=infra/terraform apply -auto-approve
```

To deploy a specific image tag (e.g. `v1.2.3`):

```bash
terraform apply -var="image_tag=v1.2.3"
```

## Outputs

| Output | Description |
|--------|-------------|
| `ecr_repository_url` | ECR repo URL for Docker push |
| `deployed_image` | Full container image string deployed to ECS |
| `ecs_cluster_name` | ECS cluster name |
| `task_definition_arn` | Task definition ARN |
| `scheduler_name` | EventBridge Scheduler name |
| `scheduler_arn` | EventBridge Scheduler ARN |
| `cloudwatch_log_group` | Log group for task output |
| `s3_bucket_name` | S3 artifacts bucket |
| `dynamodb_table_name` | DynamoDB state table |
| `targets_s3_uri` | S3 URI for targets.json |
| `subnet_ids` | Subnets for manual run-task |
| `security_group_id` | Security group for ECS task |

## Updating targets.json

The Terraform uploads `targets.json` from the repo root at apply time. After changing `targets.json`:

```bash
terraform apply
# Or upload manually:
aws s3 cp targets.json s3://$(terraform -chdir=infra/terraform output -raw s3_bucket_name)/targets/targets.json
```

## Variables Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `project_name` | No | `web-change-tracker` | Resource name prefix |
| `region` | No | `us-east-1` | AWS region |
| `environment` | No | `prod` | Environment tag |
| `email_from` | **Yes** | - | SES verified sender |
| `email_to` | **Yes** | - | Comma-separated recipients |
| `schedule_expression` | No | `rate(6 hours)` | EventBridge schedule |
| `enable_scheduler` | No | `true` | Enable scheduled runs |
| `image_tag` | No | `latest` | Docker image tag (e.g. `terraform apply -var="image_tag=v1.0.0"`) |
| `vpc_id` | No | default VPC | Override VPC |
| `subnet_ids` | No | default subnets | Override subnets |
| `cpu` | No | `512` | Fargate CPU units |
| `memory` | No | `1024` | Fargate memory MB |

---

## Deploy Contract

Exact variable names and how they are passed. All behavior is driven by **tfvars** (or `-var` / `TF_VAR_*`); no Terraform workspaces are used.

### How variables are passed

- **Primary:** `terraform.tfvars` (or `terraform.tfvars.json`) in `infra/terraform/`. Copy from `terraform.tfvars.example`.
- **Override at apply:** `terraform apply -var="image_tag=abc123"` or `terraform apply -var-file=prod.tfvars`.
- **Env:** `TF_VAR_<name>` (e.g. `export TF_VAR_image_tag=$(git rev-parse --short HEAD)`). Env overrides tfvars when both are set; CLI `-var` overrides env.

### Terraform variables (exact names + example values)

| Variable | Example value | Passed via |
|----------|----------------|------------|
| `project_name` | `web-change-tracker` | tfvars |
| `region` | `us-east-1` | tfvars |
| `environment` | `prod` | tfvars |
| `email_from` | `notifications@yourdomain.com` | tfvars (required) |
| `email_to` | `alerts@yourdomain.com` | tfvars (required) |
| `email_subject_prefix` | `[Web Change Report]` | tfvars |
| `schedule_expression` | `rate(6 hours)` | tfvars |
| `schedule_expression_timezone` | `America/New_York` | tfvars |
| `enable_scheduler` | `true` | tfvars |
| `vpc_id` | `null` | tfvars |
| `subnet_ids` | `null` | tfvars |
| `image_tag` | `latest` or `abc1234` | tfvars / `TF_VAR_image_tag` / `-var=image_tag=...` |
| `cpu` | `512` | tfvars |
| `memory` | `1024` | tfvars |

### Container image in task definition

- **Reference:** `image = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"`
- **Type:** Variable tag; repository URL comes from Terraform resource `aws_ecr_repository.app`.
- **Example resolved:** `123456789012.dkr.ecr.us-east-1.amazonaws.com/web-change-tracker-prod:latest` (or `:abc1234` when `image_tag` is set).
- No digest pinning; tag is mutable. Use a specific tag (e.g. git SHA) for reproducible deploys.

### ECS task command / entrypoint

- **Entrypoint:** `ENTRYPOINT ["/app/scripts/entrypoint.sh"]` (image default). Fetches `targets.json` from S3 when `TARGETS_SOURCE` is set.
- **Command override (prod):** Task definition sets `command = ["python", "spike.py", "--bubble-enrich", "--bubble-report", "--emit-bubble-json"]` so prod always runs in intended mode. No `--no-dry-run-bubble`, so Bubble write API is never called.

### Environment variables injected into the task (plaintext)

| Env var | Example / source | Notes |
|---------|------------------|--------|
| `STATE_BACKEND` | `dynamodb` | Plaintext |
| `STATE_TABLE` | `web-change-tracker-prod-state` | From Terraform resource |
| `CHANGELOG_BUCKET` | `web-change-tracker-prod-artifacts-123456789012` | S3 bucket id |
| `CHANGELOG_PREFIX` | `changelog/` | Plaintext |
| `TARGETS_SOURCE` | `s3://...-artifacts-.../targets/targets.json` | From Terraform local |
| `TARGETS_FILE` | `/app/targets.json` | Plaintext |
| `EMAIL_ENABLED` | `true` | Plaintext |
| `FROM_EMAIL` | From `var.email_from` | tfvars |
| `TO_EMAILS` | From `var.email_to` | tfvars |
| `EMAIL_SUBJECT_PREFIX` | From `var.email_subject_prefix` | tfvars |
| `ENVIRONMENT` | `prod` | Plaintext |
| `SES_REGION` | From `var.region` | tfvars |
| `AWS_REGION` | From `var.region` | tfvars |
| `OPENAI_ENABLED` | `true` | Plaintext |
| `OPENAI_API_KEY_SSM_PARAM` | `/web-change-tracker/prod/openai_api_key` | Param name only; app fetches value at runtime |
| `OPENAI_MODEL_SSM_PARAM` | `/web-change-tracker/prod/openai_model` | Param name only |
| `OPENAI_REASONING_EFFORT_SSM_PARAM` | `/web-change-tracker/prod/openai_reasoning_effort` | Param name only |
| `OPENAI_ENRICH_ONLY_IF_CHANGED` | `true` | Plaintext |
| `OPENAI_ENRICH_MAX_RESOURCES` | `25` | Plaintext |
| `OPENAI_ENRICH_MAX_EVENTS` | `10` | Plaintext |
| `PROD_OBSERVE_MODE` | `true` | RunSpec: validate bubble_enrich, refs blocked, artifacts, dry-run |
| `AI_ENRICHMENT_ENABLED` | `true` | Enables bubble enrich path |
| `ARTIFACT_OUTPUT_DIR` | `debug` | Debug artifacts dir |
| `AI_REFERENCE_FIELDS_BLOCKED` | `true` | AI must not write reference fields |
| `RUN_SPEC_VALIDATION_FAIL_FAST` | `true` | Exit on RunSpec validation failure |

### Secrets (valueFrom SSM) — never logged

Bubble credentials are injected as env vars via the task definition **secrets** block (valueFrom). The task **execution** role has `ssm:GetParameter` and `kms:Decrypt` for these parameters. The app must never log these values.

| Env var | SSM parameter path | Type |
|---------|--------------------|------|
| `BUBBLE_API_URL` | `/web-change-tracker/prod/bubble_api_url` | String |
| `BUBBLE_API_KEY` | `/web-change-tracker/prod/bubble_api_key` | **SecureString** |

Create them with the AWS CLI commands in the "Bubble API" section above. Local dev: use `.env` only (not used in ECS; do not commit).

**OpenAI:** The task **role** (not execution role) has `ssm:GetParameter` on the three OpenAI param ARNs. The app (e.g. `bubble/ssm_loader.py`) fetches those values at runtime; they are not injected by ECS.

---

## Final ECS command and env vars (prod)

What the scheduled task actually runs and which env vars it receives.

### Final ECS command

```
/app/scripts/entrypoint.sh python spike.py --bubble-enrich --bubble-report --emit-bubble-json
```

- Entrypoint runs first (fetches targets from S3 if `TARGETS_SOURCE` is set), then execs the command below.
- **CLI flags:** `--bubble-enrich` (reference enrichment), `--bubble-report` (Bubble format in report/email), `--emit-bubble-json` (write payloads to JSON). No `--no-dry-run-bubble`, so Bubble write API is never called.

### Env vars list (all names; secrets from SSM)

| Name | Source / example value |
|------|------------------------|
| `STATE_BACKEND` | `dynamodb` |
| `STATE_TABLE` | `{project}-{env}-state` |
| `CHANGELOG_BUCKET` | artifacts bucket id |
| `CHANGELOG_PREFIX` | `changelog/` |
| `TARGETS_SOURCE` | `s3://{bucket}/targets/targets.json` |
| `TARGETS_FILE` | `/app/targets.json` |
| `EMAIL_ENABLED` | `true` |
| `FROM_EMAIL` | tfvars `email_from` |
| `TO_EMAILS` | tfvars `email_to` |
| `EMAIL_SUBJECT_PREFIX` | tfvars `email_subject_prefix` |
| `ENVIRONMENT` | `prod` |
| `SES_REGION` | tfvars `region` |
| `AWS_REGION` | tfvars `region` |
| `OPENAI_ENABLED` | `true` |
| `OPENAI_API_KEY_SSM_PARAM` | `/web-change-tracker/prod/openai_api_key` |
| `OPENAI_MODEL_SSM_PARAM` | `/web-change-tracker/prod/openai_model` |
| `OPENAI_REASONING_EFFORT_SSM_PARAM` | `/web-change-tracker/prod/openai_reasoning_effort` |
| `OPENAI_ENRICH_ONLY_IF_CHANGED` | `true` |
| `OPENAI_ENRICH_MAX_RESOURCES` | `25` |
| `OPENAI_ENRICH_MAX_EVENTS` | `10` |
| `PROD_OBSERVE_MODE` | `true` |
| `AI_ENRICHMENT_ENABLED` | `true` |
| `ARTIFACT_OUTPUT_DIR` | `debug` |
| `AI_REFERENCE_FIELDS_BLOCKED` | `true` |
| `RUN_SPEC_VALIDATION_FAIL_FAST` | `true` |
| `BUBBLE_API_URL` | **Secrets (valueFrom SSM)** `/web-change-tracker/prod/bubble_api_url` |
| `BUBBLE_API_KEY` | **Secrets (valueFrom SSM)** `/web-change-tracker/prod/bubble_api_key` (SecureString) |
