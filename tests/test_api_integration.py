"""End-to-end API integration tests using Flask's test client. Exercises
the actual /webhook, /api/deployments, /api/versions, and
/api/deployments/rollback routes together, with the FakeDockerManager
injected in place of DockerManager so no real Docker daemon is required.
"""

import os
import json
import hmac
import hashlib
import importlib

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch, fake_docker):
    db_path = str(tmp_path / "api_test.db")
    monkeypatch.setenv("AUTOPOD_DB_PATH", db_path)
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "test-webhook-secret")

    import database
    importlib.reload(database)

    import auth
    importlib.reload(auth)

    import version_manager
    importlib.reload(version_manager)

    import deployment_engine
    importlib.reload(deployment_engine)

    import app as app_module
    importlib.reload(app_module)

    # Swap in the fake Docker manager so tests don't need a real daemon.
    app_module.docker_mgr = fake_docker

    app_module.app.config["TESTING"] = True
    test_client = app_module.app.test_client()
    yield test_client, fake_docker
    database.close_db_connection()


def _sign(body: bytes, secret: str = "test-webhook-secret") -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_webhook_rejects_missing_signature(client):
    test_client, _ = client
    resp = test_client.post("/webhook", json={"test": True, "name": "demo"})
    assert resp.status_code == 401


