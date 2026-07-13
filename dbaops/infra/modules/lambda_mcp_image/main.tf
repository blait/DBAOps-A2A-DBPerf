############################################
# lambda_mcp_image module — 컨테이너 이미지 기반 MCP Lambda
############################################
# ECR repo 만 만든다. 이미지 빌드/push 는 scripts/build_mcp_images.sh 로.
# 이 모듈을 첫 apply 할 때 image_uri 가 비어있을 수 있어 placeholder image 를 사용.

resource "aws_ecr_repository" "this" {
  name                 = "dbaops-mcp-${var.tool_name}"
  image_tag_mutability = "MUTABLE"
  force_delete         = true
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_security_group" "this" {
  name_prefix = "dbaops-${var.environment}-${var.tool_name}-"
  vpc_id      = var.vpc_id
  description = "Lambda MCP ${var.tool_name}"

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "dbaops-${var.environment}-${var.tool_name}" }
}

resource "aws_cloudwatch_log_group" "this" {
  name              = "/aws/lambda/dbaops-${var.environment}-${var.tool_name}"
  retention_in_days = 7
}

resource "aws_lambda_function" "this" {
  count         = var.image_pushed ? 1 : 0
  function_name = "dbaops-${var.environment}-${var.tool_name}"
  role          = var.role_arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.this.repository_url}:${var.image_tag}"
  architectures = ["arm64"]
  timeout       = var.timeout
  memory_size   = var.memory_size

  environment {
    variables = var.env_vars
  }

  vpc_config {
    subnet_ids         = var.subnet_ids
    security_group_ids = concat([aws_security_group.this.id], var.extra_security_group_ids)
  }

  depends_on = [aws_cloudwatch_log_group.this]
}
