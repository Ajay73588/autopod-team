"""tests/test_webhook_delivery.py

Tests for Phase 8: Webhook Replay and Deduplication Protection (X-GitHub-Delivery).

Covers:
1. Valid first delivery with X-GitHub-Delivery is processed and recorded.
2. Repeated delivery with identical X-GitHub-Delivery is safely ignored (idempotent 200).
3. Missing X-GitHub-Delivery is processed (backward compatibility with synthetic/manual payloads).
4. Invalid HMAC signature is rejected with 401 and does NOT record delivery ID.
5. Concurrent duplicate deliveries handled safely via database unique constraint.
"""

import json
import hmac
import hashlib
import importlib
import concurrent.futures
import pytest
import database


SECRET = "test-webhook-secret"


@pytest.fixture()
def client(tmp_path, monkeypatch, fake_docker):
    db_path = str(tmp_path / "webhook_delivery_test.db")
    monkeypatch.setenv("AUTOPOD_DB_PATH", db_path)
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)

    import database as db_mod
    importlib.reload(db_mod)

    import auth
    importlib.reload(auth)

    import version_manager
    importlib.reload(version_manager)

    import deployment_engine
    importlib.reload(deployment_engine)

    import app as app_module
    importlib.reload(app_module)

    app_module.docker_mgr = fake_docker
    app_module.app.config["TESTING"] = True
    test_client = app_module.app.test_client()
    yield test_client, fake_docker
    db_mod.close_db_connection()


def _sign(body_bytes, secret=SECRET):
    return "sha256=" + hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()


def test_valid_first_delivery_is_processed(client):
    test_client, _ = client
    payload = {"test": True, "name": "app-deliv-1"}
    body = json.dumps(payload).encode("utf-8")
    delivery_id = "deliv-uuid-001"

    resp = test_client.post(
        "/webhook",
        data=body,
        content_type="application/json",
        headers={
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Delivery": delivery_id,
            "X-GitHub-Event": "push",
        },
    )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "success"
    assert database.is_webhook_delivery_processed(delivery_id) is True
    details = database.get_webhook_delivery(delivery_id)
    assert details is not None
    assert details["delivery_id"] == delivery_id
    assert details["event_type"] == "push"


def test_repeated_delivery_id_is_safely_ignored(client):
    test_client, _ = client
    payload = {"test": True, "name": "app-deliv-dup"}
    body = json.dumps(payload).encode("utf-8")
    delivery_id = "deliv-uuid-dup"

    # First delivery
    resp1 = test_client.post(
        "/webhook",
        data=body,
        content_type="application/json",
        headers={
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Delivery": delivery_id,
        },
    )
    assert resp1.status_code == 200
    assert resp1.get_json()["status"] == "success"

    initial_deployments = len(database.list_deployments())

    # Second delivery with identical delivery ID (replay/retry)
    resp2 = test_client.post(
        "/webhook",
        data=body,
        content_type="application/json",
        headers={
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Delivery": delivery_id,
        },
    )
    assert resp2.status_code == 200
    data2 = resp2.get_json()
    assert data2["status"] == "ignored"
    assert data2["data"]["duplicate"] is True
    assert delivery_id in data2["message"]

    # Verify no additional deployment was started
    final_deployments = len(database.list_deployments())
    assert final_deployments == initial_deployments


def test_missing_delivery_id_is_processed(client):
    test_client, _ = client
    payload = {"test": True, "name": "app-no-deliv"}
    body = json.dumps(payload).encode("utf-8")

    resp = test_client.post(
        "/webhook",
        data=body,
        content_type="application/json",
        headers={
            "X-Hub-Signature-256": _sign(body),
            # No X-GitHub-Delivery header
        },
    )
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "success"


def test_invalid_signature_does_not_record_delivery_id(client):
    test_client, _ = client
    payload = {"test": True, "name": "app-bad-sig"}
    body = json.dumps(payload).encode("utf-8")
    delivery_id = "deliv-unauth-999"

    resp = test_client.post(
        "/webhook",
        data=body,
        content_type="application/json",
        headers={
            "X-Hub-Signature-256": "sha256=invalid00000000000000000000000000000000000000000000000000000000",
            "X-GitHub-Delivery": delivery_id,
        },
    )
    assert resp.status_code == 401
    assert database.is_webhook_delivery_processed(delivery_id) is False


