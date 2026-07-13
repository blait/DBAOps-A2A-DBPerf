output "cluster_arn" {
  value = aws_msk_serverless_cluster.this.arn
}

output "cluster_name" {
  value = aws_msk_serverless_cluster.this.cluster_name
}

output "security_group_id" {
  value = aws_security_group.this.id
}
