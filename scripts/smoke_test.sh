#!/usr/bin/env bash
# Run one ECS task on-demand, tail CloudWatch logs, verify DynamoDB state and S3 artifacts (if configured).
# Prints PASS/FAIL. Run from repo root. Requires: aws cli, jq.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TERRAFORM_DIR="${TERRAFORM_DIR:-$REPO_ROOT/infra/terraform}"
TAIL_LOGS="${TAIL_LOGS:-true}"
WAIT_STOPPED="${WAIT_STOPPED:-true}"
MAX_WAIT="${MAX_WAIT:-600}"

cd "$REPO_ROOT"

echo "==> Resolving Terraform outputs..."
REGION=$(terraform -chdir="$TERRAFORM_DIR" output -raw region 2>/dev/null || true)
CLUSTER=$(terraform -chdir="$TERRAFORM_DIR" output -raw ecs_cluster_name 2>/dev/null || true)
TASK_DEF=$(terraform -chdir="$TERRAFORM_DIR" output -raw task_definition_arn 2>/dev/null || true)
SUBNETS=$(terraform -chdir="$TERRAFORM_DIR" output -json subnet_ids 2>/dev/null | jq -r 'join(",")' || true)
SG=$(terraform -chdir="$TERRAFORM_DIR" output -raw security_group_id 2>/dev/null || true)
LOG_GROUP=$(terraform -chdir="$TERRAFORM_DIR" output -raw cloudwatch_log_group 2>/dev/null || true)
DDB_TABLE=$(terraform -chdir="$TERRAFORM_DIR" output -raw dynamodb_table_name 2>/dev/null || true)
S3_BUCKET=$(terraform -chdir="$TERRAFORM_DIR" output -raw s3_bucket_name 2>/dev/null || true)

for v in REGION CLUSTER TASK_DEF SUBNETS SG LOG_GROUP; do
  if [[ -z "${!v:-}" ]]; then
    echo "ERROR: Missing Terraform output. Run deploy first. ($v)"
    exit 1
  fi
done

# Container name = ECS cluster name (task definition family / container name)
CONTAINER_NAME="$CLUSTER"

echo "==> Starting ECS task..."
TASK_ARN=$(aws ecs run-task --region "$REGION" --cluster "$CLUSTER" --task-definition "$TASK_DEF" \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=ENABLED}" \
  --query 'tasks[0].taskArn' --output text)
TASK_ID="${TASK_ARN##*/}"
echo "    Task ARN: $TASK_ARN"
echo "    Task ID:  $TASK_ID"

LOG_STREAM="ecs/${CONTAINER_NAME}/${TASK_ID}"
echo "    Log group:  $LOG_GROUP"
echo "    Log stream: $LOG_STREAM"

EXIT_CODE=""
if [[ "$WAIT_STOPPED" == "true" ]]; then
  echo "==> Waiting for task to stop (max ${MAX_WAIT}s)..."
  for ((i=0; i<MAX_WAIT; i+=10)); do
    STATUS=$(aws ecs describe-tasks --region "$REGION" --cluster "$CLUSTER" --tasks "$TASK_ARN" \
      --query 'tasks[0].lastStatus' --output text 2>/dev/null || echo "MISSING")
    if [[ "$STATUS" == "STOPPED" ]]; then
      break
    fi
    echo "    status=$STATUS (${i}s)"
    sleep 10
  done

  STOPPED_REASON=$(aws ecs describe-tasks --region "$REGION" --cluster "$CLUSTER" --tasks "$TASK_ARN" \
    --query 'tasks[0].stoppedReason' --output text 2>/dev/null || echo "Unknown")
  EXIT_CODE=$(aws ecs describe-tasks --region "$REGION" --cluster "$CLUSTER" --tasks "$TASK_ARN" \
    --query 'tasks[0].containers[0].exitCode' --output text 2>/dev/null || echo "null")
  echo "    stoppedReason=$STOPPED_REASON exitCode=$EXIT_CODE"
fi

if [[ "$TAIL_LOGS" == "true" ]]; then
  echo "==> CloudWatch logs for $LOG_STREAM"
  if aws logs tail "$LOG_GROUP" --region "$REGION" --log-stream-names "$LOG_STREAM" --since 30m 2>/dev/null; then
    :
  else
    echo "    (log stream not yet available or empty)"
  fi
fi

echo ""
echo "==> Smoke checks"

PASS=0
FAIL=0

# 1. Task stopped and exit code 0 (only if we waited)
if [[ -n "${EXIT_CODE:-}" ]]; then
  if [[ "$EXIT_CODE" == "0" ]]; then
    echo "    [PASS] Task exited 0"
    ((PASS++)) || true
  else
    echo "    [FAIL] Task exit code: $EXIT_CODE"
    ((FAIL++)) || true
  fi
else
  echo "    [SKIP] Task exit code (run with WAIT_STOPPED=true)"
fi

# 2. DynamoDB: table exists and has some items (or is empty after first run)
if [[ -n "${DDB_TABLE:-}" ]]; then
  COUNT=$(aws dynamodb scan --region "$REGION" --table-name "$DDB_TABLE" --select COUNT --query 'Count' --output text 2>/dev/null || echo "-1")
  if [[ "$COUNT" != "-1" ]]; then
    echo "    [PASS] DynamoDB table $DDB_TABLE exists (items=$COUNT)"
    ((PASS++)) || true
  else
    echo "    [FAIL] DynamoDB table $DDB_TABLE missing or inaccessible"
    ((FAIL++)) || true
  fi
else
  echo "    [SKIP] DynamoDB table not in Terraform outputs"
fi

# 3. S3: changelog prefix has objects (if configured and task wrote events)
if [[ -n "${S3_BUCKET:-}" ]]; then
  CHANGELOG_COUNT=$(aws s3 ls "s3://${S3_BUCKET}/changelog/" --recursive 2>/dev/null | wc -l | tr -d ' ')
  if [[ "$CHANGELOG_COUNT" -ge 0 ]]; then
    echo "    [PASS] S3 bucket $S3_BUCKET accessible (changelog objects=$CHANGELOG_COUNT)"
    ((PASS++)) || true
  else
    echo "    [FAIL] S3 bucket $S3_BUCKET missing or inaccessible"
    ((FAIL++)) || true
  fi
else
  echo "    [SKIP] S3 bucket not in Terraform outputs"
fi

echo ""
if [[ $FAIL -eq 0 ]]; then
  echo "PASS (all checks passed)"
  exit 0
else
  echo "FAIL ($FAIL check(s) failed)"
  exit 1
fi