def test_concurrent_duplicate_deliveries_handled_safely(client):
    """Test thread-safety of atomic delivery recording under concurrency."""
    delivery_id = "deliv-concurrent-42"
    results = []

    def try_record():
        return database.record_webhook_delivery(delivery_id, event_type="push")

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(try_record) for _ in range(8)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    # Exactly ONE thread must succeed in recording; the remaining 7 must report duplicate (False)
    assert results.count(True) == 1
    assert results.count(False) == 7
    assert database.is_webhook_delivery_processed(delivery_id) is True


def test_github_ping_event_returns_200_without_deploying(client):
    """GitHub sends X-GitHub-Event: ping on webhook setup.
    Must respond with HTTP 200 without triggering deployment or creating records."""
    test_client, _ = client
    payload = {
        "zen": "Keep it logically awesome.",
        "hook_id": 12345678,
        "repository": {"clone_url": "https://github.com/acme/unregistered-repo.git", "name": "unregistered-repo"}
    }
    body = json.dumps(payload).encode("utf-8")
    delivery_id = "deliv-ping-001"

    initial_deployments = len(database.list_deployments())

    resp = test_client.post(
        "/webhook",
        data=body,
        content_type="application/json",
        headers={
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Delivery": delivery_id,
            "X-GitHub-Event": "ping",
        },
    )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["data"]["event"] == "ping"
    assert data["data"]["zen"] == "Keep it logically awesome."

    # Zero deployments must have been triggered
    assert len(database.list_deployments()) == initial_deployments
    # Delivery ID was atomically recorded
    assert database.is_webhook_delivery_processed(delivery_id) is True


def test_unsupported_github_event_type_is_safely_ignored(client):
    """Events other than 'push' (e.g. pull_request, issues, star) must be safely
    ignored with HTTP 200 without starting builds or deployments."""
    test_client, _ = client
    payload = {"action": "opened", "pull_request": {"id": 99}}
    body = json.dumps(payload).encode("utf-8")

    initial_deployments = len(database.list_deployments())

    resp = test_client.post(
        "/webhook",
        data=body,
        content_type="application/json",
        headers={
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Delivery": "deliv-pr-001",
            "X-GitHub-Event": "pull_request",
        },
    )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ignored"
    assert "pull_request" in data["message"]
    assert len(database.list_deployments()) == initial_deployments


def test_branch_deletion_push_is_safely_ignored(client):
    """Branch deletion pushes ('after' has all zeros or 'deleted': True) must be ignored."""
    test_client, _ = client
    payload = {
        "ref": "refs/heads/feature-old",
        "deleted": True,
        "after": "0000000000000000000000000000000000000000",
        "repository": {"clone_url": "https://github.com/acme/app.git", "name": "app"}
    }
    body = json.dumps(payload).encode("utf-8")

    initial_deployments = len(database.list_deployments())

    resp = test_client.post(
        "/webhook",
        data=body,
        content_type="application/json",
        headers={
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Delivery": "deliv-del-001",
            "X-GitHub-Event": "push",
        },
    )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ignored"
    assert "deletion" in data["message"].lower()
    assert len(database.list_deployments()) == initial_deployments


def test_webhook_trailing_slash_is_handled(client):
    """Test that POST /webhook/ (with trailing slash) matches and processes identically to /webhook."""
    test_client, _ = client
    payload = {"test": True, "name": "app-slash-test"}
    body = json.dumps(payload).encode("utf-8")
    delivery_id = "deliv-slash-001"

    resp = test_client.post(
        "/webhook/",
        data=body,
        content_type="application/json",
        headers={
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Delivery": delivery_id,
            "X-GitHub-Event": "push",
        },
    )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "success"
    assert database.is_webhook_delivery_processed(delivery_id) is True


def test_api_remove_protected_infrastructure_container_returns_403(client):
    """Test that POST /api/containers/remove rejects removing core infrastructure with HTTP 403."""
    test_client, _ = client
    for protected in ["autopod-frontend", "autopod-backend", "autopod-webhook-proxy", "/autopod-frontend"]:
        resp = test_client.post(
            "/api/containers/remove",
            json={"container_name": protected},
            headers={"X-Admin-Key": "test-admin-key"}
        )
        assert resp.status_code == 403
        data = resp.get_json()
        assert data["success"] is False
        assert "protected" in data["error"].lower()


