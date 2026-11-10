# AutoPod Cloud -- Amazon ECR Repository for sweet-treats-bakery.
# Provides secure, private container image storage within AWS us-east-1.
# Worker nodes pull deployment images directly from this repository over
# outbound HTTPS using EC2 instance profile credentials.

resource "aws_ecr_repository" "sweet_treats_bakery" {
  name                 = "autopod-sweet-treats-bakery"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = {
    Name        = "autopod-sweet-treats-bakery"
    Application = "sweet-treats-bakery"
    ManagedBy   = "Terraform"
  }
}

resource "aws_ecr_lifecycle_policy" "sweet_treats_bakery" {
  repository = aws_ecr_repository.sweet_treats_bakery.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Retain last 10 deployment images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 10
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}

output "ecr_repository_url" {
  description = "Amazon ECR repository URL for sweet-treats-bakery"
  value       = aws_ecr_repository.sweet_treats_bakery.repository_url
}
