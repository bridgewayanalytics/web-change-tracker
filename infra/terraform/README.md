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

```bash
terraform apply
```

### 4. Build and Push Docker Image

After `terraform apply`, push the app image to ECR. Use the same tag you deploy with (default: `latest`):

```bash
# From project root
AWS_REGION=$(terraform -chdir=infra/terraform output -raw region 2>/dev/null || echo "us-east-1")
ECR_URL=$(terraform -chdir=infra/terraform output -raw ecr_repository_url)

aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$ECR_URL"
docker build -t web-change-tracker .
IMAGE_TAG="${IMAGE_TAG:-latest}"
docker tag web-change-tracker:latest "$ECR_URL:$IMAGE_TAG"
docker push "$ECR_URL:$IMAGE_TAG"
```

To deploy a specific image tag:

```bash
terraform apply -var="image_tag=v1.2.3"
```

### 5. Run the Task Manually (Optional)

```bash
aws ecs run-task \
  --cluster $(terraform -chdir=infra/terraform output -raw ecs_cluster_name) \
  --task-definition $(terraform -chdir=infra/terraform output -raw task_definition_arn) \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[...],securityGroups=[...],assignPublicIp=ENABLED}"
```
(Use AWS Console ECS → Clusters → Run Task for easier manual runs.)

## Outputs

| Output | Description |
|--------|-------------|
| `ecr_repository_url` | ECR repo URL for Docker push |
| `ecs_cluster_name` | ECS cluster name |
| `task_definition_arn` | Task definition ARN |
| `scheduler_name` | EventBridge Scheduler name |
| `s3_bucket_name` | S3 artifacts bucket |
| `dynamodb_table_name` | DynamoDB state table |
| `targets_s3_uri` | S3 URI for targets.json |

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
