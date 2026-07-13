############################################
# ECS Streamlit module — public demo via CloudFront → ALB → Fargate Spot
############################################
# CloudFront default *.cloudfront.net 도메인 / 인증 없음 / 1 task.
# 첫 apply 는 image_pushed=false 로 ECR/ALB/CloudFront 만 생성. 이미지 push 후
# image_pushed=true 로 두 번째 apply → ECS service 가 task 를 띄운다.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# CloudFront IP prefix list — ALB SG 가 CloudFront edge 만 허용하도록.
data "aws_ec2_managed_prefix_list" "cloudfront" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

############################################
# ECR
############################################

resource "aws_ecr_repository" "streamlit" {
  name                 = "dbaops-streamlit"
  image_tag_mutability = "MUTABLE"
  force_delete         = true
  image_scanning_configuration { scan_on_push = true }
}

############################################
# IAM
############################################

resource "aws_iam_role" "exec" {
  name = "dbaops-${var.environment}-streamlit-exec"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "exec_managed" {
  role       = aws_iam_role.exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role" "task" {
  name = "dbaops-${var.environment}-streamlit-task"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action = "sts:AssumeRole"
    }]
  })
}

# Streamlit 컨테이너가 호출하는 AWS API 권한:
# - bedrock-agentcore:InvokeAgentRuntime — 에이전트 호출
# - bedrock-agentcore-control:* (read) — runtime 메타 조회
# - ecs:RunTask / DescribeTasks / DescribeTaskDefinition / ListTasks / StopTask — 시나리오 트리거 + 모니터
# - ecs:DescribeServices — 자기 자신 확인용
# - logs:GetLogEvents / DescribeLogStreams — 시나리오 task 로그 tail
# - iam:PassRole — RunTask 시 generator task role 통과
# - ec2:DescribeSecurityGroups — UI 가 generator SG 자동 lookup
resource "aws_iam_role_policy" "task" {
  role = aws_iam_role.task.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:InvokeAgentRuntime",
          "bedrock-agentcore:GetAgentRuntime",
          "bedrock-agentcore-control:GetAgentRuntime",
          "bedrock-agentcore-control:ListAgentRuntimes",
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ecs:RunTask",
          "ecs:DescribeTasks",
          "ecs:DescribeTaskDefinition",
          "ecs:ListTasks",
          "ecs:StopTask",
          "ecs:DescribeServices",
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = ["iam:PassRole"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ec2:DescribeSecurityGroups",
          "ec2:DescribeSubnets",
          "ec2:DescribeVpcs",
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "logs:GetLogEvents",
          "logs:DescribeLogStreams",
          "logs:DescribeLogGroups",
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = ["sts:GetCallerIdentity"]
        Resource = "*"
      },
    ]
  })
}

############################################
# Security groups
############################################

