# AutoPod Cloud -- EC2-1 (control plane) and the Launch Template used by
# the Auto Scaling Group for application nodes (EC2-2/EC2-3/...).
# ARCHITECTURE.md Section 3.1/4.1.

data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-x86_64"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }

  filter {
    name   = "architecture"
    values = ["x86_64"]
  }
}

locals {
  # app_node_ami_id has no default (variables.tf) because a stale default
  # AMI is worse than an explicit error -- but if the team leaves it
  # unset, fall back to the latest Amazon Linux 2023 AMI looked up above
  # rather than hard-failing, since that's a reasonable default for a
  # student project's app nodes.
  app_node_ami = var.app_node_ami_id != "" ? var.app_node_ami_id : data.aws_ami.al2023.id
}

# ---------------------------------------------------------------------------
# EC2-1: Control plane (fixed instance, NOT in the Auto Scaling Group)
# ---------------------------------------------------------------------------

resource "aws_instance" "control_plane" {
  ami                         = data.aws_ami.al2023.id
  instance_type               = var.control_plane_instance_type
  subnet_id                   = aws_subnet.public[0].id
  vpc_security_group_ids      = [aws_security_group.control_plane.id]
  key_name                    = var.key_pair_name
  associate_public_ip_address = true
  iam_instance_profile        = aws_iam_instance_profile.control_plane.name

  # IMDSv2 only. Hop limit 2 so boto3 inside the AutoPod container (one
  # network hop behind the host) can still reach instance credentials.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  # Deliberately minimal user_data here beyond package freshness and SSM agent
  # assurance -- Terraform does not install Docker/Prometheus/Grafana/AutoPod itself
  # (ARCHITECTURE.md Section 4.2). That is Ansible's job, run against this
  # instance after `terraform apply` (see infra/ansible/).
  user_data = <<-EOF
    #!/bin/bash
    dnf update -y
    dnf install -y amazon-ssm-agent
    systemctl enable --now amazon-ssm-agent
  EOF

  tags = {
    Name = "autopod-control-plane"
    Role = "control-plane"
  }
}

# ---------------------------------------------------------------------------
# Launch Template for application nodes (used by the ASG in autoscaling.tf)
# ---------------------------------------------------------------------------

resource "aws_launch_template" "app_node" {
  name_prefix   = "autopod-app-node-"
  image_id      = local.app_node_ami
  instance_type = var.app_node_instance_type
  key_name      = var.key_pair_name

  iam_instance_profile {
    name = aws_iam_instance_profile.app_node.name
  }

  network_interfaces {
    associate_public_ip_address = true
    security_groups             = [aws_security_group.app_node.id]
  }

  user_data = base64encode(templatefile("${path.module}/templates/app_node_user_data.sh.tpl", {
    control_plane_api_url = var.autopod_control_plane_api_url != "" ? var.autopod_control_plane_api_url : "http://${aws_instance.control_plane.private_ip}",
    app_target_port       = var.app_target_port,
    aws_region            = var.aws_region
  }))

  tag_specifications {
    resource_type = "instance"
    tags = {
      Name = "autopod-app-node"
      Role = "app-node"
    }
  }

  lifecycle {
    create_before_destroy = true
  }
}
