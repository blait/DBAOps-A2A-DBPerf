############################################
# DBAOps-Agent — PoC 환경 (서울 단일 리전)
############################################

module "network" {
  source = "../../modules/network"

  environment         = var.environment
  vpc_cidr            = var.vpc_cidr
  azs                 = var.azs
  enable_nat_instance = true
  enable_s3_endpoint  = true
  interface_endpoints = [
    "secretsmanager",
    "bedrock-runtime",
    "bedrock-agentcore",
    "bedrock-agentcore-control"
  ]
}

module "iam" {
  source      = "../../modules/iam"
  environment = var.environment
}

module "s3_logs" {
  source      = "../../modules/s3_logs"
  environment = var.environment
}

module "ec2_prometheus" {
  source = "../../modules/ec2_prometheus"

  environment = var.environment
  vpc_id      = module.network.vpc_id
  vpc_cidr    = module.network.vpc_cidr
  subnet_id   = module.network.private_subnet_ids[0]
  use_spot    = true
}

module "aurora_postgres" {
  source = "../../modules/aurora_postgres"

  environment        = var.environment
  vpc_id             = module.network.vpc_id
  vpc_cidr           = module.network.vpc_cidr
  private_subnet_ids = module.network.private_subnet_ids
  create_reader      = true
}

module "rds_mysql" {
  source = "../../modules/rds_mysql"

  environment        = var.environment
  vpc_id             = module.network.vpc_id
  vpc_cidr           = module.network.vpc_cidr
  private_subnet_ids = module.network.private_subnet_ids
}

module "msk_serverless" {
  source = "../../modules/msk_serverless"

  environment        = var.environment
  vpc_id             = module.network.vpc_id
  vpc_cidr           = module.network.vpc_cidr
  private_subnet_ids = module.network.private_subnet_ids
}

module "agentcore" {
  source           = "../../modules/agentcore"
  environment      = var.environment
  region           = var.region
  bedrock_model_id = var.bedrock_model_id
}

############################################
# MSK bootstrap brokers — data plane API 호출 (apply 시 1회)
############################################

data "aws_msk_bootstrap_brokers" "this" {
  cluster_arn = module.msk_serverless.cluster_arn
}

module "ecs_generators" {
  source = "../../modules/ecs_generators"

  environment        = var.environment
  region             = var.region
  vpc_id             = module.network.vpc_id
  private_subnet_ids = module.network.private_subnet_ids

  logs_bucket     = module.s3_logs.bucket_name
  logs_bucket_arn = module.s3_logs.bucket_arn

  pg_host          = replace(module.aurora_postgres.endpoint, "/:.*$/", "")
  pg_dbname        = module.aurora_postgres.database_name
  pg_secret_arn    = module.aurora_postgres.master_user_secret_arn
  mysql_host       = replace(module.rds_mysql.endpoint, "/:.*$/", "")
  mysql_dbname     = module.rds_mysql.database_name
  mysql_secret_arn = module.rds_mysql.master_user_secret_arn

  msk_bootstrap = data.aws_msk_bootstrap_brokers.this.bootstrap_brokers_sasl_iam
  kafka_topic   = "dbaops.orders"
}

############################################
# MCP Lambdas — 모든 컨테이너 이미지 기반
############################################
# 흐름: 첫 apply 는 image_pushed=false 로 ECR repo 만 생성 → scripts/build_mcp_images.sh 로
# 이미지 push → 두 번째 apply 에 image_pushed=true 로 함수 생성.
# variable mcp_images_pushed 로 한꺼번에 토글한다.
#
# (구버전 lambda_prometheus_query / lambda_cloudwatch_metrics / lambda_sql_readonly
#  은 awslabs / community MCP 서버로 대체되어 제거됨.)


############################################
# 우리 PoC 특화 MCP (직접 작성)
############################################

module "lambda_rds_pi" {
  source = "../../modules/lambda_mcp_image"

  environment  = var.environment
  tool_name    = "rds-pi"
  image_pushed = var.mcp_images_pushed
  timeout      = 30
  memory_size  = 512
  vpc_id       = module.network.vpc_id
  subnet_ids   = module.network.private_subnet_ids
  role_arn     = module.iam.mcp_lambda_base_role_arn
}

module "lambda_msk_metrics" {
  source = "../../modules/lambda_mcp_image"

  environment  = var.environment
  tool_name    = "msk-metrics"
  image_pushed = var.mcp_images_pushed
  timeout      = 30
  memory_size  = 512
  vpc_id       = module.network.vpc_id
  subnet_ids   = module.network.private_subnet_ids
  role_arn     = module.iam.mcp_lambda_base_role_arn

  env_vars = {
    KAFKA_CLUSTER_NAME  = module.msk_serverless.cluster_name
    KAFKA_DEFAULT_TOPIC = "dbaops.orders"
    KAFKA_DEFAULT_CG    = "dbaops-paused"
  }
}

module "lambda_s3_log_fetch" {
  source = "../../modules/lambda_mcp_image"

  environment  = var.environment
  tool_name    = "s3-log-fetch"
  image_pushed = var.mcp_images_pushed
  timeout      = 60
  memory_size  = 512
  vpc_id       = module.network.vpc_id
  subnet_ids   = module.network.private_subnet_ids
  role_arn     = module.iam.mcp_lambda_base_role_arn
}

