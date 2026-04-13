# -----------------------------------------------------------------------------
# Data sources: default VPC + subnets (no manual networking required)
# -----------------------------------------------------------------------------

data "aws_caller_identity" "current" {}

data "aws_vpc" "default" {
  count   = var.vpc_id == null ? 1 : 0
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [local.vpc_id]
  }
}

locals {
  name    = "${var.project_name}-${var.environment}"
  vpc_id  = var.vpc_id != null ? var.vpc_id : data.aws_vpc.default[0].id
  subnets = coalesce(var.subnet_ids, data.aws_subnets.default.ids)

  # SSM parameter names for OpenAI (used for IAM and env)
  openai_api_key_param = "/web-change-tracker/prod/openai_api_key"
  openai_model_param   = "/web-change-tracker/prod/openai_model"
  openai_effort_param  = "/web-change-tracker/prod/openai_reasoning_effort"

  # SSM parameter ARNs for IAM (trim leading slash for ARN path)
  openai_ssm_param_arns = [
    "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${local.openai_api_key_param}",
    "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${local.openai_model_param}",
    "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${local.openai_effort_param}",
  ]

  # DB credential SSM parameter names (loaded at runtime by ssm_loader.load_db_env_from_ssm)
  # Populate these values in SSM before enabling PGVECTOR_ENABLED=true
  db_ip_param       = "/web-change-tracker/prod/database_ip"
  db_name_param     = "/web-change-tracker/prod/database_name"
  db_port_param     = "/web-change-tracker/prod/database_port"
  db_user_param     = "/web-change-tracker/prod/database_username_chatkit"
  db_password_param = "/web-change-tracker/prod/database_password_chatkit"

  db_ssm_param_arns = [
    "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${local.db_ip_param}",
    "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${local.db_name_param}",
    "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${local.db_port_param}",
    "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${local.db_user_param}",
    "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${local.db_password_param}",
  ]

  # Bubble API: injected via ECS secrets (valueFrom); execution role needs SSM + KMS
  bubble_api_url_param = "/web-change-tracker/prod/bubble_api_url"
  bubble_api_key_param = "/web-change-tracker/prod/bubble_api_key"
  bubble_ssm_param_arns = [
    "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${local.bubble_api_url_param}",
    "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${local.bubble_api_key_param}",
  ]
}

# -----------------------------------------------------------------------------
# ECR repository
# -----------------------------------------------------------------------------

resource "aws_ecr_repository" "app" {
  name                 = local.name
  image_tag_mutability = "MUTABLE"
  force_delete         = true
}

# -----------------------------------------------------------------------------
# S3 bucket for artifacts (versioning enabled)
# Targets, changelog, and reports
# -----------------------------------------------------------------------------

resource "aws_s3_bucket" "artifacts" {
  bucket = "${local.name}-artifacts-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  versioning_configuration {
    status = "Enabled"
  }
}

# Upload targets.json from repo
resource "aws_s3_object" "targets" {
  bucket  = aws_s3_bucket.artifacts.id
  key     = "targets/targets.json"
  content = file("${path.module}/../../targets.json")
  etag    = filemd5("${path.module}/../../targets.json")
}

locals {
  targets_s3_uri = "s3://${aws_s3_bucket.artifacts.id}/${aws_s3_object.targets.key}"
}

# -----------------------------------------------------------------------------
# DynamoDB table (state, keyed by target_id)
# -----------------------------------------------------------------------------

resource "aws_dynamodb_table" "state" {
  name         = "${local.name}-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "target_id"

  attribute {
    name = "target_id"
    type = "S"
  }
}

# -----------------------------------------------------------------------------
# CloudWatch log group
# -----------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${local.name}"
  retention_in_days = 14
}

# -----------------------------------------------------------------------------
# Security group (outbound internet for fetching websites)
# -----------------------------------------------------------------------------

resource "aws_security_group" "task" {
  name_prefix = "${local.name}-"
  description = "ECS task: outbound internet for web scraping"
  vpc_id      = local.vpc_id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
    description = "Outbound for scraping and AWS APIs"
  }

  lifecycle {
    create_before_destroy = true
  }
}

