"""REAL-Docker tests for node_deployer.deploy_to_node.

These run the exact docker commands node_deployer issues against a real
Docker daemon (via a runner that executes them locally instead of over
SSH), then check real behaviour with real HTTP requests. They are skipped
automatically when no Docker daemon is reachable.

What is simulated: only SSH transport (commands run locally) and the
registry (`docker pull X` succeeds when X already exists locally, because
the sandbox/CI may have no registry access). Everything else -- container
lifecycle, port publishing, port-conflict errors, health probing -- is real.

Test images are built without any registry access: a tiny rootfs made from
the host's own Python is `docker import`ed. Images: ::v1 and ::v2 serve
HTTP on port 80; ::bad exits immediately.
"""

import importlib
import os
import shutil
import socket
import subprocess
import time
import urllib.request

import pytest

IMAGE_V1 = "autopod-rdtest:v1"
IMAGE_V2 = "autopod-rdtest:v2"
IMAGE_BAD = "autopod-rdtest:bad"
NAME = "autopod-rdtest-container"


def _can_run():
    import sys
    if sys.platform != "linux":
        return False
    if not shutil.which("ldd"):
        return False
    return _docker_available()


pytestmark = pytest.mark.skipif(not _can_run(), reason="requires Linux host with reachable Docker daemon and ldd")


def _build_rootfs(dest):
    py = os.path.realpath(shutil.which("python3"))
    os.makedirs(f"{dest}/usr/bin", exist_ok=True)
    shutil.copy(py, f"{dest}/usr/bin/python3")
    out = subprocess.run(["ldd", py], capture_output=True, text=True).stdout
    for tok in out.replace("=>", " ").split():
        if tok.startswith("/") and os.path.exists(tok):
            os.makedirs(f"{dest}{os.path.dirname(tok)}", exist_ok=True)
            shutil.copy(os.path.realpath(tok), f"{dest}{tok}")
    # The dynamic loader may appear under its own path in ldd output only.
    libdir = subprocess.run(
        [py, "-c", "import sysconfig;print(sysconfig.get_paths()['stdlib'])"], capture_output=True, text=True
    ).stdout.strip()
    shutil.copytree(libdir, f"{dest}{libdir}", symlinks=False, dirs_exist_ok=True)


def _import_image(rootfs, tag, cmd):
    tar = subprocess.Popen(["tar", "-C", rootfs, "-c", "."], stdout=subprocess.PIPE)
    subprocess.run(["docker", "import", "-c", f"CMD {cmd}", "-", tag], stdin=tar.stdout, check=True,
                   capture_output=True)
    tar.stdout.close()
    tar.wait()


@pytest.fixture(scope="module")
def images(tmp_path_factory):
    if not _can_run():
        pytest.skip("requires Linux host with reachable Docker daemon and ldd")
    rootfs = str(tmp_path_factory.mktemp("rdtest_rootfs"))
    _build_rootfs(rootfs)
    serve = '["/usr/bin/python3","-m","http.server","80"]'
    _import_image(rootfs, IMAGE_V1, serve)
    _import_image(rootfs, IMAGE_V2, serve)
    _import_image(rootfs, IMAGE_BAD, '["/usr/bin/python3","-c","raise SystemExit(1)"]')
    yield
    for tag in (IMAGE_V1, IMAGE_V2, IMAGE_BAD):
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)


class LocalDockerRunner:
    """Runs the remote command locally instead of over SSH."""

    def __init__(self):
        self.calls = []

    def run(self, host, remote_command):
        self.calls.append(remote_command)
        if remote_command.startswith("docker pull "):
            image = remote_command.split()[-1]
            ok = subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0
            return ok, "", "" if ok else "image not found locally (registry simulated)"
        r = subprocess.run(remote_command, shell=True, capture_output=True, text=True, timeout=60)
        return r.returncode == 0, r.stdout, r.stderr


@pytest.fixture()
def env(images):
    import node_deployer
    importlib.reload(node_deployer)
    node_deployer.NODE_HEALTH_CHECK_RETRIES = 6
    node_deployer.NODE_HEALTH_CHECK_DELAY_SECONDS = 0.5
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    runner = LocalDockerRunner()
    yield node_deployer.NodeDeployer(ssh_runner=runner), port, runner
    for n in (NAME, f"{NAME}-candidate"):
        subprocess.run(["docker", "rm", "-f", n], capture_output=True)


def _image_of(name):
    r = subprocess.run(["docker", "inspect", name, "--format", "{{.Config.Image}}"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def _http_ok(port):
    for _ in range(10):
        try:
            return urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2).status == 200
        except Exception:
            time.sleep(0.3)
    return False


def test_docker_refuses_two_containers_on_one_published_port(env):
    """The fact that made the old candidate-beside-live design impossible."""
    _deployer, port, _ = env
    subprocess.run(["docker", "run", "-d", "--name", NAME, "-p", f"{port}:80", IMAGE_V1], check=True, capture_output=True)
    second = subprocess.run(["docker", "run", "-d", "--name", f"{NAME}-candidate", "-p", f"{port}:80", IMAGE_V2],
                            capture_output=True, text=True)
    assert second.returncode != 0
    assert "port is already allocated" in second.stderr


def test_fresh_deploy_serves_http(env):
    deployer, port, _ = env
    result = deployer.deploy_to_node("127.0.0.1", IMAGE_V1, NAME, port, health_check_path="/")
    assert result["status"] == "success", result
    assert _image_of(NAME) == IMAGE_V1
    assert _http_ok(port)


def test_update_of_live_node_replaces_container_without_port_conflict(env):
    """D1 regression, against real Docker."""
    deployer, port, _ = env
    assert deployer.deploy_to_node("127.0.0.1", IMAGE_V1, NAME, port, health_check_path="/")["status"] == "success"
    result = deployer.deploy_to_node("127.0.0.1", IMAGE_V2, NAME, port, health_check_path="/")
    assert result["status"] == "success", result
    assert _image_of(NAME) == IMAGE_V2
    assert _http_ok(port)


def test_bad_new_version_is_rolled_back_to_previous_and_keeps_serving(env):
    deployer, port, runner = env
    assert deployer.deploy_to_node("127.0.0.1", IMAGE_V1, NAME, port, health_check_path="/")["status"] == "success"

    result = deployer.deploy_to_node("127.0.0.1", IMAGE_BAD, NAME, port, health_check_path="/")

    assert result["status"] == "error"
    assert result["restored"] is True
    assert _image_of(NAME) == IMAGE_V1
    assert _http_ok(port)
    assert not any("rename" in c for c in runner.calls)


def test_missing_image_leaves_live_container_untouched(env):
    deployer, port, _ = env
    assert deployer.deploy_to_node("127.0.0.1", IMAGE_V1, NAME, port, health_check_path="/")["status"] == "success"
    result = deployer.deploy_to_node("127.0.0.1", "autopod-rdtest:does-not-exist", NAME, port, health_check_path="/")
    assert result["status"] == "error"
    assert _image_of(NAME) == IMAGE_V1
    assert _http_ok(port)
