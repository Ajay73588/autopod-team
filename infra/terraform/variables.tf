# AutoPod Cloud -- Terraform variables.
#
# Nothing here is hardcoded to a specific AWS account, AMI, or key pair --
# every value a team member needs to supply is a variable, most with a
# sensible default that still requires conscious review (instance types,
# CIDR ranges) and a few with NO default where a wrong guess would be
# actively dangerous (the SSH key pair name, the admin CIDR).
#
# Usage: copy this pattern into a `terraform.tfvars` file (gitignored --
# see infra/terraform/.gitignore) rather than editing defaults here.

variable "aws_region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "us-east-1"
}

variable "availability_zones" {
  description = "Two AZs for the two public subnets (ALB requires >= 2)."
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b"]
}

variable "vpc_cidr" {
  description = "CIDR block for the AutoPod VPC."
  type        = string
  default     = "10.0.0.0/16"
}

variable "public_subnet_cidrs" {
  description = "CIDR blocks for the two public subnets, one per AZ, in the same order as availability_zones."
  type        = list(string)
  default     = ["10.0.1.0/24", "10.0.2.0/24"]
}

variable "key_pair_name" {
  description = <<-EOT
    Name of an EXISTING EC2 key pair in this AWS account/region, used for
    SSH access to the control plane and app nodes. Terraform does not
    create this for you (a created private key would have to be stored
    somewhere, which is a secret-handling problem this project avoids by
    requiring the team to create/import their own key pair first via the
    AWS console or `aws ec2 import-key-pair`). No default -- `terraform
    plan` will prompt for it if unset, which is intentional friction
    against accidentally applying with a placeholder.
  EOT
  type        = string
}

variable "admin_cidr" {
  description = <<-EOT
    CIDR block allowed to SSH (22) and reach the control plane's protected
    dashboard and API routes (/ and /api/*) via the Nginx reverse proxy.
    Should be the team's own IP(s) in /32 form (e.g. "203.0.113.4/32"),
    never "0.0.0.0/0" -- see ARCHITECTURE.md Section 12.2. No default,
    same reasoning as key_pair_name.
  EOT
  type        = string
}

variable "control_plane_instance_type" {
  description = "EC2 instance type for the control plane (EC2-1: AutoPod backend/dashboard + Prometheus + Grafana)."
  type        = string
  default     = "t3.small"
}

variable "app_node_instance_type" {
  description = "EC2 instance type for Auto-Scaling-managed application nodes."
  type        = string
  default     = "t3.micro"
}

variable "app_node_ami_id" {
  description = <<-EOT
    AMI ID for application nodes (Launch Template). Left with no default
    deliberately -- AMI IDs are region- and time-specific, and a stale
    default is worse than an explicit error asking the team to supply a
    current Amazon Linux 2023 AMI ID for their chosen region (e.g. via
    `aws ec2 describe-images --owners amazon --filters
    "Name=name,Values=al2023-ami-2023.*-x86_64" --query
    "sort_by(Images,&CreationDate)[-1].ImageId"`).
  EOT
  type        = string
}

variable "asg_min_size" {
  description = "Auto Scaling Group minimum size (ARCHITECTURE.md Section 10: min=2)."
  type        = number
  default     = 2
}

variable "asg_desired_capacity" {
  description = "Auto Scaling Group desired capacity."
  type        = number
  default     = 2
}

variable "asg_max_size" {
  description = <<-EOT
    Auto Scaling Group MAXIMUM size. Deliberately capped low
    (ARCHITECTURE.md Section 10: max=4) as a cost-control safety measure --
    a runaway scale-out event (e.g. a CloudWatch alarm misconfiguration,
    or a crash-loop that spikes CPU) can never cost more than
    `asg_max_size` instances' worth of EC2 time. Do not raise this without
    a deliberate reason.
  EOT
  type        = number
  default     = 4
}

variable "scale_out_cpu_threshold" {
  description = "Average CPU percent that triggers a scale-out alarm (ARCHITECTURE.md Section 10.1)."
  type        = number
  default     = 70
}

variable "scale_in_cpu_threshold" {
  description = "Average CPU percent that triggers a scale-in alarm."
  type        = number
  default     = 20
}

variable "app_target_port" {
  description = "Default container port the ALB target group health-checks and routes to on app nodes. A specific registered application's own target_port (AutoPod's own DB concept) is a deploy-time detail, not an infrastructure one -- this is the port app-node containers are expected to publish by convention."
  type        = number
  default     = 8081
}

variable "health_check_path" {
  description = "Default ALB target group health check path."
  type        = string
  default     = "/"
}

variable "autopod_control_plane_api_url" {
  description = <<-EOT
    URL (http://<control-plane-private-or-public-ip>:5000) that a newly
    launched app node's bootstrap user-data script calls to fetch the
    current version to self-deploy (ARCHITECTURE.md Section 10.2). Left
    blank by default and resolved via the control-plane EC2 resource's own
    IP at apply time in ec2.tf -- exposed here only in case the team wants
    to override it (e.g. pointing at a DNS name instead of a raw IP).
  EOT
  type        = string
  default     = ""
}

variable "applications" {
  description = <<-EOT
    Optional map of registered applications for multi-app ALB path routing and target groups.
    Format: { app_name = { path_pattern = string, target_port = number, health_check_path = string } }
  EOT
  type = map(object({
    path_pattern      = string
    target_port       = number
    health_check_path = string
  }))
  default = {}
}

