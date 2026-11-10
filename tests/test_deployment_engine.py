"""Deployment engine tests -- the core CI/CD pipeline (item 6.4/6.6).

These use FakeDockerManager (see conftest.py) instead of a real Docker
daemon, so they can run anywhere, but they exercise the REAL
deployment_engine.py logic: ordering, failure handling, and the
old-container-is-never-touched-until-the-new-one-is-healthy guarantee
that fixes the historical bug documented in
CURRENT_STATUS_AND_IMPLEMENTATION_PLAN.md section 7.6.
"""

import importlib
import pytest


TEST_PAYLOAD = {"test": True, "name": "demo-app"}


@pytest.fixture(autouse=True)
def enable_local_fallback_for_fake_docker_tests(monkeypatch):
    """test_deployment_engine.py exercises single-node FakeDockerManager
    container lifecycle mechanics (_deploy_image). Enable AUTOPOD_ALLOW_LOCAL_FALLBACK
    specifically for these isolated fake-docker unit tests."""
    monkeypatch.setenv("AUTOPOD_ALLOW_LOCAL_FALLBACK", "true")


def _reload_engine(database):
    """deployment_engine imports database at module scope; reload it so it
    binds to the same (test-isolated) database module instance."""
    import deployment_engine
    importlib.reload(deployment_engine)
    return deployment_engine


def test_deploy_fails_when_app_never_answers_health_check_path(database, monkeypatch):
    """Application-level health gating (ARCHITECTURE.md Section 6.3): even
    if Docker reports the candidate container as 'running', a deployment
    must still fail if the app never answers on its health_check_path --
    and, per the existing safety contract, the previous container must be
    left running untouched."""
    from tests.conftest import FakeDockerManager
    # Keep the retry loop fast -- this test deliberately exhausts it.
    monkeypatch.setenv("HEALTH_CHECK_RETRIES", "2")
    monkeypatch.setenv("HEALTH_CHECK_DELAY_SECONDS", "0")
    fake = FakeDockerManager(health_sequence=[True], http_health_result=False)
    engine = _reload_engine(database)

    # First deploy succeeds (http_health_result True-by-default at this
    # point isn't reached since engine checks per-call; simulate a healthy
    # first deploy by temporarily allowing the http check).
    fake.http_health_result = True
    first = engine.deploy(fake, TEST_PAYLOAD, triggered_by="test")
    assert first["status"] == "success"
    running_before = set(fake.running_containers)

    # Now simulate the app never responding on its health path.
    fake.http_health_result = False
    second = engine.deploy(fake, TEST_PAYLOAD, triggered_by="test")

    assert second["status"] == "error"
    assert "health check" in second["message"].lower()
    assert fake.running_containers == running_before


def test_webhook_deploy_rejected_for_unregistered_repo(database, fake_docker):
    """Multi-application isolation: a payload with a repo_url that nobody
    registered must be rejected (allow_auto_register=False) before any
    clone/build happens, and must NOT create a deployment row or an
    application row."""
    engine = _reload_engine(database)
    # Not a github.com URL so a rejected deploy can never reach the network
    # clone step even if the rejection logic were broken.
    payload = {"repository": {"clone_url": "https://git.example.com/acme/unregistered", "name": "unregistered"}}

    result = engine.deploy(fake_docker, payload, triggered_by="webhook", allow_auto_register=False)

    assert result["status"] == "rejected"
    assert database.list_applications() == []
    assert database.list_deployments() == []
    assert fake_docker.calls == []


def test_webhook_deploy_succeeds_for_registered_repo(database, fake_docker):
    """A repo_url that WAS pre-registered deploys normally even with
    allow_auto_register=False."""
    engine = _reload_engine(database)
    # Not a github.com URL -- deploy() falls back to create_demo_project()
    # without attempting a network clone, keeping this test fast/offline.
    repo_url = "https://git.example.com/acme/acme-app"
    database.register_application("acme-app", repo_url=repo_url)
    payload = {"repository": {"clone_url": repo_url, "name": "acme-app"}}

    result = engine.deploy(fake_docker, payload, triggered_by="webhook", allow_auto_register=False)

    assert result["status"] == "success"
    assert len(database.list_applications()) == 1


def test_successful_deployment_records_success_and_current_version(database, fake_docker):
    engine = _reload_engine(database)

    result = engine.deploy(fake_docker, TEST_PAYLOAD, triggered_by="test")

    assert result["status"] == "success"
    assert "deployment_id" in result
    assert "version_id" in result

    deployment = database.get_deployment(result["deployment_id"])
    assert deployment["status"] == "success"

    apps = database.list_applications()
    assert len(apps) == 1
    current = database.get_current_version(apps[0]["id"])
    assert current is not None
    assert current["id"] == result["version_id"]


