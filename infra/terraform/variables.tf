# Naming
variable "project_name" {
  description = "Project name prefix for resources"
  type        = string
  default     = "web-change-tracker"
}

variable "region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Environment (e.g. dev, staging, prod)"
  type        = string
  default     = "prod"
}

# Email
variable "email_from" {
  description = "SES verified sender email"
  type        = string
}

variable "email_to" {
  description = "Comma-separated recipient emails"
  type        = string
}

variable "email_subject_prefix" {
  description = "Email subject prefix (default: [Web Change Report])"
  type        = string
  default     = "[Web Change Report]"
}

# Schedule
variable "schedule_expression" {
  description = "EventBridge Scheduler expression (e.g. rate(6 hours) or cron(0 */6 * * ? *))"
  type        = string
  default     = "rate(6 hours)"
}

variable "enable_scheduler" {
  description = "Enable EventBridge Scheduler to run the ECS task on schedule"
  type        = bool
  default     = true
}

# Networking - use default VPC via data sources; override if needed
variable "vpc_id" {
  description = "VPC ID (leave null to use default VPC)"
  type        = string
  default     = null
}

variable "subnet_ids" {
  description = "Subnet IDs for ECS task (leave null to use default VPC subnets)"
  type        = list(string)
  default     = null
}

# Container image
variable "image_tag" {
  description = "Docker image tag for the ECR repository (default: latest)"
  type        = string
  default     = "latest"
}

# Task sizing
variable "cpu" {
  description = "Fargate task CPU units (256, 512, 1024, 2048, 4096)"
  type        = number
  default     = 512
}

variable "memory" {
  description = "Fargate task memory in MB (512, 1024, 2048, 4096, 8192, 16384)"
  type        = number
  default     = 1024
}
