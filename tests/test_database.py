"""Database CRUD tests for the new applications/versions/deployments tables
(Part 12 / item 6.5 of CURRENT_STATUS_AND_IMPLEMENTATION_PLAN.md)."""


def test_get_or_create_application_creates_once(database):
    app1 = database.get_or_create_application("myapp", "https://github.com/x/myapp")
    app2 = database.get_or_create_application("myapp", "https://github.com/x/myapp")
    assert app1["id"] == app2["id"]
    assert app1["name"] == "myapp"


def test_create_and_list_versions(database):
    app = database.get_or_create_application("myapp")
    v1 = database.create_version(app["id"], "myapp:abc123-20260101-000000", "abc123")
    v2 = database.create_version(app["id"], "myapp:def456-20260102-000000", "def456")

    versions = database.list_versions(app["id"])
    assert len(versions) == 2
    assert {v["id"] for v in versions} == {v1["id"], v2["id"]}


def test_set_and_get_current_version(database):
    app = database.get_or_create_application("myapp")
    v1 = database.create_version(app["id"], "myapp:v1")
    v2 = database.create_version(app["id"], "myapp:v2")

    database.set_current_version(app["id"], v1["id"])
    current = database.get_current_version(app["id"])
    assert current["id"] == v1["id"]

    # Moving "current" to v2 should un-mark v1.
    database.set_current_version(app["id"], v2["id"])
    current = database.get_current_version(app["id"])
    assert current["id"] == v2["id"]


def test_create_and_update_deployment(database):
    app = database.get_or_create_application("myapp")
    version = database.create_version(app["id"], "myapp:v1")

    deployment = database.create_deployment(
        application_id=app["id"], version_id=version["id"],
        status="in_progress", triggered_by="webhook"
    )
    assert deployment["status"] == "in_progress"

    updated = database.update_deployment(deployment["id"], status="success", finished=True)
    assert updated["status"] == "success"
    assert updated["finished_at"] is not None


def test_list_deployments_includes_version_fields(database):
    app = database.get_or_create_application("myapp")
    version = database.create_version(app["id"], "myapp:v1", "abc123")
    database.create_deployment(application_id=app["id"], version_id=version["id"], status="success")

    deployments = database.list_deployments(application_id=app["id"])
    assert len(deployments) == 1
    assert deployments[0]["image_tag"] == "myapp:v1"
    assert deployments[0]["git_commit"] == "abc123"


def test_register_application_sets_port_and_health_path(database):
    app = database.register_application(
        "payments", repo_url="https://github.com/acme/payments",
        target_port=8090, health_check_path="/healthz"
    )
    assert app["target_port"] == "8090"
    assert app["health_check_path"] == "/healthz"

    found = database.find_application_by_repo_url("https://github.com/acme/payments")
    assert found["id"] == app["id"]


def test_register_application_rejects_duplicate_name(database):
    database.register_application("payments", repo_url="https://github.com/acme/payments")
    try:
        database.register_application("payments", repo_url="https://github.com/acme/other")
        assert False, "expected ValueError for duplicate name"
    except ValueError:
        pass


def test_register_application_rejects_duplicate_repo_url(database):
    database.register_application("payments", repo_url="https://github.com/acme/payments")
    try:
        database.register_application("payments-2", repo_url="https://github.com/acme/payments")
        assert False, "expected ValueError for duplicate repo_url"
    except ValueError:
        pass


def test_find_application_by_repo_url_returns_none_when_unregistered(database):
    assert database.find_application_by_repo_url("https://github.com/nobody/nothing") is None


def test_create_deployment_records_target_node(database):
    app = database.get_or_create_application("myapp")
    deployment = database.create_deployment(application_id=app["id"], target_node="10.0.1.5")
    assert deployment["target_node"] == "10.0.1.5"

    updated = database.update_deployment(deployment["id"], target_node="10.0.1.6")
    assert updated["target_node"] == "10.0.1.6"


def test_register_app_node_is_idempotent_upsert(database):
    first = database.register_app_node("10.0.1.5")
    assert first["status"] == "in_service"

    second = database.register_app_node("10.0.1.5", status="draining")
    assert second["id"] == first["id"]
    assert second["status"] == "draining"

    nodes = database.list_app_nodes()
    assert len(nodes) == 1


def test_list_app_nodes_filters_by_status(database):
    database.register_app_node("10.0.1.5", status="in_service")
    database.register_app_node("10.0.1.6", status="draining")

    in_service = database.list_app_nodes(status="in_service")
    assert [n["ip_address"] for n in in_service] == ["10.0.1.5"]


def test_set_app_node_status(database):
    database.register_app_node("10.0.1.5")
    updated = database.set_app_node_status("10.0.1.5", "terminated")
    assert updated["status"] == "terminated"


