############################################
# MSK Serverless module — Phase 3
############################################
# IAM auth, private subnets. 토픽은 Kafka admin client 로 별도 생성 (ECS init).

resource "aws_security_group" "this" {
  name_prefix = "dbaops-${var.environment}-msk-"
  vpc_id      = var.vpc_id
  description = "MSK Serverless"

  ingress {
    description = "Kafka IAM from VPC"
    from_port   = 9098
    to_port     = 9098
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "dbaops-${var.environment}-msk" }
}

resource "aws_msk_serverless_cluster" "this" {
  cluster_name = "dbaops-${var.environment}"

  vpc_config {
    subnet_ids         = var.private_subnet_ids
    security_group_ids = [aws_security_group.this.id]
  }

  client_authentication {
    sasl {
      iam {
        enabled = true
      }
    }
  }

  tags = { Name = "dbaops-${var.environment}-msk" }
}
