variable "environment" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "vpc_cidr" {
  type = string
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "engine_version" {
  type    = string
  default = "15.17"
}

variable "instance_class" {
  type    = string
  default = "db.t4g.medium"
}

variable "database_name" {
  type    = string
  default = "dbaops"
}

variable "master_username" {
  type    = string
  default = "dbaops_admin"
}

variable "create_reader" {
  type    = bool
  default = true
}