module "lambda_aws_api" {
  source = "../../modules/lambda_mcp_image"

  environment  = var.environment
  tool_name    = "aws-api"
  image_pushed = var.mcp_images_pushed
  timeout      = 30
  memory_size  = 512
  vpc_id       = module.network.vpc_id
  subnet_ids   = module.network.private_subnet_ids
  role_arn     = module.iam.mcp_lambda_base_role_arn
}


############################################
# 기성 MCP 서버 wrap (awslabs / community) — stdio MCP 를 Lambda 에서 spawn
############################################

# awslabs cloudwatch-mcp-server (16+ tools: 메트릭/알람/Logs Insights)
module "lambda_awslabs_cloudwatch" {
  source = "../../modules/lambda_mcp_image"

  environment  = var.environment
  tool_name    = "awslabs-cloudwatch"
  image_pushed = var.mcp_images_pushed
  timeout      = 60
  memory_size  = 1024
  vpc_id       = module.network.vpc_id
  subnet_ids   = module.network.private_subnet_ids
  role_arn     = module.iam.mcp_lambda_base_role_arn
}

# awslabs aws-documentation-mcp-server (4 tools, public docs.aws.amazon.com 호출)
module "lambda_awslabs_aws_doc" {
  source = "../../modules/lambda_mcp_image"

  environment  = var.environment
  tool_name    = "awslabs-aws-doc"
  image_pushed = var.mcp_images_pushed
  timeout      = 30
  memory_size  = 512
  vpc_id       = module.network.vpc_id
  subnet_ids   = module.network.private_subnet_ids
  role_arn     = module.iam.mcp_lambda_base_role_arn
}

# awslabs aws-api-mcp-server (call_aws / suggest_aws_commands — read-only mode)
module "lambda_awslabs_aws_api" {
  source = "../../modules/lambda_mcp_image"

  environment  = var.environment
  tool_name    = "awslabs-aws-api"
  image_pushed = var.mcp_images_pushed
  timeout      = 60
  memory_size  = 1024
  vpc_id       = module.network.vpc_id
  subnet_ids   = module.network.private_subnet_ids
  role_arn     = module.iam.mcp_lambda_base_role_arn
}

# pab1it0/prometheus-mcp-server — self-hosted EC2 Prometheus
module "lambda_community_prometheus" {
  source = "../../modules/lambda_mcp_image"

  environment  = var.environment
  tool_name    = "community-prometheus"
  image_pushed = var.mcp_images_pushed
  timeout      = 30
  memory_size  = 512
  vpc_id       = module.network.vpc_id
  subnet_ids   = module.network.private_subnet_ids
  role_arn     = module.iam.mcp_lambda_base_role_arn

  env_vars = {
    PROMETHEUS_URL = module.ec2_prometheus.prometheus_endpoint
  }
}

# crystaldba/postgres-mcp (restricted RO mode) — Aurora PG via libpq
module "lambda_community_postgres" {
  source = "../../modules/lambda_mcp_image"

  environment  = var.environment
  tool_name    = "community-postgres"
  image_pushed = var.mcp_images_pushed
  timeout      = 60
  memory_size  = 1024
  vpc_id       = module.network.vpc_id
  subnet_ids   = module.network.private_subnet_ids
  role_arn     = module.iam.mcp_lambda_base_role_arn

  env_vars = {
    PG_HOST       = replace(module.aurora_postgres.endpoint, "/:.*$/", "")
    PG_DBNAME     = module.aurora_postgres.database_name
    PG_SECRET_ARN = module.aurora_postgres.master_user_secret_arn
    PG_PORT       = "5432"
  }
}

# benborla/mcp-server-mysql (RO default) — RDS MySQL via wire protocol
module "lambda_community_mysql" {
  source = "../../modules/lambda_mcp_image"

  environment  = var.environment
  tool_name    = "community-mysql"
  image_pushed = var.mcp_images_pushed
  timeout      = 60
  memory_size  = 1024
  vpc_id       = module.network.vpc_id
  subnet_ids   = module.network.private_subnet_ids
  role_arn     = module.iam.mcp_lambda_base_role_arn

  env_vars = {
    MYSQL_HOST       = replace(module.rds_mysql.endpoint, "/:.*$/", "")
    MYSQL_DB         = module.rds_mysql.database_name
    MYSQL_SECRET_ARN = module.rds_mysql.master_user_secret_arn
    MYSQL_PORT       = "3306"
  }
}

############################################
# Streamlit UI (CloudFront → ALB → Fargate Spot)
############################################

module "streamlit" {
  source = "../../modules/ecs_streamlit"

  environment           = var.environment
  region                = var.region
  vpc_id                = module.network.vpc_id
  public_subnet_ids     = module.network.public_subnet_ids
  private_subnet_ids    = module.network.private_subnet_ids
  ecs_cluster_arn       = module.ecs_generators.cluster_arn
  ecs_cluster_name      = module.ecs_generators.cluster_name
  gen_security_group_id = module.ecs_generators.task_security_group_id
  agentcore_runtime_arn = var.agentcore_runtime_arn
  image_pushed          = var.streamlit_image_pushed
}
