#!/usr/bin/env bash
# Build Docker image, push to ECR, run terraform apply with new image tag, optionally run one task.
# Run from repo root. Requires: docker, aws cli, terraform. Optional --run-task uses python3 to parse Terraform JSON.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TERRAFORM_DIR="${TERRAFORM_DIR:-$REPO_ROOT/infra/terraform}"
IMAGE_TAG="${IMAGE_TAG:-$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo "latest")}"
RUN_TASK_AFTER="${RUN_TASK_AFTER:-false}"

usage() {
  echo "Usage: $0 [--run-task] [--tag TAG]"
  echo "  --run-task   After apply, run one ECS task (no ECS service; forces use of new image)"
  echo "  --tag TAG    Image tag (default: git short SHA)"
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-task) RUN_TASK_AFTER=true; shift ;;
    --tag)       IMAGE_TAG="$2"; shift 2 ;;
    -h|--help)   usage ;;
    *)           echo "Unknown option: $1"; usage ;;
  esac
done

cd "$REPO_ROOT"

echo "==> Resolving Terraform outputs (region, ECR URL)..."
REGION=$(terraform -chdir="$TERRAFORM_DIR" output -raw region)
ECR_URL=$(terraform -chdir="$TERRAFORM_DIR" output -raw ecr_repository_url)

if [[ -z "${REGION:-}" || -z "${ECR_URL:-}" ]]; then
  echo "ERROR: Terraform outputs missing. Run 'terraform apply' once in $TERRAFORM_DIR to create ECR and outputs."
  exit 1
fi

echo "==> Building bubble sync Lambda package..."
LAMBDA_BUILD=$(mktemp -d)
trap "rm -rf $LAMBDA_BUILD" EXIT
pip3 install requests -t "$LAMBDA_BUILD" -q --disable-pip-version-check
cp -r bubble storage "$LAMBDA_BUILD/"
cp infra/lambda/bubble_sync/handler.py "$LAMBDA_BUILD/"
(cd "$LAMBDA_BUILD" && zip -r "$REPO_ROOT/infra/lambda/bubble_sync.zip" . -q)
echo "    Lambda zip: infra/lambda/bubble_sync.zip ($(du -sh "$REPO_ROOT/infra/lambda/bubble_sync.zip" | cut -f1))"

echo "==> Building Docker image (tag=$IMAGE_TAG)..."
echo "    (Image must include spike.py with --bubble-enrich, --bubble-report, --emit-bubble-json support.)"
docker build -t "${ECR_URL}:${IMAGE_TAG}" .

echo "==> Logging in to ECR..."
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$ECR_URL"

echo "==> Pushing to ECR..."
docker push "${ECR_URL}:${IMAGE_TAG}"

echo "==> Terraform apply with image_tag=$IMAGE_TAG..."
terraform -chdir="$TERRAFORM_DIR" apply -var="image_tag=$IMAGE_TAG" -auto-approve

echo "==> Deploy complete. Image: ${ECR_URL}:${IMAGE_TAG}"

if [[ "$RUN_TASK_AFTER" == "true" ]]; then
  echo "==> Running one ECS task (no service; this uses the new task definition)..."
  CLUSTER=$(terraform -chdir="$TERRAFORM_DIR" output -raw ecs_cluster_name)
  TASK_DEF=$(terraform -chdir="$TERRAFORM_DIR" output -raw task_definition_arn)
  SUBNETS=$(terraform -chdir="$TERRAFORM_DIR" output -json subnet_ids | python3 -c "import sys,json; print(','.join(json.load(sys.stdin)))")
  SG=$(terraform -chdir="$TERRAFORM_DIR" output -raw security_group_id)
  TASK_ARN=$(aws ecs run-task --region "$REGION" --cluster "$CLUSTER" --task-definition "$TASK_DEF" \
    --launch-type FARGATE \
    --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=ENABLED}" \
    --query 'tasks[0].taskArn' --output text)
  echo "    Task started: $TASK_ARN"
  echo "    Tail logs: scripts/smoke_test.sh (or CloudWatch log group from terraform output cloudwatch_log_group)"
fi
