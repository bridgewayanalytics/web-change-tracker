# -----------------------------------------------------------------------------
# DynamoDB Streams → Lambda: auto-validate Bubble syncs to chatkit_production_config
#
# Detects and corrects: garbage labels, label count mismatches, field key renames.
# Prevents dashboard breakage from Bubble admin syncs.
# -----------------------------------------------------------------------------

# Import the existing chatkit_production_config table into Terraform state.
# This table was created externally (ChatKit platform); we import it so we
# can enable DynamoDB Streams on it.
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
    # Don't destroy this shared table if removed from Terraform
    prevent_destroy = true
  }
}

# -----------------------------------------------------------------------------
# Lambda function: validate_config_sync
# -----------------------------------------------------------------------------

data "archive_file" "validate_config" {
  type        = "zip"
  source_dir  = "${path.module}/../lambda/validate_config_sync"
  output_path = "${path.module}/../lambda/validate_config_sync.zip"
}

resource "aws_lambda_function" "validate_config" {
  function_name    = "${local.name}-validate-config-sync"
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.validate_config.output_path
  source_code_hash = data.archive_file.validate_config.output_base64sha256
  role             = aws_iam_role.validate_config_lambda.arn
  timeout          = 30
  memory_size      = 128

  environment {
    variables = {
      LOG_LEVEL = "INFO"
    }
  }
}

resource "aws_cloudwatch_log_group" "validate_config" {
  name              = "/aws/lambda/${local.name}-validate-config-sync"
  retention_in_days = 14
}

# -----------------------------------------------------------------------------
# DynamoDB Stream → Lambda event source mapping
# -----------------------------------------------------------------------------

resource "aws_lambda_event_source_mapping" "config_stream" {
  event_source_arn  = aws_dynamodb_table.chatkit_config.stream_arn
  function_name     = aws_lambda_function.validate_config.arn
  starting_position = "LATEST"
  batch_size        = 1
}

# -----------------------------------------------------------------------------
# IAM: Lambda execution role
# -----------------------------------------------------------------------------

resource "aws_iam_role" "validate_config_lambda" {
  name = "${local.name}-validate-config-lambda"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "validate_config_lambda" {
  name = "${local.name}-validate-config-lambda"
  role = aws_iam_role.validate_config_lambda.id

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
        Sid    = "DynamoDBStream"
        Effect = "Allow"
        Action = [
          "dynamodb:GetRecords",
          "dynamodb:GetShardIterator",
          "dynamodb:DescribeStream",
          "dynamodb:ListStreams"
        ]
        Resource = "${aws_dynamodb_table.chatkit_config.arn}/stream/*"
      },
      {
        Sid    = "DynamoDBReadWrite"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:UpdateItem"
        ]
        Resource = aws_dynamodb_table.chatkit_config.arn
      }
    ]
  })
}
