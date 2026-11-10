"""AutoPod Cloud -- P1 Universal Multi-Application Deployment Engine Automated Test Suite

Covers:
- Part A: Universal Framework Detector (Static, React/Vite, CRA, Node Express, Node generic, Flask, FastAPI, Dockerfile, Unsupported)
- Part B: Smart Dockerfile Synthesizer (Preservation, Multi-stage builds, non-root, .dockerignore, validation)
- Part C: Worker Ingress Gateway Manager (Config generation, 301 trailing slash redirects, atomic apply, backup/rollback, recovery)
- Part D: Deployment Engine Integration (Tech detection, synthesis, port allocation, route assignment, health check, public URL)
- Part E: Real Application Scenarios (Sweet Treats Bakery, Crypto Dashboard, independent updates, failed update route preservation)
- Part F: Webhook Integration (Targeted dispatch, rejection of unregistered repos, signature preservation)
"""

import os
import json
import shutil
import tempfile
import pytest

import database
import deployment_engine
import tech_detector
from dockerfile_synthesizer import DockerfileSynthesizer
from ingress_manager import IngressManager
from tests.conftest import FakeDockerManager


# ===========================================================================
# PART A: Technology Detector Tests
# ===========================================================================

def test_detect_custom_dockerfile(tmp_path):
    """Detects custom Dockerfile in repository root and extracts EXPOSE port."""
    repo_dir = tmp_path / "custom-docker"
    repo_dir.mkdir()
    (repo_dir / "Dockerfile").write_text("FROM nginx:alpine\nEXPOSE 8080\nCMD ['nginx']")

    spec = tech_detector.detect_technology(str(repo_dir))
    assert spec.supported is True
    assert spec.framework == "dockerfile"
    assert spec.runtime == "docker"
    assert spec.has_custom_dockerfile is True
    assert spec.default_port == 8080


def test_detect_static_html_app(tmp_path):
    """Detects pure static HTML/CSS/JS application without backend dependencies."""
    repo_dir = tmp_path / "static-app"
    repo_dir.mkdir()
    (repo_dir / "index.html").write_text("<!DOCTYPE html><html><body><h1>Hello</h1></body></html>")
    (repo_dir / "styles.css").write_text("body { color: red; }")
    (repo_dir / "app.js").write_text("console.log('static');")

    spec = tech_detector.detect_technology(str(repo_dir))
    assert spec.supported is True
    assert spec.framework == "static"
    assert spec.runtime == "nginx"
    assert spec.has_custom_dockerfile is False
    assert spec.default_port == 80


def test_detect_react_vite(tmp_path):
    """Detects React SPA with Vite bundler."""
    repo_dir = tmp_path / "react-vite"
    repo_dir.mkdir()
    pkg = {
        "name": "vite-app",
        "version": "1.0.0",
        "dependencies": {"react": "^18.2.0"},
        "devDependencies": {"vite": "^5.0.0", "@vitejs/plugin-react": "^4.0.0"},
        "scripts": {"build": "vite build"}
    }
    (repo_dir / "package.json").write_text(json.dumps(pkg))
    (repo_dir / "index.html").write_text("<html><div id='root'></div></html>")

    spec = tech_detector.detect_technology(str(repo_dir))
    assert spec.supported is True
    assert spec.framework == "react-vite"
    assert spec.runtime == "node-nginx"
    assert spec.build_dir == "dist"
    assert spec.default_port == 80


def test_detect_react_cra(tmp_path):
    """Detects Create React App with react-scripts."""
    repo_dir = tmp_path / "react-cra"
    repo_dir.mkdir()
    pkg = {
        "name": "cra-app",
        "dependencies": {"react": "^18.0.0", "react-scripts": "5.0.1"},
        "scripts": {"build": "react-scripts build"}
    }
    (repo_dir / "package.json").write_text(json.dumps(pkg))

    spec = tech_detector.detect_technology(str(repo_dir))
    assert spec.supported is True
    assert spec.framework == "react-cra"
    assert spec.runtime == "node-nginx"
    assert spec.build_dir == "build"


