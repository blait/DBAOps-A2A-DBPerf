############################################
# EC2 Prometheus module — Phase 1
############################################
# t4g.small Spot, gp3 20GB, user_data 로 Prometheus + Node Exporter 부트스트랩.
# Lambda(prometheus_query) 가 :9090 으로 호출.

data "aws_ami" "al2023_arm64" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-*-arm64"]
  }
}

resource "aws_security_group" "prometheus" {
  name_prefix = "dbaops-${var.environment}-prom-"
  vpc_id      = var.vpc_id
  description = "Prometheus + Node Exporter"

  ingress {
    description = "Prometheus from VPC"
    from_port   = 9090
    to_port     = 9090
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  ingress {
    description = "Node Exporter from VPC"
    from_port   = 9100
    to_port     = 9100
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "dbaops-${var.environment}-prometheus" }
}

resource "aws_iam_role" "prometheus" {
  name = "dbaops-${var.environment}-prometheus"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "prometheus_ssm" {
  role       = aws_iam_role.prometheus.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "prometheus_cw" {
  role = aws_iam_role.prometheus.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "cloudwatch:GetMetricStatistics",
        "cloudwatch:ListMetrics",
        "ec2:DescribeTags"
      ]
      Resource = "*"
    }]
  })
}

resource "aws_iam_instance_profile" "prometheus" {
  name = "dbaops-${var.environment}-prometheus"
  role = aws_iam_role.prometheus.name
}

locals {
  user_data = <<-EOT
    #!/bin/bash
    set -euo pipefail

    dnf update -y
    dnf install -y wget tar gzip

    # Node Exporter
    NE_VERSION=1.8.2
    cd /opt
    wget -q https://github.com/prometheus/node_exporter/releases/download/v$${NE_VERSION}/node_exporter-$${NE_VERSION}.linux-arm64.tar.gz
    tar xzf node_exporter-$${NE_VERSION}.linux-arm64.tar.gz
    mv node_exporter-$${NE_VERSION}.linux-arm64 /opt/node_exporter
    cat >/etc/systemd/system/node_exporter.service <<UNIT
    [Unit]
    Description=Node Exporter
    After=network.target
    [Service]
    ExecStart=/opt/node_exporter/node_exporter
    Restart=always
    [Install]
    WantedBy=multi-user.target
    UNIT
    systemctl enable --now node_exporter

    # Prometheus
    PROM_VERSION=2.55.0
    cd /opt
    wget -q https://github.com/prometheus/prometheus/releases/download/v$${PROM_VERSION}/prometheus-$${PROM_VERSION}.linux-arm64.tar.gz
    tar xzf prometheus-$${PROM_VERSION}.linux-arm64.tar.gz
    mv prometheus-$${PROM_VERSION}.linux-arm64 /opt/prometheus
    mkdir -p /var/lib/prometheus

    cat >/opt/prometheus/prometheus.yml <<CFG
    global:
      scrape_interval: 15s
    scrape_configs:
      - job_name: prometheus
        static_configs:
          - targets: ['localhost:9090']
      - job_name: node
        static_configs:
          - targets: ['localhost:9100']
    CFG

    cat >/etc/systemd/system/prometheus.service <<UNIT
    [Unit]
    Description=Prometheus
    After=network.target
    [Service]
    ExecStart=/opt/prometheus/prometheus \
      --config.file=/opt/prometheus/prometheus.yml \
      --storage.tsdb.path=/var/lib/prometheus \
      --storage.tsdb.retention.time=7d \
      --web.listen-address=0.0.0.0:9090
    Restart=always
    [Install]
    WantedBy=multi-user.target
    UNIT
    systemctl enable --now prometheus
  EOT
}

resource "aws_instance" "prometheus" {
  ami                         = data.aws_ami.al2023_arm64.id
  instance_type               = var.instance_type
  subnet_id                   = var.subnet_id
  vpc_security_group_ids      = [aws_security_group.prometheus.id]
  iam_instance_profile        = aws_iam_instance_profile.prometheus.name
  user_data                   = local.user_data
  user_data_replace_on_change = true
  associate_public_ip_address = false

  root_block_device {
    volume_size = 20
    volume_type = "gp3"
  }

  dynamic "instance_market_options" {
    for_each = var.use_spot ? [1] : []
    content {
      market_type = "spot"
      spot_options {
        spot_instance_type = "one-time"
      }
    }
  }

  tags = { Name = "dbaops-${var.environment}-prometheus" }
}