def test_webhook_accepts_valid_signature_and_deploys(client):
    test_client, fake_docker = client
    body = json.dumps({"test": True, "name": "demo"}).encode()
    signature = _sign(body)

    resp = test_client.post(
        "/webhook", data=body, content_type="application/json",
        headers={"X-Hub-Signature-256": signature}
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["status"] == "success"

    # A deployment row should now exist and be visible via the API.
    deployments_resp = test_client.get("/api/deployments")
    deployments = deployments_resp.get_json()["data"]
    assert len(deployments) == 1
    assert deployments[0]["status"] == "success"


def test_admin_key_required_for_container_actions(client):
    test_client, _ = client
    resp = test_client.post("/api/containers/start", json={"container_name": "x"})
    assert resp.status_code == 401

    resp = test_client.post(
        "/api/containers/start", json={"container_name": "x"},
        headers={"X-Admin-Key": "test-admin-key"}
    )
    assert resp.status_code != 401


def test_register_application_requires_admin_key(client):
    test_client, _ = client
    resp = test_client.post("/api/applications", json={"name": "payments"})
    assert resp.status_code == 401


def test_manual_deployment_requires_admin_key(client):
    """POST /api/deployments must require valid X-Admin-Key."""
    test_client, _ = client
    # 1. Missing admin key -> 401
    resp = test_client.post("/api/deployments", json={"test": True, "name": "manual-app"})
    assert resp.status_code == 401
    assert "Unauthorized" in resp.get_json()["error"]

    # 2. Invalid admin key -> 401
    resp = test_client.post(
        "/api/deployments",
        json={"test": True, "name": "manual-app"},
        headers={"X-Admin-Key": "wrong-key"}
    )
    assert resp.status_code == 401
    assert "Unauthorized" in resp.get_json()["error"]

    # 3. Valid admin key -> 200 and success
    resp = test_client.post(
        "/api/deployments",
        json={"test": True, "name": "manual-app"},
        headers={"X-Admin-Key": "test-admin-key"}
    )
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "success"

    # Verify deployment row was created
    deployments = test_client.get("/api/deployments").get_json()["data"]
    assert any(d["status"] == "success" for d in deployments)


def test_webhook_manual_deploy_with_admin_key_succeeds_without_signature(client):
    """POST /webhook with valid X-Admin-Key must succeed even without X-Hub-Signature-256."""
    test_client, _ = client
    resp = test_client.post(
        "/webhook",
        json={"test": True, "name": "admin-webhook-app"},
        headers={"X-Admin-Key": "test-admin-key"}
    )
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "success"


def test_all_state_changing_endpoints_require_admin_key(client):
    """SECURITY REGRESSION: every sensitive state-changing or log export
    endpoint must reject unauthenticated requests with HTTP 401."""
    test_client, _ = client

    unauth_post_routes = [
        ("/api/sync", {}),
        ("/api/deployments", {"repository": {"clone_url": "https://github.com/test/repo"}}),
        ("/api/containers/start", {"container_name": "c"}),
        ("/api/containers/stop", {"container_name": "c"}),
        ("/api/containers/restart", {"container_name": "c"}),
        ("/api/containers/remove", {"container_name": "c"}),
        ("/api/images/pull", {"image_name": "img"}),
        ("/api/images/remove", {"image_name": "img"}),
        ("/api/images/tag", {"source_image": "a", "target_tag": "b"}),
        ("/api/images/push", {"image_name": "img"}),
        ("/api/docker/login", {"username": "u", "password": "p"}),
        ("/api/docker/logout", {}),
        ("/api/docker/push", {"image_name": "img"}),
        ("/api/applications", {"name": "app"}),
        ("/api/deployments/rollback", {"version_id": 1}),
        ("/api/deployments/rolling", {"application_id": 1, "version_id": 1}),
        ("/api/deployments/rolling-rollback", {"application_id": 1, "version_id": 1}),
        ("/api/app-nodes/sync", {}),
    ]

    for route, payload in unauth_post_routes:
        resp = test_client.post(route, json=payload)
        assert resp.status_code == 401, f"{route} did not return 401: {resp.status_code}"

    unauth_get_routes = [
        "/api/logs",
        "/api/logs/advanced",
        "/api/logs/export",
        "/api/logs/stream",
    ]

    for route in unauth_get_routes:
        resp = test_client.get(route)
        assert resp.status_code == 401, f"{route} did not return 401: {resp.status_code}"


def test_webhook_rejects_tampered_payload_even_with_valid_looking_signature(client):
    """SECURITY REGRESSION: a signature generated for one body must fail when
    presented with a modified body (tamper resistance)."""
    test_client, _ = client
    body_a = json.dumps({"test": True, "name": "app-a"}).encode()
    body_b = json.dumps({"test": True, "name": "app-b"}).encode()
    sig_for_a = _sign(body_a)

    resp = test_client.post(
        "/webhook", data=body_b, content_type="application/json",
        headers={"X-Hub-Signature-256": sig_for_a}
    )
    assert resp.status_code == 401


def test_webhook_rejects_non_sha256_or_malformed_signature_headers(client):
    """SECURITY REGRESSION: malformed or deprecated algorithms (e.g. sha1) must be rejected."""
    test_client, _ = client
    body = json.dumps({"test": True, "name": "demo"}).encode()

    for bad_header in ("sha1=abcdef123456", "invalid-format", "sha256=", ""):
        resp = test_client.post(
            "/webhook", data=body, content_type="application/json",
            headers={"X-Hub-Signature-256": bad_header}
        )
        assert resp.status_code == 401



def test_app_nodes_endpoint_returns_empty_list_on_fresh_install(client):
    """Nothing populates app_nodes yet (no AWS polling exists); the
    endpoint should still work and return an empty list, not error."""
    test_client, _ = client
    resp = test_client.get("/api/app-nodes")
    assert resp.status_code == 200
    assert resp.get_json()["data"] == []


def test_register_and_list_application(client):
    test_client, _ = client
    resp = test_client.post(
        "/api/applications",
        json={
            "name": "payments",
            "repo_url": "https://github.com/acme/payments",
            "target_port": 8090,
            "health_check_path": "/healthz",
        },
        headers={"X-Admin-Key": "test-admin-key"},
    )
    assert resp.status_code == 201
    created = resp.get_json()["data"]
    assert created["name"] == "payments"
    assert created["target_port"] == "8090"

    list_resp = test_client.get("/api/applications")
    apps = list_resp.get_json()["data"]
    assert any(a["name"] == "payments" for a in apps)

    duplicate_resp = test_client.post(
        "/api/applications", json={"name": "payments"},
        headers={"X-Admin-Key": "test-admin-key"},
    )
    assert duplicate_resp.status_code == 409


def test_webhook_signature_only_push_rejected_for_unregistered_repo(client):
    """A real GitHub push (valid HMAC signature, no admin key) targeting a
    repo nobody registered must be rejected with 404, and must not create
    an application or a deployment row -- multi-application isolation."""
    test_client, _ = client
    body = json.dumps({
        "repository": {"clone_url": "https://git.example.com/acme/unregistered", "name": "unregistered"}
    }).encode()

    resp = test_client.post(
        "/webhook", data=body, content_type="application/json",
        headers={"X-Hub-Signature-256": _sign(body)}
    )
    assert resp.status_code == 404
    assert resp.get_json()["status"] == "rejected"

    apps_resp = test_client.get("/api/applications")
    assert apps_resp.get_json()["data"] == []


def test_webhook_signature_only_push_succeeds_for_registered_repo(client):
    test_client, _ = client
    repo_url = "https://git.example.com/acme/registered-app"
    reg_resp = test_client.post(
        "/api/applications",
        json={"name": "registered-app", "repo_url": repo_url},
        headers={"X-Admin-Key": "test-admin-key"},
    )
    assert reg_resp.status_code == 201

    body = json.dumps({"repository": {"clone_url": repo_url, "name": "registered-app"}}).encode()
    resp = test_client.post(
        "/webhook", data=body, content_type="application/json",
        headers={"X-Hub-Signature-256": _sign(body)}
    )
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "success"


def test_app_nodes_sync_requires_admin_key(client):
    test_client, _ = client
    resp = test_client.post("/api/app-nodes/sync", json={})
    assert resp.status_code == 401


def test_app_nodes_sync_reports_real_error_without_aws_access(client):
    """No AWS credentials are configured in this test environment, so this
    must report a real failure (not a fake success) -- confirming the
    endpoint doesn't silently pretend to have synced anything."""
    test_client, _ = client
    resp = test_client.post(
        "/api/app-nodes/sync", json={"asg_name": "nonexistent-test-asg"},
        headers={"X-Admin-Key": "test-admin-key"},
    )
    assert resp.status_code in (501, 502)
    assert resp.get_json()["success"] is False


def test_rolling_deploy_requires_admin_key(client):
    test_client, _ = client
    resp = test_client.post("/api/deployments/rolling", json={"application_id": 1, "version_id": 1})
    assert resp.status_code == 401


def test_rolling_deploy_requires_target_port_and_uses_known_nodes(client):
    """End-to-end: register an application WITH a target_port, record a
    version, register two fake app_nodes, swap in a fake SSH runner (no
    real network), and confirm the rolling-deploy API actually rolls out
    to both and records per-node deployment rows."""
    test_client, fake_docker = client
    import app as app_module
    import database
    import node_deployer as node_deployer_module

    reg_resp = test_client.post(
        "/api/applications",
        json={"name": "fleet-app", "repo_url": "https://git.example.com/acme/fleet-app", "target_port": 8090},
        headers={"X-Admin-Key": "test-admin-key"},
    )
    assert reg_resp.status_code == 201
    application_id = reg_resp.get_json()["data"]["id"]

    version = database.create_version(application_id, "autopod-fleet-app:v1", "abc123")

    database.register_app_node("10.0.1.5")
    database.register_app_node("10.0.1.6")

    class _AlwaysHealthySSHRunner:
        def __init__(self):
            self.running = set()

        def run(self, host, remote_command):
            if remote_command.startswith("docker run "):
                name = remote_command.split()[remote_command.split().index("--name") + 1]
                self.running.add(name)
                return True, "", ""
            if remote_command.startswith("docker inspect "):
                name = remote_command.split()[2]
                if name not in self.running:
                    return False, "", "no such object"
                return (True, "autopod-fleet-app:v1", "") if ".Config.Image" in remote_command else (True, "true", "")
            if remote_command.startswith("docker stop ") or remote_command.startswith("docker rm "):
                self.running.discard(remote_command.split()[-1])
                return True, "", ""
            return True, "", ""

    original_init = node_deployer_module.NodeDeployer.__init__
    original_http_check = node_deployer_module.health_probe.http_health_check
    node_deployer_module.NodeDeployer.__init__ = lambda self, ssh_runner=None: original_init(self, ssh_runner=_AlwaysHealthySSHRunner())
    # The container-state check is covered by the fake SSH runner above;
    # stub out the actual network HTTP probe too, since these node IPs
    # (10.0.1.x) aren't real hosts this test can reach.
    node_deployer_module.health_probe.http_health_check = lambda host, port, path="/", timeout=3: True
    try:
        resp = test_client.post(
            "/api/deployments/rolling",
            json={"application_id": application_id, "version_id": version["id"]},
            headers={"X-Admin-Key": "test-admin-key"},
        )
    finally:
        node_deployer_module.NodeDeployer.__init__ = original_init
        node_deployer_module.health_probe.http_health_check = original_http_check

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert len(body["data"]["results"]) == 2

    deployments = test_client.get(f"/api/deployments?application_id={application_id}").get_json()["data"]
    assert len(deployments) == 2
    assert {d["target_node"] for d in deployments} == {"10.0.1.5", "10.0.1.6"}


def test_rolling_rollback_end_to_end_via_api(client):
    test_client, _ = client
    reg_resp = test_client.post(
        "/api/applications",
        json={"name": "rollback-app", "target_port": 8082, "health_check_path": "/"},
        headers={"X-Admin-Key": "test-admin-key"},
    )
    assert reg_resp.status_code == 201
    application_id = reg_resp.get_json()["data"]["id"]

    import database
    version_1 = database.create_version(application_id, "rollback-app:v1")
    version_2 = database.create_version(application_id, "rollback-app:v2")
    database.set_current_version(application_id, version_2["id"])

    database.register_app_node("10.0.1.10")
    database.register_app_node("10.0.1.11")

    import node_deployer as node_deployer_module

    class _AlwaysHealthySSHRunner:
        def __init__(self):
            self.running = set()

        def run(self, host, remote_command):
            if remote_command.startswith("docker run "):
                name = remote_command.split()[remote_command.split().index("--name") + 1]
                self.running.add(name)
                return True, "", ""
            if remote_command.startswith("docker inspect "):
                name = remote_command.split()[2]
                if name not in self.running:
                    return False, "", "no such object"
                return (True, "rollback-app:v1", "") if ".Config.Image" in remote_command else (True, "true", "")
            if remote_command.startswith("docker stop ") or remote_command.startswith("docker rm "):
                self.running.discard(remote_command.split()[-1])
                return True, "", ""
            return True, "", ""

    original_init = node_deployer_module.NodeDeployer.__init__
    original_http_check = node_deployer_module.health_probe.http_health_check
    node_deployer_module.NodeDeployer.__init__ = lambda self, ssh_runner=None: original_init(self, ssh_runner=_AlwaysHealthySSHRunner())
    node_deployer_module.health_probe.http_health_check = lambda host, port, path="/", timeout=3: True
    try:
        resp = test_client.post(
            "/api/deployments/rolling-rollback",
            json={"application_id": application_id, "version_id": version_1["id"]},
            headers={"X-Admin-Key": "test-admin-key"},
        )
    finally:
        node_deployer_module.NodeDeployer.__init__ = original_init
        node_deployer_module.health_probe.http_health_check = original_http_check

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert body["data"]["status"] == "success"
    assert len(body["data"]["results"]) == 2

    deployments = test_client.get(f"/api/deployments?application_id={application_id}").get_json()["data"]
    assert len(deployments) == 2
    assert {d["target_node"] for d in deployments} == {"10.0.1.10", "10.0.1.11"}
    assert all(d["is_rollback"] == 1 for d in deployments)
    assert all(d["triggered_by"] == "rolling_rollback_api" for d in deployments)


def test_rollback_end_to_end_via_api(client):
    test_client, fake_docker = client
    body = json.dumps({"test": True, "name": "demo"}).encode()

    # Deploy version A
    resp_a = test_client.post(
        "/webhook", data=body, content_type="application/json",
        headers={"X-Hub-Signature-256": _sign(body)}
    )
    version_a_id = resp_a.get_json()["data"]["version_id"]

    # Deploy version B
    resp_b = test_client.post(
        "/webhook", data=body, content_type="application/json",
        headers={"X-Hub-Signature-256": _sign(body)}
    )
    assert resp_b.get_json()["data"]["version_id"] != version_a_id

    # Versions endpoint should show both, with B (the latest) current.
    versions_resp = test_client.get("/api/versions")
    versions = versions_resp.get_json()["data"]
    assert len(versions) == 2

    # Roll back to A without the admin key -> 401
    resp = test_client.post("/api/deployments/rollback", json={"version_id": version_a_id})
    assert resp.status_code == 401

    # Roll back to A with the admin key -> success
    resp = test_client.post(
        "/api/deployments/rollback", json={"version_id": version_a_id},
        headers={"X-Admin-Key": "test-admin-key"}
    )
    assert resp.status_code == 200
    body_json = resp.get_json()
    assert body_json["success"] is True
    assert body_json["deployment"]["version_id"] == version_a_id


def test_applications_current_versions_returns_all_active_apps(client):
    """REGRESSION (D4): multi-app bootstrap support across registered applications."""
    test_client, _ = client
    import database
    import version_manager

    reg1 = test_client.post(
        "/api/applications",
        json={"name": "app-one", "repo_url": "https://git.example.com/acme/app-one", "target_port": 8081},
        headers={"X-Admin-Key": "test-admin-key"},
    )
    app1 = reg1.get_json()["data"]
    v1 = database.create_version(app1["id"], "autopod-app-one:v1", "commit1")
    version_manager.set_current(app1["id"], v1["id"])

    reg2 = test_client.post(
        "/api/applications",
        json={"name": "app-two", "repo_url": "https://git.example.com/acme/app-two", "target_port": 8082},
        headers={"X-Admin-Key": "test-admin-key"},
    )
    app2 = reg2.get_json()["data"]
    v2 = database.create_version(app2["id"], "autopod-app-two:v2", "commit2")
    version_manager.set_current(app2["id"], v2["id"])

    resp = test_client.get("/api/applications/current-versions")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["success"] is True
    data = payload["data"]
    assert len(data) == 2
    names = {d["application_name"] for d in data}
    assert names == {"app-one", "app-two"}
    ports = {str(d["target_port"]) for d in data}
    assert ports == {"8081", "8082"}


def test_rolling_deploy_without_target_port_explains_why(client):
    test_client, _ = client
    import database
    reg = test_client.post(
        "/api/applications", json={"name": "noport", "repo_url": "https://git.example.com/acme/noport"},
        headers={"X-Admin-Key": "test-admin-key"},
    )
    app_id = reg.get_json()["data"]["id"]
    version = database.create_version(app_id, "autopod-noport:v1", "abc")
    resp = test_client.post(
        "/api/deployments/rolling", json={"application_id": app_id, "version_id": version["id"]},
        headers={"X-Admin-Key": "test-admin-key"},
    )
    assert resp.status_code == 400
    assert "target_port" in resp.get_json()["error"]


def test_versions_current_filter_returns_only_current_version_after_rollback(client):
    """The ASG bootstrap script calls /api/versions?current=true and deploys
    data[0]. After a rollback the current version is NOT the newest one, so
    the filter must return exactly the current version."""
    test_client, _ = client
    body = json.dumps({"test": True, "name": "demo"}).encode()
    hdr = {"X-Hub-Signature-256": _sign(body)}
    version_a = test_client.post("/webhook", data=body, content_type="application/json", headers=hdr).get_json()["data"]["version_id"]
    test_client.post("/webhook", data=body, content_type="application/json", headers=hdr)
    rb = test_client.post("/api/deployments/rollback", json={"version_id": version_a},
                          headers={"X-Admin-Key": "test-admin-key"})
    assert rb.status_code == 200

    data = test_client.get("/api/versions?current=true").get_json()["data"]
    assert [v["id"] for v in data] == [version_a]


def _run_bootstrap_script(tmp_path, versions_json, control_plane_url="http://10.0.0.5:5000", default_port="8080"):
    """Render the real Terraform user-data template and execute it with bash,
    with shims for dnf/systemctl/usermod/curl/docker on PATH. curl returns
    `versions_json`; docker just logs its arguments. Returns (exit_code,
    docker_log_lines, curl_urls, stderr)."""
    import os
    import subprocess

    if os.name == "nt":
        pytest.skip("Bootstrap script execution test requires a POSIX environment (Linux/macOS)")

    template = open(os.path.join(os.path.dirname(__file__), "..", "infra", "terraform", "templates",
                                 "app_node_user_data.sh.tpl")).read()
    rendered = (template.replace("${control_plane_api_url}", control_plane_url)
                        .replace("${app_target_port}", default_port)
                        .replace("$${", "${"))
    script = tmp_path / "bootstrap.sh"
    script.write_text(rendered)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    resp = tmp_path / "resp.json"
    resp.write_text(versions_json)
    dlog, clog = tmp_path / "docker.log", tmp_path / "curl.log"
    for noop in ("dnf", "systemctl", "usermod"):
        (bindir / noop).write_text("#!/bin/sh\nexit 0\n")
    (bindir / "docker").write_text(f'#!/bin/sh\necho "$@" >> {dlog}\nexit 0\n')
    (bindir / "curl").write_text(f'#!/bin/sh\nfor a in "$@"; do last="$a"; done\necho "$last" >> {clog}\ncat {resp}\n')
    for f in bindir.iterdir():
        f.chmod(0o755)

    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")
    r = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env, timeout=30)
    docker_lines = dlog.read_text().splitlines() if dlog.exists() else []
    curl_urls = clog.read_text().splitlines() if clog.exists() else []
    return r.returncode, docker_lines, curl_urls, r.stderr


