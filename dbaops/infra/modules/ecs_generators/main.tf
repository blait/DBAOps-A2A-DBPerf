############################################
# ECS generators module — Phase 2
############################################
# Fargate Spot, EventBridge Scheduler 트리거.
# data_generator + log_generator task definition 을 한 모듈에서 관리.

locals {
  data_workloads = ["baseline", "lock_contention", "slow_query", "connection_spike", "kafka_isr_shrink", "cpu_burn", "disk_io_burst"]
  log_sources    = ["postgres", "mysql", "kafka"]
}

############################################
# ECR repos
############################################

resource "aws_ecr_repository" "data_gen" {
  name                 = "dbaops-data-generator"
  image_tag_mutability = "MUTABLE"
  force_delete         = true
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_ecr_repository" "log_gen" {
  name                 = "dbaops-log-generator"
  image_tag_mutability = "MUTABLE"
  force_delete         = true
  image_scanning_configuration { scan_on_push = true }
}

############################################
# Cluster + IAM
############################################

resource "aws_ecs_cluster" "this" {
  name = "dbaops-${var.environment}"
  setting {
    name  = "containerInsights"
    value = "disabled"
  }
}

resource "aws_ecs_cluster_capacity_providers" "this" {
  cluster_name       = aws_ecs_cluster.this.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE_SPOT"
    weight            = 1
    base              = 0
  }
}

resource "aws_iam_role" "exec" {
  name = "dbaops-${var.environment}-ecs-exec"
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

resource "aws_iam_role_policy" "exec_secrets" {
  role = aws_iam_role.exec.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["secretsmanager:GetSecretValue"]
      Resource = compact([var.pg_secret_arn, var.mysql_secret_arn])
    }]
  })
}

resource "aws_iam_role" "task" {
  name = "dbaops-${var.environment}-ecs-task"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "task" {
  role = aws_iam_role.task.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue"]
        Resource = compact([var.pg_secret_arn, var.mysql_secret_arn])
      },
      {
        Effect = "Allow"
        Action = [
          "kafka-cluster:Connect",
          "kafka-cluster:DescribeCluster",
          "kafka-cluster:ReadData",
          "kafka-cluster:WriteData",
          "kafka-cluster:CreateTopic",
          "kafka-cluster:DescribeTopic",
          "kafka-cluster:AlterTopic",
          "kafka-cluster:AlterGroup",
          "kafka-cluster:DescribeGroup"
        ]
        Resource = ["*"]
      },
      {
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject",
          "s3:ListBucket"
        ]
        Resource = compact([
          var.logs_bucket_arn,
          var.logs_bucket_arn != "" ? "${var.logs_bucket_arn}/*" : ""
        ])
      },
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogStreams"
        ]
        Resource = "*"
      }
    ]
  })
}

resource "aws_security_group" "task" {
  name_prefix = "dbaops-${var.environment}-gen-"
  vpc_id      = var.vpc_id
  description = "ECS generator tasks"
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "dbaops-${var.environment}-gen" }
}

resource "aws_cloudwatch_log_group" "task" {
  name              = "/ecs/dbaops-${var.environment}-generators"
  retention_in_days = 7
}

############################################
# Data generator task definitions (workload별)
############################################

resource "aws_ecs_task_definition" "data_gen" {
  for_each                 = toset(local.data_workloads)
  family                   = "dbaops-${var.environment}-data-${replace(each.key, "_", "-")}"
  cpu                      = "256"
  memory                   = "512"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  execution_role_arn       = aws_iam_role.exec.arn
  task_role_arn            = aws_iam_role.task.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }
  container_definitions = jsonencode([{
    name  = "data-gen"
    image = "${aws_ecr_repository.data_gen.repository_url}:latest"
    essential = true
    environment = [
      { name = "WORKLOAD",        value = each.key },
      { name = "DURATION_SEC",    value = tostring(var.duration_sec[each.key]) },
      { name = "AWS_REGION",      value = var.region },
      { name = "PG_HOST",         value = var.pg_host },
      { name = "PG_DBNAME",       value = var.pg_dbname },
      { name = "PG_SECRET_ARN",   value = var.pg_secret_arn },
      { name = "MYSQL_HOST",      value = var.mysql_host },
      { name = "MYSQL_DBNAME",    value = var.mysql_dbname },
      { name = "MYSQL_SECRET_ARN", value = var.mysql_secret_arn },
      { name = "MSK_BOOTSTRAP",   value = var.msk_bootstrap },
      { name = "KAFKA_TOPIC",     value = var.kafka_topic },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.task.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "data-${replace(each.key, "_", "-")}"
      }
    }
  }])
}

############################################
# Log generator task definitions (source × mode)
############################################

resource "aws_ecs_task_definition" "log_gen" {
  for_each                 = { for s in local.log_sources : s => s }
  family                   = "dbaops-${var.environment}-log-${each.key}"
  cpu                      = "256"
  memory                   = "512"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  execution_role_arn       = aws_iam_role.exec.arn
  task_role_arn            = aws_iam_role.task.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }
  container_definitions = jsonencode([{
    name  = "log-gen"
    image = "${aws_ecr_repository.log_gen.repository_url}:latest"
    essential = true
    environment = [
      { name = "SOURCE",       value = each.key },
      { name = "MODE",         value = "baseline" },
      { name = "DURATION_SEC", value = "300" },
      { name = "AWS_REGION",   value = var.region },
      { name = "S3_BUCKET",    value = var.logs_bucket },
      { name = "S3_PREFIX",    value = "logs" },
      { name = "CW_LOG_GROUP", value = "/dbaops/${var.environment}/${each.key}" },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.task.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "log-${each.key}"
      }
    }
  }])
}

############################################
# EventBridge Scheduler — workload별 cron
############################################

resource "aws_iam_role" "scheduler" {
  name = "dbaops-${var.environment}-scheduler"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  role = aws_iam_role.scheduler.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["ecs:RunTask"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = ["iam:PassRole"]
        Resource = [aws_iam_role.exec.arn, aws_iam_role.task.arn]
      }
    ]
  })
}

resource "aws_scheduler_schedule" "data_gen" {
  for_each   = toset(local.data_workloads)
  name       = "dbaops-${var.environment}-data-${replace(each.key, "_", "-")}"
  group_name = "default"

  flexible_time_window { mode = "OFF" }

  schedule_expression = var.schedules[each.key]

  target {
    arn      = aws_ecs_cluster.this.arn
    role_arn = aws_iam_role.scheduler.arn
    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.data_gen[each.key].arn
      task_count          = 1
      capacity_provider_strategy {
        capacity_provider = "FARGATE_SPOT"
        weight            = 1
        base              = 0
      }
      network_configuration {
        subnets          = var.private_subnet_ids
        security_groups  = [aws_security_group.task.id]
        assign_public_ip = false
      }
    }
  }
}

resource "aws_scheduler_schedule" "log_gen" {
  for_each   = { for s in local.log_sources : s => s }
  name       = "dbaops-${var.environment}-log-${each.key}"
  group_name = "default"

  flexible_time_window { mode = "OFF" }

  schedule_expression = "rate(5 minutes)"

  target {
    arn      = aws_ecs_cluster.this.arn
    role_arn = aws_iam_role.scheduler.arn
    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.log_gen[each.key].arn
      task_count          = 1
      capacity_provider_strategy {
        capacity_provider = "FARGATE_SPOT"
        weight            = 1
        base              = 0
      }
      network_configuration {
        subnets          = var.private_subnet_ids
        security_groups  = [aws_security_group.task.id]
        assign_public_ip = false
      }
    }
  }
}
