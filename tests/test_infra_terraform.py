"""Structural regression tests for the Terraform files.

IMPORTANT LIMIT: these parse the .tf files with python-hcl2 and assert on
the resulting structure. They catch the specific defects fixed in the
2026-10-19 audit (missing security-group rule, missing IAM role, drift
between aws_nodes.py and the IAM policy) and would fail if someone removed
those pieces. They are NOT `terraform validate` and prove nothing about
how AWS will behave; that needs the real binary and, later, a live plan.
"""

import glob
import os
import re

import pytest

hcl2 = pytest.importorskip("hcl2")

TF_DIR = os.path.join(os.path.dirname(__file__), "..", "infra", "terraform")
ROOT = os.path.join(os.path.dirname(__file__), "..")


def _norm(o):
    if isinstance(o, dict):
        return {_norm(k): _norm(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_norm(x) for x in o]
    if isinstance(o, str) and len(o) > 1 and o[0] == '"' and o[-1] == '"':
        return o[1:-1]
    return o


@pytest.fixture(scope="module")
def tf():
    """{'resource': {type: {name: body}}, 'data': {type: {name: body}}}"""
    out = {"resource": {}, "data": {}}
    for path in glob.glob(os.path.join(TF_DIR, "*.tf")):
        with open(path) as f:
            doc = _norm(hcl2.load(f))
        for kind in ("resource", "data"):
            for block in doc.get(kind, []):
                for rtype, named in block.items():
                    for name, body in named.items():
                        out[kind].setdefault(rtype, {})[name] = body
    return out


def _ingress_sources(sg, port_expr):
    """Source SG references / CIDRs of ingress rules covering `port_expr`."""
    found = []
    for rule in sg.get("ingress", []):
        if rule.get("from_port") == port_expr and rule.get("to_port") == port_expr:
            found += rule.get("security_groups", []) + rule.get("cidr_blocks", [])
    return found


def test_app_nodes_accept_health_probe_from_control_plane_on_app_port(tf):
    """REGRESSION (D2)."""
    sg = tf["resource"]["aws_security_group"]["app_node"]
    sources = _ingress_sources(sg, "${var.app_target_port}")
    assert "${aws_security_group.control_plane.id}" in sources


def test_app_port_is_still_reachable_from_alb_and_not_from_the_internet(tf):
    sg = tf["resource"]["aws_security_group"]["app_node"]
    sources = _ingress_sources(sg, "${var.app_target_port}")
    assert "${aws_security_group.alb.id}" in sources
    assert "0.0.0.0/0" not in sources


def test_control_plane_instance_has_instance_profile_and_imdsv2(tf):
    """REGRESSION (D3): previously no iam_instance_profile at all."""
    inst = tf["resource"]["aws_instance"]["control_plane"]
    assert inst["iam_instance_profile"] == "${aws_iam_instance_profile.control_plane.name}"
    imds = inst["metadata_options"][0]
    assert imds["http_tokens"] == "required"


def test_control_plane_profile_role_policy_chain_is_wired(tf):
    profile = tf["resource"]["aws_iam_instance_profile"]["control_plane"]
    assert profile["role"] == "${aws_iam_role.control_plane.name}"
    policy = tf["resource"]["aws_iam_role_policy"]["control_plane_permissions"]
    assert policy["role"] == "${aws_iam_role.control_plane.id}"
    assert "control_plane_permissions" in policy["policy"]
    assert "ec2_assume_role" in tf["resource"]["aws_iam_role"]["control_plane"]["assume_role_policy"]


def _control_plane_actions(tf):
    doc = tf["data"]["aws_iam_policy_document"]["control_plane_permissions"]
    actions = []
    for st in doc["statement"]:
        assert st["effect"] == "Allow"
        actions += st["actions"]
    return actions


def test_control_plane_policy_is_describe_only_and_covers_discovery(tf):
    actions = _control_plane_actions(tf)
    assert "autoscaling:DescribeAutoScalingGroups" in actions
    assert "ec2:DescribeInstances" in actions
    for a in actions:
        assert a != "*" and not a.endswith(":*"), a
        assert re.match(r"^[a-z0-9-]+:(Describe|Get|List)", a), f"non read-only action: {a}"


def test_iam_policy_covers_every_boto3_call_aws_nodes_makes(tf):
    """Drift guard: map each boto3 method used in aws_nodes.py to its IAM
    action and require it to be granted."""
    src = open(os.path.join(ROOT, "aws_nodes.py")).read()
    service_of = {"_asg": "autoscaling", "_ec2": "ec2"}
    needed = set()
    for client_attr, method in re.findall(r"self\.(_asg|_ec2)\.(\w+)\(", src):
        action = "".join(p.capitalize() for p in method.split("_"))
        needed.add(f"{service_of[client_attr]}:{action}")
    assert needed, "no boto3 calls found; pattern out of date"
    granted = set(_control_plane_actions(tf))
    assert needed <= granted, f"aws_nodes.py needs {needed - granted} but the control-plane policy lacks it"


def test_asg_max_size_remains_capped(tf):
    """Cost guard from the project constraints."""
    variables = {}
    with open(os.path.join(TF_DIR, "variables.tf")) as f:
        doc = _norm(hcl2.load(f))
    for block in doc["variable"]:
        variables.update(block)
    assert int(variables["asg_max_size"]["default"]) <= 4


def test_multi_app_alb_routing_structures(tf):
    """REGRESSION (D4): multi-app ALB target groups and listener rules exist."""
    assert "apps" in tf["resource"]["aws_lb_target_group"]
    apps_tg = tf["resource"]["aws_lb_target_group"]["apps"]
    assert apps_tg["for_each"] == "${var.applications}"

    assert "app_routes" in tf["resource"]["aws_lb_listener_rule"]
    rule = tf["resource"]["aws_lb_listener_rule"]["app_routes"]
    assert rule["for_each"] == "${var.applications}"
    assert rule["listener_arn"] == "${aws_lb_listener.http.arn}"


def test_control_plane_sg_eliminates_direct_port_5000_public_exposure(tf):
    """REGRESSION (D6): port 5000 must NOT be exposed to 0.0.0.0/0 or the public."""
    sg = tf["resource"]["aws_security_group"]["control_plane"]
    sources_5000 = _ingress_sources(sg, 5000)
    assert "0.0.0.0/0" not in sources_5000
    assert len(sources_5000) == 0


def test_control_plane_sg_exposes_port_80_for_nginx_proxy(tf):
    """REGRESSION (D6): port 80 is the HTTP ingress point for the Nginx proxy."""
    sg = tf["resource"]["aws_security_group"]["control_plane"]
    sources_80 = _ingress_sources(sg, 80)
    assert "0.0.0.0/0" in sources_80


def test_control_plane_ssh_restricted_to_admin_cidr(tf):
    """SSH port 22 must be restricted to admin_cidr."""
    sg = tf["resource"]["aws_security_group"]["control_plane"]
    sources_22 = _ingress_sources(sg, 22)
    assert "${var.admin_cidr}" in sources_22
    assert "0.0.0.0/0" not in sources_22


def test_ecr_repository_and_iam_wiring(tf):
    """ECR repository, lifecycle policy, and role policies must exist and be wired."""
    ecr = tf["resource"]["aws_ecr_repository"]["sweet_treats_bakery"]
    assert ecr["name"] == "autopod-sweet-treats-bakery"
    assert ecr["image_tag_mutability"] == "MUTABLE"

    lifecycle = tf["resource"]["aws_ecr_lifecycle_policy"]["sweet_treats_bakery"]
    assert lifecycle["repository"] == "${aws_ecr_repository.sweet_treats_bakery.name}"

    cp_ecr = tf["resource"]["aws_iam_role_policy"]["control_plane_ecr"]
    assert cp_ecr["role"] == "${aws_iam_role.control_plane.id}"

    node_ecr = tf["resource"]["aws_iam_role_policy"]["app_node_ecr"]
    assert node_ecr["role"] == "${aws_iam_role.app_node.id}"


