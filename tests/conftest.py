"""
Shared pytest fixtures for the AutoPod Cloud backend test suite.

Run with (from the repo root):
    pip install -r requirements.txt
    pytest tests/ -v
"""

import os
import sys
import importlib
import tempfile
import pytest

# Make sure the repo root (one level up from tests/) is importable.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture(autouse=True)
def default_test_local_fallback(monkeypatch):
    """By default in fake-docker unit/integration tests, allow local container execution
    so tests do not fail on absent worker fleets. Tests verifying worker-only enforcement
    (such as test_p0_critical_reliability.py) explicitly set AUTOPOD_ALLOW_LOCAL_FALLBACK='false'.
    In production environments, AUTOPOD_ALLOW_LOCAL_FALLBACK defaults to False."""
    monkeypatch.setenv("AUTOPOD_ALLOW_LOCAL_FALLBACK", "true")


@pytest.fixture()
def temp_db_path(tmp_path):
    """A fresh SQLite file path per test."""
    return str(tmp_path / "test_autopod.db")


@pytest.fixture()
def database(temp_db_path, monkeypatch):
    """Reload database.py against an isolated, per-test SQLite file."""
    monkeypatch.setenv("AUTOPOD_DB_PATH", temp_db_path)
    import database as db_module
    importlib.reload(db_module)
    db_module.init_db()
    yield db_module
    db_module.close_db_connection()


class FakeDockerManager:
    """A stand-in for DockerManager that doesn't need a real Docker daemon.

    Lets deployment_engine tests exercise the actual pipeline logic
    (ordering, failure handling, health-check gating) without needing
    Docker installed in the test environment. Each test configures the
    `build_result` / `health_sequence` it wants to simulate.
    """

    def __init__(self, build_result=True, run_result=True, health_sequence=None, push_result=True,
                 http_health_result=True):
        self.build_result = build_result
        self.run_result = run_result
        # health_sequence: list of booleans returned on successive
        # is_container_healthy() calls for the CURRENT candidate container;
        # once exhausted, keeps returning the last value.
        self.health_sequence = health_sequence if health_sequence is not None else [True]
        self.push_result = push_result
        # http_health_result: what http_health_check() reports. Every
        # application now has a (default "/") health_check_path, so
        # deployment_engine always calls this during a deploy -- tests that
        # want to simulate "container up but app never answers" set this
        # to False instead of touching health_sequence.
        self.http_health_result = http_health_result

        self.running_containers = set()
        self.calls = []
        self.run_ports = []  # (container_name, ports) for every run_container call
        self._health_index = 0

    def build_image(self, path, image_name):
        self.calls.append(("build_image", image_name))
        return self.build_result

    def push_image(self, image_name, registry=None, username=None):
        self.calls.append(("push_image", image_name))
        return self.push_result

    def ensure_ecr_repository(self, repository_name, region=None):
        self.calls.append(("ensure_ecr_repository", repository_name))
        return True

    def login_ecr(self, registry=None, region=None):
        self.calls.append(("login_ecr", registry))
        return True

    def pull_image(self, image_name):
        self.calls.append(("pull_image", image_name))
        return True

    def container_exists(self, name):
        return name in self.running_containers

    def run_container(self, image_name, container_name, ports=None, env_vars=None):
        self.calls.append(("run_container", container_name, image_name))
        self.run_ports.append((container_name, ports))
        if self.run_result:
            self.running_containers.add(container_name)
        return self.run_result

    def is_container_healthy(self, container_name, timeout=None):
        if self._health_index < len(self.health_sequence):
            result = self.health_sequence[self._health_index]
        else:
            result = self.health_sequence[-1]
        self._health_index += 1
        return result

    def http_health_check(self, port, path="/", timeout=3):
        self.calls.append(("http_health_check", port, path))
        return self.http_health_result

    def stop_container(self, name):
        self.calls.append(("stop_container", name))
        return True

    def remove_container(self, name, force=True):
        self.calls.append(("remove_container", name))
        self.running_containers.discard(name)
        return True

    def sync_with_db(self):
        self.calls.append(("sync_with_db",))
        return True


@pytest.fixture()
def fake_docker():
    return FakeDockerManager()
