"""
Tests for AWS SSM Worker Deployment (Step 29).

Validates Boto3SSMRunner and NodeDeployer using mocked boto3 SSM and EC2 clients.
No real AWS commands or network calls are made.
"""

import pytest
from unittest.mock import MagicMock, patch
import node_deployer as nd


class MockBoto3SSM:
    """Mock SSM client simulating AWS RunShellScript invocations."""

    def __init__(self):
        self.commands_sent = []
        self.next_command_id = "cmd-123456"
        self.default_status = "Success"
        self.default_stdout = ""
        self.default_stderr = ""
        self.status_map = {}  # cmd_id -> status
        self.stdout_map = {}
        self.stderr_map = {}

    def send_command(self, InstanceIds, DocumentName, Parameters, TimeoutSeconds):
        cmd_id = f"cmd-{len(self.commands_sent) + 1}"
        cmd_str = Parameters["commands"][0]
        self.commands_sent.append({
            "CommandId": cmd_id,
            "InstanceIds": InstanceIds,
            "DocumentName": DocumentName,
            "Command": cmd_str,
            "TimeoutSeconds": TimeoutSeconds,
        })
        return {"Command": {"CommandId": cmd_id}}

    def get_command_invocation(self, CommandId, InstanceId):
        status = self.status_map.get(CommandId, self.default_status)
        stdout = self.stdout_map.get(CommandId, self.default_stdout)
        stderr = self.stderr_map.get(CommandId, self.default_stderr)
        return {
            "Status": status,
            "StandardOutputContent": stdout,
            "StandardErrorContent": stderr,
        }


class MockBoto3EC2:
    """Mock EC2 client simulating DescribeInstances lookups."""

    def __init__(self, ip_map=None):
        self.ip_map = ip_map or {}

    def describe_instances(self, Filters):
        target_ip = None
        for f in Filters:
            if f["Name"] in ("private-ip-address", "ip-address"):
                target_ip = f["Values"][0]
                break

        instances = []
        if target_ip and target_ip in self.ip_map:
            instances.append({"InstanceId": self.ip_map[target_ip]})

        return {"Reservations": [{"Instances": instances}]} if instances else {"Reservations": []}


def test_ssm_runner_successful_command():
    mock_ssm = MockBoto3SSM()
    mock_ssm.default_stdout = "container-id-123\n"
    runner = nd.Boto3SSMRunner(ssm_client=mock_ssm, timeout=10, poll_interval=0.01)

    ok, stdout, stderr = runner.run("i-086803910a4c52091", "docker ps")

    assert ok is True
    assert stdout.strip() == "container-id-123"
    assert stderr == ""
    assert len(mock_ssm.commands_sent) == 1
    assert mock_ssm.commands_sent[0]["InstanceIds"] == ["i-086803910a4c52091"]
    assert mock_ssm.commands_sent[0]["Command"] == "docker ps"


def test_ssm_runner_command_failure():
    mock_ssm = MockBoto3SSM()
    mock_ssm.default_status = "Failed"
    mock_ssm.default_stderr = "Error response from daemon: container not found"
    runner = nd.Boto3SSMRunner(ssm_client=mock_ssm, timeout=10, poll_interval=0.01)

    ok, stdout, stderr = runner.run("i-086803910a4c52091", "docker inspect non-existent")

    assert ok is False
    assert "container not found" in stderr


def test_ssm_runner_timeout():
    mock_ssm = MockBoto3SSM()
    mock_ssm.default_status = "TimedOut"
    runner = nd.Boto3SSMRunner(ssm_client=mock_ssm, timeout=5, poll_interval=0.01)

    ok, stdout, stderr = runner.run("i-086803910a4c52091", "docker pull slow-image")

    assert ok is False
    assert "timed out" in stderr.lower()


def test_ssm_runner_resolves_ip_from_database(database):
    mock_ssm = MockBoto3SSM()
    database.register_app_node("10.0.1.5", status="in_service", instance_id="i-086803910a4c52091")
    runner = nd.Boto3SSMRunner(ssm_client=mock_ssm, timeout=10, poll_interval=0.01)

    ok, stdout, stderr = runner.run("10.0.1.5", "docker ps")

    assert ok is True
    assert mock_ssm.commands_sent[0]["InstanceIds"] == ["i-086803910a4c52091"]


