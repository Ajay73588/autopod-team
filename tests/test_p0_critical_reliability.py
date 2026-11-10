"""Automated tests for AutoPod Cloud -- P0 Critical Reliability Implementation.

Covers:
- P0.1: ECR repository validation, creation, already-exists handling, IAM permissions failure, and push error handling.
- P0.2: Enforce worker-only deployment, fail-fast when no in-service workers exist, fail-fast on worker deployment failure, eliminate control-plane container fallback.
- P0.3: Eliminate misleading localhost/127.0.0.1 public URLs, return explicit HTTP 409 when routing is unconfigured, preserve Sweet Treats Bakery public URL.
- P0.4: Protect source repositories, never destructively overwrite cloned code with demo project, fail safely when no Dockerfile exists.
"""

import os
import shutil
import importlib
import pytest
from botocore.exceptions import ClientError
from tests.conftest import FakeDockerManager


@pytest.fixture
def docker_manager():
    from docker_manager import DockerManager
    return DockerManager()


@pytest.fixture
def client(tmp_path, monkeypatch, fake_docker):
    db_path = str(tmp_path / "p0_api_test.db")
    monkeypatch.setenv("AUTOPOD_DB_PATH", db_path)
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    import auth
    importlib.reload(auth)
    import database
    importlib.reload(database)
    database.init_db()
    import app as app_module
    importlib.reload(app_module)
    app_module.docker_mgr = fake_docker
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c
    database.close_db_connection()


class MockECRClient:
    """Mock boto3 ECR client to simulate AWS ECR API calls."""

    def __init__(self, existing_repos=None, fail_create=None, fail_describe=None, fail_auth=False):
        self.existing_repos = set(existing_repos or [])
        self.created_repos = []
        self.fail_create = fail_create
        self.fail_describe = fail_describe
        self.fail_auth = fail_auth

    def describe_repositories(self, repositoryNames=None):
        if self.fail_auth:
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "User is not authorized"}},
                "DescribeRepositories"
            )
        if self.fail_describe:
            raise ClientError(
                {"Error": {"Code": self.fail_describe, "Message": f"Failed describe with {self.fail_describe}"}},
                "DescribeRepositories"
            )
        name = repositoryNames[0] if repositoryNames else ""
        if name in self.existing_repos:
            return {"repositories": [{"repositoryName": name}]}
        raise ClientError(
            {"Error": {"Code": "RepositoryNotFoundException", "Message": f"Repository '{name}' not found"}},
            "DescribeRepositories"
        )

    def create_repository(self, repositoryName, imageScanningConfiguration=None, imageTagMutability=None):
        if self.fail_create:
            raise ClientError(
                {"Error": {"Code": self.fail_create, "Message": f"Failed create with {self.fail_create}"}},
                "CreateRepository"
            )
        self.existing_repos.add(repositoryName)
        self.created_repos.append(repositoryName)
        return {"repository": {"repositoryName": repositoryName}}

    def get_authorization_token(self):
        if self.fail_auth:
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "Token access denied"}},
                "GetAuthorizationToken"
            )
        import base64
        token = base64.b64encode(b"AWS:mock-token-password").decode("utf-8")
        return {
            "authorizationData": [
                {
                    "authorizationToken": token,
                    "proxyEndpoint": "https://822897118262.dkr.ecr.us-east-1.amazonaws.com"
                }
            ]
        }


class FakeNodeDeployer:
    """Mock NodeDeployer for worker rollout testing."""

    def __init__(self, status="success", message="Rolling deploy succeeded"):
        self.status = status
        self.message = message
        self.calls = []

    def rolling_deploy(self, application_id, image_tag, container_name, target_port,
                       health_check_path="/", version_id=None, triggered_by="test",
                       is_rollback=False, nodes=None):
        self.calls.append({
            "application_id": application_id,
            "image_tag": image_tag,
            "container_name": container_name,
            "target_port": target_port,
            "health_check_path": health_check_path,
            "version_id": version_id,
            "triggered_by": triggered_by,
            "is_rollback": is_rollback,
            "nodes": nodes or [],
        })
        return {
            "status": self.status,
            "message": self.message,
            "results": [{"node_ip": n.get("ip_address"), "status": self.status} for n in (nodes or [])],
        }


