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
      { name = "ENVIRONMENT", value = var.environment },
      { name = "SES_REGION", value = var.region },
      { name = "AWS_REGION", value = var.region }
    ]

    essential = true
  }])
}

