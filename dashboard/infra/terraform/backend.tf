terraform {
  backend "s3" {
    bucket  = "bridgeway-tf-state-prod"
    key     = "naic-dashboard/terraform.tfstate"
    region  = "us-east-1"
    encrypt = true
  }
}
