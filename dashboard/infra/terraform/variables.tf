variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Environment name (e.g. dev, prod)"
  type        = string
  default     = "prod"
}

variable "bubble_artifact_bucket" {
  description = "S3 bucket name for bubble_reports (read access)"
  type        = string
}

variable "ecr_repository_url" {
  description = "ECR repository URL for the dashboard image (e.g. 123456789012.dkr.ecr.us-east-1.amazonaws.com/naic-dashboard). Omit to use the ECR repo created by this module."
  type        = string
  default     = null
}

variable "ecr_image_tag" {
  description = "Docker image tag to deploy"
  type        = string
  default     = "latest"
}

variable "ssm_bubble_api_url_param" {
  description = "SSM parameter name for Bubble API URL"
  type        = string
  default     = "/naic-dashboard/prod/bubble_api_url"
}

variable "ssm_bubble_api_key_param" {
  description = "SSM parameter name for Bubble API key"
  type        = string
  default     = "/naic-dashboard/prod/bubble_api_key"
}

variable "use_ssm_for_bubble_credentials" {
  description = "If true, app fetches Bubble API URL/key from SSM at runtime. If false, use bubble_api_url/bubble_api_key variables (dev only)."
  type        = bool
  default     = true
}

variable "bubble_api_url" {
  description = "Bubble API URL (dev only, when use_ssm_for_bubble_credentials = false)"
  type        = string
  default     = null
  sensitive   = true
}

variable "bubble_api_key" {
  description = "Bubble API key (dev only, when use_ssm_for_bubble_credentials = false)"
  type        = string
  default     = null
  sensitive   = true
}

variable "bubble_report_latest_key" {
  description = "S3 key for latest report"
  type        = string
  default     = "bubble_reports/latest.json"
}

variable "bubble_report_recent_runs_prefix" {
  description = "S3 prefix for recent run reports"
  type        = string
  default     = "bubble_reports/runs/"
}

variable "bubble_report_recent_runs_limit" {
  description = "If > 0, enable fetching recent runs (requires S3 ListBucket)"
  type        = number
  default     = 0
}

# ── Auth0 ─────────────────────────────────────────────────────────────────────

variable "auth0_secret" {
  description = "AUTH0_SECRET — random 32+ char string used to encrypt the session cookie"
  type        = string
  sensitive   = true
}

variable "auth0_base_url" {
  description = "AUTH0_BASE_URL — public URL of the dashboard (e.g. https://admin.bridgewayanalytics.com)"
  type        = string
}

variable "auth0_domain" {
  description = "AUTH0_DOMAIN — Auth0 tenant domain (e.g. dev-8v84cybp75y58rsd.us.auth0.com)"
  type        = string
  default     = "dev-8v84cybp75y58rsd.us.auth0.com"
}

variable "auth0_client_id" {
  description = "AUTH0_CLIENT_ID — Auth0 Application Client ID"
  type        = string
}

variable "auth0_client_secret" {
  description = "AUTH0_CLIENT_SECRET — Auth0 Application Client Secret"
  type        = string
  sensitive   = true
}


variable "acm_certificate_arn" {
  description = "ACM certificate ARN for HTTPS on the ALB"
  type        = string
}

variable "vpc_id" {
  description = "VPC ID for ECS and ALB"
  type        = string
}

variable "public_subnet_ids" {
  description = "Public subnet IDs for ALB"
  type        = list(string)
}

variable "private_subnet_ids" {
  description = "Private subnet IDs for ECS tasks"
  type        = list(string)
}
