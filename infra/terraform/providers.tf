# AutoPod Cloud -- Terraform provider configuration.
#
# Scope reminder (ARCHITECTURE.md Section 4.1/4.2): this creates AWS
# networking, compute, load-balancing, scaling and monitoring-alarm
# INFRASTRUCTURE only. It does not install Docker, configure any host, or
# deploy the AutoPod application itself -- that's Ansible's job (see
# infra/ansible/), run after `terraform apply` against the IPs this
# produces.
#
# IMPORTANT: Nothing in this directory has been applied against a real AWS
# account from this repository. See PROJECT_PROGRESS.md "Infrastructure
# Status" for exactly what has and hasn't been verified, and NEVER run
# `terraform apply` without the team's explicit go-ahead and real AWS
# credentials configured outside this repo (never commit credentials here).

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Local state is an accepted, deliberate choice for a small team that can
  # coordinate `terraform apply` manually (ARCHITECTURE.md Section 4.4). A
  # remote backend (S3 + DynamoDB lock table) is listed there as an
  # OPTIONAL upgrade, not required for this project's scope. If the team
  # adds one later, add a `backend "s3" {}` block here and run
  # `terraform init -migrate-state`.
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = "autopod-cloud"
      ManagedBy = "terraform"
    }
  }
}
