output "ecr_repository_url" {
  value = aws_ecr_repository.this.repository_url
}

output "function_name" {
  value = try(aws_lambda_function.this[0].function_name, null)
}

output "function_arn" {
  value = try(aws_lambda_function.this[0].arn, null)
}

output "security_group_id" {
  value = aws_security_group.this.id
}
