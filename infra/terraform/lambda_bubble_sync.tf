# -----------------------------------------------------------------------------
# Lambda: bubble_sync
#
# Runs bubble_sync.sync_alert() synchronously — invoked directly by the
# NAICDashboard API route instead of ECS RunTask, eliminating the ~70s
# Fargate cold-start delay. Lambda cold start is ~1-3s.
#
# Built by scripts/deploy.sh: pip install requests + project code → zip.
# -----------------------------------------------------------------------------

resource "aws_lambda_function" "bubble_sync" {
  function_name    = "${local.name}-bubble-sync"
  runtime          = "python3.12"
  handler          = "handler.lambda_handler"
  filename         = "${path.module}/../lambda/bubble_sync.zip"
  source_code_hash = filebase64sha256("${path.module}/../lambda/bubble_sync.zip")
  role             = aws_iam_role.bubble_sync_lambda.arn
  timeout          = 300
  memory_size      = 512

  environment {
    variables = {
      CHANGELOG_BUCKET = aws_s3_bucket.artifacts.id
      EIDARIX_VERSION  = "test"
    }
  }
}

resource "aws_cloudwatch_log_group" "bubble_sync" {
  name              = "/aws/lambda/${local.name}-bubble-sync"
  retention_in_days = 14
}

# Allow the NAICDashboard ECS task role to invoke this Lambda
resource "aws_lambda_permission" "bubble_sync_dashboard" {
  statement_id  = "AllowDashboardECSTask"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.bubble_sync.function_name
  principal     = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
}

# -----------------------------------------------------------------------------
# IAM role for the Lambda function
# -----------------------------------------------------------------------------

resource "aws_iam_role" "bubble_sync_lambda" {
  name = "${local.name}-bubble-sync-lambda"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "bubble_sync_lambda" {
  name = "${local.name}-bubble-sync-lambda"
  role = aws_iam_role.bubble_sync_lambda.id

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
        Sid    = "S3ReadWriteAlerts"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject"
        ]
        Resource = "${aws_s3_bucket.artifacts.arn}/alerts/*"
      }
    ]
  })
}