def test_detect_nodejs_express(tmp_path):
    """Detects Node.js Express backend application."""
    repo_dir = tmp_path / "express-api"
    repo_dir.mkdir()
    pkg = {
        "name": "express-api",
        "main": "server.js",
        "dependencies": {"express": "^4.18.2"},
        "scripts": {"start": "node server.js"}
    }
    (repo_dir / "package.json").write_text(json.dumps(pkg))
    (repo_dir / "server.js").write_text("const express = require('express');")

    spec = tech_detector.detect_technology(str(repo_dir))
    assert spec.supported is True
    assert spec.framework == "node-express"
    assert spec.runtime == "node"
    assert spec.start_cmd in ("node server.js", "npm start")
    assert spec.default_port == 3000


def test_detect_nodejs_generic(tmp_path):
    """Detects generic Node.js service with start script."""
    repo_dir = tmp_path / "generic-node"
    repo_dir.mkdir()
    pkg = {
        "name": "worker-service",
        "scripts": {"start": "node index.js"}
    }
    (repo_dir / "package.json").write_text(json.dumps(pkg))
    (repo_dir / "index.js").write_text("console.log('running');")

    spec = tech_detector.detect_technology(str(repo_dir))
    assert spec.supported is True
    assert spec.framework == "nodejs"
    assert spec.runtime == "node"
    assert spec.start_cmd == "npm start"


def test_detect_python_flask(tmp_path):
    """Detects Python Flask application."""
    repo_dir = tmp_path / "flask-app"
    repo_dir.mkdir()
    (repo_dir / "requirements.txt").write_text("flask>=2.0.0\nrequests\n")
    (repo_dir / "app.py").write_text("from flask import Flask\napp = Flask(__name__)")

    spec = tech_detector.detect_technology(str(repo_dir))
    assert spec.supported is True
    assert spec.framework == "python-flask"
    assert spec.runtime == "python"
    assert "gunicorn" in spec.start_cmd
    assert spec.default_port == 8000


