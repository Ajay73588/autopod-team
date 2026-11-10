"""
node_deployer.py

Control-plane-side multi-node rolling deployment orchestrator
(ARCHITECTURE.md Section 8 / Section 14).

Supports two execution backends:
1. Boto3SSMRunner (Default for AWS): Uses AWS Systems Manager (SSM) Run Command
   (AWS-RunShellScript) over outbound HTTPS (port 443). Eliminates the need
   for open inbound SSH port 22 or manually distributed SSH keys. Uses the
   worker's IAM instance role to authenticate to Amazon ECR without passing
   credentials or private keys through command arguments.
2. SubprocessSSHRunner (Compatibility / Legacy): Uses system SSH client and
   restricted autopod-deploy user with forced-command wrapper script.

Per-node design: STOP-THEN-START with automatic restore (see deploy_to_node).
1. Pull first (a failed pull leaves the live container untouched).
2. Stop and remove the live container.
3. Start the replacement container on the designated target port.
4. Health-check the replacement (Docker state and HTTP probe).
5. On failure, automatically restore the recorded previous image and verify it.

Rolling-deploy semantics:
Nodes are deployed to ONE AT A TIME, stopping at the first failure.
Each node's outcome is recorded in its own `deployments` row via `target_node`.
"""

import os
import re
import subprocess
import time
import logging

import database
import health_probe

logger = logging.getLogger('autopod')

NODE_HEALTH_CHECK_RETRIES = int(os.environ.get("NODE_HEALTH_CHECK_RETRIES", "8"))
NODE_HEALTH_CHECK_DELAY_SECONDS = float(os.environ.get("NODE_HEALTH_CHECK_DELAY_SECONDS", "1.5"))

AUTOPOD_DEPLOY_METHOD = os.environ.get("AUTOPOD_DEPLOY_METHOD", "ssm")  # "ssm" or "ssh"
AUTOPOD_AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
AUTOPOD_SSM_TIMEOUT = int(os.environ.get("AUTOPOD_SSM_TIMEOUT", "120"))

AUTOPOD_DEPLOY_SSH_USER = os.environ.get("AUTOPOD_DEPLOY_SSH_USER", "autopod-deploy")
AUTOPOD_DEPLOY_SSH_KEY_PATH = os.environ.get("AUTOPOD_DEPLOY_SSH_KEY_PATH")
AUTOPOD_DEPLOY_SSH_TIMEOUT = int(os.environ.get("AUTOPOD_DEPLOY_SSH_TIMEOUT", "60"))

# Validation patterns
SAFE_IMAGE_PATTERN = re.compile(r"^[a-zA-Z0-9_./:-]+$")
ECR_IMAGE_PATTERN = re.compile(
    r"^([0-9]{12})\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com/([a-zA-Z0-9._-]+)(:[a-zA-Z0-9._-]+)?$"
)
SAFE_CONTAINER_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
SAFE_INSTANCE_ID_PATTERN = re.compile(r"^i-[0-9a-f]{8,17}$")


def validate_image_reference(image_tag, allowed_registries=None):
    """Validates image_tag to prevent command injection and ensure approved references.
    Returns True if valid, False otherwise."""
    if not image_tag or not isinstance(image_tag, str):
        return False
    image_tag = image_tag.strip()
    # Reject shell metacharacters and invalid chars
    if any(ch in image_tag for ch in (";", "&", "|", "`", "$", "(", ")", "<", ">", "\\", "\n", "\r", " ")):
        return False
    if not SAFE_IMAGE_PATTERN.match(image_tag):
        return False
    if allowed_registries:
        matched = any(image_tag.startswith(reg.rstrip("/") + "/") for reg in allowed_registries)
        if not matched:
            return False
    return True


def is_ecr_image(image_tag):
    """Returns True if image_tag is an Amazon ECR image reference."""
    if not image_tag or not isinstance(image_tag, str):
        return False
    return ".dkr.ecr." in image_tag and "amazonaws.com" in image_tag


