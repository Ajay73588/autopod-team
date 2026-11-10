"""
tests/test_webhook_ingress.py

Automated security and functionality tests for the dedicated webhook-only ingress proxy.
Verifies Phase 10.8 requirements:
1. Static configuration guarantees: exact match location = /webhook, method restriction, deny-by-default.
2. Live ingress proxy forwarding: valid HMAC webhook requests reach the backend and succeed.
3. Live ingress proxy denial: all other backend routes (/health, /api/*, /) return 404.
4. Method restriction: GET, PUT, DELETE, PATCH to /webhook return 403 Forbidden.
5. Path variations and traversals: /webhook/, /webhook/extra, /webhook/.., /WEBHOOK return 404.
"""

import os
import hmac
import hashlib
import json
import socket
import urllib.request
import urllib.error
import pytest


INGRESS_HOST = "127.0.0.1"
INGRESS_PORT = int(os.environ.get("WEBHOOK_INGRESS_PORT", "8080"))
INGRESS_BASE_URL = f"http://{INGRESS_HOST}:{INGRESS_PORT}"
CONF_PATH = os.path.join(
    os.path.dirname(__file__), "..", "infra", "nginx", "webhook_ingress.conf"
)
WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "dummy-local-webhook-secret")


def _is_ingress_running():
    """Probe whether the ingress proxy is listening on INGRESS_PORT."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        res = s.connect_ex((INGRESS_HOST, INGRESS_PORT))
        return res == 0
    finally:
        s.close()


def _request_ingress(path, method="GET", data=None, headers=None):
    """Send an HTTP request to the ingress proxy and return (status_code, body_text)."""
    url = f"{INGRESS_BASE_URL}{path}"
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="ignore")


# ---------------------------------------------------------------------------
# Static Configuration Security Tests
# ---------------------------------------------------------------------------

def test_webhook_ingress_conf_file_exists():
    """Verify webhook_ingress.conf exists in infra/nginx."""
    assert os.path.isfile(CONF_PATH), f"Missing config file at {CONF_PATH}"


def test_webhook_ingress_conf_strict_directives():
    """Verify critical security directives are configured in webhook_ingress.conf."""
    with open(CONF_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    # Deny-by-default catch-all
    assert "location / {" in content
    assert "return 404" in content

    # Exact literal matching for /webhook
    assert "location = /webhook {" in content

    # Method restriction denying all non-POST/OPTIONS methods
    assert "limit_except POST OPTIONS {" in content
    assert "deny all;" in content

    # Fixed upstream target
    assert "proxy_pass http://autopod-backend:5000/webhook;" in content

    # Server tokens suppression
    assert "server_tokens off;" in content


# ---------------------------------------------------------------------------
# Live Ingress HTTP Tests
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _is_ingress_running(), reason="Ingress proxy is not running on port 8080")
class TestLiveWebhookIngress:
    """Live HTTP verification against the running autopod-webhook-proxy."""

    def test_forward_valid_signed_ping(self):
        """Valid HMAC-SHA256 ping payload is forwarded through ingress to backend and returns 200 OK."""
        payload = json.dumps({"zen": "Safety over speed.", "hook_id": 99991}).encode("utf-8")
        sig = "sha256=" + hmac.new(WEBHOOK_SECRET.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        delivery_id = f"test-ping-{os.urandom(4).hex()}"

        headers = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
            "X-GitHub-Event": "ping",
            "X-GitHub-Delivery": delivery_id,
        }

        status, body = _request_ingress("/webhook", method="POST", data=payload, headers=headers)
        assert status == 200
        data = json.loads(body)
        assert data.get("status") == "ok"
        assert "GitHub ping event received successfully" in data.get("message", "")

    def test_forward_unsigned_webhook_is_rejected_by_backend(self):
        """Unsigned POST /webhook forwarded through ingress is rejected by backend HMAC check with 401."""
        payload = b'{"zen": "no signature"}'
        headers = {"Content-Type": "application/json"}

        status, body = _request_ingress("/webhook", method="POST", data=payload, headers=headers)
        assert status == 401
        data = json.loads(body)
        assert "Invalid or missing webhook signature" in data.get("message", "")

    def test_forward_invalid_signature_is_rejected(self):
        """POST /webhook with forged HMAC signature returns 401."""
        payload = b'{"zen": "tampered"}'
        headers = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": "sha256=ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
            "X-GitHub-Event": "ping",
        }

        status, body = _request_ingress("/webhook", method="POST", data=payload, headers=headers)
        assert status == 401

    def test_forward_delivery_deduplication(self):
        """Replaying the same delivery ID through ingress returns HTTP 200 ignored."""
        payload = json.dumps({"zen": "Idempotent delivery.", "hook_id": 88881}).encode("utf-8")
        sig = "sha256=" + hmac.new(WEBHOOK_SECRET.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        delivery_id = f"test-dedup-{os.urandom(6).hex()}"

        headers = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
            "X-GitHub-Event": "ping",
            "X-GitHub-Delivery": delivery_id,
        }

        # First delivery: processed
        status1, _ = _request_ingress("/webhook", method="POST", data=payload, headers=headers)
        assert status1 == 200

        # Duplicate delivery: ignored
        status2, body2 = _request_ingress("/webhook", method="POST", data=payload, headers=headers)
        assert status2 == 200
        data2 = json.loads(body2)
        assert data2.get("status") == "ignored"
        assert data2.get("data", {}).get("duplicate") is True

    @pytest.mark.parametrize("method", ["GET", "PUT", "DELETE", "PATCH"])
    def test_disallowed_http_methods_on_webhook_return_403(self, method):
        """Non-POST/OPTIONS methods to /webhook are denied by Nginx with 403 Forbidden."""
        status, _ = _request_ingress("/webhook", method=method)
        assert status == 403

    @pytest.mark.parametrize("forbidden_path", [
        "/",
        "/health",
        "/api/status",
        "/api/containers",
        "/api/applications",
        "/api/deployments",
        "/api/debug/docker-health",
        "/api/debug/containers",
        "/api/logs/stream",
        "/api/logs/export",
        "/api/docker/login",
        "/api/docker/check-login",
        "/api/images",
        "/static/index.html",
    ])
    def test_internal_backend_paths_are_denied_with_404(self, forbidden_path):
        """Every backend route other than /webhook is denied by Nginx with 404 Not Found."""
        status, body = _request_ingress(forbidden_path, method="GET")
        assert status == 404
        assert "Endpoint not exposed on webhook ingress" in body

        # Also verify POST to these internal paths is denied with 404
        post_status, post_body = _request_ingress(
            forbidden_path, method="POST", data=b"{}", headers={"Content-Type": "application/json"}
        )
        assert post_status == 404
        assert "Endpoint not exposed on webhook ingress" in post_body

    @pytest.mark.parametrize("evasion_path", [
        "/webhook/",                  # trailing slash
        "/webhook/extra",             # sub-path
        "/webhook/sub/path",          # deep sub-path
        "/webhook/../health",         # path traversal to health
        "/webhook/../api/status",     # path traversal to api
        "/WEBHOOK",                   # case variation
        "/WebHook",                   # mixed case
        "/webhook.json",              # file extension variation
        "/webhook.php",               # extension probe
        "/webhook%20",                # encoded space
        "/api/../webhook/../api",     # multiple traversals
    ])
    def test_path_evasion_and_traversal_attempts_return_404(self, evasion_path):
        """Path variations, trailing slashes, and traversal attempts are strictly denied with 404."""
        status, body = _request_ingress(
            evasion_path, method="POST", data=b"{}", headers={"Content-Type": "application/json"}
        )
        assert status == 404
        assert "Endpoint not exposed on webhook ingress" in body