# ALB — CloudFront edge 만 인바운드 80 허용
resource "aws_security_group" "alb" {
  name_prefix = "dbaops-${var.environment}-streamlit-alb-"
  vpc_id      = var.vpc_id
  description = "Streamlit ALB - CloudFront edges only"

  ingress {
    description     = "HTTP from CloudFront edges"
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    prefix_list_ids = [data.aws_ec2_managed_prefix_list.cloudfront.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "dbaops-${var.environment}-streamlit-alb" }
}

# ECS task — ALB SG 만 인바운드 8501 허용
resource "aws_security_group" "task" {
  name_prefix = "dbaops-${var.environment}-streamlit-task-"
  vpc_id      = var.vpc_id
  description = "Streamlit ECS task - ALB only"

  ingress {
    description     = "Streamlit from ALB"
    from_port       = 8501
    to_port         = 8501
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "dbaops-${var.environment}-streamlit-task" }
}

############################################
# ALB
############################################

resource "aws_lb" "this" {
  name               = "dbaops-${var.environment}-streamlit"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.public_subnet_ids
  idle_timeout       = 600  # Streamlit WebSocket
}

resource "aws_lb_target_group" "this" {
  name_prefix = "dbsl-"
  port        = 8501
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  health_check {
    enabled             = true
    path                = "/_stcore/health"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 15
    timeout             = 5
    matcher             = "200"
  }

  # WebSocket session 재사용
  stickiness {
    enabled = true
    type    = "lb_cookie"
    cookie_duration = 3600
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.this.arn
  }
}

############################################
# CloudWatch Logs
############################################

resource "aws_cloudwatch_log_group" "task" {
  name              = "/ecs/dbaops-${var.environment}-streamlit"
  retention_in_days = 7
}

############################################
# ECS task definition
############################################

resource "aws_ecs_task_definition" "streamlit" {
  family                   = "dbaops-${var.environment}-streamlit"
  cpu                      = "512"
  memory                   = "1024"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  execution_role_arn       = aws_iam_role.exec.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([{
    name      = "streamlit"
    image     = "${aws_ecr_repository.streamlit.repository_url}:${var.image_tag}"
    essential = true
    portMappings = [
      { containerPort = 8501, protocol = "tcp" }
    ]
    environment = [
      { name = "AWS_REGION",            value = var.region },
      { name = "BEDROCK_REGION",        value = var.region },
      { name = "AGENTCORE_RUNTIME_ARN", value = var.agentcore_runtime_arn },
      { name = "ECS_CLUSTER",           value = var.ecs_cluster_name },
      { name = "ECS_SUBNETS",           value = join(",", var.private_subnet_ids) },
      { name = "ECS_SECURITY_GROUPS",   value = var.gen_security_group_id },
      { name = "GEN_REFRESH_SEC",       value = "5" },
      { name = "STREAMLIT_SERVER_HEADLESS",      value = "true" },
      { name = "STREAMLIT_SERVER_ENABLE_CORS",   value = "false" },
      { name = "STREAMLIT_SERVER_ENABLE_XSRF_PROTECTION", value = "false" },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.task.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "streamlit"
      }
    }
  }])
}

############################################
# ECS service — image push 후에만 생성 (count 토글)
############################################

resource "aws_ecs_service" "streamlit" {
  count           = var.image_pushed ? 1 : 0
  name            = "dbaops-${var.environment}-streamlit"
  cluster         = var.ecs_cluster_arn
  task_definition = aws_ecs_task_definition.streamlit.arn
  desired_count   = 1
  launch_type     = null

  capacity_provider_strategy {
    capacity_provider = "FARGATE_SPOT"
    weight            = 1
    base              = 0
  }

  network_configuration {
    subnets          = var.public_subnet_ids   # ALB 와 같은 public subnet, NAT 없이 ECR pull
    security_groups  = [aws_security_group.task.id]
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.this.arn
    container_name   = "streamlit"
    container_port   = 8501
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  depends_on = [aws_lb_listener.http]

  lifecycle {
    ignore_changes = [task_definition]   # update-service 로 외부에서 갱신 가능
  }
}

############################################
# CloudFront — *.cloudfront.net 도메인, ALB origin
############################################

resource "aws_cloudfront_distribution" "this" {
  enabled             = true
  comment             = "DBAOps-Agent Streamlit demo"
  default_root_object = ""
  http_version        = "http2"
  is_ipv6_enabled     = true
  price_class         = "PriceClass_200"   # 한국 + 주요 지역만

  origin {
    domain_name = aws_lb.this.dns_name
    origin_id   = "alb"

    custom_origin_config {
      http_port                = 80
      https_port               = 443
      origin_protocol_policy   = "http-only"
      origin_ssl_protocols     = ["TLSv1.2"]
      origin_read_timeout      = 60
      origin_keepalive_timeout = 60
    }
  }

  default_cache_behavior {
    target_origin_id       = "alb"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true

    # Streamlit 은 dynamic + WebSocket — 캐시 끄고 모든 헤더/쿠키/쿼리 forward.
    cache_policy_id          = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad" # CachingDisabled
    origin_request_policy_id = "216adef6-5c7f-47e4-b989-5492eafa07d3" # AllViewer
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}
