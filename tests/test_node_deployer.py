"""Tests for node_deployer.py -- the multi-node SSH rolling deployment
orchestrator. Uses a FakeSSHRunner (no real SSH/network needed) so these
run anywhere, while exercising the REAL node_deployer.py logic: command
sequencing, health-check gating, and the "leave it untouched on failure"
safety contract, applied per-node.
"""

import importlib


def _reload(database):
    import node_deployer
    importlib.reload(node_deployer)
    return node_deployer


class FakeSSHRunner:
    """Simulates remote nodes' Docker state without any real SSH call.

    State is kept PER HOST. It deliberately models the one real-Docker
    behaviour the first version of this fake missed (and that therefore hid
    a bug): `docker run -p H:80` fails with "port is already allocated" if
    another RUNNING container on that host already publishes H.

    Knobs: `pull_result`, `unreachable`, `unhealthy_images` (containers from
    these images start but report not-running, like an app that crashes),
    `run_fail_images` (docker run itself fails for these images).
    """

    def __init__(self, run_result=True, pull_result=True, unreachable=False,
                 unhealthy_images=(), run_fail_images=()):
        self.run_result = run_result
        self.pull_result = pull_result
        self.unreachable = unreachable
        self.unhealthy_images = set(unhealthy_images)
        self.run_fail_images = set(run_fail_images)
        self.containers = {}  # (host, name) -> {"image", "port", "running"}
        self.calls = []

    # Test helpers -------------------------------------------------------
    def seed(self, host, name, image, port):
        self.containers[(host, name)] = {"image": image, "port": port, "running": True}

    def image_of(self, host, name):
        c = self.containers.get((host, name))
        return c["image"] if c else None

    def exists(self, host, name):
        return (host, name) in self.containers

    @property
    def running_containers(self):
        return {name for (_h, name), c in self.containers.items() if c["running"]}

    # SSH interface ------------------------------------------------------
    def run(self, host, remote_command):
        self.calls.append((host, remote_command))
        if self.unreachable:
            return False, "", "ssh: connect to host: Connection refused"

        if remote_command.startswith("docker pull "):
            return self.pull_result, "", ("" if self.pull_result else "pull failed")

        if remote_command.startswith("docker run "):
            parts = remote_command.split()
            name = parts[parts.index("--name") + 1]
            port = int(parts[parts.index("-p") + 1].split(":")[0])
            image = parts[-1]
            if not self.run_result or image in self.run_fail_images:
                return False, "", "run failed"
            if (host, name) in self.containers:
                return False, "", f"Conflict. The container name /{name} is already in use"
            for (h, _n), c in self.containers.items():
                if h == host and c["running"] and c["port"] == port:
                    return False, "", f"Bind for 0.0.0.0:{port} failed: port is already allocated"
            self.containers[(host, name)] = {
                "image": image, "port": port, "running": image not in self.unhealthy_images,
            }
            return True, "", ""

        if remote_command.startswith("docker stop "):
            c = self.containers.get((host, remote_command.split()[-1]))
            if c:
                c["running"] = False
            return True, "", ""

        if remote_command.startswith("docker rm "):
            self.containers.pop((host, remote_command.split()[-1]), None)
            return True, "", ""

        if remote_command.startswith("docker inspect "):
            name = remote_command.split()[2]
            c = self.containers.get((host, name))
            if not c:
                return False, "", "No such object"
            if ".Config.Image" in remote_command:
                return True, c["image"], ""
            return True, ("true" if c["running"] else "false"), ""

        return True, "", ""


def _fast(nd):
    nd.NODE_HEALTH_CHECK_RETRIES = 2
    nd.NODE_HEALTH_CHECK_DELAY_SECONDS = 0


def test_deploy_to_node_succeeds_on_a_fresh_node(database):
    nd = _reload(database)
    ssh = FakeSSHRunner()
    deployer = nd.NodeDeployer(ssh_runner=ssh)

    result = deployer.deploy_to_node(
        "10.0.1.5", "myapp:v1", "autopod-myapp-container", 8081, health_check_path=None
    )

    assert result["status"] == "success"
    assert ssh.image_of("10.0.1.5", "autopod-myapp-container") == "myapp:v1"


