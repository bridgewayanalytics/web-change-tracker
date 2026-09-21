# CloudWatch alarm: fires when no HEALTH_RUN_COMPLETE log line appears in 8 hours.
# This catches schedule/ECS failures that prevent the pipeline from finishing — the kind
# of silent failure that went undetected for 15 days in the Playwright version mismatch incident.
#
# How it works:
#   1. Metric filter watches the ECS log group for "HEALTH_RUN_COMPLETE" (the line that
#      run_tracker.finalize() emits on every successful normal pipeline run).
#   2. A CloudWatch alarm checks the 8-hour sum of that metric.
#   3. If the sum drops below 1 (no completed run), treat_missing_data = "breaching" fires
#      the alarm even when no data points exist (pipeline never started).
#   4. SNS topic → email subscribers get notified.

# ---- SNS topic ----------------------------------------------------------------

resource "aws_sns_topic" "health_alerts" {
  name = "${local.name}-health-alerts"
}

# One subscription per recipient email (split the comma-separated var)
resource "aws_sns_topic_subscription" "health_email" {
  for_each = toset([
    for e in split(",", var.email_to) : trimspace(e) if trimspace(e) != ""
  ])

  topic_arn = aws_sns_topic.health_alerts.arn
  protocol  = "email"
  endpoint  = each.value
}

# ---- Metric filter -----------------------------------------------------------

resource "aws_cloudwatch_log_metric_filter" "health_run_complete" {
  name           = "${local.name}-health-run-complete"
  log_group_name = aws_cloudwatch_log_group.app.name
  pattern        = "HEALTH_RUN_COMPLETE"

  metric_transformation {
    name          = "RunsCompleted"
    namespace     = "WebChangeTracker"
    value         = "1"
    default_value = "0"
  }
}

# ---- Alarm -------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "no_pipeline_run" {
  alarm_name          = "${local.name}-no-pipeline-run-8h"
  alarm_description   = "No completed web-change-tracker pipeline run in the last 8 hours. Check ECS task history and CloudWatch logs."
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 1
  metric_name         = "RunsCompleted"
  namespace           = "WebChangeTracker"
  # 8 hours — pipeline runs every 6h; this gives a 2h grace window
  period     = 28800
  statistic  = "Sum"
  threshold  = 1
  # Fire even when no data (no log events → metric never emitted → alarm)
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.health_alerts.arn]
  ok_actions    = [aws_sns_topic.health_alerts.arn]
}

# ---- Canary notifier ---------------------------------------------------------
# Publishes a test email directly to the SNS topic twice a week (Tue + Fri)
# so recipients can confirm health alert delivery is working. Subject is clearly
# labeled "[Automated Test]" to distinguish it from real alerts.

data "archive_file" "health_canary" {
  type        = "zip"
  source_dir  = "${path.module}/../lambda/health_canary_notifier"
  output_path = "${path.module}/../lambda/health_canary_notifier.zip"
}

resource "aws_lambda_function" "health_canary" {
  function_name    = "${local.name}-health-canary-notifier"
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.health_canary.output_path
  source_code_hash = data.archive_file.health_canary.output_base64sha256
  role             = aws_iam_role.health_canary.arn
  timeout          = 15
  memory_size      = 128

  environment {
    variables = {
      SNS_TOPIC_ARN = aws_sns_topic.health_alerts.arn
    }
  }
}

resource "aws_cloudwatch_log_group" "health_canary" {
  name              = "/aws/lambda/${local.name}-health-canary-notifier"
  retention_in_days = 14
}

resource "aws_iam_role" "health_canary" {
  name = "${local.name}-health-canary"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "health_canary" {
  name = "${local.name}-health-canary"
  role = aws_iam_role.health_canary.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.region}:${data.aws_caller_identity.current.account_id}:*"
      },
      {
        Sid      = "SNSPublish"
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = aws_sns_topic.health_alerts.arn
      }
    ]
  })
}

# EventBridge Scheduler: Tuesday and Friday at 10:00 AM ET (14:00 UTC)
resource "aws_scheduler_schedule" "health_canary" {
  name       = "${local.name}-health-canary"
  group_name = "default"

  flexible_time_window { mode = "OFF" }

  schedule_expression          = "cron(0 14 ? * TUE,FRI *)"
  schedule_expression_timezone = "America/New_York"

  target {
    arn      = aws_lambda_function.health_canary.arn
    role_arn = aws_iam_role.health_canary_scheduler.arn
    input    = "{}"
  }
}

resource "aws_iam_role" "health_canary_scheduler" {
  name = "${local.name}-health-canary-scheduler"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "health_canary_scheduler" {
  name = "${local.name}-health-canary-scheduler"
  role = aws_iam_role.health_canary_scheduler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunction"]
      Resource = aws_lambda_function.health_canary.arn
    }]
  })
}

resource "aws_lambda_permission" "health_canary_scheduler" {
  statement_id  = "AllowSchedulerInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.health_canary.function_name
  principal     = "scheduler.amazonaws.com"
  source_arn    = aws_scheduler_schedule.health_canary.arn
}
