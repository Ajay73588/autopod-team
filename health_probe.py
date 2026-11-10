"""
health_probe.py

A tiny, dependency-free HTTP health check used in two places:
  - docker_manager.DockerManager.http_health_check() -- probes a container
    on the local Docker host (always host="127.0.0.1").
  - node_deployer.NodeDeployer -- probes a candidate container on a REMOTE
    application node, over the network, as part of multi-node rolling
    deployment (ARCHITECTURE.md Section 8).

Pulled out into its own module (rather than duplicated, or imported from
docker_manager by node_deployer, which would be a confusing dependency
direction) specifically so both call sites share one implementation of
"what counts as healthy."
"""

import urllib.request
import urllib.error


def http_health_check(host, port, path="/", timeout=3):
    """GET http://<host>:<port><path> and treat any non-5xx, non-connection
    -error response as healthy.

    A connection error (app not listening yet, host unreachable) is treated
    as "not healthy yet" rather than raising, so a caller's retry loop can
    keep polling instead of crashing on the first attempt.
    """
    if not path:
        path = "/"
    if not path.startswith("/"):
        path = "/" + path
    url = f"http://{host}:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status < 500
    except urllib.error.HTTPError as e:
        # Any HTTP response (even 4xx) means something is listening and
        # answering requests -- "healthy enough" for a generic deployed app
        # that may not have a dedicated health route.
        return e.code < 500
    except Exception:
        # Connection refused/reset, timeout, DNS, etc. -- not reachable yet.
        return False
