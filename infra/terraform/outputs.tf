output "region" {
  description = "AWS region"
  value       = var.region
}

output "ecr_repository_url" {
  description = "ECR repository URL for pushing the Docker image"
  value       = aws_ecr_repository.app.repository_url
}

output "ecs_cluster_name" {
  description = "ECS cluster name"
  value       = aws_ecs_cluster.main.name
}

output "task_definition_arn" {
  description = "ECS task definition ARN"
  value       = aws_ecs_task_definition.app.arn
}

output "scheduler_name" {
  description = "EventBridge Scheduler schedule name"
  value       = var.enable_scheduler ? aws_scheduler_schedule.ecs[0].name : null
}

output "s3_bucket_name" {
  description = "S3 bucket for artifacts (targets, changelog, reports)"
  value       = aws_s3_bucket.artifacts.id
}

output "dynamodb_table_name" {
  description = "DynamoDB state table name"
  value       = aws_dynamodb_table.state.name
}

output "targets_s3_uri" {
  description = "S3 URI for targets.json (used by TARGETS_SOURCE)"
  value       = local.targets_s3_uri
}
