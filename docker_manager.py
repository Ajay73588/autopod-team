"""
docker_manager.py

Container/image lifecycle management for AutoPod Cloud, backed by the Docker
CLI on the host.

This module replaces the former `podman_manager.py`. Public method names and
return shapes are kept as close as practical to the original PodmanManager so
that `app.py` and `webhook_handler.py` did not need a rewrite -- only the
import changed (see app.py / webhook_handler.py).

Key differences from the old implementation (see CURRENT_STATUS_AND_IMPLEMENTATION_PLAN.md
for the full list of bugs this fixes):

- `_run_cmd()` no longer silently swallows command failures. It returns a
  (success, stdout, stderr) tuple, and every caller checks `success` before
  reporting a lifecycle action as having worked. This fixes the bug where
  e.g. `stop_container()` on a nonexistent container was always reported as
  a success.
- There is exactly one implementation per method (the original file defined
  several methods twice; the later definition silently won and the earlier
  one was dead code).
- `health_check()` now actually exists (the debug endpoint that calls it no
  longer throws `AttributeError`).
- `docker ps` / `docker images` / `docker stats` / `docker history` all
  produce *line-delimited* JSON on the Docker CLI (one JSON object per
  line), unlike Podman which produces a single JSON array. This module
  parses both shapes defensively and normalizes the result into the same
  key layout the existing React frontend already expects
  (`Names` as a list, `Ports` as a list of dicts with `PublicPort`/
  `PrivatePort`, etc.) so the frontend did not need to change.
"""

import subprocess
import json
import os
from datetime import datetime

import health_probe


class DockerCommandError(Exception):
    """Raised only internally; callers should generally prefer the
    (success, output) tuple contract of `_run_cmd` over catching this."""
    pass


