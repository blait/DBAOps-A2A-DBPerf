output "ecr_repository_url" {
  value = aws_ecr_repository.agent.repository_url
}

output "ecr_repository_name" {
  value = aws_ecr_repository.agent.name
}

output "cognito_user_pool_id" {
  value = aws_cognito_user_pool.this.id
}

output "cognito_app_client_id" {
  value = aws_cognito_user_pool_client.streamlit.id
}

output "runtime_role_arn" {
  value = aws_iam_role.runtime.arn
}

output "gateway_role_arn" {
  value = aws_iam_role.gateway.arn
}