def build_ecr_login_cmd(image_tag, default_region=None):
    """Builds the ECR login command executed on the worker node.
    Authenticates using the worker's IAM instance profile via IMDSv2.
    Never passes credentials or private keys in the command arguments."""
    registry = image_tag.split("/")[0]
    region = default_region or AUTOPOD_AWS_REGION
    if ".dkr.ecr." in registry:
        parts = registry.split(".")
        if len(parts) >= 4:
            region = parts[3]
    return f"aws ecr get-login-password --region {region} | docker login --username AWS --password-stdin {registry}"


class SubprocessSSHRunner:
    """Real SSH runner: shells out to the system `ssh` client. Only ever
    issues the whitelisted docker subcommands the autopod-deploy
    wrapper script on the remote node allows."""

    def __init__(self, user=None, key_path=None, timeout=None):
        self.user = user or AUTOPOD_DEPLOY_SSH_USER
        self.key_path = key_path or AUTOPOD_DEPLOY_SSH_KEY_PATH
        self.timeout = timeout or AUTOPOD_DEPLOY_SSH_TIMEOUT

    def run(self, host, remote_command, timeout=None):
        """Returns (success, stdout, stderr). Never raises."""
        cmd = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"ConnectTimeout={min(self.timeout, 15)}",
        ]
        if self.key_path:
            cmd += ["-i", self.key_path]
        cmd += [f"{self.user}@{host}", remote_command]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
            return result.returncode == 0, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return False, "", f"ssh command to {host} timed out after {self.timeout}s"
        except FileNotFoundError:
            return False, "", "ssh binary not found on this host"
        except Exception as e:
            return False, "", f"ssh command to {host} failed: {e}"