def test_update_of_running_node_does_not_hit_port_conflict(database):
    """REGRESSION (D1): the old candidate-beside-live design failed every
    update of a node already serving the app, because both containers
    published the same host port. The fake now models that Docker behaviour."""
    nd = _reload(database)
    _fast(nd)
    ssh = FakeSSHRunner()
    ssh.seed("10.0.1.5", "autopod-myapp-container", "myapp:v1", 8081)
    deployer = nd.NodeDeployer(ssh_runner=ssh)

    result = deployer.deploy_to_node("10.0.1.5", "myapp:v2", "autopod-myapp-container", 8081)

    assert result["status"] == "success"
    assert ssh.image_of("10.0.1.5", "autopod-myapp-container") == "myapp:v2"
    assert ssh.running_containers == {"autopod-myapp-container"}


def test_unhealthy_new_version_restores_previous_version(database):
    nd = _reload(database)
    _fast(nd)
    ssh = FakeSSHRunner(unhealthy_images={"myapp:v2"})
    ssh.seed("10.0.1.5", "autopod-myapp-container", "myapp:v1", 8081)
    deployer = nd.NodeDeployer(ssh_runner=ssh)

    result = deployer.deploy_to_node("10.0.1.5", "myapp:v2", "autopod-myapp-container", 8081)

    assert result["status"] == "error"
    assert result["restored"] is True
    assert "restored" in result["message"].lower()
    assert ssh.image_of("10.0.1.5", "autopod-myapp-container") == "myapp:v1"
    assert ssh.running_containers == {"autopod-myapp-container"}


def test_restore_failure_is_reported_loudly(database):
    nd = _reload(database)
    _fast(nd)
    ssh = FakeSSHRunner(unhealthy_images={"myapp:v2"}, run_fail_images={"myapp:v1"})
    ssh.seed("10.0.1.5", "autopod-myapp-container", "myapp:v1", 8081)
    deployer = nd.NodeDeployer(ssh_runner=ssh)

    result = deployer.deploy_to_node("10.0.1.5", "myapp:v2", "autopod-myapp-container", 8081)

    assert result["status"] == "error"
    assert result["restored"] is False
    assert "RESTORE ALSO FAILED" in result["message"]


def test_first_deploy_failure_has_nothing_to_restore(database):
    nd = _reload(database)
    _fast(nd)
    ssh = FakeSSHRunner(unhealthy_images={"myapp:v1"})
    deployer = nd.NodeDeployer(ssh_runner=ssh)

    result = deployer.deploy_to_node("10.0.1.5", "myapp:v1", "autopod-myapp-container", 8081)

    assert result["status"] == "error"
    assert result["restored"] is None
    assert not ssh.exists("10.0.1.5", "autopod-myapp-container")


def test_only_whitelisted_docker_subcommands_are_issued(database):
    """The Ansible forced-command wrapper allows only docker
    pull/stop/rm/run/ps/inspect. No rename or other shell commands."""
    nd = _reload(database)
    _fast(nd)
    ssh = FakeSSHRunner(unhealthy_images={"myapp:v2"})
    ssh.seed("10.0.1.5", "autopod-myapp-container", "myapp:v1", 8081)
    nd.NodeDeployer(ssh_runner=ssh).deploy_to_node("10.0.1.5", "myapp:v2", "autopod-myapp-container", 8081)
    allowed = ("docker pull ", "docker stop ", "docker rm ", "docker run ", "docker ps", "docker inspect ")
    assert ssh.calls
    assert all(cmd.startswith(allowed) for _h, cmd in ssh.calls), ssh.calls


def test_pull_failure_leaves_running_version_untouched(database):
    nd = _reload(database)
    ssh = FakeSSHRunner(pull_result=False)
    ssh.seed("10.0.1.5", "autopod-myapp-container", "myapp:v1", 8081)
    deployer = nd.NodeDeployer(ssh_runner=ssh)

    result = deployer.deploy_to_node("10.0.1.5", "myapp:v2", "autopod-myapp-container", 8081)

    assert result["status"] == "error"
    assert ssh.image_of("10.0.1.5", "autopod-myapp-container") == "myapp:v1"
    assert ssh.running_containers == {"autopod-myapp-container"}


