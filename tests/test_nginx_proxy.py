"""
tests/test_nginx_proxy.py

Regression and security tests for the Nginx reverse proxy configuration (Defect D6 resolution).
Verifies:
1. Exact path matching (= /webhook, = /health) for public endpoints.
2. Fail-safe denial when administrator CIDR is unset or empty.
3. Administrator CIDR allowlisting for dashboard (/) and API (/api/).
4. Internal VPC CIDR allowlisting for application node bootstrap.
5. Flask ProxyFix middleware handling for proxied requests.
"""

import os
import jinja2
import pytest


TEMPLATE_DIR = os.path.join(
    os.path.dirname(__file__), "..", "infra", "ansible", "roles", "autopod", "templates"
)


def _render_nginx_template(context):
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATE_DIR),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("autopod_nginx.conf.j2")
    return template.render(context)


def test_nginx_template_exact_matching_for_public_endpoints():
    """SECURITY: /webhook and /health must use exact matching (=) so alternate paths,
    path traversal (e.g. /webhook/../api/logs), or sub-paths are not exposed publicly."""
    rendered = _render_nginx_template({
        "autopod_admin_cidr": "203.0.113.10/32",
        "autopod_backend_port": 5000,
        "autopod_nginx_port": 80,
    })

    assert "location = /webhook {" in rendered
    assert "location = /health {" in rendered
    # Confirm proxy destinations
    assert "proxy_pass http://127.0.0.1:5000/webhook;" in rendered
    assert "proxy_pass http://127.0.0.1:5000/health;" in rendered


def test_nginx_template_restricts_dashboard_and_api_when_admin_cidr_is_set():
    """Admin CIDR is allowlisted for both / and /api/ with a trailing deny all."""
    admin_cidr = "198.51.100.25/32"
    rendered = _render_nginx_template({
        "autopod_admin_cidr": admin_cidr,
        "autopod_vpc_cidr": "10.0.0.0/16",
    })

    assert f"allow {admin_cidr};" in rendered
    assert "allow 10.0.0.0/16;" in rendered
    assert "deny all;" in rendered


def test_nginx_template_fails_safely_when_admin_cidr_is_unset_or_empty():
    """SECURITY: If admin CIDR is unset, empty, or whitespace, fail safely by denying
    all non-loopback clients. Must NOT generate a permissive allow directive."""
    for empty_val in ("", "   ", None):
        ctx = {"autopod_admin_cidr": empty_val, "autopod_vpc_cidr": ""}
        rendered = _render_nginx_template(ctx)

        # Confirm deny all is present in protected locations
        assert "deny all;" in rendered
        # Confirm no allow directives other than loopback
        lines = [line.strip() for line in rendered.splitlines() if line.strip().startswith("allow ")]
        for line in lines:
            assert line in ("allow 127.0.0.1;", "allow ::1;"), f"Unexpected permissive rule: {line}"


def test_nginx_template_allows_vpc_cidr_for_app_nodes_in_api():
    """App nodes in the VPC need access to /api/applications/current-versions during bootstrap."""
    rendered = _render_nginx_template({
        "autopod_admin_cidr": "198.51.100.0/24",
        "autopod_vpc_cidr": "10.0.0.0/16",
    })

    assert "allow 10.0.0.0/16;" in rendered


def test_app_wsgi_app_is_wrapped_with_proxyfix():
    """Verify AutoPod application wsgi_app is wrapped with ProxyFix middleware."""
    import app as autopod_app
    from werkzeug.middleware.proxy_fix import ProxyFix
    assert isinstance(autopod_app.app.wsgi_app, ProxyFix)


def test_flask_proxyfix_recognizes_real_ip_from_proxy():
    """Verify ProxyFix middleware correctly resolves client IP and scheme when behind Nginx with X-Forwarded-For."""
    from flask import Flask, request, jsonify
    from werkzeug.middleware.proxy_fix import ProxyFix

    test_app = Flask("test_proxyfix_verification")
    test_app.wsgi_app = ProxyFix(test_app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    @test_app.route("/ip")
    def get_ip():
        return jsonify({"remote_addr": request.remote_addr, "scheme": request.scheme})

    with test_app.test_client() as client:
        resp = client.get(
            "/ip",
            headers={
                "X-Forwarded-For": "203.0.113.195",
                "X-Forwarded-Proto": "https",
            },
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["remote_addr"] == "203.0.113.195"
        assert data["scheme"] == "https"




def test_nginx_configuration_includes_sse_and_websocket_support():
    """Verify SSE streaming for logs and WebSocket headers for frontend are configured."""
    rendered = _render_nginx_template({
        "autopod_admin_cidr": "203.0.113.10/32",
    })
    assert "proxy_buffering off;" in rendered
    assert "Upgrade $http_upgrade;" in rendered
    assert "client_max_body_size 50M;" in rendered

