# NAIC Dashboard - Terraform

Deploys the dashboard to AWS: ECR, ECS Fargate (ARM64), ALB, CloudWatch logs, and IAM.

## Resources

| Resource | Description |
|----------|-------------|
| **ECR** | Repository for the dashboard Docker image |
| **ECS** | Cluster, Fargate task definition (ARM64), service |
| **ALB** | Application Load Balancer, target group, HTTP listener |
| **CloudWatch** | Log group for ECS (14-day retention) |
| **IAM** | Task execution role (ECR, logs), task role (S3 read/write, DynamoDB read) |
| **Security groups** | ALB (public 80), ECS (private, ALB only) |

## What the dashboard reads/writes

- **S3** — reads `alerts/alerts_table.jsonl` and `alerts/document_extractions_table.jsonl`; writes rerun accept patches and deletes rerun result objects
- **DynamoDB** (`chatkit_production_config`) — reads `chat:web-tracking-agent` and `chat:document-data-extraction` for dynamic column schema

No Bubble API credentials or SSM Bubble parameters are needed.

## Required environment variables (ECS task)

| Variable | Description |
|----------|-------------|
| `BUBBLE_ARTIFACT_BUCKET` | S3 bucket containing `alerts/alerts_table.jsonl` and `alerts/document_extractions_table.jsonl` |
| `AWS_REGION` | AWS region (default: `us-east-1`) |
| `CHATKIT_CONFIG_TABLE` | DynamoDB config table (default: `chatkit_production_config`) |

## Prerequisites

- Terraform >= 1.0
- AWS CLI configured (`AWS_PROFILE=bridgeway`)
- Existing VPC with public and private subnets

## Deploy

The recommended deploy path is via the project deploy script, which builds the image, pushes to ECR, and runs `terraform apply` automatically:

```bash
AWS_PROFILE=bridgeway ./scripts/deploy.sh
```

To run Terraform directly:

```bash
cd infra/terraform
terraform init
terraform plan
terraform apply
```

## Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `bubble_artifact_bucket` | S3 bucket name for alerts data | (required) |
| `vpc_id` | VPC ID | (required) |
| `public_subnet_ids` | Subnets for ALB (at least 2 across AZs) | (required) |
| `private_subnet_ids` | Subnets for ECS tasks | (required) |
| `ecr_repository_url` | ECR URL (omit to use created repo) | null |
| `ecr_image_tag` | Docker image tag | `latest` |

## Outputs

- `alb_dns_name` — ALB DNS name
- `alb_url` — Full URL (`http://…`)
- `ecr_repository_url` — ECR repo for pushing image
- `ecs_cluster_name` — ECS cluster
- `ecs_service_name` — ECS service

## Live infrastructure

- **ALB URL:** `http://naic-dashboard-alb-1818799928.us-east-1.elb.amazonaws.com`
- **ECS cluster:** `naic-dashboard-cluster`
- **ECS service:** `naic-dashboard-service`
- **AWS profile:** `bridgeway`
