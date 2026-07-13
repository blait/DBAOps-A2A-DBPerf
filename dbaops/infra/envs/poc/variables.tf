variable "region" {
  description = "AWS region (서울 단일 리전)"
  type        = string
  default     = "ap-northeast-2"
}

variable "environment" {
  description = "환경 식별자"
  type        = string
  default     = "poc"
}

variable "vpc_cidr" {
  description = "PoC VPC CIDR"
  type        = string
  default     = "10.40.0.0/16"
}

variable "azs" {
  description = "사용할 가용영역 (2개)"
  type        = list(string)
  default     = ["ap-northeast-2a", "ap-northeast-2c"]
}

variable "bedrock_model_id" {
  description = "Bedrock 모델 ID"
  type        = string
  default     = "claude-opus-4-7"
}

variable "mcp_images_pushed" {
  description = "MCP Lambda 이미지가 ECR 에 push 된 후 true 로 두 번째 apply"
  type        = bool
  default     = false
}

variable "streamlit_image_pushed" {
  description = "Streamlit 이미지가 ECR 에 push 된 후 true 로 두 번째 apply (ECS service 생성)"
  type        = bool
  default     = false
}

variable "agentcore_runtime_arn" {
  description = "Streamlit task 가 사용할 AgentCore Runtime ARN. register_gateway_targets.py 가 만든 후 -var 로 주입. 빈 값이면 UI 가 경고만 표시."
  type        = string
  default     = ""
}
