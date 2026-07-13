variable "environment" {
  type = string
}

variable "region" {
  type    = string
  default = "ap-northeast-2"
}

variable "bedrock_model_id" {
  type    = string
  default = "claude-opus-4-7"
}
