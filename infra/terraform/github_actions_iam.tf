# -----------------------------------------------------------------------------
# GitHub Actions IAM role — permissions needed to run terraform apply
#
# The role itself (github-actions-web-change-tracker) is managed outside this
# Terraform (created with OIDC trust for the GitHub org). This file attaches
# the inline policies that let terraform apply read and manage the resources
# declared in this config.
#
# Bootstrap: run once locally before the first successful CI deploy:
#   aws iam put-role-policy \
#     --profile bridgeway \
#     --role-name github-actions-web-change-tracker \
#     --policy-name terraform-dynamodb-bootstrap \
#     --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["dynamodb:DescribeTable","dynamodb:ListTagsOfResource","iam:GetRole","iam:GetRolePolicy","iam:PutRolePolicy","iam:DeleteRolePolicy","iam:ListAttachedRolePolicies","iam:ListRolePolicies"],"Resource":"*"}]}'
# After that, terraform apply will manage this policy going forward and the
# bootstrap policy can be removed.
# -----------------------------------------------------------------------------

data "aws_iam_role" "github_actions" {
  name = "github-actions-web-change-tracker"
}

resource "aws_iam_role_policy" "github_actions_dynamodb" {
  name = "terraform-dynamodb"
  role = data.aws_iam_role.github_actions.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DynamoDBManage"
        Effect = "Allow"
        Action = [
          "dynamodb:CreateTable",
          "dynamodb:DeleteTable",
          "dynamodb:DescribeContinuousBackups",
          "dynamodb:DescribeKinesisStreamingDestination",
          "dynamodb:DescribeStream",
          "dynamodb:DescribeTable",
          "dynamodb:DescribeTimeToLive",
          "dynamodb:ListStreams",
          "dynamodb:ListTagsOfResource",
          "dynamodb:TagResource",
          "dynamodb:UntagResource",
          "dynamodb:UpdateContinuousBackups",
          "dynamodb:UpdateTable",
          "dynamodb:UpdateTimeToLive",
        ]
        Resource = "*"
      }
    ]
  })
}
