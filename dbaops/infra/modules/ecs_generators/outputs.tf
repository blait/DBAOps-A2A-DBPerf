output "cluster_name" {
  value = aws_ecs_cluster.this.name
}

output "cluster_arn" {
  value = aws_ecs_cluster.this.arn
}

output "data_gen_repo_url" {
  value = aws_ecr_repository.data_gen.repository_url
}

output "log_gen_repo_url" {
  value = aws_ecr_repository.log_gen.repository_url
}

output "task_security_group_id" {
  value = aws_security_group.task.id
}