def test_detect_python_fastapi(tmp_path):
    """Detects Python FastAPI application."""
    repo_dir = tmp_path / "fastapi-app"
    repo_dir.mkdir()
    (repo_dir / "requirements.txt").write_text("fastapi>=0.100.0\npydantic\n")
    (repo_dir / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()")

    spec = tech_detector.detect_technology(str(repo_dir))
    assert spec.supported is True
    assert spec.framework == "python-fastapi"
    assert spec.runtime == "python"
    assert "uvicorn" in spec.start_cmd
    assert spec.default_port == 8000


def test_detect_unsupported_archetype(tmp_path):
    """Returns supported=False for unrecognized codebases without crashing or guessing."""
    repo_dir = tmp_path / "unknown-repo"
    repo_dir.mkdir()
    (repo_dir / "somefile.txt").write_text("random text")

    spec = tech_detector.detect_technology(str(repo_dir))
    assert spec.supported is False
    assert spec.framework == "unsupported"
    assert "Unsupported application archetype" in spec.error_message


def test_detect_config_override(tmp_path):
    """Explicit configuration override overrides automatic detection."""
    repo_dir = tmp_path / "overridden-app"
    repo_dir.mkdir()
    (repo_dir / "index.html").write_text("<h1>Overridden</h1>")

    override = {
        "target_port": 9000,
        "health_check_path": "/status",
        "build_cmd": "make build"
    }
    spec = tech_detector.detect_technology(str(repo_dir), config_override=override)
    assert spec.default_port == 9000
    assert spec.health_check_path == "/status"


# ===========================================================================
# PART B: Dockerfile Synthesizer Tests
# ===========================================================================

def test_synthesizer_preserves_existing_dockerfile(tmp_path):
    """Synthesizer leaves pre-existing Dockerfile untouched."""
    repo_dir = tmp_path / "repo-with-dockerfile"
    repo_dir.mkdir()
    orig_content = "FROM alpine\n# Original custom file\nCMD ['echo', 'custom']\n"
    (repo_dir / "Dockerfile").write_text(orig_content)

    spec = tech_detector.detect_technology(str(repo_dir))
    ok, bdir, err = DockerfileSynthesizer.synthesize(str(repo_dir), spec, target_port=8081)

    assert ok is True
    assert err is None
    # Content must NOT be modified
    assert (repo_dir / "Dockerfile").read_text() == orig_content


def test_synthesizer_generates_static_dockerfile_and_conf(tmp_path):
    """Synthesizes Nginx container and default.conf for static projects."""
    repo_dir = tmp_path / "repo-static"
    repo_dir.mkdir()
    (repo_dir / "index.html").write_text("<h1>Static Site</h1>")
    (repo_dir / "style.css").write_text("body { margin: 0; }")

    spec = tech_detector.detect_technology(str(repo_dir))
    ok, bdir, err = DockerfileSynthesizer.synthesize(str(repo_dir), spec, target_port=8082)

    assert ok is True
    assert err is None
    dockerfile_content = (repo_dir / "Dockerfile").read_text()
    assert "FROM nginx:alpine" in dockerfile_content
    assert "COPY default.conf /etc/nginx/conf.d/default.conf" in dockerfile_content
    assert "EXPOSE 80" in dockerfile_content

    # default.conf created
    assert (repo_dir / "default.conf").exists()
    assert "try_files $uri $uri/ /index.html =404;" in (repo_dir / "default.conf").read_text()

    # .dockerignore created
    assert (repo_dir / ".dockerignore").exists()
    assert ".git" in (repo_dir / ".dockerignore").read_text()


def test_synthesizer_generates_multistage_vite(tmp_path):
    """Synthesizes multi-stage builder->server container for Vite projects."""
    repo_dir = tmp_path / "repo-vite"
    repo_dir.mkdir()
    (repo_dir / "package.json").write_text('{"dependencies": {"vite": "5.0.0"}}')

    spec = tech_detector.detect_technology(str(repo_dir))
    ok, bdir, err = DockerfileSynthesizer.synthesize(str(repo_dir), spec)

    assert ok is True
    content = (repo_dir / "Dockerfile").read_text()
    assert "FROM node:20-alpine AS builder" in content
    assert "RUN npm run build" in content
    assert "FROM nginx:alpine" in content
    assert "COPY --from=builder /app/dist /usr/share/nginx/html/" in content


def test_synthesizer_generates_node_production(tmp_path):
    """Synthesizes Node.js container with non-root user."""
    repo_dir = tmp_path / "repo-node"
    repo_dir.mkdir()
    (repo_dir / "package.json").write_text('{"dependencies": {"express": "4.18.2"}, "scripts": {"start": "node server.js"}}')

    spec = tech_detector.detect_technology(str(repo_dir))
    ok, bdir, err = DockerfileSynthesizer.synthesize(str(repo_dir), spec)

    assert ok is True
    content = (repo_dir / "Dockerfile").read_text()
    assert "USER node" in content
    assert "NODE_ENV=production" in content
    assert ('CMD ["node", "server.js"]' in content or 'CMD ["npm", "start"]' in content)


def test_synthesizer_generates_flask_gunicorn(tmp_path):
    """Synthesizes Python Flask container running Gunicorn WSGI server."""
    repo_dir = tmp_path / "repo-flask"
    repo_dir.mkdir()
    (repo_dir / "requirements.txt").write_text("flask")
    (repo_dir / "app.py").write_text("from flask import Flask\napp = Flask(__name__)")

    spec = tech_detector.detect_technology(str(repo_dir))
    ok, bdir, err = DockerfileSynthesizer.synthesize(str(repo_dir), spec)

    assert ok is True
    content = (repo_dir / "Dockerfile").read_text()
    assert "pip install --no-cache-dir gunicorn" in content
    assert "gunicorn" in content
    assert "app:app" in content


# ===========================================================================
# PART C: Worker Ingress Gateway Tests
# ===========================================================================

def test_ingress_generate_config_root_and_subpaths(tmp_path):
    """Generates valid Nginx config mapping '/' to Bakery and '/crypto/' to Crypto Dashboard."""
    mgr = IngressManager(conf_dir=str(tmp_path), gateway_port=8080)
    apps = [
        {"name": "sweet-treats-bakery", "target_port": "8081", "routing_path": "/", "lifecycle_status": "active"},
        {"name": "crypto-price-analysis-dashboard", "target_port": "8082", "routing_path": "/crypto/", "lifecycle_status": "active"}
    ]

    conf = mgr.generate_config(apps)

    # 1. Gateway port 8080
    assert "listen 8080 default_server;" in conf
    # 2. Health check
    assert "location = /gateway-health" in conf
    # 3. 301 trailing slash redirect for /crypto
    assert "location = /crypto {\n        return 301 /crypto/;\n    }" in conf
    # 4. /crypto/ proxy_pass with trailing slash to strip prefix
    assert "location /crypto/ {\n        proxy_pass http://127.0.0.1:8082/;" in conf
    # 5. Root route for Bakery
    assert "location / {\n        proxy_pass http://127.0.0.1:8081;" in conf
    # 6. WebSocket upgrade headers
    assert "proxy_set_header Upgrade $http_upgrade;" in conf


def test_ingress_validate_syntax_valid_and_invalid(tmp_path):
    """Validates balanced braces and semicolon termination."""
    mgr = IngressManager(conf_dir=str(tmp_path), gateway_port=8080)

    # Valid config
    valid_conf = "server {\n    listen 8080 default_server;\n    location / {\n        proxy_pass http://127.0.0.1:8081;\n    }\n}\n"
    ok, err = mgr.validate_syntax(valid_conf)
    assert ok is True
    assert err is None

    # Unbalanced braces
    bad_conf = "server {\n    listen 8080 default_server;\n"
    ok, err = mgr.validate_syntax(bad_conf)
    assert ok is False
    assert "Unbalanced braces" in err

    # Missing semicolon
    bad_semi = "server {\n    listen 8080 default_server\n}\n"
    ok, err = mgr.validate_syntax(bad_semi)
    assert ok is False
    assert "missing terminating semicolon" in err


def test_ingress_atomic_apply_and_backup(tmp_path):
    """Applying config writes active conf file and creates backup."""
    mgr = IngressManager(conf_dir=str(tmp_path), gateway_port=8080, enable_system_reload=False)
    apps = [{"name": "app1", "target_port": "8081", "routing_path": "/"}]

    ok, msg = mgr.apply_config(apps)
    assert ok is True
    assert os.path.isfile(mgr.conf_path)

    # Apply update with second app
    apps.append({"name": "app2", "target_port": "8082", "routing_path": "/app2/"})
    ok, msg = mgr.apply_config(apps)
    assert ok is True
    assert os.path.isfile(mgr.backup_path)
    assert "/app2/" in open(mgr.conf_path).read()


def test_ingress_app_removal_preserves_other_routes(tmp_path):
    """Removing an app from the gateway keeps all other routes intact."""
    mgr = IngressManager(conf_dir=str(tmp_path), gateway_port=8080, enable_system_reload=False)
    apps = [
        {"name": "app1", "target_port": "8081", "routing_path": "/"},
        {"name": "app2", "target_port": "8082", "routing_path": "/app2/"},
        {"name": "app3", "target_port": "8083", "routing_path": "/app3/"}
    ]
    mgr.apply_config(apps)

    # Remove app2
    ok, msg = mgr.remove_route("app2", apps)
    assert ok is True
    active_conf = open(mgr.conf_path).read()
    assert "/app2" not in active_conf
    assert "http://127.0.0.1:8081" in active_conf
    assert "/app3/" in active_conf


# ===========================================================================
# PART D & E: Deployment Engine Integration & Real Application Scenarios
# ===========================================================================

def test_deployment_engine_end_to_end_static_crypto(database, fake_docker, tmp_path, monkeypatch):
    """Deploys a static frontend application (Crypto Dashboard archetype):
    Framework detection -> Dockerfile synthesis -> Port 8082 -> Path /crypto/ -> Public URL."""
    monkeypatch.setenv("AUTOPOD_ALB_URL", "http://autopod-alb-513586547.us-east-1.elb.amazonaws.com")
    monkeypatch.setenv("AUTOPOD_ALLOW_LOCAL_FALLBACK", "true")

    # Pre-register sweet treats on 8081
    database.register_application("sweet-treats-bakery", "https://github.com/Ajay73588/sweet-treats-bakery", target_port=8081, routing_path="/")

    # Mock git clone with Crypto Dashboard source code (index.html, style.css, script.js)
    def mock_clone(url, path, branch=None, commit_sha=None):
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "index.html"), "w", encoding="utf-8") as f:
            f.write("<!DOCTYPE html><html><head><title>Crypto Dashboard</title><link rel='stylesheet' href='style.css'></head><body><h1>Crypto</h1><script src='script.js'></script></body></html>")
        with open(os.path.join(path, "style.css"), "w", encoding="utf-8") as f:
            f.write("body { background: #000; color: #fff; }")
        with open(os.path.join(path, "script.js"), "w", encoding="utf-8") as f:
            f.write("console.log('crypto loaded');")
        return True

    monkeypatch.setattr(deployment_engine, "clone_repository", mock_clone)

    repo_url = "https://github.com/Ajay73588/Crypto-Price-Analysis-Dashboard.git"
    payload = {
        "repository": {
            "name": "crypto-price-analysis-dashboard",
            "clone_url": repo_url
        }
    }

    result = deployment_engine.deploy(fake_docker, payload=payload, allow_auto_register=True)

    assert result["status"] == "success"
    # Port must be 8082 (allocated after bakery's 8081)
    assert result["port"] == 8082
    assert result["routing_path"] == "/crypto-price-analysis-dashboard/"
    assert result["framework"] == "static"
    # Public URL resolved properly under ALB
    assert result["public_url"] == "http://autopod-alb-513586547.us-east-1.elb.amazonaws.com/crypto-price-analysis-dashboard/"

    # Verify DB persistence
    app = database.find_application_by_repo_url(repo_url)
    assert app is not None
    assert app["framework"] == "static"
    assert app["target_port"] == "8082"
    assert app["routing_path"] == "/crypto-price-analysis-dashboard/"
    assert app["public_url"] == "http://autopod-alb-513586547.us-east-1.elb.amazonaws.com/crypto-price-analysis-dashboard/"


