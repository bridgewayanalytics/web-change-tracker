# -----------------------------------------------------------------------------
# EventBridge Scheduler - runs ECS Fargate task on schedule
# Uses same subnets and security groups as the ECS task (local.subnets, aws_security_group.task)
# -----------------------------------------------------------------------------

resource "aws_iam_role" "scheduler" {
  count = var.enable_scheduler ? 1 : 0

  name = "${local.name}-scheduler"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "scheduler.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  count = var.enable_scheduler ? 1 : 0

  name = "${local.name}-scheduler"
  role = aws_iam_role.scheduler[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ecs:RunTask"]
        Resource = aws_ecs_task_definition.app.arn
      },
      {
        Effect   = "Allow"
        Action   = ["iam:PassRole"]
        Resource = [aws_iam_role.execution.arn, aws_iam_role.task.arn]
        Condition = {
          StringLike = {
            "iam:PassedToService" = "ecs-tasks.amazonaws.com"
          }
        }
      }
    ]
  })
}

resource "aws_scheduler_schedule" "ecs" {
  count = var.enable_scheduler ? 1 : 0

  name       = "${local.name}-schedule"
  group_name = "default"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression          = var.schedule_expression
  schedule_expression_timezone = var.schedule_expression_timezone

  target {
    arn      = aws_ecs_cluster.main.arn
    role_arn = aws_iam_role.scheduler[0].arn

    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.app.arn
      launch_type         = "FARGATE"
      task_count          = 1

      network_configuration {
        subnets          = local.subnets
        security_groups  = [aws_security_group.task.id]
        assign_public_ip = true
      }
    }
  }
}
