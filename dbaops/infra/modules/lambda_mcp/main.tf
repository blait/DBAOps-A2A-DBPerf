############################################
# lambda_mcp module — MCP 도구 Lambda 1개를 패키징/배포
############################################
# 호출자: source_dir 에 handler.py + requirements.txt 가 있어야 한다.
# Lambda 는 VPC private subnet 에 attach (Prometheus / DB 접근).

data "archive_file" "code" {
  type        = "zip"
  source_dir  = var.source_dir
  output_path = "${path.module}/.build/${var.tool_name}.zip"
  excludes    = ["__pycache__", ".pytest_cache", ".venv", "*.pyc"]
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
  function_name    = "dbaops-${var.environment}-${var.tool_name}"
  role             = var.role_arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = var.handler
  filename         = data.archive_file.code.output_path
  source_code_hash = data.archive_file.code.output_base64sha256
  timeout          = var.timeout
  memory_size      = var.memory_size

  environment {
    variables = var.env_vars
  }

  vpc_config {
    subnet_ids         = var.subnet_ids
    security_group_ids = concat([aws_security_group.this.id], var.extra_security_group_ids)
  }

  depends_on = [aws_cloudwatch_log_group.this]
}