def test_deployment_engine_end_to_end_dockerfile_bakery(database, fake_docker, tmp_path, monkeypatch):
    """Deploys Sweet Treats Bakery with its real existing custom Dockerfile:
    Detects custom Dockerfile -> Preserves Dockerfile -> Port 8081 -> Path / -> Root ALB URL."""
    monkeypatch.setenv("AUTOPOD_ALB_URL", "http://autopod-alb-513586547.us-east-1.elb.amazonaws.com")
    monkeypatch.setenv("AUTOPOD_ALLOW_LOCAL_FALLBACK", "true")

    orig_dockerfile = "FROM nginx:alpine\nCOPY . /usr/share/nginx/html\nEXPOSE 80\nCMD ['nginx', '-g', 'daemon off;']\n"

    def mock_clone(url, path, branch=None, commit_sha=None):
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "index.html"), "w", encoding="utf-8") as f:
            f.write("<h1>Sweet Treats Bakery</h1>")
        with open(os.path.join(path, "Dockerfile"), "w", encoding="utf-8") as f:
            f.write(orig_dockerfile)
        return True

    monkeypatch.setattr(deployment_engine, "clone_repository", mock_clone)

    repo_url = "https://github.com/Ajay73588/sweet-treats-bakery.git"
    payload = {
        "repository": {
            "name": "sweet-treats-bakery",
            "clone_url": repo_url
        }
    }

    result = deployment_engine.deploy(fake_docker, payload=payload, allow_auto_register=True)

    assert result["status"] == "success"
    assert result["port"] == 8081
    assert result["routing_path"] == "/"
    assert result["framework"] == "dockerfile"
    assert result["public_url"] == "http://autopod-alb-513586547.us-east-1.elb.amazonaws.com/"