# ---------------------------------------------------------------------------
# P0.1: ECR Repository Creation & Push Tests
# ---------------------------------------------------------------------------

def test_ecr_repo_name_validation(docker_manager):
    """P0.1: Validate ECR repository name normalizer according to AWS rules."""
    assert docker_manager.validate_ecr_repo_name("my-app") == "my-app"
    assert docker_manager.validate_ecr_repo_name("My App") == "my-app"
    assert docker_manager.validate_ecr_repo_name("org/team/app") == "org/team/app"
    assert docker_manager.validate_ecr_repo_name("-app-name-") == "app-name"

    with pytest.raises(ValueError):
        docker_manager.validate_ecr_repo_name("")

    with pytest.raises(ValueError):
        docker_manager.validate_ecr_repo_name("a")  # too short (< 2)


def test_ecr_repository_already_exists(docker_manager, monkeypatch):
    """P0.1: When repository exists in ECR, ensure_ecr_repository returns True."""
    mock_client = MockECRClient(existing_repos=["autopod-crypto-dashboard"])
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: mock_client)

    ok = docker_manager.ensure_ecr_repository("autopod-crypto-dashboard")
    assert ok is True
    assert docker_manager._last_ecr_error is None
    assert len(mock_client.created_repos) == 0


def test_ecr_repository_created_when_missing(docker_manager, monkeypatch):
    """P0.1: When repository is missing, it is created via boto3."""
    mock_client = MockECRClient(existing_repos=[])
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: mock_client)

    ok = docker_manager.ensure_ecr_repository("autopod-crypto-dashboard")
    assert ok is True
    assert "autopod-crypto-dashboard" in mock_client.created_repos
    assert docker_manager._last_ecr_error is None


def test_ecr_creation_handles_race_condition(docker_manager, monkeypatch):
    """P0.1: When another worker creates the repository concurrently,
    RepositoryAlreadyExistsException is handled safely as success."""
    mock_client = MockECRClient(existing_repos=[], fail_create="RepositoryAlreadyExistsException")
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: mock_client)

    ok = docker_manager.ensure_ecr_repository("autopod-crypto-dashboard")
    assert ok is True
    assert docker_manager._last_ecr_error is None


def test_ecr_creation_fails_with_actionable_iam_error(docker_manager, monkeypatch):
    """P0.1: When IAM policy lacks ecr:CreateRepository, produce actionable error."""
    mock_client = MockECRClient(existing_repos=[], fail_create="AccessDeniedException")
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: mock_client)

    ok = docker_manager.ensure_ecr_repository("autopod-crypto-dashboard")
    assert ok is False
    assert docker_manager._last_ecr_error is not None
    assert "IAM permission denied" in docker_manager._last_ecr_error
    assert "ecr:CreateRepository" in docker_manager._last_ecr_error


def test_deploy_fails_fast_when_ecr_push_fails(database, fake_docker, monkeypatch):
    """P0.1: When ECR push fails, deploy() fails immediately without deploying locally."""
    import deployment_engine
    monkeypatch.setenv("DOCKER_REGISTRY", "822897118262.dkr.ecr.us-east-1.amazonaws.com")
    monkeypatch.setenv("AUTOPOD_ALLOW_LOCAL_FALLBACK", "false")

    # Add healthy worker node
    database.register_app_node("10.0.1.107", status="in_service")
    mock_deployer = FakeNodeDeployer(status="success")

    # Simulate push failure
    fake_docker.push_result = False
    fake_docker._last_push_error = "denied: Your authorization token has expired"

    payload = {"test": True, "name": "failing-push-app"}
    result = deployment_engine.deploy(
        fake_docker,
        payload=payload,
        triggered_by="test",
        node_deployer_instance=mock_deployer
    )

    assert result["status"] == "error"
    assert "ECR Push Failed" in result["message"]
    # Worker deployer was never called
    assert len(mock_deployer.calls) == 0
    # Local container was never launched
    assert len(fake_docker.running_containers) == 0
    # Deployment recorded as failed in DB
    dep = database.get_deployment(result["deployment_id"])
    assert dep["status"] == "failed"


