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

output "cloudwatch_log_group" {
  description = "CloudWatch log group for task output"
  value       = aws_cloudwatch_log_group.app.name
}

output "state_table_name" {
  description = "DynamoDB state table name"
  value       = local.state_table_name
}

output "changelog_bucket_name" {
  description = "S3 changelog bucket name"
  value       = local.changelog_bucket_name
}

output "eventbridge_rule_name" {
  description = "EventBridge schedule rule name"
  value       = aws_cloudwatch_event_rule.schedule.name
}
