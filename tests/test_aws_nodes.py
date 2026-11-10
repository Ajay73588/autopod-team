"""Tests for aws_nodes.py -- AWS Auto Scaling Group polling that
populates the app_nodes table. Uses a FakeAsgClient standing in for
Boto3AsgClient, so these run without boto3 being installed or any real
AWS credentials/network access -- they exercise the REAL reconciliation
logic in sync_app_nodes_from_asg().
"""

import pytest

import aws_nodes


class FakeAsgClient:
    def __init__(self, in_service_ids, ip_map):
        self._in_service_ids = in_service_ids
        self._ip_map = ip_map
        self.calls = []

    def describe_in_service_instance_ids(self, asg_name):
        self.calls.append(("describe_in_service_instance_ids", asg_name))
        return self._in_service_ids

    def describe_instance_ips(self, instance_ids):
        self.calls.append(("describe_instance_ips", tuple(instance_ids)))
        return {i: self._ip_map[i] for i in instance_ids if i in self._ip_map}


def test_sync_registers_in_service_nodes(database):
    client = FakeAsgClient(
        in_service_ids=["i-1", "i-2"],
        ip_map={
            "i-1": {"public": "203.0.113.10", "private": "10.0.1.10"},
            "i-2": {"public": "203.0.113.11", "private": "10.0.1.11"},
        },
    )

    summary = aws_nodes.sync_app_nodes_from_asg(client=client, asg_name="test-asg")

    assert summary["in_service_count"] == 2
    assert summary["in_service_ips"] == ["10.0.1.10", "10.0.1.11"]  # private is the default
    nodes = {n["ip_address"]: n for n in database.list_app_nodes()}
    assert nodes["10.0.1.10"]["status"] == "in_service"
    assert nodes["10.0.1.11"]["status"] == "in_service"


def test_sync_uses_private_ip_when_configured(database):
    client = FakeAsgClient(
        in_service_ids=["i-1"],
        ip_map={"i-1": {"public": "203.0.113.10", "private": "10.0.1.10"}},
    )

    summary = aws_nodes.sync_app_nodes_from_asg(client=client, asg_name="test-asg", ip_attribute="private")

    assert summary["in_service_ips"] == ["10.0.1.10"]


def test_sync_marks_removed_nodes_as_terminated(database):
    # First sync: two nodes in service.
    client1 = FakeAsgClient(
        in_service_ids=["i-1", "i-2"],
        ip_map={
            "i-1": {"public": "203.0.113.10", "private": None},
            "i-2": {"public": "203.0.113.11", "private": None},
        },
    )
    aws_nodes.sync_app_nodes_from_asg(client=client1, asg_name="test-asg", ip_attribute="public")

    # Second sync: the ASG scaled in -- only one node remains.
    client2 = FakeAsgClient(
        in_service_ids=["i-1"],
        ip_map={"i-1": {"public": "203.0.113.10", "private": None}},
    )
    summary = aws_nodes.sync_app_nodes_from_asg(client=client2, asg_name="test-asg", ip_attribute="public")

    assert summary["newly_terminated_ips"] == ["203.0.113.11"]
    nodes = {n["ip_address"]: n for n in database.list_app_nodes()}
    assert nodes["203.0.113.10"]["status"] == "in_service"
    assert nodes["203.0.113.11"]["status"] == "terminated"


def test_sync_skips_instances_with_no_ip_of_the_configured_kind(database):
    client = FakeAsgClient(
        in_service_ids=["i-1"],
        ip_map={"i-1": {"public": None, "private": "10.0.1.10"}},
    )

    summary = aws_nodes.sync_app_nodes_from_asg(client=client, asg_name="test-asg", ip_attribute="public")

    assert summary["in_service_count"] == 0
    assert summary["skipped_instances_without_ip"] == ["i-1"]
    assert database.list_app_nodes() == []


def test_boto3_client_raises_clear_error_when_boto3_not_available(monkeypatch):
    """If boto3 genuinely isn't installed, this must fail loudly with a
    clear message -- never silently report success."""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "boto3":
            raise ImportError("No module named 'boto3'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(RuntimeError, match="boto3 is not installed"):
        aws_nodes.Boto3AsgClient()


def test_default_ip_attribute_is_private_and_env_example_agrees():
    """REGRESSION (D7): SG-reference rules only match private-IP traffic, so
    the default must be private -- in code and in the documented template."""
    import importlib, os
    importlib.reload(aws_nodes)
    assert aws_nodes.NODE_IP_ATTRIBUTE == "private"
    env_example = open(os.path.join(os.path.dirname(__file__), "..", ".env.example")).read()
    assert "AUTOPOD_NODE_IP_ATTRIBUTE=private" in env_example
    assert "AUTOPOD_NODE_IP_ATTRIBUTE=public" not in env_example


def test_env_var_can_still_select_public(monkeypatch):
    import importlib
    monkeypatch.setenv("AUTOPOD_NODE_IP_ATTRIBUTE", "public")
    importlib.reload(aws_nodes)
    assert aws_nodes.NODE_IP_ATTRIBUTE == "public"
    monkeypatch.delenv("AUTOPOD_NODE_IP_ATTRIBUTE")
    importlib.reload(aws_nodes)
