output "vpc_id" {
  value = module.network.vpc_id
}

output "private_subnet_ids" {
  value = module.network.private_subnet_ids
}

output "public_subnet_ids" {
  value = module.network.public_subnet_ids
}

output "logs_bucket" {
  value = module.s3_logs.bucket_name
}

output "prometheus_endpoint" {
  value = module.ec2_prometheus.prometheus_endpoint
}

output "prometheus_instance_id" {
  value = module.ec2_prometheus.instance_id
}

output "aurora_endpoint" {
  value = module.aurora_postgres.endpoint
}

output "aurora_secret_arn" {
  value = module.aurora_postgres.master_user_secret_arn
}

output "aurora_writer_resource_id" {
  value = module.aurora_postgres.writer_resource_id
}

output "ecr_repository_url" {
  value = module.agentcore.ecr_repository_url
}

output "cognito_user_pool_id" {
  value = module.agentcore.cognito_user_pool_id
}

output "cognito_app_client_id" {
  value = module.agentcore.cognito_app_client_id
}

output "agentcore_runtime_role_arn" {
  value = module.agentcore.runtime_role_arn
}

output "agentcore_gateway_role_arn" {
  value = module.agentcore.gateway_role_arn
}

output "mysql_endpoint" {
  value = module.rds_mysql.endpoint
}

output "mysql_secret_arn" {
  value = module.rds_mysql.master_user_secret_arn
}

output "mysql_resource_id" {
  value = module.rds_mysql.resource_id
}

output "msk_cluster_arn" {
  value = module.msk_serverless.cluster_arn
}

output "msk_cluster_name" {
  value = module.msk_serverless.cluster_name
}

output "msk_bootstrap_brokers" {
  value = data.aws_msk_bootstrap_brokers.this.bootstrap_brokers_sasl_iam
}

output "ecs_cluster_name" {
  value = module.ecs_generators.cluster_name
}

output "data_gen_repo_url" {
  value = module.ecs_generators.data_gen_repo_url
}

output "log_gen_repo_url" {
  value = module.ecs_generators.log_gen_repo_url
}

output "mcp_repo_rds_pi" {
  value = module.lambda_rds_pi.ecr_repository_url
}

output "mcp_repo_msk_metrics" {
  value = module.lambda_msk_metrics.ecr_repository_url
}

output "mcp_repo_s3_log_fetch" {
  value = module.lambda_s3_log_fetch.ecr_repository_url
}

output "mcp_repo_aws_api" {
  value = module.lambda_aws_api.ecr_repository_url
}

output "mcp_repo_awslabs_cloudwatch" {
  value = module.lambda_awslabs_cloudwatch.ecr_repository_url
}

output "mcp_repo_awslabs_aws_doc" {
  value = module.lambda_awslabs_aws_doc.ecr_repository_url
}

output "mcp_repo_awslabs_aws_api" {
  value = module.lambda_awslabs_aws_api.ecr_repository_url
}

output "mcp_repo_community_prometheus" {
  value = module.lambda_community_prometheus.ecr_repository_url
}

output "mcp_repo_community_postgres" {
  value = module.lambda_community_postgres.ecr_repository_url
}

output "mcp_repo_community_mysql" {
  value = module.lambda_community_mysql.ecr_repository_url
}

output "mcp_lambda_arns" {
  value = {
    # 우리 PoC 특화 (유지)
    "rds-pi"               = module.lambda_rds_pi.function_arn
    "msk-metrics"          = module.lambda_msk_metrics.function_arn
    "s3-log-fetch"         = module.lambda_s3_log_fetch.function_arn
    "aws-api"              = module.lambda_aws_api.function_arn
    # 기성 MCP 서버 wrap
    "awslabs-cloudwatch"   = module.lambda_awslabs_cloudwatch.function_arn
    "awslabs-aws-doc"      = module.lambda_awslabs_aws_doc.function_arn
    "awslabs-aws-api"      = module.lambda_awslabs_aws_api.function_arn
    "community-prometheus" = module.lambda_community_prometheus.function_arn
    "community-postgres"   = module.lambda_community_postgres.function_arn
    "community-mysql"      = module.lambda_community_mysql.function_arn
  }
}

############################################
# Streamlit UI
############################################

output "streamlit_url" {
  description = "공개 CloudFront URL"
  value       = module.streamlit.cloudfront_url
}

output "streamlit_alb_dns" {
  value = module.streamlit.alb_dns_name
}

output "streamlit_repo_url" {
  value = module.streamlit.streamlit_repo_url
}
