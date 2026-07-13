variable "environment" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "vpc_cidr" {
  type = string
}

variable "subnet_id" {
  description = "private subnet id (NAT 라우트 필요)"
  type        = string
}

variable "instance_type" {
  type    = string
  default = "t4g.small"
}

variable "use_spot" {
  description = "Spot 사용 여부"
  type        = bool
  default     = true
}
