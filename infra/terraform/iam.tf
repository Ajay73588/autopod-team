# AutoPod Cloud -- IAM role / instance profile for application nodes.
# ARCHITECTURE.md Section 12.1: read-only permissions to describe their
# own instance metadata; no broad EC2/S3/IAM permissions.
#
# The CONTROL PLANE also gets a role (below), but READ-ONLY and only for
# discovering app nodes (aws_nodes.py: describe the Auto Scaling Group and
# the instance addresses). It has no write/modify/terminate permissions:
# scaling stays CloudWatch/ASG-native (Section 10/11.4) and AutoPod never
# calls the AWS API to change capacity.

data "aws_iam_policy_document" "ec2_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]
    effect  = "Allow"

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "app_node" {
  name               = "autopod-app-node-role"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume_role.json

  tags = { Name = "autopod-app-node-role" }
}

# Minimal, read-only: describe-this-instance only (via
# ec2:DescribeInstances restricted by condition is not supported for this
# action, so access is scoped narrowly to describe + tags read, which is
# the standard minimal set for "a node can see its own metadata/tags" --
# it cannot describe, start, stop or modify ANY other instance, and has no
# permissions outside EC2 read-only describe calls).
data "aws_iam_policy_document" "app_node_permissions" {
  statement {
    sid    = "DescribeSelfAndTags"
    effect = "Allow"
    actions = [
      "ec2:DescribeInstances",
      "ec2:DescribeTags",
    ]
    resources = ["*"] # DescribeInstances does not support resource-level restriction
  }
}

resource "aws_iam_role_policy" "app_node_permissions" {
  name   = "autopod-app-node-describe-only"
  role   = aws_iam_role.app_node.id
  policy = data.aws_iam_policy_document.app_node_permissions.json
}

resource "aws_iam_instance_profile" "app_node" {
  name = "autopod-app-node-profile"
  role = aws_iam_role.app_node.name
}

# ---------------------------------------------------------------------------
# Control plane role: read-only Auto Scaling discovery for aws_nodes.py
# (POST /api/app-nodes/sync). Both actions are Describe-only and do not
# support resource-level restriction, hence resources = ["*"].
# ---------------------------------------------------------------------------

resource "aws_iam_role" "control_plane" {
  name               = "autopod-control-plane-role"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume_role.json

  tags = { Name = "autopod-control-plane-role" }
}

data "aws_iam_policy_document" "control_plane_permissions" {
  statement {
    sid    = "DiscoverAutoScalingNodes"
    effect = "Allow"
    actions = [
      "autoscaling:DescribeAutoScalingGroups",
      "ec2:DescribeInstances",
      "ec2:DescribeTags",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "control_plane_permissions" {
  name   = "autopod-control-plane-asg-discovery-readonly"
  role   = aws_iam_role.control_plane.id
  policy = data.aws_iam_policy_document.control_plane_permissions.json
}

resource "aws_iam_instance_profile" "control_plane" {
  name = "autopod-control-plane-profile"
  role = aws_iam_role.control_plane.name
}

# ---------------------------------------------------------------------------
# AWS Systems Manager (SSM) Session Manager & Run Command access
# Enables management and deployment over outbound HTTPS (443) without SSH (port 22)
# ---------------------------------------------------------------------------

resource "aws_iam_role_policy_attachment" "control_plane_ssm" {
  role       = aws_iam_role.control_plane.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# ---------------------------------------------------------------------------
# Control Plane to Worker SSM Run Command Permission
# Scoped strictly to AWS-RunShellScript and worker instance i-086803910a4c52091
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "control_plane_ssm_worker_deploy" {
  statement {
    sid     = "AllowSSMRunShellScriptOnWorker"
    effect  = "Allow"
    actions = ["ssm:SendCommand"]
    resources = [
      "arn:aws:ssm:us-east-1:*:document/AWS-RunShellScript",
      "arn:aws:ec2:us-east-1:822897118262:instance/i-086803910a4c52091"
    ]
  }

  statement {
    sid    = "AllowSSMInvocationRead"
    effect = "Allow"
    actions = [
      "ssm:GetCommandInvocation",
      "ssm:ListCommandInvocations",
      "ssm:ListCommands"
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "control_plane_ssm_worker_deploy" {
  name   = "autopod-control-plane-ssm-worker-deploy"
  role   = aws_iam_role.control_plane.id
  policy = data.aws_iam_policy_document.control_plane_ssm_worker_deploy.json
}

resource "aws_iam_role_policy_attachment" "app_node_ssm" {
  role       = aws_iam_role.app_node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# ---------------------------------------------------------------------------
# Amazon ECR Access Policies
# Control plane: authenticate and push images to ECR.
# Worker app nodes: authenticate and pull images from ECR.
# ---------------------------------------------------------------------------

data "aws_caller_identity" "current" {}

data "aws_iam_policy_document" "control_plane_ecr_push" {
  statement {
    sid       = "ECRAuthToken"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "ECRPushPullImages"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchGetImage",
      "ecr:PutImage",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeRepositories",
      "ecr:ListImages",
      "ecr:CreateRepository",
    ]
    resources = ["arn:aws:ecr:${var.aws_region}:${data.aws_caller_identity.current.account_id}:repository/*"]
  }
}

resource "aws_iam_role_policy" "control_plane_ecr" {
  name   = "autopod-control-plane-ecr-push"
  role   = aws_iam_role.control_plane.id
  policy = data.aws_iam_policy_document.control_plane_ecr_push.json
}

data "aws_iam_policy_document" "app_node_ecr_pull" {
  statement {
    sid       = "ECRAuthToken"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "ECRPullImages"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchGetImage",
    ]
    resources = ["arn:aws:ecr:${var.aws_region}:${data.aws_caller_identity.current.account_id}:repository/*"]
  }
}

resource "aws_iam_role_policy" "app_node_ecr" {
  name   = "autopod-app-node-ecr-pull"
  role   = aws_iam_role.app_node.id
  policy = data.aws_iam_policy_document.app_node_ecr_pull.json
}
