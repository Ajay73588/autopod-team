# AutoPod Cloud -- Security groups.
# Implements the exact communication matrix in ARCHITECTURE.md Section
# 12.2 -- every ingress rule below corresponds to one row of that table,
# referenced in a comment. No port is opened "just in case."

# ---------------------------------------------------------------------------
# ALB security group
# ---------------------------------------------------------------------------

resource "aws_security_group" "alb" {
  name        = "autopod-alb-sg"
  description = "AutoPod ALB: public HTTP(S) in, forwards to app nodes"
  vpc_id      = aws_vpc.autopod.id

  # Matrix row: Internet -> ALB SG, port 80 (443 optional, not opened by
  # default since no certificate is provisioned in this core design).
  ingress {
    description = "Public HTTP access"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "ALB to app nodes (any port, narrowed by the app-node SG ingress rule)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "autopod-alb-sg" }
}

# ---------------------------------------------------------------------------
# Control plane security group (EC2-1)
# ---------------------------------------------------------------------------

resource "aws_security_group" "control_plane" {
  name        = "autopod-control-plane-sg"
  description = "AutoPod control plane: admin SSH/dashboard access, GitHub webhook inbound"
  vpc_id      = aws_vpc.autopod.id

  # Matrix row: Admin (team IP) -> Control Plane SG, port 22 (SSH administration)
  ingress {
    description = "Admin SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.admin_cidr]
  }

  # Reverse proxy HTTP entry point (port 80):
  # Exposes the Nginx reverse proxy to receive inbound traffic.
  # Nginx enforces Layer 7 routing:
  # - /webhook (exact) and /health (exact) are publicly accessible.
  # - All other endpoints (/ and /api/*) are restricted to admin_cidr (and VPC CIDR).
  # Direct public access to Flask port 5000 and frontend port 3000 is eliminated (resolving D6).
  ingress {
    description = "HTTP access to AutoPod Nginx reverse proxy"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # Matrix row: Prometheus (on this host) scraping app nodes is EGRESS from
  # here, not ingress -- see the egress rule below.

  egress {
    description = "Control plane outbound (docker push, git clone, SSH to app nodes, Prometheus scrape)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "autopod-control-plane-sg" }
}

# ---------------------------------------------------------------------------
# Application node security group (ASG instances, EC2-2/EC2-3/...)
# ---------------------------------------------------------------------------

resource "aws_security_group" "app_node" {
  name        = "autopod-app-node-sg"
  description = "AutoPod application nodes: ALB-only app traffic, control-plane-only admin access"
  vpc_id      = aws_vpc.autopod.id

  # Matrix row: ALB SG -> App Node SG, target_port (load-balanced app traffic)
  ingress {
    description     = "Load-balanced application traffic from the ALB"
    from_port       = var.app_target_port
    to_port         = var.app_target_port
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  # Matrix row: Control Plane SG -> App Node SG, target_port (rolling-deploy health probe).
  # node_deployer.py probes http://<node-private-ip>:<target_port><health_check_path>
  # from the control plane after every per-node deploy. Without this rule only
  # the ALB could reach the app port and every real rolling deploy would
  # report a failed health check.
  ingress {
    description     = "Rolling-deploy health probe from the control plane"
    from_port       = var.app_target_port
    to_port         = var.app_target_port
    protocol        = "tcp"
    security_groups = [aws_security_group.control_plane.id]
  }

  ingress {
    description     = "Multi-app dynamic port range from ALB"
    from_port       = 8080
    to_port         = 8099
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  ingress {
    description     = "Multi-app dynamic port range health probe from control plane"
    from_port       = 8080
    to_port         = 8099
    protocol        = "tcp"
    security_groups = [aws_security_group.control_plane.id]
  }


  # Matrix row: Admin (team IP) -> App Node SG, port 22 (direct SSH administration, optional vs. jump host)
  ingress {
    description = "Admin SSH (direct; the control plane can also be used as a jump host)"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.admin_cidr]
  }

  # Matrix row: Control Plane SG -> App Node SG, port 22 (autopod-deploy restricted-command SSH)
  ingress {
    description     = "autopod-deploy restricted SSH from the control plane (Section 7.3/8)"
    from_port       = 22
    to_port         = 22
    protocol        = "tcp"
    security_groups = [aws_security_group.control_plane.id]
  }

  # Matrix row: Control Plane SG -> App Node SG, ports 9100/8080 (Prometheus scraping node_exporter/cAdvisor)
  ingress {
    description     = "Prometheus scrape: node_exporter"
    from_port       = 9100
    to_port         = 9100
    protocol        = "tcp"
    security_groups = [aws_security_group.control_plane.id]
  }

  ingress {
    description     = "Prometheus scrape: cAdvisor"
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    security_groups = [aws_security_group.control_plane.id]
  }

  egress {
    description = "App node outbound (docker pull, fetch current version from control plane)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "autopod-app-node-sg" }
}
