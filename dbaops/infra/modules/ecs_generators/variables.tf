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

variable "private_subnet_ids" {
  type = list(string)
}

variable "logs_bucket" {
  type = string
}

variable "logs_bucket_arn" {
  type = string
}

variable "pg_host" {
  type = string
}

variable "pg_dbname" {
  type    = string
  default = "dbaops"
}

variable "pg_secret_arn" {
  type = string
}

variable "mysql_host" {
  type = string
}

variable "mysql_dbname" {
  type    = string
  default = "dbaops"
}

variable "mysql_secret_arn" {
  type = string
}

variable "msk_bootstrap" {
  description = "MSK bootstrap brokers (콤마 구분)"
  type        = string
  default     = ""
}

variable "kafka_topic" {
  type    = string
  default = "dbaops.orders"
}

variable "duration_sec" {
  description = "워크로드별 지속 시간 (초)"
  type        = map(number)
  default = {
    baseline         = 600
    lock_contention  = 180
    slow_query       = 120
    connection_spike = 90
    kafka_isr_shrink = 60
    cpu_burn         = 180
    disk_io_burst    = 240
  }
}

variable "schedules" {
  description = "EventBridge Scheduler cron — workload별"
  type        = map(string)
  default = {
    baseline         = "rate(15 minutes)"
    lock_contention  = "rate(30 minutes)"
    slow_query       = "rate(20 minutes)"
    connection_spike = "rate(45 minutes)"
    kafka_isr_shrink = "rate(60 minutes)"
    cpu_burn         = "rate(50 minutes)"
    disk_io_burst    = "rate(55 minutes)"
  }
}
