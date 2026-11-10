"""
Tests for deployment_engine.py multi-node SSM integration.
Validates that:
1. Registered in-service workers trigger rolling_deploy over SSM.
2. Environments with no registered workers retain local deployment behavior.
3. SSM rollout failure updates deployment to 'failed' and does NOT set version as current.
4. Rollback targets the worker fleet when workers exist.
5. Rollback failure updates deployment to 'failed' and does NOT change current version.
"""

import pytest
import database
import version_manager
import deployment_engine
from webhook_handler import create_demo_project


@pytest.fixture
def mock_git_clone(monkeypatch):
    """Avoid real network git clone in unit tests."""
    def _mock_clone(url, path, branch=None, commit_sha=None):
        create_demo_project(path, "test-app")
        return True
    monkeypatch.setattr(deployment_engine, "clone_repository", _mock_clone)
    return _mock_clone


class FakeNodeDeployer:
    """Mock NodeDeployer for testing deployment_engine integration."""
    def __init__(self, status="success", message="Mock rolling deploy success"):
        self.status = status
        self.message = message
        self.calls = []

    def rolling_deploy(self, application_id, image_tag, container_name, target_port,
                       health_check_path=None, version_id=None, triggered_by="rolling_deploy",
                       is_rollback=False, nodes=None):
        call_info = {
            "application_id": application_id,
            "image_tag": image_tag,
            "container_name": container_name,
            "target_port": target_port,
            "health_check_path": health_check_path,
            "version_id": version_id,
            "triggered_by": triggered_by,
            "is_rollback": is_rollback,
            "nodes": nodes,
        }
        self.calls.append(call_info)
        return {
            "status": self.status,
            "message": self.message,
            "results": [
                {
                    "node": n.get("instance_id") or n["ip_address"],
                    "status": self.status,
                    "message": self.message,
                }
                for n in (nodes or [])
            ]
        }


def test_deploy_uses_rolling_deployer_when_workers_exist(database, fake_docker, mock_git_clone):
    """When in-service app nodes exist, deploy() delegates to rolling_deploy."""
    # Register application with target_port
    app = database.register_application(
        "sweet-treats-bakery",
        "https://github.com/Ajay73588/sweet-treats-bakery",
        target_port=8081,
        health_check_path="/"
    )

    # Register an in-service worker
    database.register_app_node("10.0.1.107", instance_id="i-086803910a4c52091")

    mock_deployer = FakeNodeDeployer(status="success")

    payload = {
        "repository": {
            "name": "sweet-treats-bakery",
            "clone_url": "https://github.com/Ajay73588/sweet-treats-bakery",
        },
        "ref": "refs/heads/main",
        "after": "7ce6ea57295a116c29998d3dd925c1c6f2b9532b",
    }

    result = deployment_engine.deploy(
        fake_docker,
        payload=payload,
        triggered_by="webhook",
        node_deployer_instance=mock_deployer
    )

    assert result["status"] == "success"
    assert len(mock_deployer.calls) == 1
    call = mock_deployer.calls[0]
    assert call["application_id"] == app["id"]
    assert call["target_port"] == 8081
    assert call["container_name"] == "autopod-sweet-treats-bakery-container"
    assert call["is_rollback"] is False
    assert len(call["nodes"]) == 1
    assert call["nodes"][0]["ip_address"] == "10.0.1.107"

    # Verify version was marked current on success
    current_ver = version_manager.get_current_version(app["id"])
    assert current_ver is not None
    assert current_ver["id"] == result["version_id"]

    # Verify overall deployment status is success
    dep = database.get_deployment(result["deployment_id"])
    assert dep["status"] == "success"


def test_deploy_fails_when_no_workers_exist(database, fake_docker, mock_git_clone, monkeypatch):
    """P0.2: When no app nodes are registered/in_service, deploy() fails safely
    and does NOT launch containers locally on the control plane."""
    monkeypatch.setenv("AUTOPOD_ALLOW_LOCAL_FALLBACK", "false")
    app = database.register_application(
        "local-app",
        "https://github.com/example/local-app",
        target_port=8080
    )

    mock_deployer = FakeNodeDeployer(status="success")

    payload = {
        "repository": {
            "name": "local-app",
            "clone_url": "https://github.com/example/local-app",
        },
        "ref": "refs/heads/main",
        "after": "abcdef1234567890abcdef1234567890abcdef12",
    }

    result = deployment_engine.deploy(
        fake_docker,
        payload=payload,
        triggered_by="webhook",
        node_deployer_instance=mock_deployer
    )

    # P0.2: Fails with clear diagnostic; mock_deployer was NOT called
    assert result["status"] == "error"
    assert "Worker fleet unavailable" in result["message"]
    assert len(mock_deployer.calls) == 0
    dep = database.get_deployment(result["deployment_id"])
    assert dep["status"] == "failed"