# -----------------------------------------------------------------------------
# ECS cluster
# -----------------------------------------------------------------------------

resource "aws_ecs_cluster" "main" {
  name = local.name
}

# -----------------------------------------------------------------------------
# IAM: Execution role (pull image, write logs)
# -----------------------------------------------------------------------------

resource "aws_iam_role" "execution" {
  name = "${local.name}-execution"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "ecs-tasks.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Execution role: read Bubble credentials from SSM and DB credentials from Secrets Manager
resource "aws_iam_role_policy" "execution_ssm_bubble" {
  name = "${local.name}-execution-ssm-bubble"
  role = aws_iam_role.execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "SSMGetBubbleParams"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:GetParameters"]
        Resource = local.bubble_ssm_param_arns
      },
      {
        Sid      = "KMSDecryptBubble"
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = "arn:aws:kms:${var.region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"
      },
      {
        Sid      = "SecretsManagerGetDB"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = "arn:aws:secretsmanager:${var.region}:${data.aws_caller_identity.current.account_id}:secret:bridgeway/database-*"
      }
    ]
  })
}

# -----------------------------------------------------------------------------
# IAM: Task role (DynamoDB, S3, SES, CloudWatch Logs)
# -----------------------------------------------------------------------------

resource "aws_iam_role" "task" {
  name = "${local.name}-task"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "ecs-tasks.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_role_policy" "task" {
  name = "${local.name}-task"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "CloudWatchLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.app.arn}:*"
      },
      {
        Sid      = "DynamoDB"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:BatchGetItem", "dynamodb:BatchWriteItem"]
        Resource = aws_dynamodb_table.state.arn
      },
      {
        Sid      = "DynamoDBChatConfig"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem"]
        Resource = "arn:aws:dynamodb:us-east-1:815039343351:table/chatkit_production_config"
      },
      {
        Sid      = "S3ReadTargets"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "arn:aws:s3:::${aws_s3_bucket.artifacts.id}/targets/*"
      },
      {
        Sid      = "S3WriteArtifacts"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:AbortMultipartUpload"]
        Resource = "arn:aws:s3:::${aws_s3_bucket.artifacts.id}/*"
      },
      {
        Sid      = "S3ListBucket"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.artifacts.arn
      },
      {
        Sid      = "SES"
        Effect   = "Allow"
        Action   = ["ses:SendEmail", "ses:SendRawEmail"]
        Resource = "*"
      },
      {
        Sid      = "SSMGetOpenAIParams"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = local.openai_ssm_param_arns
      },
      {
        Sid      = "SSMGetDBParams"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = local.db_ssm_param_arns
      },
      {
        Sid      = "KMSDecryptOpenAI"
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = "arn:aws:kms:${var.region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"
      }
    ]
  })
}

# -----------------------------------------------------------------------------
# ECS task definition
# -----------------------------------------------------------------------------

