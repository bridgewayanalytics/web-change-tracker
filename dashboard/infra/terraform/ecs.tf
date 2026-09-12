locals {
  ecr_repository_url = coalesce(
    var.ecr_repository_url,
    "${data.aws_caller_identity.current.account_id}.dkr.ecr.${local.region}.amazonaws.com/${aws_ecr_repository.dashboard.name}"
  )

  container_environment = concat(
    [
      { name = "NODE_ENV", value = "production" },
      { name = "AWS_REGION", value = local.region },
      { name = "BUBBLE_ARTIFACT_BUCKET", value = var.bubble_artifact_bucket },
      { name = "BUBBLE_REPORT_LATEST_KEY", value = var.bubble_report_latest_key },
      { name = "BUBBLE_REPORT_RECENT_RUNS_PREFIX", value = var.bubble_report_recent_runs_prefix },
      { name = "BUBBLE_REPORT_RECENT_RUNS_LIMIT", value = tostring(var.bubble_report_recent_runs_limit) },
      # Auth0
      { name = "AUTH0_SECRET", value = var.auth0_secret },
      { name = "APP_BASE_URL", value = var.auth0_base_url },
      { name = "AUTH0_DOMAIN", value = var.auth0_domain },
      { name = "AUTH0_CLIENT_ID", value = var.auth0_client_id },
      { name = "AUTH0_CLIENT_SECRET", value = var.auth0_client_secret },
    ],
    (!var.use_ssm_for_bubble_credentials && var.bubble_api_url != null && var.bubble_api_key != null) ? [
      { name = "BUBBLE_API_URL", value = var.bubble_api_url },
      { name = "BUBBLE_API_KEY", value = var.bubble_api_key },
    ] : []
  )
}

resource "aws_ecs_cluster" "main" {
  name = "${local.name_prefix}-cluster"
}

resource "aws_ecs_task_definition" "main" {
  family                   = local.name_prefix
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "512"
  memory                   = "1024"

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  execution_role_arn = aws_iam_role.ecs_execution.arn
  task_role_arn      = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([
    {
      name      = "dashboard"
      image     = "${local.ecr_repository_url}:${var.ecr_image_tag}"
      essential = true

      portMappings = [
        {
          containerPort = 3000
          protocol      = "tcp"
        }
      ]

      environment = local.container_environment

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.ecs.name
          "awslogs-region"        = local.region
          "awslogs-stream-prefix" = "ecs"
        }
      }

    }
  ])
}

resource "aws_ecs_service" "main" {
  name            = "${local.name_prefix}-service"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.main.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.ecs.id]
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.main.arn
    container_name   = "dashboard"
    container_port   = 3000
  }
}
