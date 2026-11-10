"""
aws_nodes.py

Polls the AWS Auto Scaling Group for its currently in-service application
nodes and syncs the result into the `app_nodes` table (ARCHITECTURE.md
Section 6.3), which node_deployer.py then rolls deployments out to.

This is read-only against AWS (describe-* calls only -- it never creates,
modifies, or terminates anything) and is meant to be called periodically
(e.g. before each rolling deployment, or on a short poll) so `app_nodes`
reflects reality even as the ASG scales out/in.

NOT YET EXERCISED against a real AWS account in this session: no AWS
credentials have been available. The AWS calls are made through a thin,
injectable client wrapper (`Boto3AsgClient`) specifically so this logic
is still fully unit-testable with a fake client standing in for boto3 --
see tests/test_aws_nodes.py.
"""

import os
import logging

import database

logger = logging.getLogger('autopod')

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
AUTOSCALING_GROUP_NAME = os.environ.get("AUTOSCALING_GROUP_NAME", "autopod-app-nodes-asg")

# Which instance IP to register. Default is "private": the control plane
# and the app nodes share one VPC, and the security-group rules that let the
# control plane reach nodes (SSH, health probe, Prometheus scrape) reference
# the control-plane SECURITY GROUP. AWS only matches such rules for traffic
# between private addresses; traffic sent to a node's public IP leaves via the
# internet gateway with the control plane's public IP as source, so those
# rules would not match. Use "public" only if you deliberately replace them
# with CIDR rules.
NODE_IP_ATTRIBUTE = os.environ.get("AUTOPOD_NODE_IP_ATTRIBUTE", "private")  # "private" or "public"


class Boto3AsgClient:
    """Thin wrapper around the two boto3 calls this module needs. Exists
    so `sync_app_nodes_from_asg()` can be unit-tested against a fake of
    THIS class instead of mocking boto3 internals directly, and so the
    (optional) `boto3` import only happens when this class is actually
    instantiated -- the rest of AutoPod Cloud does not require boto3 to
    be installed at all."""

    def __init__(self, region=None):
        try:
            import boto3
        except ImportError as e:
            raise RuntimeError(
                "boto3 is not installed. Install it with `pip install boto3` "
                "to use AWS Auto Scaling Group polling (see requirements.txt)."
            ) from e
        self._asg = boto3.client("autoscaling", region_name=region or AWS_REGION)
        self._ec2 = boto3.client("ec2", region_name=region or AWS_REGION)

    def describe_in_service_instance_ids(self, asg_name):
        """Returns the list of instance IDs the ASG currently reports as
        InService (i.e. not Pending/Terminating/etc)."""
        resp = self._asg.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
        groups = resp.get("AutoScalingGroups", [])
        if not groups:
            raise ValueError(f"Auto Scaling Group '{asg_name}' not found")
        return [
            inst["InstanceId"]
            for inst in groups[0].get("Instances", [])
            if inst.get("LifecycleState") == "InService"
        ]

    def describe_instance_ips(self, instance_ids):
        """Returns {instance_id: {"public": ip_or_None, "private": ip_or_None}}."""
        if not instance_ids:
            return {}
        resp = self._ec2.describe_instances(InstanceIds=instance_ids)
        result = {}
        for reservation in resp.get("Reservations", []):
            for inst in reservation.get("Instances", []):
                result[inst["InstanceId"]] = {
                    "public": inst.get("PublicIpAddress"),
                    "private": inst.get("PrivateIpAddress"),
                }
        return result


def sync_app_nodes_from_asg(client=None, asg_name=None, ip_attribute=None):
    """Poll the ASG, reconcile `app_nodes` to match reality:
      - every currently InService instance's IP is registered/refreshed
        with status "in_service"
      - any node AutoPod previously knew about that is NOT in the current
        InService set is marked "terminated" (not deleted -- it stays in
        the table as deployment history may still reference it via
        `deployments.target_node`)

    Returns a summary dict. Raises RuntimeError (boto3 not installed) or
    whatever the AWS client raises (e.g. NoCredentialsError, ClientError
    for an ASG that doesn't exist, etc.) -- this function deliberately
    does NOT swallow those into a fake-success result; the caller is
    expected to report the real error rather than claim nodes were
    synced when they weren't.
    """
    asg_name = asg_name or AUTOSCALING_GROUP_NAME
    ip_attribute = ip_attribute or NODE_IP_ATTRIBUTE
    client = client or Boto3AsgClient()

    instance_ids = client.describe_in_service_instance_ids(asg_name)
    ip_map = client.describe_instance_ips(instance_ids)

    current_ips = set()
    skipped_no_ip = []
    for instance_id in instance_ids:
        ips = ip_map.get(instance_id, {})
        ip = ips.get(ip_attribute)
        if not ip:
            skipped_no_ip.append(instance_id)
            continue
        current_ips.add(ip)
        database.register_app_node(ip, status="in_service", instance_id=instance_id)

    previously_known = {n["ip_address"] for n in database.list_app_nodes()}
    newly_terminated = previously_known - current_ips
    for ip in newly_terminated:
        database.set_app_node_status(ip, "terminated")

    summary = {
        "asg_name": asg_name,
        "in_service_count": len(current_ips),
        "in_service_ips": sorted(current_ips),
        "newly_terminated_ips": sorted(newly_terminated),
        "skipped_instances_without_ip": skipped_no_ip,
    }
    logger.info(f"app_nodes synced from ASG '{asg_name}': {summary}")
    return summary
