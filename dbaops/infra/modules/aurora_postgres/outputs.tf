output "cluster_identifier" {
  value = aws_rds_cluster.this.cluster_identifier
}

output "endpoint" {
  value = aws_rds_cluster.this.endpoint
}

output "reader_endpoint" {
  value = aws_rds_cluster.this.reader_endpoint
}

output "port" {
  value = aws_rds_cluster.this.port
}

output "database_name" {
  value = aws_rds_cluster.this.database_name
}

output "security_group_id" {
  value = aws_security_group.this.id
}

output "master_user_secret_arn" {
  value = try(aws_rds_cluster.this.master_user_secret[0].secret_arn, null)
}

output "writer_resource_id" {
  description = "Performance Insights 용 dbi-resource-id"
  value       = aws_rds_cluster_instance.writer.dbi_resource_id
}