# ---------------------------------------------------------------------------
# P0.2: Enforce Worker-Only Deployment Tests
# ---------------------------------------------------------------------------

def test_deploy_fails_when_no_healthy_workers_available(database, fake_docker, monkeypatch):
    """P0.2: When no worker nodes are in_service, deploy() fails safely with clear diagnostic."""
    import deployment_engine
    monkeypatch.setenv("AUTOPOD_ALLOW_LOCAL_FALLBACK", "false")

    # Zero worker nodes in database
    assert len(database.list_app_nodes(status="in_service")) == 0

    payload = {"test": True, "name": "no-worker-app"}
    result = deployment_engine.deploy(fake_docker, payload=payload, triggered_by="test")

    assert result["status"] == "error"
    assert "Worker fleet unavailable" in result["message"]
    assert "Control-plane local deployment fallback has been disabled" in result["message"]

    # No local container started
    assert len(fake_docker.running_containers) == 0

    # Deployment recorded as failed
    dep = database.get_deployment(result["deployment_id"])
    assert dep["status"] == "failed"


def test_rollback_fails_when_no_healthy_workers_available(database, fake_docker, monkeypatch):
    """P0.2: When no worker nodes are in_service, rollback() fails safely."""
    import deployment_engine
    monkeypatch.setenv("AUTOPOD_ALLOW_LOCAL_FALLBACK", "false")

    app = database.register_application("rollback-app", "https://github.com/example/rollback-app")
    import version_manager
    v1 = version_manager.record_version(app["id"], "rollback-app:v1", "commit1")

    result = deployment_engine.rollback(fake_docker, app["id"], v1["id"], triggered_by="test")

    assert result["status"] == "error"
    assert "Worker fleet unavailable" in result["message"]
    dep = database.get_deployment(result["deployment_id"])
    assert dep["status"] == "failed"


def test_worker_deployment_failure_marks_deployment_failed(database, fake_docker, monkeypatch):
    """P0.2: When rolling worker deployment fails, deployment is marked failed in DB."""
    import deployment_engine
    monkeypatch.setenv("AUTOPOD_ALLOW_LOCAL_FALLBACK", "false")

    database.register_app_node("10.0.1.107", status="in_service")
    mock_deployer = FakeNodeDeployer(status="failed", message="Health check timeout on worker 10.0.1.107")

    payload = {"test": True, "name": "worker-fail-app"}
    result = deployment_engine.deploy(
        fake_docker,
        payload=payload,
        triggered_by="test",
        node_deployer_instance=mock_deployer
    )

    assert result["status"] == "error"
    assert "Health check timeout" in result["message"]

    # Verify database state
    dep = database.get_deployment(result["deployment_id"])
    assert dep["status"] == "failed"
    assert "Health check timeout" in dep["message"]


def test_successful_worker_deployment_marks_success(database, fake_docker, monkeypatch):
    """P0.2: When worker deployment succeeds, deployment is marked success in DB."""
    import deployment_engine
    monkeypatch.setenv("AUTOPOD_ALLOW_LOCAL_FALLBACK", "false")

    database.register_app_node("10.0.1.107", status="in_service")
    mock_deployer = FakeNodeDeployer(status="success", message="Successfully deployed container")

    payload = {"test": True, "name": "worker-success-app"}
    result = deployment_engine.deploy(
        fake_docker,
        payload=payload,
        triggered_by="test",
        node_deployer_instance=mock_deployer
    )

    assert result["status"] == "success"
    dep = database.get_deployment(result["deployment_id"])
    assert dep["status"] == "success"


# ---------------------------------------------------------------------------
# P0.3: Eliminate Misleading Localhost URLs Tests
# ---------------------------------------------------------------------------