def test_deploy_to_node_fails_cleanly_when_pull_fails(database):
    nd = _reload(database)
    ssh = FakeSSHRunner(pull_result=False)
    deployer = nd.NodeDeployer(ssh_runner=ssh)

    result = deployer.deploy_to_node("10.0.1.5", "myapp:v1", "autopod-myapp-container", 8081)

    assert result["status"] == "error"
    assert "pull" in result["message"].lower()


def test_deploy_to_node_fails_cleanly_when_node_unreachable(database):
    nd = _reload(database)
    ssh = FakeSSHRunner(unreachable=True)
    deployer = nd.NodeDeployer(ssh_runner=ssh)

    result = deployer.deploy_to_node("10.0.1.5", "myapp:v1", "autopod-myapp-container", 8081)

    assert result["status"] == "error"


def test_rolling_deploy_creates_one_deployment_row_per_node(database):
    nd = _reload(database)
    ssh = FakeSSHRunner()
    deployer = nd.NodeDeployer(ssh_runner=ssh)

    app = database.get_or_create_application("myapp")
    version = database.create_version(app["id"], "myapp:v1")
    database.register_app_node("10.0.1.5")
    database.register_app_node("10.0.1.6")

    result = deployer.rolling_deploy(
        application_id=app["id"], image_tag="myapp:v1",
        container_name="autopod-myapp-container", target_port=8081,
        version_id=version["id"],
    )

    assert result["status"] == "success"
    assert len(result["results"]) == 2
    deployments = database.list_deployments(application_id=app["id"])
    assert len(deployments) == 2
    assert {d["target_node"] for d in deployments} == {"10.0.1.5", "10.0.1.6"}
    assert all(d["status"] == "success" for d in deployments)


def test_rolling_deploy_stops_at_first_failure_and_leaves_remaining_nodes_untouched(database):
    nd = _reload(database)
    nd.NODE_HEALTH_CHECK_RETRIES = 2
    nd.NODE_HEALTH_CHECK_DELAY_SECONDS = 0

    app = database.get_or_create_application("myapp")
    version = database.create_version(app["id"], "myapp:v1")
    database.register_app_node("10.0.1.5")
    database.register_app_node("10.0.1.6")
    database.register_app_node("10.0.1.7")

    # A single shared SSH runner whose run_container step fails only for
    # the second node, to prove the third node is never even attempted.
    class FailOnSecondNodeRunner(FakeSSHRunner):
        def __init__(self):
            super().__init__()
            self.attempted_hosts = []

        def run(self, host, remote_command):
            if remote_command.startswith("docker pull "):
                self.attempted_hosts.append(host)
                if host == "10.0.1.6":
                    return False, "", "simulated pull failure"
            return super().run(host, remote_command)

    ssh = FailOnSecondNodeRunner()
    deployer = nd.NodeDeployer(ssh_runner=ssh)

    result = deployer.rolling_deploy(
        application_id=app["id"], image_tag="myapp:v1",
        container_name="autopod-myapp-container", target_port=8081,
        version_id=version["id"],
    )

    assert result["status"] == "partial_failure"
    assert ssh.attempted_hosts == ["10.0.1.5", "10.0.1.6"]  # 10.0.1.7 never reached

    deployments = {d["target_node"]: d for d in database.list_deployments(application_id=app["id"])}
    assert deployments["10.0.1.5"]["status"] == "success"
    assert deployments["10.0.1.6"]["status"] == "failed"
    assert "10.0.1.7" not in deployments  # no row created for an untouched node


def test_rolling_deploy_reports_error_when_no_nodes_known(database):
    nd = _reload(database)
    deployer = nd.NodeDeployer(ssh_runner=FakeSSHRunner())
    app = database.get_or_create_application("myapp")

    result = deployer.rolling_deploy(
        application_id=app["id"], image_tag="myapp:v1",
        container_name="autopod-myapp-container", target_port=8081,
    )

    assert result["status"] == "error"
    assert database.list_deployments(application_id=app["id"]) == []
