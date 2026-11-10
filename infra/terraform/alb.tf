# AutoPod Cloud -- Application Load Balancer, listener, target group.
# ARCHITECTURE.md Section 9 / 4.1.

resource "aws_lb" "autopod" {
  name               = "autopod-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id

  tags = { Name = "autopod-alb" }
}

resource "aws_lb_target_group" "app_nodes" {
  name     = "autopod-app-nodes-tg"
  port     = var.app_target_port
  protocol = "HTTP"
  vpc_id   = aws_vpc.autopod.id

  health_check {
    enabled             = true
    path                = var.health_check_path
    protocol            = "HTTP"
    matcher             = "200-399"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  # Short connection-draining timeout, appropriate for a stateless demo
  # app (ARCHITECTURE.md Section 10.3) -- in-flight requests get a chance
  # to finish before a scaled-in instance is deregistered and terminated.
  deregistration_delay = 30

  tags = { Name = "autopod-app-nodes-tg" }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.autopod.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app_nodes.arn
  }

  # HTTPS (443) with a real certificate is listed as an Optional Component
  # (ARCHITECTURE.md Section 15), not core -- add an aws_lb_listener on
  # 443 plus an aws_acm_certificate if/when the team has a domain name to
  # issue one for.
}

resource "aws_lb_target_group" "apps" {
  for_each = var.applications

  name     = substr("autopod-${replace(each.key, "_", "-")}-tg", 0, 32)
  port     = each.value.target_port
  protocol = "HTTP"
  vpc_id   = aws_vpc.autopod.id

  health_check {
    enabled             = true
    path                = each.value.health_check_path
    protocol            = "HTTP"
    matcher             = "200-399"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  deregistration_delay = 30
  tags                 = { Name = "autopod-${each.key}-tg" }
}

resource "aws_lb_listener_rule" "app_routes" {
  for_each     = var.applications
  listener_arn = aws_lb_listener.http.arn

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.apps[each.key].arn
  }

  condition {
    path_pattern {
      values = [each.value.path_pattern]
    }
  }
}