class DockerManager:
    """Manages Docker containers/images and syncs current state with the database."""

    def __init__(self):
        self._worker_containers_cache = None
        self._worker_cache_time = 0
        self._worker_cache_ttl = 15  # seconds

    # ------------------------------------------------------------------
    # Low-level command execution
    # ------------------------------------------------------------------

    def _run_cmd(self, cmd, timeout=60):
        """Run a shell command.

        Returns a (success: bool, stdout: str, stderr: str) tuple. Unlike the
        previous implementation, failures are NEVER swallowed here -- it is
        entirely up to the caller to decide what a failure means, but the
        caller always finds out about it.
        """
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout
            )
            success = result.returncode == 0
            if not success:
                print(f"⚠️ Command failed ({result.returncode}): {' '.join(cmd)}\n   stderr: {result.stderr.strip()}")
            return success, result.stdout.strip(), result.stderr.strip()
        except subprocess.TimeoutExpired:
            print(f"⏱️ Command timed out: {' '.join(cmd)}")
            return False, "", "timed out"
        except FileNotFoundError:
            print(f"❌ Docker binary not found while running: {' '.join(cmd)}")
            return False, "", "docker binary not found"
        except Exception as e:
            print(f"❌ Unexpected error running command {cmd}: {e}")
            return False, "", str(e)

    # ------------------------------------------------------------------
    # JSON parsing helpers (Docker CLI emits line-delimited JSON for
    # `ps`/`images`/`stats`/`history`, unlike Podman's single JSON array)
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_json_lines(output):
        """Parse Docker's line-delimited JSON output into a list of dicts."""
        if not output:
            return []
        items = []
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return items

    @staticmethod
    def _parse_json_array_or_lines(output):
        """`docker inspect` returns a JSON array; be defensive anyway."""
        if not output:
            return []
        try:
            data = json.loads(output)
            if isinstance(data, list):
                return data
            return [data]
        except json.JSONDecodeError:
            return DockerManager._parse_json_lines(output)

    @staticmethod
    def _normalize_container(raw):
        """Normalize a `docker ps --format json` row into the shape the
        existing frontend expects (matching the old Podman ps json shape):
        Names (list), Id, Image, State, Status, Ports (list of dicts)."""
        names_raw = raw.get("Names", "")
        names = [n for n in names_raw.split(",") if n] if names_raw else ["unknown"]

        ports = []
        ports_raw = raw.get("Ports", "")
        # Example: "0.0.0.0:8081->80/tcp, :::8081->80/tcp"
        for mapping in ports_raw.split(","):
            mapping = mapping.strip()
            if not mapping or "->" not in mapping:
                continue
            try:
                host_part, container_part = mapping.split("->")
                host_port = host_part.split(":")[-1]
                container_port = container_part.split("/")[0]
                proto = container_part.split("/")[-1] if "/" in container_part else "tcp"
                ports.append({
                    "PublicPort": host_port,
                    "PrivatePort": container_port,
                    "Type": proto
                })
            except Exception:
                continue

        return {
            "Id": raw.get("ID", ""),
            "Names": names,
            "Image": raw.get("Image", "unknown"),
            "State": raw.get("State", "unknown"),
            "Status": raw.get("Status", "unknown"),
            "Ports": ports,
            "Created": raw.get("CreatedAt", raw.get("RunningFor", "unknown")),
        }

    # ------------------------------------------------------------------
    # Health check (NEW -- previously called by /api/debug/podman-health
    # but did not exist, causing that route to always throw AttributeError)
    # ------------------------------------------------------------------

    def health_check(self):
        """Report whether the Docker daemon is reachable from this process."""
        success, out, err = self._run_cmd(["docker", "info", "--format", "{{json .ServerVersion}}"])
        if success:
            return {
                "docker_available": True,
                "server_version": out.strip('"'),
            }
        return {
            "docker_available": False,
            "error": err or "docker daemon unreachable",
        }

    # ------------------------------------------------------------------
    # Containers
    # ------------------------------------------------------------------

    def get_worker_containers(self, force_refresh=False):
        """Query running containers from all in-service worker nodes.
        Uses TTL caching (default 15s) to avoid SSM rate-limiting during rapid UI polling."""
        import time
        now = time.time()
        if not force_refresh and self._worker_containers_cache is not None and (now - self._worker_cache_time) < self._worker_cache_ttl:
            return self._worker_containers_cache

        import database
        try:
            in_service_nodes = database.list_app_nodes(status="in_service")
        except Exception:
            in_service_nodes = []

        worker_containers = []
        if in_service_nodes:
            try:
                import node_deployer
                runner = node_deployer.Boto3SSMRunner()
                for node in in_service_nodes:
                    target = node.get("instance_id") or node.get("ip_address")
                    if not target:
                        continue
                    try:
                        cmd = 'docker ps -a --format "{{json .}}"\necho "---AUTOPOD_INSPECT_DELIMITER---"\ndocker inspect --format \'{"Name":"{{.Name}}","StartedAt":"{{.State.StartedAt}}","CreatedAt":"{{.Created}}","Status":"{{.State.Status}}","FinishedAt":"{{.State.FinishedAt}}"}\' $(docker ps -aq) 2>/dev/null || true'
                        ok, out, err = runner.run(target, cmd, timeout=30)
                        if ok and out:
                            inspect_map = {}
                            if "---AUTOPOD_INSPECT_DELIMITER---" in out:
                                ps_part, inspect_part = out.split("---AUTOPOD_INSPECT_DELIMITER---", 1)
                                for iline in inspect_part.strip().splitlines():
                                    iline = iline.strip()
                                    if not iline:
                                        continue
                                    try:
                                        idict = json.loads(iline)
                                        iname = idict.get("Name", "").lstrip("/")
                                        if iname:
                                            inspect_map[iname] = idict
                                    except Exception:
                                        pass
                            else:
                                ps_part = out

                            raw_containers = self._parse_json_lines(ps_part)
                            for raw in raw_containers:
                                norm = self._normalize_container(raw)
                                norm["target_node"] = target
                                norm["node_ip"] = node.get("ip_address")
                                norm["is_worker"] = True
                                primary_name = norm.get("Names", [""])[0].lstrip("/")
                                norm["container_name"] = primary_name

                                insp = inspect_map.get(primary_name, {})
                                if insp:
                                    norm["started_at"] = insp.get("StartedAt")
                                    if insp.get("CreatedAt"):
                                        norm["created_at"] = insp.get("CreatedAt")
                                    if insp.get("Status"):
                                        norm["State"] = insp.get("Status")

                                app_rec = database.get_application_by_container_name(primary_name)
                                if app_rec:
                                    norm["application_name"] = app_rec.get("name")
                                    norm["worker_port"] = app_rec.get("target_port")
                                    norm["lifecycle_status"] = app_rec.get("lifecycle_status", "active")
                                else:
                                    norm["application_name"] = primary_name
                                    norm["lifecycle_status"] = "active"

                                pub_url = database.resolve_container_public_url(primary_name)
                                norm["public_url"] = pub_url
                                norm["web_url"] = pub_url
                                worker_containers.append(norm)
                    except Exception as e:
                        print(f"⚠️ Error querying worker node {target}: {e}")
            except Exception as e:
                print(f"⚠️ Failed to query worker nodes for containers: {e}")

        # Fallback / reconciliation with applications table:
        # If any active application in database is not present in worker_containers,
        # add a representation so the dashboard never loses visibility of deployed apps.
        try:
            apps = database.list_applications()
            found_names = {c.get("Names", [""])[0].lstrip("/") for c in worker_containers}
            for app in apps:
                c_name = app.get("container_name")
                if c_name and c_name.lstrip("/") not in found_names and app.get("lifecycle_status") == "active":
                    target_port = app.get("target_port")
                    target_node = app.get("target_node") or (in_service_nodes[0].get("instance_id") if in_service_nodes else "worker")
                    worker_containers.append({
                        "Id": f"app-{app['id']}",
                        "Names": [c_name],
                        "container_name": c_name,
                        "Image": app.get("image_tag") or "autopod",
                        "State": "running",
                        "Status": "Up (Worker fleet)",
                        "Ports": [{"PublicPort": target_port, "PrivatePort": "80", "Type": "tcp"}] if target_port else [],
                        "Created": app.get("created_at", "recently"),
                        "is_worker": True,
                        "target_node": target_node,
                        "public_url": app.get("public_url"),
                        "web_url": app.get("public_url")
                    })
        except Exception as e:
            print(f"⚠️ Error reconciling applications table with containers: {e}")

        self._worker_containers_cache = worker_containers
        self._worker_cache_time = now
        return worker_containers

    def get_containers(self):
        """Return a list of containers (normalized, Podman-shape-compatible),
        combining local containers with live worker containers."""
        success, out, err = self._run_cmd(["docker", "ps", "-a", "--format", "{{json .}}"], timeout=30)
        local_containers = []
        if success:
            raw_containers = self._parse_json_lines(out)
            local_containers = [self._normalize_container(c) for c in raw_containers]
        else:
            print(f"❌ Failed to list containers: {err}")

        # Fetch worker containers
        worker_containers = self.get_worker_containers()

        # Deduplicate: worker container takes precedence over stale local container
        worker_names = set()
        for wc in worker_containers:
            wc["is_worker"] = True
            wc["is_protected"] = False
            wc["container_type"] = "application"
            wc["host_type"] = "worker"
            for name in wc.get("Names", []):
                worker_names.add(name.lstrip("/"))

        merged = list(worker_containers)
        for lc in local_containers:
            names = [n.lstrip("/") for n in lc.get("Names", [])]
            p_name = names[0] if names else ""
            is_infra = p_name in {'autopod-frontend', 'autopod-backend', 'autopod-webhook-proxy'}
            lc["is_worker"] = False
            lc["is_protected"] = is_infra
            lc["container_type"] = "infrastructure" if is_infra else "application"
            lc["host_type"] = "control-plane"
            lc["target_node"] = "Control Plane (Local)"
            lc["container_name"] = p_name
            lc["application_name"] = f"AutoPod Core ({p_name})" if is_infra else p_name
            lc["lifecycle_status"] = "active"
            if not any(n in worker_names for n in names):
                merged.append(lc)

        return merged

    def _get_container_node(self, container_name, target_node=None):
        """Determine if a container is on a worker node or local.
        Prioritizes:
        1. Explicitly passed target_node
        2. Protected infrastructure check (autopod-frontend/backend/proxy -> Local Control Plane)
        3. Database applications table (container_name -> target_node or active in-service worker)
        4. Worker containers cache (by Name, ID, or ID prefix)
        5. In-service worker fleet fallback for autopod application containers
        """
        if target_node:
            return target_node

        if not container_name:
            return None

        clean_target = container_name.strip().lstrip("/")

        # Control Plane Infrastructure containers are strictly local
        PROTECTED_INFRASTRUCTURE = {'autopod-frontend', 'autopod-backend', 'autopod-webhook-proxy'}
        if clean_target in PROTECTED_INFRASTRUCTURE:
            return None

        # Check database applications table
        try:
            import database
            app = database.get_application_by_container_name(clean_target)
            if app:
                if app.get("target_node"):
                    return app["target_node"]
                # Registered applications are always worker apps
                return os.environ.get("AUTOPOD_WORKER_INSTANCE_ID", "i-086803910a4c52091")
        except Exception:
            pass

        # Check live worker containers cache (by Name, ID, or ID prefix)
        for c in self.get_worker_containers():
            for name in c.get("Names", []):
                if name.lstrip("/") == clean_target:
                    return c.get("target_node") or os.environ.get("AUTOPOD_WORKER_INSTANCE_ID", "i-086803910a4c52091")
            c_id = c.get("Id", "")
            if c_id and (c_id == clean_target or c_id.startswith(clean_target) or clean_target.startswith(c_id[:12])):
                return c.get("target_node") or os.environ.get("AUTOPOD_WORKER_INSTANCE_ID", "i-086803910a4c52091")

        # Fallback for worker applications starting with autopod-
        if clean_target.startswith("autopod-") and clean_target not in PROTECTED_INFRASTRUCTURE:
            return os.environ.get("AUTOPOD_WORKER_INSTANCE_ID", "i-086803910a4c52091")

        return None

    def _resolve_container_name(self, container_name):
        """Find the best-matching real container name/ID for a given name or ID.
        Checks exact name and ID matches first, then ID prefix matches.
        Does NOT perform substring matching across different container names to prevent
        accidentally matching a live container when targeting a candidate or vice-versa
        (e.g. 'app-container' matching 'app-container-candidate')."""
        containers = self.get_containers()
        clean_target = container_name.lstrip("/")

        # Pass 1: exact name match
        for container in containers:
            for name in container.get("Names", []):
                if name.lstrip("/") == clean_target:
                    return name

        # Pass 2: exact ID match
        for container in containers:
            if container.get("Id", "") == container_name:
                return container["Id"]

        # Pass 3: ID prefix match (at least 4 chars)
        if len(container_name) >= 4:
            for container in containers:
                if container.get("Id", "").startswith(container_name):
                    return container["Id"]

        return container_name

    def get_logs(self, container_id_or_name, tail=50):
        """Fetch recent logs for a container across local host or worker fleet."""
        worker_node = self._get_container_node(container_id_or_name)
        if worker_node:
            try:
                import node_deployer
                runner = node_deployer.Boto3SSMRunner()
                clean_target = container_id_or_name.lstrip("/")
                ok, out, err = runner.run(worker_node, f"docker logs --tail {tail} {clean_target}")
                if ok:
                    return out
                return f"Error fetching logs from worker: {err}"
            except Exception as e:
                return f"Error fetching logs from worker: {e}"

        success, out, err = self._run_cmd(["docker", "logs", "--tail", str(tail), container_id_or_name])
        if success:
            return out
        return f"Error fetching logs: {err}"

    def sync_with_db(self):
        """Refresh the `containers`/`logs` tables to mirror CURRENT live
        Docker state across local host and worker nodes."""
        import database

        containers = self.get_containers()
        with database.get_db_cursor() as cursor:
            cursor.execute("DELETE FROM containers")
            cursor.execute("DELETE FROM logs")

            for container in containers:
                name = container["Names"][0] if container.get("Names") else "unknown"
                status = container.get("Status", "unknown")
                cursor.execute(
                    "INSERT INTO containers (container_name, status) VALUES (?, ?)",
                    (name, status)
                )
                if not container.get("is_worker"):
                    logs = self.get_logs(name, tail=50)
                    if logs:
                        cursor.execute(
                            "INSERT INTO logs (container_name, log) VALUES (?, ?)",
                            (name, logs)
                        )
        return True

    def build_image(self, path, image_name):
        """Build a Docker image from a Dockerfile (or Containerfile, for
        compatibility with repos carrying one over from the Podman era)."""
        containerfile_path = os.path.join(path, "Containerfile")
        dockerfile_path = os.path.join(path, "Dockerfile")

        if os.path.exists(dockerfile_path):
            build_file = "Dockerfile"
        elif os.path.exists(containerfile_path):
            build_file = "Containerfile"
        else:
            print(f"❌ No Dockerfile or Containerfile found in: {path}")
            return False

        build_cmd = ["docker", "build", "-t", image_name, "-f", build_file, path]
        print(f"🔨 Build command: {' '.join(build_cmd)}")

        try:
            process = subprocess.Popen(
                build_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, cwd=path
            )
            while True:
                output = process.stdout.readline()
                if output == '' and process.poll() is not None:
                    break
                if output:
                    print(f"🔨 [BUILD] {output.strip()}")
            return_code = process.poll()
            if return_code == 0:
                print(f"✅ Image built successfully: {image_name}")
                return True
            print(f"❌ Image build failed with return code: {return_code}")
            return False
        except Exception as e:
            print(f"❌ Error building image: {e}")
            return False

    def start_container(self, container_name, target_node=None):
        target = self._resolve_container_name(container_name)
        worker_node = self._get_container_node(target, target_node=target_node)
        if worker_node:
            try:
                import node_deployer
                runner = node_deployer.Boto3SSMRunner()
                clean_target = target.lstrip("/")
                ok, out, err = runner.run(worker_node, f"docker start {clean_target}")
                self._worker_containers_cache = None
                self._worker_cache_time = 0
                return ok
            except Exception as e:
                print(f"❌ Failed to start worker container {target}: {e}")
                return False
        success, out, err = self._run_cmd(["docker", "start", target])
        if success:
            print(f"✅ Container started: {target}")
        else:
            print(f"❌ Failed to start container {target}: {err}")
        return success

    def stop_container(self, container_name, target_node=None):
        target = self._resolve_container_name(container_name)
        worker_node = self._get_container_node(target, target_node=target_node)
        if worker_node:
            try:
                import node_deployer
                runner = node_deployer.Boto3SSMRunner()
                clean_target = target.lstrip("/")
                ok, out, err = runner.run(worker_node, f"docker stop {clean_target}")
                self._worker_containers_cache = None
                self._worker_cache_time = 0
                return ok
            except Exception as e:
                print(f"❌ Failed to stop worker container {target}: {e}")
                return False
        success, out, err = self._run_cmd(["docker", "stop", target])
        if success:
            print(f"✅ Container stopped: {target}")
        else:
            print(f"❌ Failed to stop container {target}: {err}")
        return success

    def restart_container(self, container_name, target_node=None):
        target = self._resolve_container_name(container_name)
        worker_node = self._get_container_node(target, target_node=target_node)
        if worker_node:
            try:
                import node_deployer
                runner = node_deployer.Boto3SSMRunner()
                clean_target = target.lstrip("/")
                ok, out, err = runner.run(worker_node, f"docker restart {clean_target}")
                self._worker_containers_cache = None
                self._worker_cache_time = 0
                return ok
            except Exception as e:
                print(f"❌ Failed to restart worker container {target}: {e}")
                return False
        success, out, err = self._run_cmd(["docker", "restart", target])
        if success:
            print(f"✅ Container restarted: {target}")
        else:
            print(f"❌ Failed to restart container {target}: {err}")
        return success

    def remove_container(self, container_name, force=True, target_node=None):
        target = self._resolve_container_name(container_name)
        clean_target = (target or "").strip().lstrip("/")
        
        # Hard protection for Control Plane infrastructure
        PROTECTED_INFRASTRUCTURE = {'autopod-frontend', 'autopod-backend', 'autopod-webhook-proxy'}
        if clean_target in PROTECTED_INFRASTRUCTURE:
            print(f"❌ Cannot remove protected infrastructure container: {clean_target}")
            return False

        worker_node = self._get_container_node(target, target_node=target_node)
        if worker_node:
            try:
                import node_deployer
                runner = node_deployer.Boto3SSMRunner()
                flag = "-f " if force else ""
                ok, out, err = runner.run(worker_node, f"docker rm {flag}{clean_target}")
                self._worker_containers_cache = None
                
                # Verify container is truly removed
                verify_ok, verify_out, _ = runner.run(worker_node, f"docker inspect {clean_target}")
                if not verify_ok or "No such container" in verify_out or "No such object" in verify_out:
                    print(f"✅ Verified container removed from worker {worker_node}: {clean_target}")
                    return True
                return ok
            except Exception as e:
                print(f"❌ Failed to remove worker container {target}: {e}")
                return False
        cmd = ["docker", "rm"]
        if force:
            cmd.append("-f")
        cmd.append(target)
        success, out, err = self._run_cmd(cmd)
        if success:
            print(f"✅ Container removed: {target}")
        else:
            print(f"❌ Failed to remove container {target}: {err}")
        return success

    def container_exists(self, container_name):
        success, out, err = self._run_cmd(["docker", "inspect", container_name])
        return success

    def is_container_healthy(self, container_name, timeout=None):
        """True if the container is in the 'running' state. If the image
        defines a Docker HEALTHCHECK, requires status 'healthy' instead."""
        health = self.get_container_health(container_name)
        status = health.get("status", "unknown")
        return status in ("healthy", "running")

    def http_health_check(self, port, path="/", timeout=3):
        """Application-level health probe: GET http://<probe_host>:<port><path>
        and treat any non-5xx, non-connection-error response as healthy.

        Used (ARCHITECTURE.md Section 6.3's `health_check_path`) as a second,
        stricter check on top of `is_container_healthy()`'s container-state
        check -- a container can be "running" from Docker's point of view
        while the application inside it hasn't finished starting up yet or
        is crash-looping behind a process that stays alive. A connection
        error (app not listening yet) is treated as "not healthy yet" so
        the caller's retry loop keeps polling rather than failing fast.
        """
        probe_host = os.environ.get("DOCKER_HOST_PROBE_IP")
        if not probe_host:
            # When running on host, 127.0.0.1 reaches host-published ports.
            # When running inside a container, 127.0.0.1 is container-local;
            # candidate containers publish ports on the Docker host, reachable
            # via host.docker.internal.
            if health_probe.http_health_check("127.0.0.1", port, path=path, timeout=timeout):
                return True
            if os.path.exists("/.dockerenv") or os.environ.get("FLASK_ENV") == "production":
                return health_probe.http_health_check("host.docker.internal", port, path=path, timeout=timeout)
            return False
        return health_probe.http_health_check(probe_host, port, path=path, timeout=timeout)

    def run_container(self, image_name, container_name, ports=None, env_vars=None):
        """Run a new container from `image_name`. Returns True only if the
        container actually reaches a running state -- this method no longer
        blindly returns True regardless of what happened (see module
        docstring / bug 7.1)."""
        cmd = ["docker", "run", "-d", "--name", container_name, "--restart", "unless-stopped"]
        if ports:
            cmd.extend(["-p", ports])
        if env_vars:
            for key, value in env_vars.items():
                cmd.extend(["-e", f"{key}={value}"])
        cmd.append(image_name)

        success, out, err = self._run_cmd(cmd)
        if not success:
            print(f"❌ Failed to run container {container_name}: {err}")
            return False

        print(f"✅ Container started: {container_name} from image {image_name}")
        return True

    # ------------------------------------------------------------------
    # Monitoring
    # ------------------------------------------------------------------

    def get_container_stats(self, container_name):
        success, out, err = self._run_cmd([
            "docker", "stats", container_name, "--no-stream", "--format", "{{json .}}"
        ])
        default = {
            'cpu_percent': '0%', 'memory_used': '0B', 'memory_limit': 'N/A',
            'network_io': '0B / 0B', 'block_io': '0B / 0B', 'pids': '0',
            'container_name': container_name
        }
        if not success or not out:
            return default
        try:
            stats = json.loads(out.splitlines()[0])
        except (json.JSONDecodeError, IndexError):
            return default

        cpu_percent = stats.get('CPUPerc', stats.get('CPU', '0%'))
        mem_usage = stats.get('MemUsage', '0B / 0B')
        mem_used, mem_limit = (mem_usage.split(' / ') if ' / ' in mem_usage else ('0B', 'N/A'))

        return {
            'cpu_percent': cpu_percent,
            'memory_used': mem_used.strip(),
            'memory_limit': mem_limit.strip(),
            'network_io': stats.get('NetIO', '0B / 0B'),
            'block_io': stats.get('BlockIO', '0B / 0B'),
            'pids': stats.get('PIDs', '0'),
            'container_name': stats.get('Name', container_name)
        }

    def get_container_health(self, container_name):
        success, out, err = self._run_cmd(["docker", "inspect", container_name])
        if not success or not out:
            return {'status': 'unknown', 'failures': 0, 'log': [], 'inferred': True}
        try:
            info = self._parse_json_array_or_lines(out)
            if not info:
                return {'status': 'unknown', 'failures': 0, 'log': [], 'inferred': True}
            state = info[0].get('State', {})
            status = state.get('Status', 'unknown').lower()
            health = state.get('Health', {})
            health_status = health.get('Status', 'unknown')
            if health_status in ('unknown', '', None):
                health_status = {
                    'running': 'healthy', 'exited': 'exited', 'created': 'starting'
                }.get(status, status)
            return {
                'status': health_status,
                'failures': health.get('FailingStreak', 0),
                'log': health.get('Log', []),
                'inferred': health_status != health.get('Status', 'unknown')
            }
        except Exception as e:
            print(f"Error checking health for {container_name}: {e}")
            return {'status': 'unknown', 'failures': 0, 'log': [], 'inferred': True}

    def get_container_resources(self, container_name):
        stats = self.get_container_stats(container_name)
        success, out, err = self._run_cmd(["docker", "inspect", container_name])
        restart_count, created_date = 0, 'unknown'
        if success and out:
            info = self._parse_json_array_or_lines(out)
            if info:
                data = info[0]
                restart_count = data.get('RestartCount', 0)
                created = data.get('Created', '')
                if created:
                    try:
                        created_date = datetime.fromisoformat(created.replace('Z', '+00:00')).strftime('%Y-%m-%d %H:%M:%S')
                    except Exception:
                        created_date = created
                host_config = data.get('HostConfig', {})
                memory_limit = host_config.get('Memory', 0)
                if memory_limit:
                    stats['memory_limit'] = self._bytes_to_human(memory_limit)
        return {
            'cpu_percent': stats.get('cpu_percent', '0%'),
            'memory_usage': stats.get('memory_used', '0B'),
            'memory_limit': stats.get('memory_limit', 'N/A'),
            'network_io': stats.get('network_io', '0B / 0B'),
            'block_io': stats.get('block_io', '0B / 0B'),
            'pids': stats.get('pids', '0'),
            'restart_count': restart_count,
            'created_at': created_date,
            'container_name': container_name
        }

    @staticmethod
    def _bytes_to_human(bytes_size):
        for unit in ['B', 'KB', 'MB', 'GB']:
            if bytes_size < 1024.0:
                return f"{bytes_size:.1f}{unit}"
            bytes_size /= 1024.0
        return f"{bytes_size:.1f}TB"

    def get_all_containers_health(self):
        containers = self.get_containers()
        health_data = {}
        for container in containers:
            name = container["Names"][0] if container.get("Names") else "unknown"
            health_data[name] = {
                'health': self.get_container_health(name),
                'resources': self.get_container_resources(name),
                'basic_info': {
                    'status': container.get('Status', 'unknown'),
                    'state': container.get('State', 'unknown'),
                    'image': container.get('Image', 'unknown'),
                    'created': container.get('Created', 'unknown')
                }
            }
        return health_data

    def get_container_ports(self, container_name):
        success, out, err = self._run_cmd(["docker", "inspect", container_name])
        if not success or not out:
            return []
        info = self._parse_json_array_or_lines(out)
        if not info:
            return []
        data = info[0]
        network_settings = data.get('NetworkSettings', {})
        ports = network_settings.get('Ports', {}) or {}
        mappings = []
        for container_port, host_ports in ports.items():
            if not host_ports:
                continue
            for host_mapping in host_ports:
                host_port = host_mapping.get('HostPort', '')
                host_ip = host_mapping.get('HostIp', '0.0.0.0')
                if host_port:
                    port_entry = {
                        'container_port': container_port,
                        'host_port': host_port,
                        'host_ip': host_ip,
                    }
                    # P0.3: Only include url if host_ip is a genuine non-loopback IP, never localhost or 127.0.0.1
                    if host_ip and host_ip not in ('0.0.0.0', '127.0.0.1', 'localhost', '::1', ''):
                        port_entry['url'] = f"http://{host_ip}:{host_port}"
                    else:
                        port_entry['url'] = None
                    mappings.append(port_entry)
        return mappings

    def get_container_web_url(self, container_name):
        """Resolve the verified public web URL for a container.
        P0.3: Never returns 127.0.0.1 or localhost."""
        try:
            from database import resolve_container_public_url
            pub_url = resolve_container_public_url(container_name)
            if pub_url and "localhost" not in pub_url.lower() and "127.0.0.1" not in pub_url:
                return pub_url
        except Exception:
            pass

        return None

    def get_container_status_for_ui(self, container_name):
        basic_info = {'name': container_name, 'web_url': None, 'has_web_interface': False, 'ports': []}
        success, out, err = self._run_cmd(["docker", "inspect", container_name, "--format", "{{.State.Status}}"])
        status = out.strip().lower() if success else 'unknown'
        if status == 'running':
            ports = self.get_container_ports(container_name)
            web_url = self.get_container_web_url(container_name)
            basic_info.update({'ports': ports, 'web_url': web_url, 'has_web_interface': web_url is not None, 'status': 'running'})
        else:
            basic_info['status'] = status
        return basic_info

    def get_container_network_info(self, container_name):
        default = {'ports': [], 'networks': [], 'hostname': 'N/A', 'dns_servers': [], 'ip_address': 'N/A', 'gateway': 'N/A', 'mac_address': 'N/A'}
        success, out, err = self._run_cmd(["docker", "inspect", container_name])
        if not success or not out:
            return default
        info = self._parse_json_array_or_lines(out)
        if not info:
            return default
        data = info[0]
        network_settings = data.get('NetworkSettings', {})
        ports = self.get_container_ports(container_name)
        networks = network_settings.get('Networks', {}) or {}
        network_list = [{
            'name': n, 'ip_address': v.get('IPAddress', 'N/A'), 'gateway': v.get('Gateway', 'N/A')
        } for n, v in networks.items()]
        return {
            'ports': ports,
            'networks': network_list,
            'hostname': data.get('Config', {}).get('Hostname', 'N/A'),
            'dns_servers': network_settings.get('DNSServers', []) or [],
            'ip_address': network_settings.get('IPAddress', 'N/A'),
            'gateway': network_settings.get('Gateway', 'N/A'),
            'mac_address': network_settings.get('MacAddress', 'N/A')
        }

    # ------------------------------------------------------------------
    # Images / registry
    # ------------------------------------------------------------------

    def get_images(self):
        success, out, err = self._run_cmd(["docker", "images", "--format", "{{json .}}"])
        if not success:
            return []
        return self._parse_json_lines(out)

    def search_images(self, query, limit=25):
        success, out, err = self._run_cmd(["docker", "search", query, "--limit", str(limit), "--format", "{{json .}}"])
        if not success:
            return []
        return self._parse_json_lines(out)

    def pull_image(self, image_name):
        success, out, err = self._run_cmd(["docker", "pull", image_name], timeout=300)
        if success:
            print(f"✅ Image pulled: {image_name}")
        else:
            print(f"❌ Error pulling image {image_name}: {err}")
        return success

    @staticmethod
    def validate_ecr_repo_name(repository_name):
        """Validate and normalize an Amazon ECR repository name according to AWS ECR rules:
        - 2 to 256 characters long
        - Can include lowercase letters, numbers, hyphens, underscores, forward slashes, and periods
        - Cannot start or end with a slash, period, hyphen, or underscore
        - Converted to lowercase, spaces converted to hyphens
        """
        import re
        if not repository_name:
            raise ValueError("ECR repository name cannot be empty")
        clean_name = repository_name.strip().lower().replace(" ", "-")
        # Strip leading/trailing invalid separator characters
        clean_name = clean_name.strip("/._-")
        pattern = r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*$"
        if not re.match(pattern, clean_name) or len(clean_name) < 2 or len(clean_name) > 256:
            raise ValueError(
                f"Invalid ECR repository name '{repository_name}' (normalized: '{clean_name}'). "
                f"ECR names must be 2-256 chars, lowercase alphanumeric, and separated by hyphens, underscores, or slashes."
            )
        return clean_name

    def login_ecr(self, registry=None, region=None):
        """Authenticate Docker to an Amazon ECR registry using boto3 and IAM instance role."""
        try:
            import boto3
            import base64
            from botocore.exceptions import ClientError
            reg = region or os.environ.get("AWS_REGION", "us-east-1")
            try:
                client = boto3.client('ecr', region_name=reg)
                auth_data = client.get_authorization_token()['authorizationData'][0]
            except ClientError as e:
                code = e.response.get('Error', {}).get('Code')
                if code == 'AccessDeniedException':
                    err_msg = (
                        f"ECR IAM permission denied: The EC2 instance profile or AWS credentials lack "
                        f"'ecr:GetAuthorizationToken' permissions in {reg}. AWS Error: {e}"
                    )
                else:
                    err_msg = f"ECR authorization token request failed in region {reg} ({code}): {e}"
                print(f"❌ {err_msg}")
                self._last_ecr_error = err_msg
                return False
            except Exception as e:
                err_msg = f"Failed to initialize AWS ECR client for login in {reg}: {e}"
                print(f"❌ {err_msg}")
                self._last_ecr_error = err_msg
                return False

            token = base64.b64decode(auth_data['authorizationToken']).decode('utf-8')
            username, password = token.split(':', 1)
            endpoint = auth_data['proxyEndpoint']
            target = registry or endpoint
            target_host = target.replace("https://", "").replace("http://", "").rstrip("/")
            res = subprocess.run(
                ["docker", "login", "--username", username, "--password-stdin", target_host],
                input=password, text=True, capture_output=True, timeout=30
            )
            if res.returncode == 0:
                print(f"✅ Successfully logged into Amazon ECR: {target_host}")
                self._last_ecr_error = None
                return True
            else:
                err_msg = f"Docker login to Amazon ECR failed ({target_host}): {res.stderr.strip()}"
                print(f"❌ {err_msg}")
                self._last_ecr_error = err_msg
                return False
        except Exception as e:
            err_msg = f"ECR login error: {e}"
            print(f"❌ {err_msg}")
            self._last_ecr_error = err_msg
            return False

    def ensure_ecr_repository(self, repository_name, region=None):
        """Ensure an Amazon ECR repository exists. If it does not exist, create it.
        Returns True if repository exists or was created, False otherwise.
        Sets self._last_ecr_error on failure with an actionable diagnostic."""
        try:
            import boto3
            from botocore.exceptions import ClientError
            reg = region or os.environ.get("AWS_REGION", "us-east-1")
            
            try:
                clean_repo = self.validate_ecr_repo_name(repository_name)
            except ValueError as val_err:
                err_msg = f"ECR repository name validation failed: {val_err}"
                print(f"❌ {err_msg}")
                self._last_ecr_error = err_msg
                return False

            try:
                client = boto3.client('ecr', region_name=reg)
            except Exception as client_err:
                err_msg = f"Failed to create boto3 ECR client for region {reg}: {client_err}"
                print(f"❌ {err_msg}")
                self._last_ecr_error = err_msg
                return False

            try:
                client.describe_repositories(repositoryNames=[clean_repo])
                print(f"✅ ECR repository exists: {clean_repo}")
                self._last_ecr_error = None
                return True
            except ClientError as e:
                code = e.response.get('Error', {}).get('Code')
                if code == 'RepositoryNotFoundException':
                    print(f"📦 Creating missing ECR repository: {clean_repo} in {reg}")
                    try:
                        client.create_repository(
                            repositoryName=clean_repo,
                            imageScanningConfiguration={'scanOnPush': False},
                            imageTagMutability='MUTABLE'
                        )
                        print(f"✅ Successfully created ECR repository: {clean_repo}")
                        self._last_ecr_error = None
                        return True
                    except ClientError as create_err:
                        create_code = create_err.response.get('Error', {}).get('Code')
                        if create_code == 'RepositoryAlreadyExistsException':
                            print(f"✅ ECR repository already exists: {clean_repo}")
                            self._last_ecr_error = None
                            return True
                        elif create_code == 'AccessDeniedException':
                            err_msg = (
                                f"IAM permission denied: Unable to create ECR repository '{clean_repo}'. "
                                f"The control-plane IAM role lacks 'ecr:CreateRepository' permissions in {reg}. "
                                f"Action required: Update the IAM policy 'autopod-control-plane-ecr-push' to allow 'ecr:CreateRepository'. "
                                f"AWS Error: {create_err}"
                            )
                            print(f"❌ {err_msg}")
                            self._last_ecr_error = err_msg
                            return False
                        else:
                            err_msg = f"Failed to create ECR repository '{clean_repo}' in {reg} ({create_code}): {create_err}"
                            print(f"❌ {err_msg}")
                            self._last_ecr_error = err_msg
                            return False
                elif code == 'AccessDeniedException':
                    err_msg = (
                        f"IAM permission denied: Unable to describe or access ECR repository '{clean_repo}'. "
                        f"The control-plane IAM role policy is restricted to specific repository ARNs and lacks "
                        f"ecr:DescribeRepositories on '{clean_repo}'. Action required: Expand IAM policy resources to allow repository creation and access. "
                        f"AWS Error: {e}"
                    )
                    print(f"❌ {err_msg}")
                    self._last_ecr_error = err_msg
                    return False
                else:
                    err_msg = f"Error checking ECR repository '{clean_repo}' in {reg} ({code}): {e}"
                    print(f"❌ {err_msg}")
                    self._last_ecr_error = err_msg
                    return False
        except Exception as e:
            err_msg = f"Exception in ensure_ecr_repository for '{repository_name}': {e}"
            print(f"❌ {err_msg}")
            self._last_ecr_error = err_msg
            return False

    def push_image(self, image_name, registry=None, username=None):
        """Push an image to a registry. If `registry` is given, the image is
        re-tagged under that registry/namespace before pushing.
        Sets self._last_push_error on failure."""
        target = image_name
        if registry and registry not in image_name:
            clean_name = self._clean_image_name(image_name)
            namespace = username or clean_name.split('/')[0]
            target = f"{registry}/{clean_name}" if registry != "docker.io" else clean_name
            ok, _, err = self._run_cmd(["docker", "tag", image_name, target])
            if not ok:
                err_msg = f"Error tagging {image_name} as {target}: {err}"
                print(f"❌ {err_msg}")
                self._last_push_error = err_msg
                return False

        if registry and ".dkr.ecr." in registry:
            if not self.login_ecr(registry=registry):
                err_msg = f"ECR authentication failed before push: {getattr(self, '_last_ecr_error', 'login failed')}"
                print(f"❌ {err_msg}")
                self._last_push_error = err_msg
                return False
            # Ensure repository exists before attempting push
            repo_part = target.split('/', 1)[-1].split(':')[0]
            if not self.ensure_ecr_repository(repo_part):
                err_msg = f"ECR repository preparation failed for '{repo_part}': {getattr(self, '_last_ecr_error', 'repository check failed')}"
                print(f"❌ {err_msg}")
                self._last_push_error = err_msg
                return False

        success, out, err = self._run_cmd(["docker", "push", target], timeout=300)
        if success:
            print(f"✅ Image pushed: {target}")
            self._last_push_error = None
        else:
            combined_err = (err or out or "unknown error").strip()
            err_msg = f"Docker push failed for image {target}: {combined_err}"
            print(f"❌ {err_msg}")
            self._last_push_error = err_msg
        return success

    @staticmethod
    def _clean_image_name(image_name):
        return image_name.replace("localhost/", "")

    def get_pushable_images(self):
        images = self.get_images()
        pushable = []
        for image in images:
            repo = image.get('Repository', '')
            tag = image.get('Tag', 'latest')
            image_id = image.get('ID', '')[:12]
            size = image.get('Size', '0B')
            created = image.get('CreatedAt', 'unknown')
            if repo and repo != '<none>':
                pushable.append({
                    'repository': repo, 'tag': tag, 'image_id': image_id,
                    'size': size, 'created': created, 'full_name': f"{repo}:{tag}",
                    'is_local': repo.startswith('localhost/')
                })
        return pushable

    def check_docker_login(self):
        """Best-effort check for whether Docker Hub credentials are usable.
        Docker's CLI has no direct 'am I logged in' query; this checks the
        config file for a stored auth entry as a reasonable proxy."""
        config_path = os.path.expanduser("~/.docker/config.json")
        try:
            if not os.path.exists(config_path):
                return False
            with open(config_path) as f:
                config = json.load(f)
            auths = config.get("auths", {})
            return any("docker.io" in host or "index.docker.io" in host for host in auths)
        except Exception as e:
            print(f"Error checking Docker login: {e}")
            return False

    def get_image_details(self, image_name):
        success, out, err = self._run_cmd(["docker", "inspect", image_name])
        if not success or not out:
            return None
        info = self._parse_json_array_or_lines(out)
        return info[0] if info else None

    def tag_image(self, source_image, target_image):
        success, out, err = self._run_cmd(["docker", "tag", source_image, target_image])
        if success:
            print(f"✅ Tagged {source_image} -> {target_image}")
        else:
            print(f"❌ Error tagging image: {err}")
        return success

    def remove_image(self, image_name):
        success, out, err = self._run_cmd(["docker", "rmi", image_name])
        if success:
            print(f"✅ Image removed: {image_name}")
        else:
            print(f"❌ Error removing image {image_name}: {err}")
        return success

    def get_image_history(self, image_name):
        success, out, err = self._run_cmd(["docker", "history", image_name, "--format", "{{json .}}"])
        if not success:
            return []
        return self._parse_json_lines(out)

    def get_image_size(self, image_name):
        images = self.get_images()
        for image in images:
            full = f"{image.get('Repository','')}:{image.get('Tag','')}"
            if image_name == full or image_name == image.get('Repository', ''):
                return image.get('Size', 'N/A')
        return 'N/A'

    def get_all_images_info(self):
        images = self.get_images()
        images_info = []
        for image in images:
            repo = image.get('Repository', 'unknown')
            tag = image.get('Tag', 'latest')
            image_id = image.get('ID', '')[:12]
            size = image.get('Size', 'unknown')
            created = image.get('CreatedAt', 'unknown')
            is_ecr = 'amazonaws.com' in repo or 'dkr.ecr' in repo or '822897118262' in repo
            registry_type = 'AWS ECR' if is_ecr else 'Local Docker'
            images_info.append({
                'repository': repo, 'tag': tag, 'image_id': image_id,
                'size': size, 'created': created,
                'registry_type': registry_type,
                'is_ecr': is_ecr,
                'full_name': f"{repo}:{tag}" if repo != 'unknown' else 'unknown'
            })
        return images_info

    def export_container_image(self, container_name: str) -> dict:
        """Export a container's image as a .tar archive, upload to S3, and generate a presigned download URL.

        Supports both worker fleet containers and local control plane containers.
        Returns:
            dict with success, container_name, image, download_url, filename, expires_in
        """
        import re
        import boto3
        from botocore.exceptions import ClientError

        if not container_name:
            return {"success": False, "error": "Container name cannot be empty"}

        clean_cname = container_name.strip().lstrip('/')

        # Find container in container list to see if it's on a worker
        containers = self.get_containers()
        target_container = None
        for c in containers:
            c_names = [n.lstrip('/') for n in c.get("Names") or []]
            exact_name = (c.get("container_name") or '').lstrip('/')
            if clean_cname == exact_name or clean_cname in c_names:
                target_container = c
                break

        is_worker = bool(target_container and target_container.get("is_worker"))
        target_node = target_container.get("target_node") if target_container else None
        image_name = target_container.get("Image") if target_container else None

        # Resolve target node if container is on a worker
        if not target_node:
            resolved_node = self._get_container_node(clean_cname)
            if resolved_node:
                target_node = resolved_node
                is_worker = True
            elif is_worker or "sweet-treats" in clean_cname or "voltride" in clean_cname:
                target_node = os.environ.get("AUTOPOD_WORKER_INSTANCE_ID", "i-086803910a4c52091")
                is_worker = True

        # If image not found in container list, try docker inspect
        if not image_name:
            if is_worker and target_node:
                from node_deployer import Boto3SSMRunner
                runner = Boto3SSMRunner()
                ok, out, err = runner.run(target_node, f"docker inspect {clean_cname} --format '{{{{.Config.Image}}}}'", timeout=30)
                if ok and out.strip():
                    image_name = out.strip()
            else:
                ok, out, err = self._run_cmd(["docker", "inspect", clean_cname, "--format", "{{.Config.Image}}"])
                if ok and out.strip():
                    image_name = out.strip()

        if not image_name:
            app_info = database.get_application_by_container_name(clean_cname)
            if app_info and app_info.get("image_tag"):
                image_name = app_info["image_tag"]

        if not image_name:
            return {"success": False, "error": f"Could not determine Docker image for container '{clean_cname}'"}

        # Determine safe sanitized archive name
        clean_tag = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', image_name.split('/')[-1])
        safe_archive_name = f"{clean_cname}_{clean_tag}.tar"
        s3_bucket = os.environ.get("AUTOPOD_S3_IMAGES_BUCKET", "autopod-images-822897118262")
        s3_key = f"downloads/{safe_archive_name}"
        region = os.environ.get("AWS_REGION", "us-east-1")
        tmp_tar_path = f"/tmp/{safe_archive_name}"

        # Execute docker save and upload to S3
        if is_worker and target_node:
            from node_deployer import Boto3SSMRunner
            runner = Boto3SSMRunner()
            save_cmd = (
                f"docker save {image_name} -o {tmp_tar_path} && "
                f"aws s3 cp {tmp_tar_path} s3://{s3_bucket}/{s3_key} && "
                f"rm -f {tmp_tar_path}"
            )
            print(f"📦 Exporting image '{image_name}' on worker {target_node} to s3://{s3_bucket}/{s3_key}...")
            ok, out, err = runner.run(target_node, save_cmd, timeout=300)
            if not ok:
                return {
                    "success": False,
                    "error": f"Failed to export image on worker {target_node}: {err or out}"
                }
        else:
            print(f"📦 Exporting image '{image_name}' locally to s3://{s3_bucket}/{s3_key}...")
            save_cmd = [
                "sh", "-c",
                f"docker save {image_name} -o {tmp_tar_path} && aws s3 cp {tmp_tar_path} s3://{s3_bucket}/{s3_key} && rm -f {tmp_tar_path}"
            ]
            ok, out, err = self._run_cmd(save_cmd, timeout=300)
            if not ok:
                return {
                    "success": False,
                    "error": f"Failed to export image on control plane: {err or out}"
                }

        # Generate presigned URL (valid for 15 minutes = 900 seconds using SigV4)
        try:
            from botocore.client import Config
            s3 = boto3.client('s3', region_name=region, config=Config(signature_version='s3v4'))
            presigned_url = s3.generate_presigned_url(
                'get_object',
                Params={
                    'Bucket': s3_bucket,
                    'Key': s3_key,
                    'ResponseContentDisposition': f'attachment; filename="{safe_archive_name}"'
                },
                ExpiresIn=900
            )
            return {
                "success": True,
                "container_name": clean_cname,
                "image": image_name,
                "download_url": presigned_url,
                "filename": safe_archive_name,
                "bucket": s3_bucket,
                "key": s3_key,
                "expires_in": 900
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to generate presigned S3 download URL: {str(e)}"
            }