def test_bootstrap_script_uses_same_container_name_and_port_as_rolling_deploy(client, tmp_path):
    """REGRESSION (D8): the ASG bootstrap script and node_deployer must name
    the container identically. This runs the real template against the real
    /api/versions?current=true response of the Flask app."""
    test_client, _ = client
    import app as app_module
    import database
    import version_manager

    reg = test_client.post(
        "/api/applications",
        json={"name": "fleet-app", "repo_url": "https://git.example.com/acme/fleet-app", "target_port": 8090},
        headers={"X-Admin-Key": "test-admin-key"},
    )
    application = reg.get_json()["data"]
    version = database.create_version(application["id"], "autopod-fleet-app:v7", "abc123")
    version_manager.set_current(application["id"], version["id"])

    versions_resp = test_client.get("/api/versions?current=true")
    payload = versions_resp.get_json()
    expected_name = app_module._container_name_for_application(application)
    assert payload["container_name"] == expected_name
    assert payload["target_port"] == "8090"

    code, docker_lines, curl_urls, stderr = _run_bootstrap_script(tmp_path, versions_resp.get_data(as_text=True))

    assert code == 0, stderr
    assert curl_urls == ["http://10.0.0.5:5000/api/versions?current=true"]
    run_lines = [l for l in docker_lines if l.startswith("run ")]
    assert len(run_lines) == 1
    assert f"--name {expected_name} " in run_lines[0]
    assert "--name autopod-app " not in run_lines[0]
    assert "-p 8090:80" in run_lines[0]
    assert run_lines[0].endswith("autopod-fleet-app:v7")
    assert "pull autopod-fleet-app:v7" in docker_lines