def test_build_failure_is_recorded_and_does_not_touch_running_container(database, fake_docker):
    engine = _reload_engine(database)

    # First, a successful deployment so there's a "previous" container.
    first = engine.deploy(fake_docker, TEST_PAYLOAD, triggered_by="test")
    assert first["status"] == "success"
    running_before = set(fake_docker.running_containers)
    assert running_before  # something is running

    # Now simulate a build failure on the next push.
    fake_docker.build_result = False
    second = engine.deploy(fake_docker, TEST_PAYLOAD, triggered_by="test")

    assert second["status"] == "error"
    deployment = database.get_deployment(second["deployment_id"])
    assert deployment["status"] == "failed"

    # The previously-running container must be untouched.
    assert fake_docker.running_containers == running_before


def test_health_check_failure_leaves_old_container_running(database, fake_docker):
    """This is the direct regression test for bug 7.6: a deployment whose
    new container never becomes healthy must NOT have removed the
    previously-working container."""
    engine = _reload_engine(database)

    first = engine.deploy(fake_docker, TEST_PAYLOAD, triggered_by="test")
    assert first["status"] == "success"
    running_before = set(fake_docker.running_containers)

    # Next deployment: the build/run succeed, but health check never
    # passes for the candidate container.
    fake_docker.health_sequence = [False] * 20
    fake_docker._health_index = 0
    second = engine.deploy(fake_docker, TEST_PAYLOAD, triggered_by="test")

    assert second["status"] == "error"
    deployment = database.get_deployment(second["deployment_id"])
    assert deployment["status"] == "failed"

    # The old container must still be exactly what it was.
    assert fake_docker.running_containers == running_before


def test_container_start_failure_is_handled_cleanly(database, fake_docker):
    engine = _reload_engine(database)

    first = engine.deploy(fake_docker, TEST_PAYLOAD, triggered_by="test")
    assert first["status"] == "success"
    running_before = set(fake_docker.running_containers)

    fake_docker.run_result = False
    second = engine.deploy(fake_docker, TEST_PAYLOAD, triggered_by="test")

    assert second["status"] == "error"
    assert fake_docker.running_containers == running_before


def test_rollback_deploys_a_previous_version_and_marks_is_rollback(database, fake_docker):
    engine = _reload_engine(database)

    first = engine.deploy(fake_docker, TEST_PAYLOAD, triggered_by="test")
    apps = database.list_applications()
    application_id = apps[0]["id"]
    version_a_id = first["version_id"]

    # Deploy a second version (v2) so there's something to roll back FROM.
    second = engine.deploy(fake_docker, TEST_PAYLOAD, triggered_by="test")
    assert second["status"] == "success"
    version_b_id = second["version_id"]
    assert version_b_id != version_a_id

    # Roll back to version A.
    rollback_result = engine.rollback(fake_docker, application_id, version_a_id, triggered_by="test_rollback")
    assert rollback_result["status"] == "success"

    deployment = database.get_deployment(rollback_result["deployment_id"])
    assert bool(deployment["is_rollback"]) is True
    assert deployment["version_id"] == version_a_id

    current = database.get_current_version(application_id)
    assert current["id"] == version_a_id


def test_rollback_to_unknown_version_raises(database, fake_docker):
    engine = _reload_engine(database)
    first = engine.deploy(fake_docker, TEST_PAYLOAD, triggered_by="test")
    apps = database.list_applications()
    application_id = apps[0]["id"]

    import pytest
    with pytest.raises(ValueError):
        engine.rollback(fake_docker, application_id, 999999, triggered_by="test")


def test_failed_rollback_leaves_current_version_running(database, fake_docker):
    engine = _reload_engine(database)

    first = engine.deploy(fake_docker, TEST_PAYLOAD, triggered_by="test")
    apps = database.list_applications()
    application_id = apps[0]["id"]
    version_a_id = first["version_id"]

    second = engine.deploy(fake_docker, TEST_PAYLOAD, triggered_by="test")
    assert second["status"] == "success"
    running_before = set(fake_docker.running_containers)

    # Make the rollback's candidate container fail health checks.
    fake_docker.health_sequence = [False] * 20
    fake_docker._health_index = 0
    result = engine.rollback(fake_docker, application_id, version_a_id, triggered_by="test")

    assert result["status"] == "error"
    # Whatever was running (version B) before the rollback attempt is
    # still running.
    assert fake_docker.running_containers == running_before


