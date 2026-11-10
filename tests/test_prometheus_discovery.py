import os
import re
import jinja2
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MONITORING_ROLE = os.path.join(ROOT, "infra", "ansible", "roles", "monitoring")
TF_DIR = os.path.join(ROOT, "infra", "terraform")


def _render_prometheus_template(context):
    template_path = os.path.join(MONITORING_ROLE, "templates", "prometheus.yml.j2")
    with open(template_path, "r", encoding="utf-8") as f:
        template_str = f.read()
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    template = env.from_string(template_str)
    rendered = template.render(context)
    return rendered


def test_prometheus_template_renders_ec2_sd_by_default():
    context = {
        "monitoring_use_ec2_sd": True,
        "aws_region": "us-east-1",
        "monitoring_sd_refresh_interval": "30s",
        "monitoring_app_node_tag_role": "app-node",
        "node_exporter_port": 9100,
        "cadvisor_port": 8080,
        "autopod_environment": "production",
    }
    rendered = _render_prometheus_template(context)

    # Scrape configs must define control_plane, node_exporter, and cadvisor jobs
    assert 'job_name: "control_plane"' in rendered
    assert 'job_name: "node_exporter"' in rendered
    assert 'job_name: "cadvisor"' in rendered

    # Local control plane job uses loopback target
    assert 'targets: ["127.0.0.1:9100"]' in rendered
    assert 'role: "control-plane"' in rendered
    assert 'node_name: "autopod-control-plane"' in rendered

    # node_exporter uses ec2_sd_configs with exact filters
    assert "ec2_sd_configs:" in rendered
    assert 'region: "us-east-1"' in rendered
    assert "port: 9100" in rendered
    assert 'refresh_interval: "30s"' in rendered

    # Assert EC2 filters: running state + Role=app-node
    assert 'name: "instance-state-name"' in rendered
    assert 'values: ["running"]' in rendered
    assert 'name: "tag:Role"' in rendered
    assert 'values: ["app-node"]' in rendered

    # Assert relabel configs filter and map labels
    assert "source_labels: [__meta_ec2_instance_state]" in rendered
    assert 'regex: "running"' in rendered
    assert "action: keep" in rendered
    assert "target_label: instance_id" in rendered
    assert "target_label: availability_zone" in rendered
    assert "target_label: instance_type" in rendered
    assert "target_label: node_name" in rendered
    assert "target_label: role" in rendered
    assert "target_label: autoscaling_group" in rendered


def test_cadvisor_ec2_sd_configuration():
    context = {
        "monitoring_use_ec2_sd": True,
        "aws_region": "us-east-1",
        "monitoring_sd_refresh_interval": "30s",
        "monitoring_app_node_tag_role": "app-node",
        "node_exporter_port": 9100,
        "cadvisor_port": 8080,
        "autopod_environment": "production",
    }
    rendered = _render_prometheus_template(context)

    # cadvisor job must have port 8080 in ec2_sd_configs and replacement cadvisor
    cadvisor_section = rendered.split('job_name: "cadvisor"')[1]
    assert "port: 8080" in cadvisor_section
    assert 'region: "us-east-1"' in cadvisor_section
    assert 'replacement: "cadvisor"' in cadvisor_section
    assert 'values: ["running"]' in cadvisor_section
    assert 'values: ["app-node"]' in cadvisor_section


def test_prometheus_template_static_fallback_mode():
    context = {
        "monitoring_use_ec2_sd": False,
        "node_exporter_port": 9100,
        "cadvisor_port": 8080,
        "autopod_environment": "staging",
        "groups": {
            "app_nodes": ["node1", "node2"],
        },
        "hostvars": {
            "node1": {"ansible_host": "10.0.1.10"},
            "node2": {"ansible_host": "10.0.1.11"},
        },
    }
    rendered = _render_prometheus_template(context)

    # In static fallback mode, ec2_sd_configs YAML key must NOT be present
    assert "ec2_sd_configs:" not in rendered
    assert "static_configs:" in rendered
    assert "10.0.1.10:9100" in rendered
    assert "10.0.1.11:9100" in rendered
    assert "10.0.1.10:8080" in rendered
    assert "10.0.1.11:8080" in rendered


def test_monitoring_defaults_file_exists_and_sets_ec2_sd_true():
    defaults_file = os.path.join(MONITORING_ROLE, "defaults", "main.yml")
    assert os.path.exists(defaults_file), "monitoring role must have defaults/main.yml"
    content = open(defaults_file, "r", encoding="utf-8").read()
    assert "monitoring_use_ec2_sd: true" in content
    assert 'aws_region: "us-east-1"' in content
    assert "node_exporter_port: 9100" in content
    assert "cadvisor_port: 8080" in content
    assert "prometheus_port: 9090" in content
    assert "grafana_port: 3001" in content


def test_monitoring_tasks_binds_prometheus_to_localhost_and_sets_aws_region():
    tasks_file = os.path.join(MONITORING_ROLE, "tasks", "main.yml")
    content = open(tasks_file, "r", encoding="utf-8").read()

    # Prometheus and Grafana must be bound to 127.0.0.1 only
    assert '"127.0.0.1:9090:9090"' in content
    assert '"127.0.0.1:3001:3000"' in content
    # Prometheus task must pass AWS_REGION
    assert 'AWS_REGION: "{{ aws_region | default(\'us-east-1\') }}"' in content


def test_control_plane_iam_policy_includes_ec2_describe_instances_and_tags():
    iam_file = os.path.join(TF_DIR, "iam.tf")
    content = open(iam_file, "r", encoding="utf-8").read()
    assert '"autoscaling:DescribeAutoScalingGroups"' in content
    assert '"ec2:DescribeInstances"' in content
    assert '"ec2:DescribeTags"' in content
    # No wildcard permissions
    for line in content.splitlines():
        if "actions" in line and "*" in line:
            assert False, f"Wildcard action found in IAM: {line}"


def test_security_group_restricts_monitoring_ports_to_control_plane():
    sec_file = os.path.join(TF_DIR, "security.tf")
    content = open(sec_file, "r", encoding="utf-8").read()

    # Ports 9100 and 8080 must be restricted to control plane SG
    assert "Prometheus scrape: node_exporter" in content
    assert "Prometheus scrape: cAdvisor" in content
    assert "security_groups = [aws_security_group.control_plane.id]" in content

    # Never exposed to 0.0.0.0/0 on 9100 or 8080
    assert not re.search(r'from_port\s*=\s*9100\s*\n\s*to_port\s*=\s*9100\s*\n\s*protocol\s*=\s*"tcp"\s*\n\s*cidr_blocks\s*=\s*\["0\.0\.0\.0/0"\]', content)
    assert not re.search(r'from_port\s*=\s*8080\s*\n\s*to_port\s*=\s*8080\s*\n\s*protocol\s*=\s*"tcp"\s*\n\s*cidr_blocks\s*=\s*\["0\.0\.0\.0/0"\]', content)


def test_asg_has_role_app_node_tag_propagated():
    asg_file = os.path.join(TF_DIR, "autoscaling.tf")
    content = open(asg_file, "r", encoding="utf-8").read()
    assert 'key                 = "Role"' in content
    assert 'value               = "app-node"' in content
    assert 'propagate_at_launch = true' in content