resource "aws_ecs_task_definition" "app" {
  family                   = local.name
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = var.cpu
  memory                   = var.memory

  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  execution_role_arn = aws_iam_role.execution.arn
  task_role_arn      = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name  = local.name
    image = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.app.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "ecs"
      }
    }

    # Prod observe: RunSpec-driven mode. Command requires spike.py to support these flags (rebuild image after changing spike.py CLI).
    # If you see "unrecognized arguments: --bubble-enrich", rebuild and push the Docker image, then run terraform apply.
    command = ["python", "spike.py", "--bubble-enrich", "--bubble-report", "--emit-bubble-json"]

    environment = [
      { name = "STATE_BACKEND", value = "dynamodb" },
      { name = "STATE_TABLE", value = aws_dynamodb_table.state.name },
      { name = "CHANGELOG_BUCKET", value = aws_s3_bucket.artifacts.id },
      { name = "CHANGELOG_PREFIX", value = "changelog/" },
      { name = "TARGETS_SOURCE", value = local.targets_s3_uri },
      { name = "TARGETS_FILE", value = "/app/targets.json" },
      { name = "EMAIL_ENABLED", value = "true" },
      { name = "FROM_EMAIL", value = var.email_from },
      { name = "TO_EMAILS", value = var.email_to },
      { name = "EMAIL_SUBJECT_PREFIX", value = var.email_subject_prefix },
      { name = "ENVIRONMENT", value = "prod" },
      { name = "SES_REGION", value = var.region },
      { name = "AWS_REGION", value = var.region },
      { name = "OPENAI_ENABLED", value = "true" },
      { name = "OPENAI_API_KEY_SSM_PARAM", value = local.openai_api_key_param },
      { name = "OPENAI_MODEL_SSM_PARAM", value = local.openai_model_param },
      { name = "OPENAI_REASONING_EFFORT_SSM_PARAM", value = local.openai_effort_param },
      { name = "OPENAI_ENRICH_ONLY_IF_CHANGED", value = "true" },
      { name = "OPENAI_ENRICH_MAX_RESOURCES", value = "25" },
      { name = "OPENAI_ENRICH_MAX_EVENTS", value = "10" },
      # RunSpec prod observe: bubble enrich on, refs blocked, debug artifacts, dry-run Bubble (validated at startup)
      { name = "PROD_OBSERVE_MODE", value = "true" },
      { name = "AI_ENRICHMENT_ENABLED", value = "true" },
      # Page change agent: enabled; pgvector off until DB credentials are added to SSM
      { name = "PAGE_CHANGE_AGENT_ENABLED", value = "true" },
      { name = "PGVECTOR_ENABLED", value = "true" },
      { name = "ARTIFACT_OUTPUT_DIR", value = "debug" },
      { name = "AI_REFERENCE_FIELDS_BLOCKED", value = "true" },
      { name = "RUN_SPEC_VALIDATION_FAIL_FAST", value = "false" },
      # Bubble tree names (must match Tree "Name" in your Bubble app; list via Data API /tree)
      { name = "BUBBLE_ORGANIZATION_TREE", value = "Organization" },
      { name = "BUBBLE_NAIC_GROUP_TREE", value = "Organization" },
      { name = "BUBBLE_TYPE1_TREE", value = "Resources Types" },
      { name = "BUBBLE_TOPIC_TREE", value = "Chronicles" },
      { name = "BUBBLE_ARTIFACT_BUCKET", value = aws_s3_bucket.artifacts.id },
      { name = "HTML_SNAPSHOT_BUCKET", value = aws_s3_bucket.artifacts.id },
      { name = "BUBBLE_ALERTS_ENABLED", value = "true" }
    ]

    # Bubble credentials from SSM; DB credentials from Secrets Manager — never in plaintext env.
    secrets = [
      { name = "BUBBLE_API_URL",         valueFrom = local.bubble_ssm_param_arns[0] },
      { name = "BUBBLE_API_KEY",         valueFrom = local.bubble_ssm_param_arns[1] },
      { name = "DATABASE_IP",            valueFrom = "arn:aws:secretsmanager:${var.region}:${data.aws_caller_identity.current.account_id}:secret:bridgeway/database-KubYXW:DATABASE_IP::" },
      { name = "DATABASE_NAME",          valueFrom = "arn:aws:secretsmanager:${var.region}:${data.aws_caller_identity.current.account_id}:secret:bridgeway/database-KubYXW:DATABASE_NAME::" },
      { name = "DATABASE_USERNAME",      valueFrom = "arn:aws:secretsmanager:${var.region}:${data.aws_caller_identity.current.account_id}:secret:bridgeway/database-KubYXW:DATABASE_USERNAME::" },
      { name = "DATABASE_PASSWORD",      valueFrom = "arn:aws:secretsmanager:${var.region}:${data.aws_caller_identity.current.account_id}:secret:bridgeway/database-KubYXW:DATABASE_PASSWORD::" }
    ]

    essential = true
  }])
}