def test_deploy_allows_local_only_when_explicitly_configured(database, fake_docker, mock_git_clone, monkeypatch):
    """When AUTOPOD_ALLOW_LOCAL_FALLBACK=true is set, legacy test mode deploys locally."""
    monkeypatch.setenv("AUTOPOD_ALLOW_LOCAL_FALLBACK", "true")
    app = database.register_application(
        "local-app",
        "https://github.com/example/local-app",
        target_port=8080
    )

    mock_deployer = FakeNodeDeployer(status="success")

    payload = {
        "repository": {
            "name": "local-app",
            "clone_url": "https://github.com/example/local-app",
        },
        "ref": "refs/heads/main",
        "after": "abcdef1234567890abcdef1234567890abcdef12",
    }

    result = deployment_engine.deploy(
        fake_docker,
        payload=payload,
        triggered_by="webhook",
        node_deployer_instance=mock_deployer
    )

    assert result["status"] == "success"
    assert len(mock_deployer.calls) == 0


def test_deploy_handles_rolling_deploy_failure_cleanly(database, fake_docker, mock_git_clone):
    """When rolling deploy fails, deployment is marked 'failed' and version is NOT current."""
    app = database.register_application(
        "failing-app",
        "https://github.com/example/failing-app",
        target_port=8081
    )
    database.register_app_node("10.0.1.200", instance_id="i-02002002002002002")

    # Record a previous healthy version first
    v1 = version_manager.record_version(app["id"], "failing-app:v1", "commit1")
    version_manager.set_current(app["id"], v1["id"])

    # Deployer that returns failure
    mock_deployer = FakeNodeDeployer(status="failed", message="Health check probe failed on worker")

    payload = {
        "repository": {
            "name": "failing-app",
            "clone_url": "https://github.com/example/failing-app",
        },
        "ref": "refs/heads/main",
        "after": "deadbeef1234567890deadbeef1234567890dead",
    }

    result = deployment_engine.deploy(
        fake_docker,
        payload=payload,
        triggered_by="webhook",
        node_deployer_instance=mock_deployer
    )

    assert result["status"] == "error"
    assert "Health check probe failed on worker" in result["message"]

    # Verify deployment record is marked failed
    dep = database.get_deployment(result["deployment_id"])
    assert dep["status"] == "failed"

    # Verify previous version v1 is STILL current, NOT the failed candidate
    current_ver = version_manager.get_current_version(app["id"])
    assert current_ver["id"] == v1["id"]
    assert current_ver["id"] != result["version_id"]


def test_rollback_uses_rolling_deployer_when_workers_exist(database, fake_docker):
    """When workers exist, rollback() delegates to rolling_deploy with is_rollback=True."""
    app = database.register_application(
        "rollback-app",
        "https://github.com/example/rollback-app",
        target_port=8081
    )
    database.register_app_node("10.0.1.107", instance_id="i-086803910a4c52091")

    # Record v1 and v2
    v1 = version_manager.record_version(app["id"], "rollback-app:v1", "commit1")
    v2 = version_manager.record_version(app["id"], "rollback-app:v2", "commit2")
    version_manager.set_current(app["id"], v2["id"])

    mock_deployer = FakeNodeDeployer(status="success", message="Rollback to v1 succeeded")

    res = deployment_engine.rollback(
        fake_docker,
        application_id=app["id"],
        target_version_id=v1["id"],
        triggered_by="dashboard",
        node_deployer_instance=mock_deployer
    )

    assert res["status"] == "success"
    assert len(mock_deployer.calls) == 1
    call = mock_deployer.calls[0]
    assert call["version_id"] == v1["id"]
    assert call["image_tag"] == "rollback-app:v1"
    assert call["is_rollback"] is True

    # Current version is now v1
    current_ver = version_manager.get_current_version(app["id"])
    assert current_ver["id"] == v1["id"]


def test_rollback_handles_failure_cleanly(database, fake_docker):
    """When rolling rollback fails, deployment is marked 'failed' and current version is unchanged."""
    app = database.register_application(
        "fail-rollback-app",
        "https://github.com/example/fail-rollback-app",
        target_port=8081
    )
    database.register_app_node("10.0.1.107", instance_id="i-086803910a4c52091")

    v1 = version_manager.record_version(app["id"], "fail-rollback-app:v1", "commit1")
    v2 = version_manager.record_version(app["id"], "fail-rollback-app:v2", "commit2")
    version_manager.set_current(app["id"], v2["id"])

    mock_deployer = FakeNodeDeployer(status="failed", message="Worker unreachable during rollback")

    res = deployment_engine.rollback(
        fake_docker,
        application_id=app["id"],
        target_version_id=v1["id"],
        triggered_by="dashboard",
        node_deployer_instance=mock_deployer
    )

    assert res["status"] == "error"
    dep = database.get_deployment(res["deployment_id"])
    assert dep["status"] == "failed"

    # Current version remains v2
    current_ver = version_manager.get_current_version(app["id"])
    assert current_ver["id"] == v2["id"]