def test_deploy_app_a_does_not_affect_app_b(database, fake_docker, monkeypatch):
    """Redeploying Sweet Treats Bakery does not restart, stop or modify Crypto Dashboard."""
    monkeypatch.setenv("AUTOPOD_ALLOW_LOCAL_FALLBACK", "true")

    # Register both applications
    bakery = database.register_application("sweet-treats-bakery", "https://github.com/test/bakery", target_port=8081, routing_path="/")
    crypto = database.register_application("crypto-dashboard", "https://github.com/test/crypto", target_port=8082, routing_path="/crypto/")

    # Mark crypto container as running
    crypto_container = "autopod-crypto-dashboard-container"
    fake_docker.running_containers.add(crypto_container)

    # Redeploy Bakery
    def mock_clone(url, path, branch=None, commit_sha=None):
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "index.html"), "w") as f:
            f.write("<h1>Bakery Updated</h1>")
        with open(os.path.join(path, "Dockerfile"), "w") as f:
            f.write("FROM nginx:alpine\nEXPOSE 80\n")
        return True

    monkeypatch.setattr(deployment_engine, "clone_repository", mock_clone)

    payload = {
        "repository": {
            "name": "sweet-treats-bakery",
            "clone_url": "https://github.com/test/bakery"
        }
    }

    res = deployment_engine.deploy(fake_docker, payload=payload)
    assert res["status"] == "success"

    # Crypto container is still running undisturbed!
    assert crypto_container in fake_docker.running_containers