def test_bootstrap_script_refuses_to_guess_a_container_name(tmp_path):
    payload = '{"success": true, "data": [{"image_tag": "x:1"}]}'
    code, docker_lines, _urls, stderr = _run_bootstrap_script(tmp_path, payload)
    assert code == 0
    assert not any(l.startswith("run ") for l in docker_lines)
    assert "container name" in stderr


def test_bootstrap_script_falls_back_to_template_port_when_none_registered(tmp_path):
    payload = '{"success": true, "data": [{"image_tag": "x:1"}], "container_name": "autopod-x-container", "target_port": null}'
    code, docker_lines, _urls, _stderr = _run_bootstrap_script(tmp_path, payload, default_port="8123")
    assert code == 0
    assert any(l.startswith("run ") and "-p 8123:80" in l for l in docker_lines)


def test_update_application_via_patch(client):
    test_client, _ = client
    # 1. Register application
    reg = test_client.post(
        "/api/applications",
        json={"name": "patch-test-app", "repo_url": "https://git.example.com/acme/patch-app", "target_port": 8095},
        headers={"X-Admin-Key": "test-admin-key"},
    )
    assert reg.status_code == 201
    app_id = reg.get_json()["data"]["id"]

    # 2. Unauthorized PATCH rejected
    unauth = test_client.patch(
        f"/api/applications/{app_id}",
        json={"health_check_path": "/health-fail"},
    )
    assert unauth.status_code == 401

    # 3. Authorized PATCH updates health_check_path and target_port
    patch_resp = test_client.patch(
        f"/api/applications/{app_id}",
        json={"health_check_path": "/health-fail", "target_port": 8096, "public_url": "http://patch-alb.example.com/"},
        headers={"X-Admin-Key": "test-admin-key"},
    )
    assert patch_resp.status_code == 200
    updated = patch_resp.get_json()["data"]
    assert updated["health_check_path"] == "/health-fail"
    assert updated["target_port"] == "8096"
    assert updated["public_url"] == "http://patch-alb.example.com/"


