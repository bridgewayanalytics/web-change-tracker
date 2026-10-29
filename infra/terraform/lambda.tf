# -----------------------------------------------------------------------------
# Shared chatkit_production_config DynamoDB table (external resource)
#
# This table was created by the ChatKit platform. We keep it in Terraform state
# so stream_enabled stays set (other services may consume the stream). The
# validate_config_sync Lambda that previously consumed this stream was removed
# in favor of the ChatKit backend owning the schema + field registry directly.
# -----------------------------------------------------------------------------

import {
  to = aws_dynamodb_table.chatkit_config
  id = "chatkit_production_config"
}

resource "aws_dynamodb_table" "chatkit_config" {
  name         = "chatkit_production_config"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "config_key"

  attribute {
    name = "config_key"
    type = "S"
  }

  stream_enabled   = true
  stream_view_type = "NEW_AND_OLD_IMAGES"

  lifecycle {
    prevent_destroy = true
  }
}

# -----------------------------------------------------------------------------
# Lambda function: recording_ingest_trigger
#
# Triggered by S3 PutObject on recordings-bucket-1 (.mp3 suffix).
# Fires ECS RunTask with RECORDING_S3_KEY override → spike.py recording_ingest
# mode: transcribe → ingest to newsreel KB → stamp matching alerts.
# -----------------------------------------------------------------------------

data "archive_file" "recording_ingest" {
  type        = "zip"
  source_dir  = "${path.module}/../lambda/recording_ingest_trigger"
  output_path = "${path.module}/../lambda/recording_ingest_trigger.zip"
}

resource "aws_lambda_function" "recording_ingest" {
  function_name    = "${local.name}-recording-ingest-trigger"
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.recording_ingest.output_path
  source_code_hash = data.archive_file.recording_ingest.output_base64sha256
  role             = aws_iam_role.recording_ingest_lambda.arn
  timeout          = 30
  memory_size      = 128

  environment {
    variables = {
      ECS_CLUSTER     = aws_ecs_cluster.main.name
      TASK_DEFINITION = aws_ecs_task_definition.app.arn
      CONTAINER_NAME  = local.name
      SUBNETS         = join(",", local.subnets)
      SECURITY_GROUP  = aws_security_group.task.id
    }
  }
}

resource "aws_cloudwatch_log_group" "recording_ingest" {
  name              = "/aws/lambda/${local.name}-recording-ingest-trigger"
  retention_in_days = 14
}

resource "aws_lambda_permission" "recording_ingest_s3" {
  statement_id  = "AllowS3Invoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.recording_ingest.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = "arn:aws:s3:::recordings-bucket-1"
}

resource "aws_s3_bucket_notification" "recordings" {
  bucket = "recordings-bucket-1"

  lambda_function {
    lambda_function_arn = aws_lambda_function.recording_ingest.arn
    events              = ["s3:ObjectCreated:*"]
    filter_suffix       = ".mp3"
  }

  depends_on = [aws_lambda_permission.recording_ingest_s3]
}

# IAM role for recording_ingest Lambda

resource "aws_iam_role" "recording_ingest_lambda" {
  name = "${local.name}-recording-ingest-lambda"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "recording_ingest_lambda" {
  name = "${local.name}-recording-ingest-lambda"
  role = aws_iam_role.recording_ingest_lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:${var.region}:${data.aws_caller_identity.current.account_id}:*"
      },
      {
        Sid      = "ECSRunTask"
        Effect   = "Allow"
        Action   = ["ecs:RunTask"]
        Resource = aws_ecs_task_definition.app.arn
      },
      {
        Sid    = "PassRole"
        Effect = "Allow"
        Action = ["iam:PassRole"]
        Resource = [
          aws_iam_role.execution.arn,
          aws_iam_role.task.arn,
        ]
        Condition = {
          StringLike = {
            "iam:PassedToService" = "ecs-tasks.amazonaws.com"
          }
        }
      }
    ]
  })
}
