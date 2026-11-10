import pytest
import os
import database
import deployment_engine
from tests.conftest import FakeDockerManager


def test_allocate_next_target_port(database):
    """Test allocating the next available target port in worker range 8081-8099."""
    # Initially no apps, should return 8081
    assert database.allocate_next_target_port() == 8081

    # Register app on 8081
    database.register_application("app1", "https://github.com/test/app1", target_port=8081)
    assert database.allocate_next_target_port() == 8082

    # Register app on 8082
    database.register_application("app2", "https://github.com/test/app2", target_port=8082)
    assert database.allocate_next_target_port() == 8083


def test_list_webhook_deliveries(database):
    """Test recording and listing webhook deliveries."""
    assert database.list_webhook_deliveries() == []

    database.record_webhook_delivery("del-001", event_type="ping")
    database.record_webhook_delivery("del-002", event_type="push")

    deliveries = database.list_webhook_deliveries(limit=10)
    assert len(deliveries) == 2
    # Ordered DESC by id (most recent first)
    assert deliveries[0]["delivery_id"] == "del-002"
    assert deliveries[0]["event_type"] == "push"
    assert deliveries[1]["delivery_id"] == "del-001"
    assert deliveries[1]["event_type"] == "ping"


def test_resolve_container_public_url_multi_app(database, monkeypatch):
    """Test resolving public URL for multi-application setup without localhost."""
    monkeypatch.setenv("AUTOPOD_ALB_URL", "http://autopod-alb-513586547.us-east-1.elb.amazonaws.com")

    # 1. Bakery container maps to root ALB URL
    bakery_url = database.resolve_container_public_url("autopod-sweet-treats-bakery-container")
    assert bakery_url == "http://autopod-alb-513586547.us-east-1.elb.amazonaws.com"

    # 2. Crypto app with explicit public_url
    database.register_application(
        "crypto-price-analysis-dashboard",
        "https://github.com/Ajay73588/Crypto-Price-Analysis-Dashboard.git",
        target_port=8082,
        public_url="http://autopod-alb-513586547.us-east-1.elb.amazonaws.com/crypto/"
    )
    crypto_url = database.resolve_container_public_url("autopod-crypto-price-analysis-dashboard-container")
    assert crypto_url == "http://autopod-alb-513586547.us-east-1.elb.amazonaws.com/crypto/"

    # 3. Internal services never resolve to a public URL
    assert database.resolve_container_public_url("autopod-backend") is None
    assert database.resolve_container_public_url("autopod-frontend") is None


def test_deploy_auto_allocates_port_and_no_localhost(database, monkeypatch):
    """Test that Quick Deploy automatically allocates next target port and never returns localhost URL."""
    # Pre-register sweet treats on 8081
    database.register_application("sweet-treats-bakery", "https://github.com/test/bakery", target_port=8081)

    def mock_clone(url, path, branch=None, commit_sha=None):
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "Dockerfile"), "w") as f:
            f.write("FROM alpine\nCMD ['echo', 'ok']")
        return True

    monkeypatch.setattr(deployment_engine, "clone_repository", mock_clone)

    fake_docker = FakeDockerManager()
    payload = {
        "repository": {
            "clone_url": "https://github.com/Ajay73588/Crypto-Price-Analysis-Dashboard.git",
            "name": "crypto-price-analysis-dashboard"
        }
    }

    result = deployment_engine.deploy(fake_docker, payload, allow_auto_register=True)
    assert result["status"] == "success"
    # Port must be 8082 (auto-allocated since 8081 is taken)
    assert result["port"] == 8082
    # access_url must NOT be present
    assert "access_url" not in result

    # Check database persistence
    app = database.find_application_by_repo_url("https://github.com/Ajay73588/Crypto-Price-Analysis-Dashboard.git")
    assert app is not None
    assert app["target_port"] == "8082"


def test_deploy_strict_failure_when_worker_rollout_fails(database, monkeypatch):
    """When worker nodes are registered, rolling deployment failure must NOT fall back to local control plane."""
    # Register an in-service app node
    database.register_app_node(
        ip_address="10.0.1.107",
        status="in_service",
        instance_id="i-worker-test"
    )

    def mock_clone(url, path, branch=None, commit_sha=None):
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "Dockerfile"), "w") as f:
            f.write("FROM alpine\nCMD ['echo', 'ok']")
        return True

    monkeypatch.setattr(deployment_engine, "clone_repository", mock_clone)

    fake_docker = FakeDockerManager()

    class FailingDeployer:
        def rolling_deploy(self, **kwargs):
            return {
                "status": "failed",
                "message": "SSM connection timeout on worker node 10.0.1.107",
                "results": []
            }

    payload = {
        "repository": {
            "clone_url": "https://github.com/Ajay73588/Crypto-Price-Analysis-Dashboard.git",
            "name": "crypto-price-analysis-dashboard"
        }
    }

    result = deployment_engine.deploy(
        fake_docker, payload, allow_auto_register=True, node_deployer_instance=FailingDeployer()
    )

    # Must be marked error, NOT success
    assert result["status"] == "error"
    assert "SSM connection timeout" in result["message"]

    # Verify container was NOT started locally on control plane as a fake fallback
    assert "autopod-crypto-price-analysis-dashboard-container" not in fake_docker.running_containers
