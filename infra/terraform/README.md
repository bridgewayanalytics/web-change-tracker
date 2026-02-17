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

### 7. Build and Push Docker Image

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