class Boto3SSMRunner:
    """AWS Systems Manager (SSM) Run Command runner.
    Executes commands on remote EC2 worker instances using AWS-RunShellScript
    over outbound HTTPS (port 443). Authenticates via the instance role."""

    def __init__(self, region=None, ssm_client=None, ec2_client=None, timeout=None,
                 poll_interval=1.0, allowed_instances=None):
        self.region = region or AUTOPOD_AWS_REGION
        self.timeout = timeout or AUTOPOD_SSM_TIMEOUT
        self.poll_interval = poll_interval
        self._ssm = ssm_client
        self._ec2 = ec2_client
        self.allowed_instances = set(allowed_instances) if allowed_instances else None
        self._ip_cache = {}

    @property
    def ssm(self):
        if self._ssm is None:
            try:
                import boto3
                self._ssm = boto3.client("ssm", region_name=self.region)
            except ImportError as e:
                raise RuntimeError(
                    "boto3 is not installed. Install boto3 to use AWS SSM deployment."
                ) from e
        return self._ssm

    @property
    def ec2(self):
        if self._ec2 is None:
            try:
                import boto3
                self._ec2 = boto3.client("ec2", region_name=self.region)
            except ImportError as e:
                raise RuntimeError(
                    "boto3 is not installed. Install boto3 to use AWS SSM deployment."
                ) from e
        return self._ec2

    def resolve_instance_id(self, target):
        """Resolves target (instance ID or private/public IP) to an EC2 instance ID."""
        if not target or not isinstance(target, str):
            return None
        target = target.strip()
        if SAFE_INSTANCE_ID_PATTERN.match(target):
            return target

        if target in self._ip_cache:
            return self._ip_cache[target]

        # Check database app_nodes
        try:
            nodes = database.list_app_nodes()
            for node in nodes:
                if node.get("ip_address") == target and node.get("instance_id"):
                    self._ip_cache[target] = node["instance_id"]
                    return node["instance_id"]
        except Exception:
            pass

        # Query EC2 DescribeInstances
        try:
            resp = self.ec2.describe_instances(
                Filters=[
                    {"Name": "private-ip-address", "Values": [target]},
                    {"Name": "instance-state-name", "Values": ["running", "pending"]},
                ]
            )
            for res in resp.get("Reservations", []):
                for inst in res.get("Instances", []):
                    inst_id = inst.get("InstanceId")
                    if inst_id:
                        self._ip_cache[target] = inst_id
                        return inst_id

            resp_pub = self.ec2.describe_instances(
                Filters=[
                    {"Name": "ip-address", "Values": [target]},
                    {"Name": "instance-state-name", "Values": ["running", "pending"]},
                ]
            )
            for res in resp_pub.get("Reservations", []):
                for inst in res.get("Instances", []):
                    inst_id = inst.get("InstanceId")
                    if inst_id:
                        self._ip_cache[target] = inst_id
                        return inst_id
        except Exception as e:
            logger.warning(f"Could not resolve IP '{target}' via EC2 describe-instances: {e}")

        return None

    def _is_command_safe(self, remote_command):
        """Ensures commands sent via SSM adhere to approved deployment patterns."""
        cmd = remote_command.strip()
        # Explicitly reject destructive system commands
        if "rm -rf /" in cmd or "rm -rf /*" in cmd:
            return False
        allowed_prefixes = (
            "docker ",
            "aws ecr get-login-password ",
            "aws s3 ",
            "curl ",
            "mkdir ",
            "echo ",
            "cp ",
            "rm ",
            "sh ",
            "bash ",
        )
        return any(cmd.startswith(p) for p in allowed_prefixes)

    def run(self, host, remote_command, timeout=None):
        """Executes remote_command on target worker via SSM Run Command.
        Returns (success, stdout, stderr). Never raises."""
        instance_id = self.resolve_instance_id(host)
        if not instance_id:
            return False, "", f"Target '{host}' could not be resolved to a valid EC2 instance ID"

        if self.allowed_instances is not None and instance_id not in self.allowed_instances:
            return False, "", f"Instance '{instance_id}' is not in the approved worker list"

        if not self._is_command_safe(remote_command):
            return False, "", f"Rejected unsafe command: {remote_command[:40]}"

        effective_timeout = max(30, int(timeout or self.timeout))
        try:
            res = self.ssm.send_command(
                InstanceIds=[instance_id],
                DocumentName="AWS-RunShellScript",
                Parameters={"commands": [remote_command]},
                TimeoutSeconds=effective_timeout
            )
            cmd_id = res["Command"]["CommandId"]
        except Exception as e:
            return False, "", f"SSM send_command to {instance_id} failed: {e}"

        max_attempts = max(1, int(effective_timeout / max(self.poll_interval, 0.1)))
        for _ in range(max_attempts):
            time.sleep(self.poll_interval)
            try:
                inv = self.ssm.get_command_invocation(
                    CommandId=cmd_id,
                    InstanceId=instance_id
                )
            except Exception:
                continue

            status = inv.get("Status")
            if status in ("Success", "Failed", "Cancelled", "TimedOut"):
                stdout = inv.get("StandardOutputContent", "")
                stderr = inv.get("StandardErrorContent", "")
                if status == "Success":
                    return True, stdout, stderr
                elif status == "TimedOut":
                    return False, stdout, f"SSM command timed out on {instance_id} after {self.timeout}s"
                else:
                    return False, stdout, stderr or f"SSM command failed with status {status}"

        return False, "", f"SSM command timed out waiting for invocation on {instance_id}"


def _fail(deployment_id, message, version_id=None, extra=None):
    if deployment_id is not None:
        database.update_deployment(deployment_id, status="failed", version_id=version_id, message=message, finished=True)
    logger.error(f"Node deployment {deployment_id} failed: {message}")
    result = {"status": "error", "message": message, "deployment_id": deployment_id}
    if extra:
        result.update(extra)
    return result


def _succeed(deployment_id, version_id, node_ip, container_name, image_tag):
    if deployment_id is not None:
        database.update_deployment(deployment_id, status="success", version_id=version_id, finished=True)
    return {
        "status": "success",
        "message": f"{container_name} ({image_tag}) deployed successfully on {node_ip}",
        "node_ip": node_ip,
        "container_name": container_name,
        "image_tag": image_tag,
        "deployment_id": deployment_id,
    }


