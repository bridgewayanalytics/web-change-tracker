# Execution role: pull image from ECR, write logs to CloudWatch
resource "aws_iam_role" "ecs_execution" {
  name = "${local.name_prefix}-ecs-execution"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ecs-tasks.amazonaws.com"
        }
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_execution" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Task role: S3 read, SSM GetParameter for Bubble credentials
resource "aws_iam_role" "ecs_task" {
  name = "${local.name_prefix}-ecs-task"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ecs-tasks.amazonaws.com"
        }
      }
    ]
  })
}

# Re-evaluate feature: ECS RunTask + DynamoDB config read + S3 write/delete
resource "aws_iam_role_policy" "ecs_task_rerun" {
  name = "${local.name_prefix}-rerun"
  role = aws_iam_role.ecs_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "RunWCTTask"
        Effect = "Allow"
        Action = ["ecs:RunTask"]
        Resource = [
          "arn:aws:ecs:${local.region}:${local.account_id}:task-definition/web-change-tracker-prod*"
        ]
      },
      {
        Sid    = "DescribeWCTTasks"
        Effect = "Allow"
        Action = ["ecs:DescribeTasks"]
        Resource = ["*"]
        Condition = {
          ArnLike = {
            "ecs:cluster" = "arn:aws:ecs:${local.region}:${local.account_id}:cluster/web-change-tracker-prod"
          }
        }
      },
      {
        Sid    = "PassWCTRoles"
        Effect = "Allow"
        Action = ["iam:PassRole"]
        Resource = [
          "arn:aws:iam::${local.account_id}:role/web-change-tracker-prod-task",
          "arn:aws:iam::${local.account_id}:role/web-change-tracker-prod-execution"
        ]
      },
      {
        Sid    = "ReadAgentConfig"
        Effect = "Allow"
        Action = ["dynamodb:GetItem"]
        Resource = [
          "arn:aws:dynamodb:${local.region}:${local.account_id}:table/chatkit_production_config",
          "arn:aws:dynamodb:${local.region}:${local.account_id}:table/chatkit_production_field_registry"
        ]
      },
      {
        Sid    = "WriteRerunAccept"
        Effect = "Allow"
        Action = ["s3:PutObject"]
        Resource = [
          "arn:aws:s3:::${var.bubble_artifact_bucket}/alerts/*"
        ]
      },
      {
        Sid    = "DiscardRerunResult"
        Effect = "Allow"
        Action = ["s3:DeleteObject"]
        Resource = [
          "arn:aws:s3:::${var.bubble_artifact_bucket}/alerts/reruns/*"
        ]
      },
      {
        Sid    = "InvokeBubbleSync"
        Effect = "Allow"
        Action = ["lambda:InvokeFunction"]
        Resource = [
          "arn:aws:lambda:${local.region}:${local.account_id}:function:web-change-tracker-prod-bubble-sync"
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy" "ecs_task_s3_ssm" {
  name = "${local.name_prefix}-s3-ssm"
  role = aws_iam_role.ecs_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        {
          Effect   = "Allow"
          Action   = ["s3:GetObject"]
          Resource = [
            "arn:aws:s3:::${var.bubble_artifact_bucket}/bubble_reports/*",
            "arn:aws:s3:::${var.bubble_artifact_bucket}/alerts/*",
            "arn:aws:s3:::${var.bubble_artifact_bucket}/pages/*",
            "arn:aws:s3:::${var.bubble_artifact_bucket}/transcripts/*"
          ]
        },
        {
          Effect = "Allow"
          Action = [
            "ssm:GetParameter",
            "ssm:GetParameters"
          ]
          Resource = [
            "arn:aws:ssm:${local.region}:${local.account_id}:parameter${var.ssm_bubble_api_url_param}",
            "arn:aws:ssm:${local.region}:${local.account_id}:parameter${var.ssm_bubble_api_key_param}",
            "arn:aws:ssm:${local.region}:${local.account_id}:parameter/web-change-tracker/prod/chatkit_internal_api_key"
          ]
        },
        {
          Effect = "Allow"
          Action = ["s3:GetObject"]
          Resource = ["arn:aws:s3:::recordings-bucket-1/*"]
        },
        {
          Effect = "Allow"
          Action = ["s3:PutObject"]
          Resource = ["arn:aws:s3:::${var.bubble_artifact_bucket}/documents/manual/*"]
        }
      ],
      var.bubble_report_recent_runs_limit > 0 ? [
        {
          Effect   = "Allow"
          Action   = ["s3:ListBucket"]
          Resource = "arn:aws:s3:::${var.bubble_artifact_bucket}"
          Condition = {
            StringLike = {
              "s3:prefix" = ["bubble_reports*"]
            }
          }
        }
      ] : []
    )
  })
}
