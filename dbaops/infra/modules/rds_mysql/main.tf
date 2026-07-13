############################################
# RDS MySQL module — Phase 2
############################################
# MySQL 8.0, db.t4g.micro, slow + general → CWLogs.
# manage_master_user_password 로 Secrets Manager 자동 관리.

resource "aws_db_subnet_group" "this" {
  name       = "dbaops-${var.environment}-mysql"
  subnet_ids = var.private_subnet_ids
  tags       = { Name = "dbaops-${var.environment}-mysql" }
}

resource "aws_security_group" "this" {
  name_prefix = "dbaops-${var.environment}-mysql-"
  vpc_id      = var.vpc_id
  description = "RDS MySQL"

  ingress {
    description = "MySQL from VPC"
    from_port   = 3306
    to_port     = 3306
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "dbaops-${var.environment}-mysql" }
}

resource "aws_db_parameter_group" "this" {
  name        = "dbaops-${var.environment}-mysql80"
  family      = "mysql8.0"
  description = "DBAOps MySQL params"

  parameter {
    name  = "slow_query_log"
    value = "1"
  }
  parameter {
    # PoC: 슬로우 쿼리 시나리오의 hash join 쿼리가 실측 ~340ms 라 1s 임계값을 넘지 못해
    # slow log 에 안 찍힘. 0.3s 로 낮춰 시나리오 효과를 보장.
    name  = "long_query_time"
    value = "0.3"
  }
  parameter {
    # 인덱스 없이 풀스캔하는 쿼리는 무조건 slow log 로 — 시나리오의 핵심 보장.
    name         = "log_queries_not_using_indexes"
    value        = "1"
    apply_method = "pending-reboot"
  }
  parameter {
    name  = "general_log"
    value = "1"
  }
  parameter {
    # TABLE 모드 — db_specialist 가 `SELECT * FROM mysql.slow_log` 로 즉시 SQL 분석.
    # FILE 형태 raw slow log 는 RDS DownloadDBLogFilePortion API 로 대체 가능 (log_specialist 경로).
    # RDS MySQL 8.0 parameter group API 가 'TABLE,FILE' 같은 comma-list 를 enum 으로 잘못
    # 처리해 InvalidParameterValue 가 떨어진다. 그래서 TABLE 단독으로 설정.
    name  = "log_output"
    value = "TABLE"
  }
  parameter {
    # t4g.micro default=0. performance_schema 끄면 events_statements_summary_by_digest /
    # data_lock_waits / events_waits_* 테이블이 비어 db_specialist 의 디지스트/락 분석 불능.
    # static 파라미터라 reboot 필수.
    name         = "performance_schema"
    value        = "1"
    apply_method = "pending-reboot"
  }
}

resource "aws_db_instance" "this" {
  identifier              = "dbaops-${var.environment}-mysql"
  engine                  = "mysql"
  engine_version          = var.engine_version
  instance_class          = var.instance_class
  allocated_storage       = 20
  storage_type            = "gp3"
  storage_encrypted       = true
  db_subnet_group_name    = aws_db_subnet_group.this.name
  vpc_security_group_ids  = [aws_security_group.this.id]
  parameter_group_name    = aws_db_parameter_group.this.name
  db_name                 = var.database_name
  username                = var.master_username
  manage_master_user_password = true
  backup_retention_period = 1
  skip_final_snapshot     = true
  apply_immediately       = true
  # t4g.micro 는 PI 미지원. db_subgraph 는 PG 의 PI 만 사용한다.
  performance_insights_enabled = false
  enabled_cloudwatch_logs_exports = ["error", "slowquery", "general"]
  publicly_accessible     = false
  deletion_protection     = false
}
