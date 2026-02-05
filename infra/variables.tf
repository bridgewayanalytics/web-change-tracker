# Networking (use existing VPC)
variable "vpc_id" {
  description = "VPC ID where the ECS task will run (private subnets with NAT for outbound)"
  type        = string
}

variable "private_subnet_ids" {
  description = "Private subnet IDs for the ECS task"
  type        = list(string)
}

variable "security_group_ids" {
  description = "Security group IDs for the ECS task. If empty, one is created with outbound HTTPS allowed."
  type        = list(string)
  default     = []
}

# Naming
variable "project_name" {
  description = "Project name prefix for resources"
  type        = string
  default     = "web-change-tracker"
}

variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

# Schedule
variable "schedule_expression" {
  description = "EventBridge schedule (e.g. rate(6 hours) or cron(0 */6 * * ? *))"
  type        = string
  default     = "rate(6 hours)"
}

# Task sizing
variable "task_cpu" {
  description = "Fargate task CPU units (256, 512, 1024, 2048, 4096)"
  type        = number
  default     = 512
}

variable "task_memory_mb" {
  description = "Fargate task memory in MB (512, 1024, 2048, 4096, 8192, 16384)"
  type        = number
  default     = 1024
}

# Storage: create or use existing
variable "create_dynamodb_table" {
  description = "Create DynamoDB table for state; if false, use existing_state_table_name"
  type        = bool
  default     = true
}

variable "existing_state_table_name" {
  description = "Existing DynamoDB table name (when create_dynamodb_table = false)"
  type        = string
  default     = ""
}

variable "create_changelog_bucket" {
  description = "Create S3 bucket for changelog; if false, use existing_changelog_bucket_name"
  type        = bool
  default     = true
}

variable "existing_changelog_bucket_name" {
  description = "Existing S3 bucket name for changelog (when create_changelog_bucket = false)"
  type        = string
  default     = ""
}

variable "changelog_prefix" {
  description = "S3 key prefix for changelog objects (default: changelog/)"
  type        = string
  default     = "changelog/"
}

# Email (non-secret env vars)
variable "enable_ses" {
  description = "Enable SES IAM permissions and pass email env vars to the task"
  type        = bool
  default     = false
}

variable "from_email" {
  description = "SES verified sender email (required if enable_ses = true)"
  type        = string
  default     = ""
}

variable "to_emails" {
  description = "Comma-separated recipient emails (required if enable_ses = true)"
  type        = string
  default     = ""
}

# Task env vars (non-secrets)
variable "use_playwright" {
  description = "1 = use Playwright, 0 = requests only"
  type        = string
  default     = "1"
}

variable "max_retries" {
  description = "Fetch retry count"
  type        = string
  default     = "3"
}

variable "backoff_seconds" {
  description = "Initial backoff seconds"
  type        = string
  default     = "2"
}

variable "delay_between_pages" {
  description = "Seconds between target fetches"
  type        = string
  default     = "1"
}