def test_ssm_runner_resolves_ip_via_ec2():
    mock_ssm = MockBoto3SSM()
    mock_ec2 = MockBoto3EC2(ip_map={"172.31.10.20": "i-09999999999999999"})
    runner = nd.Boto3SSMRunner(ssm_client=mock_ssm, ec2_client=mock_ec2, timeout=10, poll_interval=0.01)

    ok, stdout, stderr = runner.run("172.31.10.20", "docker ps")

    assert ok is True
    assert mock_ssm.commands_sent[0]["InstanceIds"] == ["i-09999999999999999"]


def test_ssm_runner_rejects_unapproved_worker():
    mock_ssm = MockBoto3SSM()
    runner = nd.Boto3SSMRunner(
        ssm_client=mock_ssm,
        allowed_instances=["i-0123456789abcdef0"],
        timeout=10,
        poll_interval=0.01
    )

    ok, stdout, stderr = runner.run("i-fedcba9876543210f", "docker ps")

    assert ok is False
    assert "not in the approved worker list" in stderr
    assert len(mock_ssm.commands_sent) == 0


def test_ssm_runner_rejects_unsafe_command():
    mock_ssm = MockBoto3SSM()
    runner = nd.Boto3SSMRunner(ssm_client=mock_ssm, timeout=10, poll_interval=0.01)

    ok, stdout, stderr = runner.run("i-086803910a4c52091", "rm -rf /")

    assert ok is False
    assert "Rejected unsafe command" in stderr
    assert len(mock_ssm.commands_sent) == 0


def test_validate_image_reference():
    assert nd.validate_image_reference("nginx:alpine") is True
    assert nd.validate_image_reference("822897118262.dkr.ecr.us-east-1.amazonaws.com/bakery:v1") is True
    # Rejection of shell injection attempts
    assert nd.validate_image_reference("nginx; rm -rf /") is False
    assert nd.validate_image_reference("nginx | curl evil.com") is False
    assert nd.validate_image_reference("nginx`id`") is False
    assert nd.validate_image_reference("nginx$(whoami)") is False
    assert nd.validate_image_reference("") is False


def test_build_ecr_login_cmd_uses_instance_profile_without_secrets():
    image = "822897118262.dkr.ecr.us-east-1.amazonaws.com/autopod-sweet-treats-bakery:v1"
    cmd = nd.build_ecr_login_cmd(image)

    assert "aws ecr get-login-password --region us-east-1" in cmd
    assert "docker login --username AWS --password-stdin 822897118262.dkr.ecr.us-east-1.amazonaws.com" in cmd
    # Ensure no credentials or keys are hardcoded in the command
    assert "AWS_SECRET_ACCESS_KEY" not in cmd
    assert "password=" not in cmd


class SmartSSMDeploymentMock:
    """Simulates realistic remote Docker state across sequential SSM Run commands."""

    def __init__(self, pull_fails=False, health_fails=False, restore_fails=False):
        self.pull_fails = pull_fails
        self.health_fails = health_fails
        self.restore_fails = restore_fails
        self.containers = {}  # name -> {"image": str, "running": bool}
        self.commands_sent = []

    def send_command(self, InstanceIds, DocumentName, Parameters, TimeoutSeconds):
        cmd = Parameters["commands"][0]
        cmd_id = f"cmd-{len(self.commands_sent) + 1}"
        self.commands_sent.append(cmd)

        status = "Success"
        stdout = ""
        stderr = ""

        if cmd.startswith("aws ecr get-login-password"):
            stdout = "Login Succeeded"
        elif cmd.startswith("docker inspect"):
            name = cmd.split()[2]
            if name in self.containers:
                c = self.containers[name]
                if "{{.Config.Image}}" in cmd:
                    stdout = c["image"]
                elif "{{.State.Running}}" in cmd:
                    stdout = "true" if c["running"] else "false"
            else:
                status = "Failed"
                stderr = "No such object"
        elif cmd.startswith("docker pull"):
            if self.pull_fails:
                status = "Failed"
                stderr = "Error: manifest not found"
        elif cmd.startswith("docker stop") or cmd.startswith("docker rm"):
            name = cmd.split()[-1]
            self.containers.pop(name, None)
        elif cmd.startswith("docker run"):
            parts = cmd.split()
            name = parts[parts.index("--name") + 1]
            img = parts[-1]
            if self.restore_fails and "previous" in img:
                status = "Failed"
                stderr = "docker run restore failed"
            else:
                running = not (self.health_fails and "v2" in img)
                self.containers[name] = {"image": img, "running": running}
        elif cmd.startswith("curl "):
            current_app = self.containers.get("autopod-bakery-container", {})
            if self.health_fails and current_app.get("image") == "bakery:v2":
                status = "Failed"
                stderr = "curl: (7) Failed to connect"
            else:
                stdout = ""

        return {"Command": {"CommandId": cmd_id, "_res": (status, stdout, stderr)}}

    def get_command_invocation(self, CommandId, InstanceId):
        for cmd_dict in reversed(self.commands_sent):
            pass
        status, stdout, stderr = "Success", "", ""
        # Look up from the last sent command
        return {"Status": status, "StandardOutputContent": stdout, "StandardErrorContent": stderr}