def test_sync_with_db_does_not_touch_deployment_history(database, fake_docker):
    """Regression test for the historical bug where container sync logic
    (DELETE FROM containers/logs) could accidentally wipe deployment
    history if it were ever routed through the same tables. The new
    applications/versions/deployments tables must survive a sync."""
    app = database.get_or_create_application("myapp")
    version = database.create_version(app["id"], "myapp:v1")
    database.create_deployment(application_id=app["id"], version_id=version["id"], status="success")

    # Attach the real DockerManager's sync behavior indirectly: we only
    # need to confirm the deployments/versions/applications tables are
    # untouched by *any* DELETE FROM containers/logs, which sync_with_db
    # performs. We simulate that directly here since DockerManager itself
    # requires a real `docker` binary to enumerate containers.
    with database.get_db_cursor() as cursor:
        cursor.execute("DELETE FROM containers")
        cursor.execute("DELETE FROM logs")

    assert database.list_applications() == [app]
    assert len(database.list_versions(app["id"])) == 1
    assert len(database.list_deployments(application_id=app["id"])) == 1


def test_normalize_repo_url_formats(database):
    """Test repository URL normalization handles .git suffixes, trailing slashes,
    mixed case schemes/domains, and SSH formats."""
    norm = database.normalize_repo_url
    assert norm("https://github.com/acme/my-app.git") == "https://github.com/acme/my-app"
    assert norm("https://github.com/acme/my-app/") == "https://github.com/acme/my-app"
    assert norm("https://github.com/acme/my-app.git/") == "https://github.com/acme/my-app"
    assert norm("HTTPS://GITHUB.COM/Acme/My-App.git") == "https://github.com/Acme/My-App"
    assert norm("http://github.com/acme/my-app.git") == "http://github.com/acme/my-app"
    assert norm("git@github.com:acme/my-app.git") == "git@github.com:acme/my-app"
    assert norm("git@github.com:acme/my-app.git/") == "git@github.com:acme/my-app"
    assert norm("") is None
    assert norm(None) is None


def test_register_and_find_application_with_equivalent_repo_urls(database):
    """Registering with one format and looking up with another equivalent format must succeed.
    Duplicate registration with equivalent format must be rejected."""
    # Register without .git
    app = database.register_application(
        "payments", repo_url="https://github.com/acme/payments", target_port=8090
    )
    assert app["repo_url"] == "https://github.com/acme/payments"

    # Look up with .git (how GitHub delivers clone_url)
    found_git = database.find_application_by_repo_url("https://github.com/acme/payments.git")
    assert found_git is not None
    assert found_git["id"] == app["id"]

    # Look up with trailing slash
    found_slash = database.find_application_by_repo_url("https://github.com/acme/payments/")
    assert found_slash is not None
    assert found_slash["id"] == app["id"]

    # Look up with .git and trailing slash
    found_both = database.find_application_by_repo_url("https://github.com/acme/payments.git/")
    assert found_both is not None
    assert found_both["id"] == app["id"]

    # Look up with uppercase scheme
    found_upper = database.find_application_by_repo_url("HTTPS://GITHUB.COM/acme/payments.git")
    assert found_upper is not None
    assert found_upper["id"] == app["id"]

    # Reject duplicate registration with equivalent URL
    try:
        database.register_application("payments-v2", repo_url="https://github.com/acme/payments.git")
        assert False, "Expected duplicate repo_url rejection"
    except ValueError as e:
        assert "already registered" in str(e)


def test_register_and_update_application_public_url(database):
    app = database.register_application(
        "my-store",
        repo_url="https://github.com/acme/store",
        target_port="8080",
        public_url="http://store.example.com/"
    )
    assert app["public_url"] == "http://store.example.com/"
    fetched = database.get_application(app["id"])
    assert fetched["public_url"] == "http://store.example.com/"
    by_name = database.get_application_by_name("my-store")
    assert by_name["public_url"] == "http://store.example.com/"

    updated = database.update_application(app["id"], public_url="http://store-v2.example.com/")
    assert updated["public_url"] == "http://store-v2.example.com/"


def test_resolve_container_public_url(database, monkeypatch):
    database.register_application(
        "sweet-treats-bakery",
        repo_url="https://github.com/Ajay73588/sweet-treats-bakery",
        target_port="8081",
        public_url="http://autopod-alb-513586547.us-east-1.elb.amazonaws.com/"
    )

    # 1. Exact and normalized container names resolve to DB public_url
    assert database.resolve_container_public_url("autopod-sweet-treats-bakery-container") == "http://autopod-alb-513586547.us-east-1.elb.amazonaws.com/"
    assert database.resolve_container_public_url("/autopod-sweet-treats-bakery-container") == "http://autopod-alb-513586547.us-east-1.elb.amazonaws.com/"

    # 2. Internal platform services return None
    assert database.resolve_container_public_url("autopod-backend") is None
    assert database.resolve_container_public_url("/autopod-frontend") is None
    assert database.resolve_container_public_url("autopod-webhook-proxy") is None

    # 3. Fallback to env var if DB public_url is not set
    database.register_application("other-app", repo_url="https://github.com/acme/other", public_url=None)
    monkeypatch.setenv("APP_PUBLIC_URL_OTHER_APP", "http://other-env.example.com/")
    assert database.resolve_container_public_url("autopod-other-app-container") == "http://other-env.example.com/"


