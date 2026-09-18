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