def test_ssm_deploy_to_node_succeeds_on_fresh_node(database):
    mock = SmartSSMDeploymentMock()

    class AdapterRunner(nd.Boto3SSMRunner):
        def __init__(self):
            super().__init__(ssm_client=mock, timeout=10, poll_interval=0.01)

        def run(self, host, remote_command):
            # Intercept and run through mock directly for test determinism
            res = mock.send_command(
                InstanceIds=[host],
                DocumentName="AWS-RunShellScript",
                Parameters={"commands": [remote_command]},
                TimeoutSeconds=10
            )
            status, stdout, stderr = res["Command"]["_res"]
            return status == "Success", stdout, stderr

    deployer = nd.NodeDeployer(runner=AdapterRunner())
    result = deployer.deploy_to_node(
        "i-086803910a4c52091",
        "822897118262.dkr.ecr.us-east-1.amazonaws.com/sweet-treats-bakery:v1",
        "autopod-bakery-container",
        8081,
        health_check_path="/"
    )

    assert result["status"] == "success"
    assert "deployed successfully" in result["message"]
    assert mock.containers["autopod-bakery-container"]["image"] == "822897118262.dkr.ecr.us-east-1.amazonaws.com/sweet-treats-bakery:v1"
    # Verify ECR login command was issued prior to pull
    assert any("aws ecr get-login-password" in cmd for cmd in mock.commands_sent)


def test_ssm_deploy_failed_pull_leaves_running_container_untouched(database):
    mock = SmartSSMDeploymentMock(pull_fails=True)
    mock.containers["autopod-bakery-container"] = {"image": "bakery:v1-previous", "running": True}

    class AdapterRunner(nd.Boto3SSMRunner):
        def __init__(self):
            super().__init__(ssm_client=mock, timeout=10, poll_interval=0.01)

        def run(self, host, remote_command):
            res = mock.send_command(
                InstanceIds=[host],
                DocumentName="AWS-RunShellScript",
                Parameters={"commands": [remote_command]},
                TimeoutSeconds=10
            )
            status, stdout, stderr = res["Command"]["_res"]
            return status == "Success", stdout, stderr

    deployer = nd.NodeDeployer(runner=AdapterRunner())
    result = deployer.deploy_to_node(
        "i-086803910a4c52091",
        "bakery:v2-new",
        "autopod-bakery-container",
        8081
    )

    assert result["status"] == "error"
    assert "docker pull" in result["message"].lower()
    assert "untouched" in result["message"].lower()
    assert mock.containers["autopod-bakery-container"]["image"] == "bakery:v1-previous"


def test_ssm_deploy_failed_health_check_triggers_rollback(database):
    mock = SmartSSMDeploymentMock(health_fails=True)
    mock.containers["autopod-bakery-container"] = {"image": "bakery:v1-previous", "running": True}

    class AdapterRunner(nd.Boto3SSMRunner):
        def __init__(self):
            super().__init__(ssm_client=mock, timeout=10, poll_interval=0.01)

        def run(self, host, remote_command):
            res = mock.send_command(
                InstanceIds=[host],
                DocumentName="AWS-RunShellScript",
                Parameters={"commands": [remote_command]},
                TimeoutSeconds=10
            )
            status, stdout, stderr = res["Command"]["_res"]
            return status == "Success", stdout, stderr

    deployer = nd.NodeDeployer(runner=AdapterRunner())
    nd.NODE_HEALTH_CHECK_RETRIES = 2
    nd.NODE_HEALTH_CHECK_DELAY_SECONDS = 0.01

    result = deployer.deploy_to_node(
        "i-086803910a4c52091",
        "bakery:v2",
        "autopod-bakery-container",
        8081,
        health_check_path="/"
    )

    assert result["status"] == "error"
    assert result.get("restored") is True
    assert "restored" in result["message"].lower()
    # The previous container is back in service
    assert mock.containers["autopod-bakery-container"]["image"] == "bakery:v1-previous"


