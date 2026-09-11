terraform {
  backend "s3" {
    bucket         = "bridgeway-tf-state-prod"
    key            = "web-change-tracker/terraform.tfstate"
    region         = "us-east-1"
    encrypt        = true
  }
}
