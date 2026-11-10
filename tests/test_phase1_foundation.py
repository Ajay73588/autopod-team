"""Unit and integration tests for Step 48C Universal Multi-Application Architecture — Phase 1: Foundation Implementation.

Tests:
1. Application registration with all metadata fields (framework, build_config, env_vars, routing_path, etc.)
2. Duplicate application rejection (enforces name and repo_url uniqueness when idempotent=False)
3. Idempotent application registration (returns existing/updated app when idempotent=True)
4. Schema migration from legacy database (verifies init_db() non-destructively upgrades schema)
5. Multiple independent applications with isolated ports and routing paths
6. Deployment-to-application relationships and metadata tracking (port, container_name, image_tag)
7. Version framework metadata tracking
8. Clean application lifecycle deletion (cascades to versions/deployments)
9. Public URL resolution across multiple applications
10. Sweet Treats Bakery default backward-compatibility and preservation
11. API endpoints for application registration, patch, get, and delete with Admin Key
"""

import json
import sqlite3
import pytest
from app import app as flask_app


def test_application_registration_all_metadata_fields(database):
    """Test registering an application with all universal metadata fields."""
    build_cfg = {"command": "npm run build", "output_dir": "dist"}
    env_vars = {"NODE_ENV": "production", "API_BASE": "/crypto/api"}

    app = database.register_application(
        name="Crypto Price Analysis Dashboard",
        repo_url="https://github.com/Ajay73588/Crypto-Price-Analysis-Dashboard.git",
        target_port=8082,
        health_check_path="/health",
        framework="react",
        build_config=build_cfg,
        env_vars=env_vars,
        routing_path="/crypto/",
        health_status="healthy",
        lifecycle_status="active",
        target_node="10.0.1.107",
        container_name="autopod-crypto-price-analysis-dashboard-container",
        image_tag="crypto:v1"
    )

    assert app["id"] is not None
    assert app["name"] == "Crypto Price Analysis Dashboard"
    assert app["repo_url"] == "https://github.com/Ajay73588/Crypto-Price-Analysis-Dashboard"
    assert app["target_port"] == "8082"
    assert app["health_check_path"] == "/health"
    assert app["framework"] == "react"
    assert json.loads(app["build_config"]) == build_cfg
    assert json.loads(app["env_vars"]) == env_vars
    assert app["env_vars_json"] == app["env_vars"]
    assert app["routing_path"] == "/crypto/"
    assert app["health_status"] == "healthy"
    assert app["lifecycle_status"] == "active"
    assert app["target_node"] == "10.0.1.107"
    assert app["container_name"] == "autopod-crypto-price-analysis-dashboard-container"
    assert app["image_tag"] == "crypto:v1"
    assert app["created_at"] is not None
    assert app["updated_at"] is not None

    # Fetch by ID
    by_id = database.get_application(app["id"])
    assert by_id["name"] == "Crypto Price Analysis Dashboard"
    assert by_id["framework"] == "react"

    # Fetch by name
    by_name = database.get_application_by_name("Crypto Price Analysis Dashboard")
    assert by_name["id"] == app["id"]

    # Fetch by repo URL
    by_repo = database.find_application_by_repo_url("https://github.com/Ajay73588/Crypto-Price-Analysis-Dashboard.git")
    assert by_repo["id"] == app["id"]


def test_duplicate_application_rejection(database):
    """Enforce uniqueness of application name and repo_url when idempotent=False."""
    database.register_application(
        name="App Alpha",
        repo_url="https://github.com/acme/app-alpha.git",
        target_port=8082
    )

    # Duplicate name rejection
    with pytest.raises(ValueError, match="already registered"):
        database.register_application(
            name="App Alpha",
            repo_url="https://github.com/acme/app-beta.git"
        )

    # Duplicate repo URL rejection
    with pytest.raises(ValueError, match="already registered"):
        database.register_application(
            name="App Beta",
            repo_url="https://github.com/acme/app-alpha.git"
        )


def test_idempotent_application_registration(database):
    """When idempotent=True, duplicate name or repo updates existing app without raising error."""
    app1 = database.register_application(
        name="Service One",
        repo_url="https://github.com/acme/service-one.git",
        target_port=8082,
        framework="python"
    )

    # Re-register with idempotent=True and updated framework
    app2 = database.register_application(
        name="Service One",
        repo_url="https://github.com/acme/service-one.git",
        target_port=8082,
        framework="fastapi",
        idempotent=True
    )

    assert app2["id"] == app1["id"]
    assert app2["framework"] == "fastapi"


