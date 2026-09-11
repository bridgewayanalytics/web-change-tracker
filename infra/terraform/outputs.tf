output "aws_region" {
  description = "AWS region"
  value       = local.region
}

output "alb_dns_name" {
  description = "ALB DNS name for the dashboard. Open http://<value> to access."
  value       = aws_lb.main.dns_name
}

output "alb_url" {
  description = "Full URL to the dashboard"
  value       = "http://${aws_lb.main.dns_name}"
}

output "ecr_repository_url" {
  description = "ECR repository URL for pushing the dashboard image"
  value       = aws_ecr_repository.dashboard.repository_url
}

output "ecs_cluster_name" {
  description = "ECS cluster name"
  value       = aws_ecs_cluster.main.name
}

output "ecs_service_name" {
  description = "ECS service name"
  value       = aws_ecs_service.main.name
}
