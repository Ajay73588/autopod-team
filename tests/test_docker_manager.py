"""Tests for docker_manager.py's low-level command handling -- specifically
the regression test for the historical bug where command failures were
silently swallowed and every lifecycle method reported success regardless
of what actually happened (item 7.1 in CURRENT_STATUS_AND_IMPLEMENTATION_PLAN.md).

These use `unittest.mock` to stub `subprocess.run` rather than requiring a
real Docker daemon, so they run in any environment.
"""

from unittest.mock import patch, MagicMock

from docker_manager import DockerManager


def _fake_completed_process(returncode=0, stdout="", stderr=""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


def test_run_cmd_reports_success_accurately():
    dm = DockerManager()
    with patch("subprocess.run", return_value=_fake_completed_process(returncode=0, stdout="ok")):
        success, out, err = dm._run_cmd(["docker", "start", "somecontainer"])
    assert success is True
    assert out == "ok"


def test_run_cmd_reports_failure_accurately():
    """This is the direct fix for the old bug: a nonzero exit code must be
    reported as a failure, not swallowed into an empty success."""
    dm = DockerManager()
    with patch("subprocess.run", return_value=_fake_completed_process(returncode=1, stderr="no such container")):
        success, out, err = dm._run_cmd(["docker", "stop", "does-not-exist"])
    assert success is False
    assert "no such container" in err


def test_stop_nonexistent_container_is_reported_as_failure():
    """Directly matches the example given in the upgrade brief: 'stop
    nonexistent container' must produce an actual error, not a silent
    success."""
    dm = DockerManager()
    with patch("subprocess.run", return_value=_fake_completed_process(returncode=1, stderr="Error: No such container")):
        result = dm.stop_container("ghost-container")
    assert result is False


def test_start_container_success_path():
    dm = DockerManager()
    with patch("subprocess.run", return_value=_fake_completed_process(returncode=0)):
        result = dm.start_container("real-container")
    assert result is True


def test_run_cmd_handles_missing_docker_binary():
    dm = DockerManager()
    with patch("subprocess.run", side_effect=FileNotFoundError()):
        success, out, err = dm._run_cmd(["docker", "ps"])
    assert success is False
    assert "not found" in err


def test_run_cmd_handles_timeout():
    import subprocess as sp
    dm = DockerManager()
    with patch("subprocess.run", side_effect=sp.TimeoutExpired(cmd="docker build", timeout=60)):
        success, out, err = dm._run_cmd(["docker", "build", "."])
    assert success is False
    assert "timed out" in err


def test_health_check_reports_daemon_unreachable():
    dm = DockerManager()
    with patch("subprocess.run", side_effect=FileNotFoundError()):
        health = dm.health_check()
    assert health["docker_available"] is False
    assert "error" in health


def test_health_check_reports_daemon_reachable():
    dm = DockerManager()
    with patch("subprocess.run", return_value=_fake_completed_process(returncode=0, stdout='"24.0.7"')):
        health = dm.health_check()
    assert health["docker_available"] is True


def test_get_containers_merges_worker_containers():
    dm = DockerManager()
    local_raw = '{"ID":"loc1","Names":"autopod-backend","Image":"backend:latest","Status":"Up 1 hour","Ports":""}'
    worker_container = {
        "Id": "wrk1",
        "Names": ["autopod-voltride-electric-mobility-companion-container"],
        "container_name": "autopod-voltride-electric-mobility-companion-container",
        "Image": "voltride:latest",
        "State": "running",
        "Status": "Up 2 hours",
        "Ports": [{"PublicPort": "8082", "PrivatePort": "80", "Type": "tcp"}],
        "is_worker": True,
        "target_node": "i-086803910a4c52091"
    }

    with patch.object(dm, "_run_cmd", return_value=(True, local_raw, "")), \
         patch.object(dm, "get_worker_containers", return_value=[worker_container]):
        containers = dm.get_containers()

    names = [c["Names"][0] for c in containers]
    assert "autopod-voltride-electric-mobility-companion-container" in names
    assert "autopod-backend" in names
    assert len(containers) == 2


def test_get_containers_prefers_worker_over_stale_local():
    dm = DockerManager()
    local_stale = '{"ID":"loc-stale","Names":"autopod-sweet-treats-bakery-container","Image":"bakery:old","Status":"Up 4 days","Ports":""}'
    worker_fresh = {
        "Id": "wrk-fresh",
        "Names": ["autopod-sweet-treats-bakery-container"],
        "container_name": "autopod-sweet-treats-bakery-container",
        "Image": "bakery:fresh",
        "State": "running",
        "Status": "Up 10 minutes",
        "Ports": [{"PublicPort": "8081", "PrivatePort": "80", "Type": "tcp"}],
        "is_worker": True,
        "target_node": "i-086803910a4c52091"
    }

    with patch.object(dm, "_run_cmd", return_value=(True, local_stale, "")), \
         patch.object(dm, "get_worker_containers", return_value=[worker_fresh]):
        containers = dm.get_containers()

    # Deduplication: only 1 container for sweet-treats-bakery, and it is the worker one
    matching = [c for c in containers if "autopod-sweet-treats-bakery-container" in c["Names"]]
    assert len(matching) == 1
    assert matching[0]["Id"] == "wrk-fresh"
    assert matching[0]["Image"] == "bakery:fresh"


def test_remove_protected_infrastructure_container_is_blocked():
    """Verify that core infrastructure containers cannot be removed via DockerManager."""
    dm = DockerManager()
    protected_names = [
        "autopod-frontend",
        "autopod-backend",
        "autopod-webhook-proxy",
        "/autopod-frontend",
        "/autopod-backend",
    ]
    for name in protected_names:
        result = dm.remove_container(name, force=True)
        assert result is False, f"Expected removing {name} to be blocked, but returned True"


def test_container_tags_infrastructure_correctly():
    """Verify that get_containers correctly flags infrastructure vs worker application."""
    dm = DockerManager()
    local_raw = '{"ID":"loc1","Names":"autopod-frontend","Image":"frontend:latest","Status":"Up 1 hour","Ports":""}'
    worker_container = {
        "Id": "wrk1",
        "Names": ["autopod-crypto-price-analysis-dashboard-container"],
        "container_name": "autopod-crypto-price-analysis-dashboard-container",
        "Image": "crypto:latest",
        "State": "running",
        "Status": "Up 2 hours",
        "Ports": [{"PublicPort": "8086", "PrivatePort": "80", "Type": "tcp"}],
        "is_worker": True,
        "target_node": "i-086803910a4c52091"
    }

    with patch.object(dm, "_run_cmd", return_value=(True, local_raw, "")), \
         patch.object(dm, "get_worker_containers", return_value=[worker_container]):
        containers = dm.get_containers()

    frontend_c = next(c for c in containers if "autopod-frontend" in c["Names"])
    assert frontend_c["is_protected"] is True
    assert frontend_c["container_type"] == "infrastructure"
    assert frontend_c["host_type"] == "control-plane"

    crypto_c = next(c for c in containers if "autopod-crypto-price-analysis-dashboard-container" in c["Names"])
    assert crypto_c["is_protected"] is False
    assert crypto_c["container_type"] == "application"
    assert crypto_c["host_type"] == "worker"
    assert crypto_c["is_worker"] is True
