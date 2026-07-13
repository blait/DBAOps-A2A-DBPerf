variable "environment" {
  type = string
}

variable "tool_name" {
  description = "MCP 도구 이름 (예: prometheus_query)"
  type        = string
}

variable "source_dir" {
  description = "Lambda 코드 디렉토리 (mcp_tools/<name>)"
  type        = string
}

variable "handler" {
  type    = string
  default = "handler.handler"
}

variable "timeout" {
  type    = number
  default = 30
}

variable "memory_size" {
  type    = number
  default = 256
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
