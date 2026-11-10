# AutoPod Cloud -- Auto Scaling Group + scale-out/scale-in policies.
# ARCHITECTURE.md Section 10.1: min 2 / desired 2 / max 4, attached to the
# ALB target group so new instances auto-register and are health-checked
# before receiving traffic.

resource "aws_autoscaling_group" "app_nodes" {
  name                = "autopod-app-nodes-asg"
  vpc_zone_identifier = aws_subnet.public[*].id

  min_size         = var.asg_min_size
  desired_capacity = var.asg_desired_capacity
  max_size         = var.asg_max_size

  launch_template {
    id      = aws_launch_template.app_node.id
    version = "$Latest"
  }

  target_group_arns = concat([aws_lb_target_group.app_nodes.arn], [for tg in aws_lb_target_group.apps : tg.arn])

  # Use the ALB's own health check (not just EC2 status checks) to decide
  # whether an instance is healthy -- this is what makes "docker run
  # succeeded but the app never answers" result in the ASG replacing the
  # instance, consistent with AutoPod's own candidate-then-promote health
  # gating philosophy (deployment_engine.py) applied at the infra layer.
  health_check_type         = "ELB"
  health_check_grace_period = 120

  tag {
    key                 = "Name"
    value               = "autopod-app-node"
    propagate_at_launch = true
  }
  tag {
    key                 = "Role"
    value               = "app-node"
    propagate_at_launch = true
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_autoscaling_policy" "scale_out" {
  name                   = "autopod-scale-out"
  autoscaling_group_name = aws_autoscaling_group.app_nodes.name
  adjustment_type        = "ChangeInCapacity"
  scaling_adjustment     = 1
  cooldown               = 120
}

resource "aws_autoscaling_policy" "scale_in" {
  name                   = "autopod-scale-in"
  autoscaling_group_name = aws_autoscaling_group.app_nodes.name
  adjustment_type        = "ChangeInCapacity"
  scaling_adjustment     = -1
  cooldown               = 120
}
