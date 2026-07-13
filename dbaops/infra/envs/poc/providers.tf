terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = "DBAOps-Agent"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