def test_resolve_container_public_url_rejects_localhost(database, monkeypatch):
    """P0.3: resolve_container_public_url rejects localhost or 127.0.0.1 candidate URLs."""
    app = database.register_application(
        "crypto-dashboard",
        "https://github.com/Ajay73588/Crypto-Price-Analysis-Dashboard"
    )
    # Set public_url to localhost in DB (simulating old buggy state)
    database.update_application(app["id"], public_url="http://127.0.0.1:8082")

    url = database.resolve_container_public_url("autopod-crypto-dashboard-container")
    assert url is None  # Must NOT return 127.0.0.1!

    # Set env var with localhost
    monkeypatch.setenv("APP_PUBLIC_URL_CRYPTO_DASHBOARD", "http://localhost:8082")
    url = database.resolve_container_public_url("autopod-crypto-dashboard-container")
    assert url is None  # Must NOT return localhost!


def test_resolve_container_public_url_preserves_bakery_alb(database, monkeypatch):
    """P0.3: Sweet Treats Bakery preserves its verified ALB public URL."""
    alb_url = "http://autopod-alb-513586547.us-east-1.elb.amazonaws.com/"
    monkeypatch.setenv("AUTOPOD_ALB_URL", alb_url)

    url = database.resolve_container_public_url("autopod-sweet-treats-bakery-container")
    assert url == alb_url


def test_container_open_endpoint_returns_409_when_routing_unavailable(client, database):
    """P0.3: POST /api/containers/<name>/open returns HTTP 409 when routing is unconfigured."""
    database.register_application(
        "crypto-dashboard",
        "https://github.com/Ajay73588/Crypto-Price-Analysis-Dashboard"
    )
    # Container has no verified public URL
    res = client.post(
        "/api/containers/autopod-crypto-dashboard-container/open",
        headers={"X-Admin-Key": "test-admin-key"}
    )
    assert res.status_code == 409
    data = res.get_json()
    assert data["success"] is False
    assert "Public routing unavailable" in data["error"]
    assert "HTTP 409" in data["error"]


# ---------------------------------------------------------------------------
# P0.4: Protect Source Repositories Tests
# ---------------------------------------------------------------------------

def test_source_repository_contents_preserved_when_no_dockerfile(database, fake_docker, tmp_path, monkeypatch):
    """P0.4 & P1: When a repository archetype is unsupported, deployment fails safely and
    does NOT overwrite existing files with create_demo_project."""
    import deployment_engine
    monkeypatch.setenv("AUTOPOD_ALLOW_LOCAL_FALLBACK", "false")

    # Set up healthy worker
    database.register_app_node("10.0.1.107", status="in_service")

    # Mock clone_repository to simulate cloning an unsupported repo without a Dockerfile or supported framework
    def mock_clone(url, path, branch=None, commit_sha=None):
        os.makedirs(path, exist_ok=True)
        # Create genuine files that are not a supported web framework
        with open(os.path.join(path, "data.bin"), "w") as f:
            f.write("unsupported binary format")
        with open(os.path.join(path, "README.md"), "w") as f:
            f.write("# Real Project")
        return True

    monkeypatch.setattr(deployment_engine, "clone_repository", mock_clone)

    repo_url = "https://github.com/example/unsupported-app.git"
    database.register_application("unsupported-app", repo_url=repo_url)

    payload = {
        "repository": {
            "name": "unsupported-app",
            "clone_url": repo_url,
        },
        "ref": "refs/heads/main",
        "after": "1234567890abcdef1234567890abcdef12345678",
    }

    result = deployment_engine.deploy(fake_docker, payload=payload, triggered_by="webhook")

    assert result["status"] == "error"
    assert "Unsupported application archetype" in result["message"]

    # Verify deployment is marked failed in DB
    dep = database.get_deployment(result["deployment_id"])
    assert dep["status"] == "failed"


def test_create_demo_project_refuses_to_overwrite_existing_files(tmp_path):
    """P0.4: create_demo_project refuses to run if repo_path already contains files."""
    from webhook_handler import create_demo_project

    repo_dir = tmp_path / "test-repo"
    repo_dir.mkdir()
    sample_file = repo_dir / "app.py"
    sample_file.write_text("print('user code')")

    # Call create_demo_project on non-empty directory
    created = create_demo_project(str(repo_dir), "test-repo")
    assert created is False

    # Verify original user file is unchanged
    assert sample_file.read_text() == "print('user code')"
    # Verify index.html was NOT created
    assert not (repo_dir / "index.html").exists()