def test_registered_target_port_is_the_port_docker_binds(database, fake_docker):
    """An application's target_port must be the host port actually passed to
    docker run, for both the candidate and the promoted container."""
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]

    engine = _reload_engine(database)
    repo_url = "https://git.example.com/acme/port-app"
    database.register_application("port-app", repo_url=repo_url, target_port=free_port)
    payload = {"repository": {"clone_url": repo_url, "name": "port-app"}}

    result = engine.deploy(fake_docker, payload, triggered_by="webhook", allow_auto_register=False)

    assert result["status"] == "success"
    assert fake_docker.run_ports, "docker run was never invoked"
    assert all(ports == f"{free_port}:80" for _, ports in fake_docker.run_ports)


def test_deploy_with_main_branch_and_commit_sha(database, fake_docker, monkeypatch):
    """Exact revision test: push to main with commit SHA passes exact ref and SHA
    to clone_repository and tags image with that commit."""
    engine = _reload_engine(database)
    cloned_args = {}

    def mock_clone(url, path, branch=None, commit_sha=None):
        cloned_args["url"] = url
        cloned_args["branch"] = branch
        cloned_args["commit_sha"] = commit_sha
        from webhook_handler import create_demo_project
        create_demo_project(path, "main-app")
        return True

    monkeypatch.setattr(engine, "clone_repository", mock_clone)

    repo_url = "https://github.com/acme/main-app"
    database.register_application("main-app", repo_url=repo_url)
    commit_sha = "6dcb09b5b57875f334f61aebed695e2e4193db5e"
    payload = {
        "ref": "refs/heads/main",
        "after": commit_sha,
        "repository": {"clone_url": repo_url, "name": "main-app"},
    }

    result = engine.deploy(fake_docker, payload, triggered_by="webhook", allow_auto_register=False)

    assert result["status"] == "success"
    assert cloned_args["branch"] == "main"
    assert cloned_args["commit_sha"] == commit_sha

    # Verify version has git_commit recorded
    version = database.get_version(result["version_id"])
    assert version["git_commit"] == commit_sha[:7]
    assert commit_sha[:7] in version["image_tag"]


def test_deploy_with_feature_branch_and_commit_sha(database, fake_docker, monkeypatch):
    """Feature branch push: ref 'refs/heads/feature/login' extracts branch 'feature/login'."""
    engine = _reload_engine(database)
    cloned_args = {}

    def mock_clone(url, path, branch=None, commit_sha=None):
        cloned_args["branch"] = branch
        cloned_args["commit_sha"] = commit_sha
        from webhook_handler import create_demo_project
        create_demo_project(path, "feature-app")
        return True

    monkeypatch.setattr(engine, "clone_repository", mock_clone)

    repo_url = "https://github.com/acme/feature-app"
    database.register_application("feature-app", repo_url=repo_url)
    commit_sha = "a1b2c3d4e5f67890123456789012345678901234"
    payload = {
        "ref": "refs/heads/feature/login",
        "after": commit_sha,
        "repository": {"clone_url": repo_url, "name": "feature-app"},
    }

    # Webhook triggers ignore non-main branches by design
    ignored_result = engine.deploy(fake_docker, payload, triggered_by="webhook", allow_auto_register=False)
    assert ignored_result["status"] == "ignored"

    # Dashboard/manual triggers deploy the requested branch
    result = engine.deploy(fake_docker, payload, triggered_by="dashboard", allow_auto_register=False)

    assert result["status"] == "success"
    assert cloned_args["branch"] == "feature/login"
    assert cloned_args["commit_sha"] == commit_sha


def test_deploy_with_missing_commit_sha(database, fake_docker, monkeypatch):
    """If push payload lacks an after commit SHA, it deploys without error."""
    engine = _reload_engine(database)
    cloned_args = {}

    def mock_clone(url, path, branch=None, commit_sha=None):
        cloned_args["branch"] = branch
        cloned_args["commit_sha"] = commit_sha
        from webhook_handler import create_demo_project
        create_demo_project(path, "no-sha-app")
        return True

    monkeypatch.setattr(engine, "clone_repository", mock_clone)

    repo_url = "https://github.com/acme/no-sha-app"
    database.register_application("no-sha-app", repo_url=repo_url)
    payload = {
        "ref": "refs/heads/main",
        "repository": {"clone_url": repo_url, "name": "no-sha-app"},
    }

    result = engine.deploy(fake_docker, payload, triggered_by="webhook", allow_auto_register=False)

    assert result["status"] == "success"
    assert cloned_args["branch"] == "main"
    assert cloned_args["commit_sha"] is None


