variable "environment" {
  type = string
}

variable "region" {
  type    = string
  default = "ap-northeast-2"
}

variable "vpc_id" {
  type = string
}

variable "public_subnet_ids" {
  type = list(string)
}

variable "private_subnet_ids" {
  description = "시나리오 트리거 시 ECS_SUBNETS env 로 전달"
  type        = list(string)
}

variable "agentcore_runtime_arn" {
  description = "비어있으면 task 가 경고만 띄움. register_gateway_targets.py 가 만든 후 -var 로 주입"
  type        = string
  default     = ""
}

variable "ecs_cluster_arn" {
  description = "기존 dbaops-poc 클러스터 재사용"
  type        = string
}

variable "ecs_cluster_name" {
  type = string
}

variable "gen_security_group_id" {
  description = "시나리오 generator task SG — UI 의 RunTask 가 이 SG 를 ECS_SECURITY_GROUPS env 로 사용"
  type        = string
}

variable "image_pushed" {
  description = "ECS service 를 만들지 결정. 첫 apply 는 false(ECR/ALB/CF 만), 이미지 push 후 true"
  type        = bool
  default     = false
}

variable "image_tag" {
  type    = string
  default = "latest"
}