def test_schema_migration_from_legacy_database(tmp_path, monkeypatch):
    """Test non-destructive migration on a simulated legacy database lacking new columns."""
    legacy_db = str(tmp_path / "legacy_autopod.db")
    conn = sqlite3.connect(legacy_db)
    cur = conn.cursor()

    # Create old v1 schema (missing framework, routing_path, env_vars, port, etc.)
    cur.execute("""
    CREATE TABLE applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        repo_url TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    cur.execute("""
    CREATE TABLE versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        application_id INTEGER NOT NULL,
        image_tag TEXT NOT NULL,
        git_commit TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        is_current INTEGER NOT NULL DEFAULT 0
    )
    """)
    cur.execute("""
    CREATE TABLE deployments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        application_id INTEGER NOT NULL,
        version_id INTEGER,
        status TEXT NOT NULL DEFAULT 'in_progress',
        is_rollback INTEGER NOT NULL DEFAULT 0,
        triggered_by TEXT,
        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        finished_at TIMESTAMP,
        message TEXT
    )
    """)
    # Insert legacy application and deployment history
    cur.execute("INSERT INTO applications (name, repo_url) VALUES ('sweet-treats-bakery', 'https://github.com/acme/bakery')")
    app_id = cur.lastrowid
    cur.execute("INSERT INTO versions (application_id, image_tag, git_commit, is_current) VALUES (?, 'bakery:v1', 'abc123', 1)", (app_id,))
    v_id = cur.lastrowid
    cur.execute("INSERT INTO deployments (application_id, version_id, status, message) VALUES (?, ?, 'success', 'Deployed')", (app_id, v_id))
    conn.commit()
    conn.close()

    # Run init_db() on legacy database
    monkeypatch.setenv("AUTOPOD_DB_PATH", legacy_db)
    import database as db_mod
    import importlib
    importlib.reload(db_mod)
    db_mod.init_db()

    # Verify columns were added non-destructively
    with db_mod.get_db_cursor() as cursor:
        cursor.execute("PRAGMA table_info(applications)")
        app_cols = {row[1] for row in cursor.fetchall()}
        expected_app_cols = {
            "id", "name", "repo_url", "target_port", "health_check_path", "public_url",
            "framework", "build_config", "env_vars", "routing_path", "health_status",
            "lifecycle_status", "target_node", "container_name", "image_tag", "created_at", "updated_at"
        }
        assert expected_app_cols.issubset(app_cols)

        cursor.execute("PRAGMA table_info(deployments)")
        dep_cols = {row[1] for row in cursor.fetchall()}
        assert {"target_node", "port", "container_name", "image_tag"}.issubset(dep_cols)

        cursor.execute("PRAGMA table_info(versions)")
        ver_cols = {row[1] for row in cursor.fetchall()}
        assert "framework" in ver_cols

    # Verify legacy Sweet Treats Bakery data was preserved and properly backfilled
    bakery = db_mod.get_application(app_id)
    assert bakery["name"] == "sweet-treats-bakery"
    assert bakery["framework"] == "static"
    assert bakery["target_port"] == "8081"
    assert bakery["routing_path"] == "/"
    assert bakery["container_name"] == "autopod-sweet-treats-bakery-container"

    # Verify deployment history was 100% preserved
    deployments = db_mod.list_deployments(app_id)
    assert len(deployments) == 1
    assert deployments[0]["status"] == "success"
    assert deployments[0]["image_tag"] == "bakery:v1"

    db_mod.close_db_connection()


def test_multiple_independent_applications_isolation(database):
    """Test registering multiple independent applications with isolated ports and paths."""
    # App 1: Sweet Treats Bakery
    app1 = database.register_application(
        name="sweet-treats-bakery",
        repo_url="https://github.com/Ajay73588/sweet-treats-bakery",
        target_port=8081,
        routing_path="/"
    )
    # App 2: Crypto Price Analysis Dashboard
    app2 = database.register_application(
        name="crypto-price-analysis-dashboard",
        repo_url="https://github.com/Ajay73588/Crypto-Price-Analysis-Dashboard.git",
        target_port=8082,
        routing_path="/crypto/"
    )
    # App 3: Analytics API
    app3 = database.register_application(
        name="analytics-api",
        repo_url="https://github.com/Ajay73588/analytics-api.git",
        target_port=8083,
        routing_path="/analytics/"
    )

    assert app1["target_port"] == "8081"
    assert app1["routing_path"] == "/"

    assert app2["target_port"] == "8082"
    assert app2["routing_path"] == "/crypto/"

    assert app3["target_port"] == "8083"
    assert app3["routing_path"] == "/analytics/"

    # Next free port should be 8084
    assert database.allocate_next_target_port() == 8084


def test_deployment_to_application_relationships_and_metadata(database):
    """Test deployment creation and updates with new metadata fields."""
    app = database.register_application(
        name="web-service",
        repo_url="https://github.com/acme/web-service",
        target_port=8085
    )
    version = database.create_version(
        application_id=app["id"],
        image_tag="123456789012.dkr.ecr.us-east-1.amazonaws.com/autopod-web-service:v1",
        git_commit="commit-aaa",
        framework="node"
    )
    assert version["framework"] == "node"

    deployment = database.create_deployment(
        application_id=app["id"],
        version_id=version["id"],
        status="in_progress",
        target_node="10.0.1.107",
        port=8085,
        container_name="autopod-web-service-container",
        image_tag=version["image_tag"]
    )
    assert deployment["port"] == 8085
    assert deployment["container_name"] == "autopod-web-service-container"
    assert deployment["target_node"] == "10.0.1.107"

    updated = database.update_deployment(
        deployment_id=deployment["id"],
        status="success",
        finished=True
    )
    assert updated["status"] == "success"
    assert updated["finished_at"] is not None

    fetched = database.get_deployment(deployment["id"])
    assert fetched["status"] == "success"
    assert fetched["port"] == 8085
    assert fetched["image_tag"] == version["image_tag"]


def test_delete_application_lifecycle_teardown(database):
    """Test deleting an application cleanly cascades to remove deployments and versions."""
    app = database.register_application(
        name="ephemeral-app",
        repo_url="https://github.com/acme/ephemeral"
    )
    ver = database.create_version(app["id"], "ephemeral:v1")
    dep = database.create_deployment(app["id"], ver["id"])

    assert database.get_application(app["id"]) is not None
    assert len(database.list_versions(app["id"])) == 1
    assert len(database.list_deployments(app["id"])) == 1

    success = database.delete_application(app["id"])
    assert success is True

    assert database.get_application(app["id"]) is None
    assert len(database.list_versions(app["id"])) == 0
    assert len(database.list_deployments(app["id"])) == 0


def test_resolve_container_public_url_for_multiple_apps(database, monkeypatch):
    """Test resolving public URLs for multi-application configuration."""
    monkeypatch.setenv("AUTOPOD_ALB_URL", "http://autopod-alb-513586547.us-east-1.elb.amazonaws.com")

    # Root Bakery app
    database.register_application(
        name="sweet-treats-bakery",
        repo_url="https://github.com/Ajay73588/sweet-treats-bakery",
        target_port=8081,
        routing_path="/"
    )
    bakery_url = database.resolve_container_public_url("autopod-sweet-treats-bakery-container")
    assert bakery_url == "http://autopod-alb-513586547.us-east-1.elb.amazonaws.com/"

    # Subpath Crypto app
    database.register_application(
        name="crypto-price-analysis-dashboard",
        repo_url="https://github.com/Ajay73588/Crypto-Price-Analysis-Dashboard.git",
        target_port=8082,
        routing_path="/crypto/"
    )
    crypto_url = database.resolve_container_public_url("autopod-crypto-price-analysis-dashboard-container")
    assert crypto_url == "http://autopod-alb-513586547.us-east-1.elb.amazonaws.com/crypto/"

    # App with custom explicit public_url
    database.register_application(
        name="custom-domain-app",
        repo_url="https://github.com/acme/custom",
        public_url="https://custom.example.com/"
    )
    custom_url = database.resolve_container_public_url("autopod-custom-domain-app-container")
    assert custom_url == "https://custom.example.com/"

    # Internal services return None
    assert database.resolve_container_public_url("autopod-backend") is None
    assert database.resolve_container_public_url("autopod-frontend") is None


def test_api_application_crud_lifecycle(database, monkeypatch):
    """Test API registration, retrieve, update, and delete endpoints with admin auth."""
    import auth
    monkeypatch.setattr(auth, "_ADMIN_KEY", "secret-test-key")
    import app as app_module
    client = app_module.app.test_client()

    headers = {"X-Admin-Key": "secret-test-key", "Content-Type": "application/json"}

    # 1. Register application via POST
    payload = {
        "name": "API Service",
        "repo_url": "https://github.com/acme/api-service.git",
        "target_port": 8086,
        "framework": "fastapi",
        "routing_path": "/api-service/",
        "build_config": {"python_version": "3.11"},
        "env_vars": {"DEBUG": "false"}
    }
    res = client.post("/api/applications", json=payload, headers=headers)
    assert res.status_code == 201
    data = res.get_json()["data"]
    app_id = data["id"]
    assert data["framework"] == "fastapi"
    assert data["routing_path"] == "/api-service/"

    # 2. Get application detail via GET
    res_get = client.get(f"/api/applications/{app_id}")
    assert res_get.status_code == 200
    assert res_get.get_json()["data"]["name"] == "API Service"

    # 3. Update application via PATCH
    patch_payload = {
        "framework": "django",
        "health_status": "healthy"
    }
    res_patch = client.patch(f"/api/applications/{app_id}", json=patch_payload, headers=headers)
    assert res_patch.status_code == 200
    patched_data = res_patch.get_json()["data"]
    assert patched_data["framework"] == "django"
    assert patched_data["health_status"] == "healthy"

    # 4. Delete application via DELETE
    res_del = client.delete(f"/api/applications/{app_id}", headers=headers)
    assert res_del.status_code == 200
    assert res_del.get_json()["success"] is True

    # 5. Verify 404 after deletion
    res_after = client.get(f"/api/applications/{app_id}")
    assert res_after.status_code == 404
