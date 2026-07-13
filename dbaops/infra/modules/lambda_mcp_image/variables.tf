variable "environment" {
  type = string
}

variable "tool_name" {
  type = string
}

variable "image_tag" {
  type    = string
  default = "latest"
}

variable "image_pushed" {
  description = "true 인 경우에만 Lambda 함수를 생성. ECR 이미지 push 후 두 번째 apply 에서 true 로 전환."
  type        = bool
  default     = false
}

variable "timeout" {
  type    = number
  default = 30
}

variable "memory_size" {
  type    = number
  default = 512
}

variable "vpc_id" {
  type = string
}

variable "subnet_ids" {
  type = list(string)
}

variable "extra_security_group_ids" {
  type    = list(string)
  default = []
}

variable "role_arn" {
  type = string
}

variable "env_vars" {
  type    = map(string)
  default = {}
}