def test_containers_public_url_and_open_endpoints(client, monkeypatch):
    test_client, _ = client

    # Register an application with public_url
    alb_url = "http://autopod-alb-513586547.us-east-1.elb.amazonaws.com/"
    reg = test_client.post(
        "/api/applications",
        json={
            "name": "sweet-treats-bakery",
            "repo_url": "https://github.com/Ajay73588/sweet-treats-bakery",
            "target_port": 8081,
            "public_url": alb_url
        },
        headers={"X-Admin-Key": "test-admin-key"},
    )
    assert reg.status_code == 201

    # 1. GET /api/containers/<name>/weburl for registered app
    resp_weburl = test_client.get("/api/containers/autopod-sweet-treats-bakery-container/weburl")
    assert resp_weburl.status_code == 200
    data_weburl = resp_weburl.get_json()
    assert data_weburl["success"] is True
    assert data_weburl["data"]["web_url"] == alb_url
    assert data_weburl["data"]["has_web_interface"] is True

    # 2. GET /api/containers/<name>/weburl for internal service
    resp_internal = test_client.get("/api/containers/autopod-backend/weburl")
    assert resp_internal.status_code == 200
    data_internal = resp_internal.get_json()
    assert data_internal["success"] is True
    assert data_internal["data"]["web_url"] is None
    assert data_internal["data"]["has_web_interface"] is False

    # 3. POST /api/containers/<name>/open for registered app
    resp_open = test_client.post(
        "/api/containers/autopod-sweet-treats-bakery-container/open",
        headers={"X-Admin-Key": "test-admin-key"}
    )
    assert resp_open.status_code == 200
    data_open = resp_open.get_json()
    assert data_open["success"] is True
    assert data_open["data"]["web_url"] == alb_url

    # 4. POST /api/containers/<name>/open for internal service -> 409 (P0.3: Routing Unavailable)
    resp_open_internal = test_client.post(
        "/api/containers/autopod-backend/open",
        headers={"X-Admin-Key": "test-admin-key"}
    )
    assert resp_open_internal.status_code == 409
    assert "internal service" in resp_open_internal.get_json()["error"]