def test_ssm_deploy_failed_restore_reported_loudly(database):
    mock = SmartSSMDeploymentMock(health_fails=True, restore_fails=True)
    mock.containers["autopod-bakery-container"] = {"image": "bakery:v1-previous", "running": True}

    class AdapterRunner(nd.Boto3SSMRunner):
        def __init__(self):
            super().__init__(ssm_client=mock, timeout=10, poll_interval=0.01)

        def run(self, host, remote_command):
            res = mock.send_command(
                InstanceIds=[host],
                DocumentName="AWS-RunShellScript",
                Parameters={"commands": [remote_command]},
                TimeoutSeconds=10
            )
            status, stdout, stderr = res["Command"]["_res"]
            return status == "Success", stdout, stderr

    deployer = nd.NodeDeployer(runner=AdapterRunner())
    nd.NODE_HEALTH_CHECK_RETRIES = 2
    nd.NODE_HEALTH_CHECK_DELAY_SECONDS = 0.01

    result = deployer.deploy_to_node(
        "i-086803910a4c52091",
        "bakery:v2",
        "autopod-bakery-container",
        8081,
        health_check_path="/"
    )

    assert result["status"] == "error"
    assert result.get("restored") is False
    assert "RESTORE ALSO FAILED" in result["message"]


def test_ssm_rolling_deploy_creates_deployment_rows_and_succeeds(database):
    mock = SmartSSMDeploymentMock()

    class AdapterRunner(nd.Boto3SSMRunner):
        def __init__(self):
            super().__init__(ssm_client=mock, timeout=10, poll_interval=0.01)

        def run(self, host, remote_command):
            res = mock.send_command(
                InstanceIds=[host],
                DocumentName="AWS-RunShellScript",
                Parameters={"commands": [remote_command]},
                TimeoutSeconds=10
            )
            status, stdout, stderr = res["Command"]["_res"]
            return status == "Success", stdout, stderr

    app = database.get_or_create_application("bakery")
    v = database.create_version(app["id"], "bakery:v1")
    database.register_app_node("10.0.1.10", status="in_service", instance_id="i-01111111111111111")
    database.register_app_node("10.0.1.11", status="in_service", instance_id="i-02222222222222222")

    deployer = nd.NodeDeployer(runner=AdapterRunner())
    result = deployer.rolling_deploy(
        application_id=app["id"],
        image_tag="bakery:v1",
        container_name="autopod-bakery-container",
        target_port=8081,
        version_id=v["id"]
    )

    assert result["status"] == "success"
    assert len(result["results"]) == 2
    deps = database.list_deployments(application_id=app["id"])
    assert len(deps) == 2
    assert {d["target_node"] for d in deps} == {"10.0.1.10", "10.0.1.11"}
    assert all(d["status"] == "success" for d in deps)


def test_ssm_rolling_deploy_stops_at_first_failure(database):
    mock = SmartSSMDeploymentMock()

    class StopOnSecondRunner(nd.Boto3SSMRunner):
        def __init__(self):
            super().__init__(ssm_client=mock, timeout=10, poll_interval=0.01)
            self.attempted_hosts = []

        def run(self, host, remote_command):
            if remote_command.startswith("docker pull"):
                self.attempted_hosts.append(host)
                if host == "i-02222222222222222":
                    return False, "", "simulated pull failure on node 2"
            res = mock.send_command(
                InstanceIds=[host],
                DocumentName="AWS-RunShellScript",
                Parameters={"commands": [remote_command]},
                TimeoutSeconds=10
            )
            status, stdout, stderr = res["Command"]["_res"]
            return status == "Success", stdout, stderr

    app = database.get_or_create_application("bakery")
    v = database.create_version(app["id"], "bakery:v1")
    database.register_app_node("10.0.1.10", status="in_service", instance_id="i-01111111111111111")
    database.register_app_node("10.0.1.11", status="in_service", instance_id="i-02222222222222222")
    database.register_app_node("10.0.1.12", status="in_service", instance_id="i-03333333333333333")

    runner = StopOnSecondRunner()
    deployer = nd.NodeDeployer(runner=runner)
    result = deployer.rolling_deploy(
        application_id=app["id"],
        image_tag="bakery:v1",
        container_name="autopod-bakery-container",
        target_port=8081,
        version_id=v["id"]
    )

    assert result["status"] == "partial_failure"
    assert runner.attempted_hosts == ["i-01111111111111111", "i-02222222222222222"]
    deps = {d["target_node"]: d for d in database.list_deployments(application_id=app["id"])}
    assert deps["10.0.1.10"]["status"] == "success"
    assert deps["10.0.1.11"]["status"] == "failed"
    assert "10.0.1.12" not in deps

