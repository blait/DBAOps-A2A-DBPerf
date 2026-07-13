terraform {
  required_version = ">= 1.7.0"

  backend "s3" {
    bucket         = "dbaops-tfstate-986930576673-ap-northeast-2"
    key            = "envs/poc/terraform.tfstate"
    region         = "ap-northeast-2"
    dynamodb_table = "dbaops-tfstate-lock"
    encrypt        = true
  }
}
