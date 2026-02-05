terraform {
  required_version = ">= 1.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

locals {
  name = var.project_name
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
# DynamoDB table (optional)
# -----------------------------------------------------------------------------

resource "aws_dynamodb_table" "state" {
  count = var.create_dynamodb_table ? 1 : 0

  name         = "${local.name}-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "target_id"

  attribute {
    name = "target_id"
    type = "S"
  }
}

locals {
  state_table_name = var.create_dynamodb_table ? aws_dynamodb_table.state[0].name : var.existing_state_table_name
}

# -----------------------------------------------------------------------------
# S3 bucket for changelog (optional)
# -----------------------------------------------------------------------------

resource "aws_s3_bucket" "changelog" {
  count = var.create_changelog_bucket ? 1 : 0

  bucket = "${local.name}-changelog-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_versioning" "changelog" {
  count = var.create_changelog_bucket ? 1 : 0

  bucket = aws_s3_bucket.changelog[0].id
  versioning_configuration {
    status = "Disabled"
  }
}

data "aws_caller_identity" "current" {}

locals {
  changelog_bucket_name = var.create_changelog_bucket ? aws_s3_bucket.changelog[0].id : var.existing_changelog_bucket_name
  changelog_prefix     = trimspace(var.changelog_prefix) != "" ? trimsuffix(var.changelog_prefix, "/") : "changelog"
}

# -----------------------------------------------------------------------------
# Security group (if not provided)
# -----------------------------------------------------------------------------

resource "aws_security_group" "task" {
  count = length(var.security_group_ids) == 0 ? 1 : 0

  name_prefix = "${local.name}-"
  description = "Security group for web-change-tracker ECS task"
  vpc_id      = var.vpc_id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
    description = "Outbound for scraping and AWS APIs"
  }
}

locals {
  security_group_ids = length(var.security_group_ids) > 0 ? var.security_group_ids : [aws_security_group.task[0].id]
}

# -----------------------------------------------------------------------------
# CloudWatch log group
# -----------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${local.name}"
  retention_in_days = 14
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
# IAM: Task role (DynamoDB, S3, SES, CloudWatch)
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
    Statement = concat(
      [
        {
          Sid       = "CloudWatchLogs"
          Effect    = "Allow"
          Action    = ["logs:CreateLogStream", "logs:PutLogEvents"]
          Resource  = "${aws_cloudwatch_log_group.app.arn}:*"
        }
      ],
      local.state_table_name != "" ? [{
        Sid       = "DynamoDB"
        Effect    = "Allow"
        Action    = ["dynamodb:GetItem", "dynamodb:PutItem"]
        Resource  = "arn:aws:dynamodb:${var.aws_region}:${data.aws_caller_identity.current.account_id}:table/${local.state_table_name}"
      }] : [],
      local.changelog_bucket_name != "" ? [
        {
          Sid       = "S3ChangelogPut"
          Effect    = "Allow"
          Action    = ["s3:PutObject", "s3:AbortMultipartUpload"]
          Resource  = "arn:aws:s3:::${local.changelog_bucket_name}/${local.changelog_prefix}/*"
        },
        {
          Sid       = "S3ChangelogList"
          Effect    = "Allow"
          Action    = ["s3:ListBucket"]
          Resource  = "arn:aws:s3:::${local.changelog_bucket_name}"
          Condition = {
            StringLike = {
              "s3:prefix" = ["${local.changelog_prefix}/*"]
            }
          }
        }
      ] : [],
      var.enable_ses ? [{
        Sid       = "SES"
        Effect    = "Allow"
        Action    = ["ses:SendEmail"]
        Resource  = "*"
      }] : []
    )
  })
}

# -----------------------------------------------------------------------------
# ECS task definition
# -----------------------------------------------------------------------------

resource "aws_ecs_task_definition" "app" {
  family                   = local.name
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = var.task_cpu
  memory                   = var.task_memory_mb

  execution_role_arn = aws_iam_role.execution.arn
  task_role_arn      = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name  = local.name
    image = "${aws_ecr_repository.app.repository_url}:latest"

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.app.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "ecs"
      }
    }

    environment = concat(
      [
        { name = "USE_PLAYWRIGHT", value = var.use_playwright },
        { name = "MAX_RETRIES", value = var.max_retries },
        { name = "BACKOFF_SECONDS", value = var.backoff_seconds },
        { name = "DELAY_BETWEEN_PAGES", value = var.delay_between_pages },
        { name = "AWS_REGION", value = var.aws_region }
      ],
      local.state_table_name != "" ? [
        { name = "STATE_BACKEND", value = "dynamodb" },
        { name = "STATE_TABLE", value = local.state_table_name }
      ] : [],
      local.changelog_bucket_name != "" ? [
        { name = "CHANGELOG_BUCKET", value = local.changelog_bucket_name },
        { name = "CHANGELOG_PREFIX", value = "${local.changelog_prefix}/" }
      ] : [],
      var.enable_ses && var.from_email != "" ? [{ name = "SEND_EMAIL", value = "true" }] : [],
      var.enable_ses && var.from_email != "" ? [{ name = "FROM_EMAIL", value = var.from_email }] : [],
      var.enable_ses && var.to_emails != "" ? [{ name = "TO_EMAILS", value = var.to_emails }] : [],
      var.enable_ses ? [{ name = "SES_REGION", value = var.aws_region }] : []
    )

    essential = true
  }])
}

# -----------------------------------------------------------------------------
# EventBridge Scheduler rule
# -----------------------------------------------------------------------------

resource "aws_cloudwatch_event_rule" "schedule" {
  name                = "${local.name}-schedule"
  description         = "Run web-change-tracker every 6 hours"
  schedule_expression = var.schedule_expression
}

resource "aws_cloudwatch_event_target" "ecs" {
  rule      = aws_cloudwatch_event_rule.schedule.name
  target_id = "ecs"
  arn       = aws_ecs_cluster.main.arn
  role_arn  = aws_iam_role.eventbridge.arn

  ecs_target {
    task_count          = 1
    task_definition_arn = aws_ecs_task_definition.app.arn
    launch_type         = "FARGATE"

    network_configuration {
      subnets          = var.private_subnet_ids
      security_groups  = local.security_group_ids
      assign_public_ip = false
    }
  }
}

resource "aws_iam_role" "eventbridge" {
  name = "${local.name}-eventbridge"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "events.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_role_policy" "eventbridge" {
  name = "${local.name}-eventbridge"
  role = aws_iam_role.eventbridge.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "ecs:RunTask"
      ]
      Resource = aws_ecs_task_definition.app.arn
    }, {
      Effect = "Allow"
      Action = "iam:PassRole"
      Resource = [
        aws_iam_role.execution.arn,
        aws_iam_role.task.arn
      ]
      Condition = {
        StringLike = {
          "iam:PassedToService" = "ecs-tasks.amazonaws.com"
        }
      }
    }]
  })
}
