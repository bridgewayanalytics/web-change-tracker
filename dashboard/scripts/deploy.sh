#!/usr/bin/env bash
set -euo pipefail

# Deploy NAIC Dashboard: build Docker, push ECR, terraform apply, restart ECS
# Usage: ./scripts/deploy.sh [--tag <tag>] [--skip-build] [--skip-terraform]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# --- Config ---
# Local: set AWS_PROFILE=bridgeway (or your profile). CI: leave AWS_PROFILE unset (OIDC credentials via env vars).
AWS_REGION="us-east-1"
ECR_REPO="naic-dashboard"
ECS_CLUSTER="naic-dashboard-cluster"
ECS_SERVICE="naic-dashboard-service"
TAG="latest"
SKIP_BUILD=false
SKIP_TERRAFORM=false

while [[ $# -gt 0 ]]; do
  case $1 in
    --tag)        TAG="$2"; shift 2 ;;
    --skip-build) SKIP_BUILD=true; shift ;;
    --skip-terraform) SKIP_TERRAFORM=true; shift ;;
    -h|--help)
      echo "Usage: ./scripts/deploy.sh [--tag <tag>] [--skip-build] [--skip-terraform]"
      echo ""
      echo "  --tag <tag>        Docker image tag (default: latest)"
      echo "  --skip-build       Skip Docker build, just push existing image"
      echo "  --skip-terraform   Skip terraform apply, just rebuild and restart ECS"
      exit 0
      ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# Build optional --profile args (empty array in CI where OIDC provides credentials)
if [[ -n "${AWS_PROFILE:-}" ]]; then
  AWS_ARGS=(--profile "$AWS_PROFILE")
else
  AWS_ARGS=()
fi

ACCOUNT_ID=$(aws "${AWS_ARGS[@]}" sts get-caller-identity --query Account --output text 2>/dev/null) || {
  echo "ERROR: AWS credentials not available."
  [[ -n "${AWS_PROFILE:-}" ]] && echo "  Run: aws sso login --profile $AWS_PROFILE"
  exit 1
}
ECR_URL="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}"

echo "=== NAIC Dashboard Deploy ==="
echo "Account:   $ACCOUNT_ID"
echo "ECR:       $ECR_URL:$TAG"
echo "Cluster:   $ECS_CLUSTER"
echo ""

# --- 1. Docker build ---
if [[ "$SKIP_BUILD" == "false" ]]; then
  echo ">>> Building Docker image (linux/arm64)..."
  cd "$ROOT_DIR"
  docker buildx build --platform linux/arm64 --load -t "${ECR_REPO}:${TAG}" .
  echo ""
else
  echo ">>> Skipping Docker build (--skip-build)"
fi

# --- 2. Push to ECR ---
echo ">>> Logging into ECR..."
aws "${AWS_ARGS[@]}" ecr get-login-password --region "$AWS_REGION" | \
  docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

echo ">>> Pushing image to ECR..."
docker tag "${ECR_REPO}:${TAG}" "${ECR_URL}:${TAG}"
docker push "${ECR_URL}:${TAG}"
if [[ "$TAG" != "latest" ]]; then
  docker tag "${ECR_REPO}:${TAG}" "${ECR_URL}:latest"
  docker push "${ECR_URL}:latest"
fi
echo ""

# --- 3. Terraform ---
if [[ "$SKIP_TERRAFORM" == "false" ]]; then
  echo ">>> Running Terraform..."
  cd "$ROOT_DIR/infra/terraform"
  terraform apply -input=false -auto-approve
  echo ""
else
  echo ">>> Skipping Terraform (--skip-terraform)"
fi

# --- 4. Force new ECS deployment ---
echo ">>> Forcing new ECS deployment..."
aws "${AWS_ARGS[@]}" ecs update-service \
  --cluster "$ECS_CLUSTER" \
  --service "$ECS_SERVICE" \
  --force-new-deployment \
  --query 'service.status' --output text

# --- 5. Wait for healthy ---
echo ">>> Waiting for new task to become healthy..."
TG_ARN=$(aws "${AWS_ARGS[@]}" elbv2 describe-target-groups \
  --names naic-dashboard-tg \
  --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null || echo "")

MAX_WAIT=180
WAITED=0
while [[ $WAITED -lt $MAX_WAIT ]]; do
  RUNNING=$(aws "${AWS_ARGS[@]}" ecs describe-services \
    --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" \
    --query 'services[0].runningCount' --output text 2>/dev/null || echo "0")

  HEALTH="unknown"
  if [[ -n "$TG_ARN" ]]; then
    HEALTH=$(aws "${AWS_ARGS[@]}" elbv2 describe-target-health \
      --target-group-arn "$TG_ARN" \
      --query 'TargetHealthDescriptions[0].TargetHealth.State' --output text 2>/dev/null || echo "unknown")
  fi

  if [[ "$RUNNING" == "1" && "$HEALTH" == "healthy" ]]; then
    echo ""
    break
  fi

  printf "\r  running=%s health=%s (%ds/%ds)" "$RUNNING" "$HEALTH" "$WAITED" "$MAX_WAIT"
  sleep 10
  WAITED=$((WAITED + 10))
done

if [[ $WAITED -ge $MAX_WAIT ]]; then
  echo ""
  echo "WARNING: Timed out waiting for healthy task. Check ECS console."
  echo "  aws ecs describe-services --cluster $ECS_CLUSTER --services $ECS_SERVICE"
  exit 1
fi

# --- Done ---
ALB_URL=$(cd "$ROOT_DIR/infra/terraform" && terraform output -raw alb_url 2>/dev/null || echo "N/A")
echo "=== Deploy complete ==="
echo "Dashboard: $ALB_URL"
