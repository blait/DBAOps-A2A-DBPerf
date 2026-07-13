variable "environment" {
  type = string
}

variable "vpc_cidr" {
  type = string
}

variable "azs" {
  type = list(string)
}

variable "enable_nat_instance" {
  description = "fck-nat 기반 t4g.nano NAT instance 활성화"
  type        = bool
  default     = true
}

variable "enable_s3_endpoint" {
  description = "S3 Gateway endpoint 활성화 (무료)"
  type        = bool
  default     = true
}

variable "interface_endpoints" {
  description = "Interface VPC endpoint 서비스 이름 (예: secretsmanager, bedrock-runtime)"
  type        = list(string)
  default     = []
}