def test_failed_update_preserves_previous_working_route(database, fake_docker, monkeypatch):
    """When an update fails health check, the previous version and working route remain active."""
    monkeypatch.setenv("AUTOPOD_ALLOW_LOCAL_FALLBACK", "true")
    monkeypatch.setenv("HEALTH_CHECK_RETRIES", "1")
    monkeypatch.setenv("HEALTH_CHECK_DELAY_SECONDS", "0")

    app = database.register_application(
        "crypto-dashboard",
        "https://github.com/test/crypto",
        target_port=8082,
        routing_path="/crypto/",
        public_url="http://autopod-alb-513586547.us-east-1.elb.amazonaws.com/crypto/"
    )
    v1 = database.create_version(app["id"], "autopod-crypto:v1", "commit1", framework="static")
    database.set_current_version(app["id"], v1["id"])

    # Simulate running v1 container
    fake_docker.running_containers.add("autopod-crypto-dashboard-container")

    # Simulate failing candidate health check on v2
    fake_docker.http_health_result = False

    def mock_clone(url, path, branch=None, commit_sha=None):
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "index.html"), "w") as f:
            f.write("<h1>Crypto Broken Update</h1>")
        return True

    monkeypatch.setattr(deployment_engine, "clone_repository", mock_clone)

    payload = {
        "repository": {
            "name": "crypto-dashboard",
            "clone_url": "https://github.com/test/crypto"
        }
    }

    res = deployment_engine.deploy(fake_docker, payload=payload)
    assert res["status"] == "error"

    # Old container was left running untouched
    assert "autopod-crypto-dashboard-container" in fake_docker.running_containers
    # Current version is still v1
    curr = database.get_current_version(app["id"])
    assert curr["id"] == v1["id"]
    # App public URL and route preserved
    app_db = database.get_application(app["id"])
    assert app_db["public_url"] == "http://autopod-alb-513586547.us-east-1.elb.amazonaws.com/crypto/"


# ===========================================================================
# PART F: Webhook Integration Tests
# ===========================================================================

def test_webhook_targeted_dispatch_matches_correct_app(database, fake_docker, monkeypatch):
    """A push to Repo A only triggers deployment for App A, and push to unregistered repo is rejected."""
    monkeypatch.setenv("AUTOPOD_ALLOW_LOCAL_FALLBACK", "true")

    app_bakery = database.register_application("sweet-treats-bakery", "https://github.com/test/bakery.git", target_port=8081)
    app_crypto = database.register_application("crypto-dashboard", "https://github.com/test/crypto.git", target_port=8082)

    deployed_apps = []

    def mock_clone(url, path, branch=None, commit_sha=None):
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "index.html"), "w") as f:
            f.write("<h1>Sample App</h1>")
        with open(os.path.join(path, "Dockerfile"), "w") as f:
            f.write("FROM alpine\nEXPOSE 80\n")
        deployed_apps.append(url)
        return True

    monkeypatch.setattr(deployment_engine, "clone_repository", mock_clone)

    # 1. Push to Bakery repo (allow_auto_register=False simulates authentic GitHub webhook)
    res_bakery = deployment_engine.deploy(
        fake_docker,
        payload={"repository": {"clone_url": "https://github.com/test/bakery.git", "name": "sweet-treats-bakery"}},
        allow_auto_register=False
    )
    assert res_bakery["status"] == "success"
    assert "https://github.com/test/bakery.git" in deployed_apps

    # 2. Push to unregistered repo -> REJECTED immediately before clone
    res_unknown = deployment_engine.deploy(
        fake_docker,
        payload={"repository": {"clone_url": "https://github.com/stranger/unknown-repo.git", "name": "unknown-repo"}},
        allow_auto_register=False
    )
    assert res_unknown["status"] == "rejected"
    assert "not registered" in res_unknown["message"]
    assert "https://github.com/stranger/unknown-repo.git" not in deployed_apps