def test_deploy_rejects_malformed_commit_sha(database, fake_docker):
    """Malicious or malformed commit SHA in payload is rejected without deploying."""
    engine = _reload_engine(database)
    repo_url = "https://github.com/acme/sec-app"
    database.register_application("sec-app", repo_url=repo_url)

    payload = {
        "ref": "refs/heads/main",
        "after": "malformed; rm -rf /",
        "repository": {"clone_url": repo_url, "name": "sec-app"},
    }

    result = engine.deploy(fake_docker, payload, triggered_by="webhook", allow_auto_register=False)

    assert result["status"] == "error"
    assert "Malformed or invalid commit SHA" in result["message"]
    # No deployment created
    assert database.list_deployments() == []


def test_deploy_rejects_malformed_ref(database, fake_docker):
    """Malicious or malformed ref (path traversal, control chars) is rejected."""
    engine = _reload_engine(database)
    repo_url = "https://github.com/acme/sec-ref-app"
    database.register_application("sec-ref-app", repo_url=repo_url)

    payload = {
        "ref": "refs/heads/../../etc/passwd",
        "after": "6dcb09b5b57875f334f61aebed695e2e4193db5e",
        "repository": {"clone_url": repo_url, "name": "sec-ref-app"},
    }

    result = engine.deploy(fake_docker, payload, triggered_by="webhook", allow_auto_register=False)

    assert result["status"] == "error"
    assert "Malformed or invalid git ref" in result["message"]
    assert database.list_deployments() == []


def test_deploy_fails_safely_when_commit_cannot_be_checked_out(database, fake_docker, monkeypatch):
    """If git clone or checkout of the requested commit fails, deployment must fail,
    record status='failed', and leave any previous container untouched."""
    engine = _reload_engine(database)

    # 1. Initial healthy deployment
    repo_url = "https://github.com/acme/safe-checkout-app"
    database.register_application("safe-checkout-app", repo_url=repo_url)

    def mock_clone_ok(url, path, branch=None, commit_sha=None):
        from webhook_handler import create_demo_project
        create_demo_project(path, "safe-checkout-app")
        return True

    monkeypatch.setattr(engine, "clone_repository", mock_clone_ok)

    payload_v1 = {
        "ref": "refs/heads/main",
        "after": "1111111111111111111111111111111111111111",
        "repository": {"clone_url": repo_url, "name": "safe-checkout-app"},
    }
    res_v1 = engine.deploy(fake_docker, payload_v1, triggered_by="webhook", allow_auto_register=False)
    assert res_v1["status"] == "success"
    running_before = set(fake_docker.running_containers)

    # 2. Second deployment where commit checkout fails
    def mock_clone_fail(url, path, branch=None, commit_sha=None):
        return False

    monkeypatch.setattr(engine, "clone_repository", mock_clone_fail)

    payload_v2 = {
        "ref": "refs/heads/main",
        "after": "2222222222222222222222222222222222222222",
        "repository": {"clone_url": repo_url, "name": "safe-checkout-app"},
    }
    res_v2 = engine.deploy(fake_docker, payload_v2, triggered_by="webhook", allow_auto_register=False)

    assert res_v2["status"] == "error"
    assert "Failed to checkout revision" in res_v2["message"]

    # Verify previous version container is left running untouched
    assert fake_docker.running_containers == running_before

    # Verify deployment record shows failed
    deployments = database.list_deployments()
    assert len(deployments) == 2
    failed_deploy = [d for d in deployments if d["id"] == res_v2["deployment_id"]][0]
    assert failed_deploy["status"] == "failed"


def test_deploy_pushes_to_ecr_and_records_full_uri(database, fake_docker, monkeypatch):
    """Verify that when DOCKER_REGISTRY is an ECR registry, images are pushed
    and recorded in the database with the full ECR URI."""
    ecr_reg = "822897118262.dkr.ecr.us-east-1.amazonaws.com"
    monkeypatch.setenv("DOCKER_REGISTRY", ecr_reg)
    engine = _reload_engine(database)

    payload = {"test": True, "name": "sweet-treats-bakery"}
    res = engine.deploy(fake_docker, payload, triggered_by="test")

    assert res["status"] == "success"
    # Check that push_image was called
    push_calls = [c for c in fake_docker.calls if c[0] == "push_image"]
    assert len(push_calls) >= 1

    # Check version record in database
    versions = database.list_versions(res["version_id"])
    app_version = database.get_version(res["version_id"])
    assert app_version is not None
    assert app_version["image_tag"].startswith(f"{ecr_reg}/autopod-sweet-treats-bakery:")

    # Check current version
    current = database.get_current_version(app_version["application_id"])
    assert current["id"] == res["version_id"]
    assert current["image_tag"] == app_version["image_tag"]