class NodeDeployer:
    def __init__(self, runner=None, ssh_runner=None, method=None,
                 allowed_instances=None, allowed_registries=None, aws_region=None):
        self.aws_region = aws_region or AUTOPOD_AWS_REGION
        self.allowed_instances = allowed_instances
        self.allowed_registries = allowed_registries

        deploy_method = method or AUTOPOD_DEPLOY_METHOD
        effective_runner = runner or ssh_runner
        if effective_runner is None:
            if deploy_method == "ssh":
                effective_runner = SubprocessSSHRunner()
            else:
                effective_runner = Boto3SSMRunner(
                    region=self.aws_region,
                    allowed_instances=self.allowed_instances
                )

        self.runner = effective_runner
        # Keep ssh_runner as an alias for backwards compatibility with tests and callers
        self.ssh_runner = effective_runner

    def _remote_container_running(self, node_ip, container_name):
        ok, out, _err = self.runner.run(
            node_ip, f"docker inspect {container_name} --format {{{{.State.Running}}}}"
        )
        return ok and out.strip().lower() == "true"

    def _wait_for_node_health(self, node_ip, container_name, port, health_check_path):
        for _attempt in range(NODE_HEALTH_CHECK_RETRIES):
            if self._remote_container_running(node_ip, container_name):
                if port and health_check_path:
                    # If using SSM, verify container with a local curl probe on the worker
                    if isinstance(self.runner, Boto3SSMRunner):
                        probe_cmd = f"curl -fsS -o /dev/null http://127.0.0.1:{port}{health_check_path}"
                        probe_ok, _o, _e = self.runner.run(node_ip, probe_cmd)
                        if probe_ok:
                            return True
                    else:
                        if health_probe.http_health_check(node_ip, port, health_check_path):
                            return True
                else:
                    return True
            time.sleep(NODE_HEALTH_CHECK_DELAY_SECONDS)
        return False

    def _remote_container_image(self, node_ip, container_name):
        """Image tag the named container was started from, or None if the
        container does not exist (or the node is unreachable)."""
        ok, out, _err = self.runner.run(
            node_ip, f"docker inspect {container_name} --format {{{{.Config.Image}}}}"
        )
        image = out.strip()
        return image if ok and image else None

    def _remove_container(self, node_ip, name):
        self.runner.run(node_ip, f"docker stop {name}")
        self.runner.run(node_ip, f"docker rm {name}")

    def _start_container(self, node_ip, container_name, image_tag, target_port):
        return self.runner.run(
            node_ip,
            f"docker run -d --name {container_name} --restart unless-stopped "
            f"-p {target_port}:80 {image_tag}"
        )

    def deploy_to_node(self, node_ip, image_tag, container_name, target_port,
                        health_check_path=None, deployment_id=None, version_id=None):
        """Deploy `image_tag` to a single node using STOP-THEN-START with
        automatic restore. Returns a result dict with `status` "success" or
        "error" (errors carry `restored`: True/False/None).

        Sequence:
          1. Validate inputs (safe image tag, container name, target port).
          2. Record the image the live container runs (its rollback target).
          3. Authenticate to ECR if needed using worker instance role.
          4. `docker pull` the new image. A failed pull leaves the live
             container completely untouched.
          5. Stop+remove the live container, start the new one on target port.
          6. Wait for Docker-state and HTTP health checks.
          7. If unhealthy, remove failed container and restore previous image.
        """
        if not validate_image_reference(image_tag, allowed_registries=self.allowed_registries):
            return _fail(
                deployment_id,
                f"Invalid or unauthorized image reference: '{image_tag}'",
                version_id=version_id,
                extra={"restored": None}
            )

        if not SAFE_CONTAINER_NAME_PATTERN.match(container_name):
            return _fail(
                deployment_id,
                f"Invalid container name: '{container_name}'",
                version_id=version_id,
                extra={"restored": None}
            )

        try:
            target_port = int(target_port)
            if not (1 <= target_port <= 65535):
                raise ValueError()
        except (TypeError, ValueError):
            return _fail(
                deployment_id,
                f"Invalid target port: '{target_port}'",
                version_id=version_id,
                extra={"restored": None}
            )

        # Tidy any leftover candidate container
        self._remove_container(node_ip, f"{container_name}-candidate")

        previous_image = self._remote_container_image(node_ip, container_name)

        # Authenticate to ECR if image is hosted on ECR
        if is_ecr_image(image_tag):
            login_cmd = build_ecr_login_cmd(image_tag, default_region=self.aws_region)
            login_ok, _l_out, login_err = self.runner.run(node_ip, login_cmd)
            if not login_ok:
                msg = f"ECR login failed on {node_ip}: {login_err.strip()}"
                if previous_image:
                    msg += " Previous version on this node was left running untouched."
                return _fail(deployment_id, msg, version_id=version_id, extra={"restored": None})

        pull_ok, _out, pull_err = self.runner.run(node_ip, f"docker pull {image_tag}")
        if not pull_ok:
            msg = f"docker pull {image_tag} failed on {node_ip}: {pull_err.strip()}"
            if previous_image:
                msg += " Previous version on this node was left running untouched."
            return _fail(deployment_id, msg, version_id=version_id, extra={"restored": None})

        self._remove_container(node_ip, container_name)

        run_ok, _out, run_err = self._start_container(node_ip, container_name, image_tag, target_port)
        healthy = run_ok and self._wait_for_node_health(node_ip, container_name, target_port, health_check_path)
        if healthy:
            return _succeed(deployment_id, version_id, node_ip, container_name, image_tag)

        # New version failed: clear it and restore the previous one.
        reason = (f"docker run failed for {container_name} on {node_ip}: {run_err.strip()}"
                  if not run_ok else
                  f"New container ({image_tag}) failed its post-deploy health check on {node_ip}.")
        self._remove_container(node_ip, container_name)

        if not previous_image:
            return _fail(deployment_id, reason + " No previous version existed on this node to restore.",
                         version_id=version_id, extra={"restored": None})

        restore_ok, _o, restore_err = self._start_container(node_ip, container_name, previous_image, target_port)
        restored = restore_ok and self._wait_for_node_health(node_ip, container_name, target_port, health_check_path)
        if restored:
            return _fail(deployment_id, reason + f" Previous version ({previous_image}) was restored and is serving.",
                         version_id=version_id, extra={"restored": True})
        return _fail(
            deployment_id,
            reason + f" RESTORE ALSO FAILED for previous version {previous_image} on {node_ip}"
            + (f": {restore_err.strip()}" if not restore_ok and restore_err.strip() else "")
            + ". This node needs manual attention.",
            version_id=version_id, extra={"restored": False},
        )

    def rolling_deploy(self, application_id, image_tag, container_name, target_port,
                        health_check_path=None, version_id=None, triggered_by="rolling_deploy",
                        is_rollback=False, nodes=None, existing_deployment_id=None):
        """Deploy `image_tag` to every in-service app node, ONE AT A TIME,
        stopping at the first failure. One `deployments` row is created per
        node via `target_node`. If existing_deployment_id is provided, the first
        node reuses it instead of creating a duplicate row."""
        if nodes is None:
            nodes = database.list_app_nodes(status="in_service")

        if not nodes:
            return {
                "status": "error",
                "message": "No in-service app nodes are known. Populate app_nodes first (see aws_nodes.sync_app_nodes_from_asg).",
                "results": [],
            }

        results = []
        for idx, node in enumerate(nodes):
            node_target = node.get("instance_id") or node["ip_address"]
            if idx == 0 and existing_deployment_id is not None:
                dep_id = existing_deployment_id
                database.update_deployment(
                    dep_id,
                    target_node=node["ip_address"],
                    version_id=version_id,
                    is_rollback=is_rollback,
                    port=target_port,
                    container_name=container_name,
                    image_tag=image_tag
                )
            else:
                deployment = database.create_deployment(
                    application_id=application_id, version_id=version_id, status="in_progress",
                    is_rollback=is_rollback, triggered_by=triggered_by, target_node=node["ip_address"]
                )
                dep_id = deployment["id"]

            result = self.deploy_to_node(
                node_target, image_tag, container_name, target_port,
                health_check_path=health_check_path,
                deployment_id=dep_id, version_id=version_id
            )
            results.append(result)
            if result["status"] != "success":
                logger.error(f"Rolling deploy stopped at {node_target}: {result['message']}")
                return {
                    "status": "partial_failure" if any(r["status"] == "success" for r in results[:-1]) else "failed",
                    "message": f"Rolling deployment stopped at {node_target}: {result['message']}",
                    "results": results,
                }

        return {
            "status": "success",
            "message": f"Rolling deployment to {len(results)} node(s) succeeded.",
            "results": results,
        }
