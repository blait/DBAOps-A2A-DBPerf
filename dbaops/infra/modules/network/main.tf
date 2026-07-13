############################################
# Network module — VPC / 2 AZ / NAT instance / VPC endpoints
############################################
# 비용 가드: NAT Gateway 대신 t4g.nano NAT instance.
# S3 endpoint 는 무료, Interface endpoint 는 사용된 ENI 시간당 과금.

data "aws_region" "current" {}

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "dbaops-${var.environment}" }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "dbaops-${var.environment}-igw" }
}

resource "aws_subnet" "public" {
  for_each                = { for i, az in var.azs : az => i }
  vpc_id                  = aws_vpc.this.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 4, each.value)
  availability_zone       = each.key
  map_public_ip_on_launch = true

  tags = {
    Name = "dbaops-${var.environment}-public-${each.key}"
    Tier = "public"
  }
}

resource "aws_subnet" "private" {
  for_each          = { for i, az in var.azs : az => i }
  vpc_id            = aws_vpc.this.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 4, each.value + 8)
  availability_zone = each.key

  tags = {
    Name = "dbaops-${var.environment}-private-${each.key}"
    Tier = "private"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  tags = { Name = "dbaops-${var.environment}-public-rt" }
}

resource "aws_route_table_association" "public" {
  for_each       = aws_subnet.public
  subnet_id      = each.value.id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "dbaops-${var.environment}-private-rt" }
}

resource "aws_route_table_association" "private" {
  for_each       = aws_subnet.private
  subnet_id      = each.value.id
  route_table_id = aws_route_table.private.id
}

############################################
# NAT instance (t4g.nano) — 비용 가드
############################################

data "aws_ami" "nat" {
  count       = var.enable_nat_instance ? 1 : 0
  most_recent = true
  # fck-nat 공식 publisher (https://fck-nat.dev/)
  owners      = ["568608671756"]

  filter {
    name   = "name"
    values = ["fck-nat-al2023-*-arm64-ebs"]
  }

  filter {
    name   = "architecture"
    values = ["arm64"]
  }
}

resource "aws_security_group" "nat" {
  count       = var.enable_nat_instance ? 1 : 0
  name_prefix = "dbaops-${var.environment}-nat-"
  vpc_id      = aws_vpc.this.id
  description = "NAT instance"

  ingress {
    description = "from VPC"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "dbaops-${var.environment}-nat" }
}

resource "aws_network_interface" "nat" {
  count             = var.enable_nat_instance ? 1 : 0
  subnet_id         = values(aws_subnet.public)[0].id
  security_groups   = [aws_security_group.nat[0].id]
  source_dest_check = false
  tags              = { Name = "dbaops-${var.environment}-nat-eni" }
}

resource "aws_instance" "nat" {
  count                = var.enable_nat_instance ? 1 : 0
  ami                  = data.aws_ami.nat[0].id
  instance_type        = "t4g.nano"
  iam_instance_profile = aws_iam_instance_profile.nat[0].name

  network_interface {
    network_interface_id = aws_network_interface.nat[0].id
    device_index         = 0
  }

  tags = { Name = "dbaops-${var.environment}-nat" }
}

resource "aws_iam_role" "nat" {
  count = var.enable_nat_instance ? 1 : 0
  name  = "dbaops-${var.environment}-nat"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "nat_ssm" {
  count      = var.enable_nat_instance ? 1 : 0
  role       = aws_iam_role.nat[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "nat" {
  count = var.enable_nat_instance ? 1 : 0
  name  = "dbaops-${var.environment}-nat"
  role  = aws_iam_role.nat[0].name
}

resource "aws_route" "private_nat" {
  count                  = var.enable_nat_instance ? 1 : 0
  route_table_id         = aws_route_table.private.id
  destination_cidr_block = "0.0.0.0/0"
  network_interface_id   = aws_network_interface.nat[0].id
}

############################################
# VPC endpoints
############################################

resource "aws_vpc_endpoint" "s3" {
  count             = var.enable_s3_endpoint ? 1 : 0
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${data.aws_region.current.name}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]
  tags              = { Name = "dbaops-${var.environment}-s3-endpoint" }
}

resource "aws_security_group" "vpce" {
  count       = length(var.interface_endpoints) > 0 ? 1 : 0
  name_prefix = "dbaops-${var.environment}-vpce-"
  vpc_id      = aws_vpc.this.id
  description = "VPC interface endpoints"

  ingress {
    description = "HTTPS from VPC"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "dbaops-${var.environment}-vpce" }
}

resource "aws_vpc_endpoint" "interface" {
  for_each            = toset(var.interface_endpoints)
  vpc_id              = aws_vpc.this.id
  service_name        = "com.amazonaws.${data.aws_region.current.name}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [for s in aws_subnet.private : s.id]
  security_group_ids  = [aws_security_group.vpce[0].id]
  private_dns_enabled = true
  tags                = { Name = "dbaops-${var.environment}-${each.key}-endpoint" }
}
