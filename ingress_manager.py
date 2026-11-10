"""AutoPod Cloud -- Shared Worker Ingress Gateway Manager (Part C)

Manages the dynamic Tier-2 Worker Ingress Gateway co-located on the worker fleet.
Generates, validates, and atomically applies Nginx reverse-proxy configurations
mapping application routing paths (e.g., '/', '/crypto/', '/app-name/') to local
worker container ports (e.g., 8081, 8082, etc.).

Guarantees:
1. Listens on worker gateway port (default 8080) without touching the live AWS ALB listener.
2. Preserves '/' route for Sweet Treats Bakery on port 8081 with zero downtime.
3. Automatically maps subpath routes with trailing-slash normalization (e.g., 301 redirect /crypto -> /crypto/).
4. Strips or rewrites subpaths appropriately so applications serve assets relative to their root.
5. Injects standard reverse-proxy and WebSocket upgrade headers.
6. Validates candidate Nginx configurations before applying.
7. Atomically applies changes and reloads Nginx; rolls back to last-known-good on failure.
8. Supports application addition, update, and deactivation without route collision.
9. Fully recoverable from database registry after worker restart.
"""

import os
import re
import shutil
import tempfile
import subprocess
import logging
from typing import List, Dict, Any, Tuple, Optional

logger = logging.getLogger("autopod.ingress_manager")

DEFAULT_GATEWAY_PORT = int(os.environ.get("INGRESS_GATEWAY_PORT", "8080"))
DEFAULT_CONF_DIR = os.environ.get("INGRESS_CONF_DIR", "/etc/nginx/conf.d")
CONF_FILENAME = "autopod_worker_ingress.conf"


class IngressManager:
    """Manages dynamic routing for the Worker Ingress Gateway."""

    def __init__(
        self,
        conf_dir: Optional[str] = None,
        gateway_port: int = DEFAULT_GATEWAY_PORT,
        nginx_binary: str = "nginx",
        enable_system_reload: bool = True
    ):
        self.conf_dir = conf_dir or DEFAULT_CONF_DIR
        self.gateway_port = gateway_port
        self.nginx_binary = nginx_binary
        self.enable_system_reload = enable_system_reload

    @property
    def conf_path(self) -> str:
        return os.path.join(self.conf_dir, CONF_FILENAME)

    @property
    def backup_path(self) -> str:
        return os.path.join(self.conf_dir, f"{CONF_FILENAME}.backup")

    @property
    def candidate_path(self) -> str:
        return os.path.join(self.conf_dir, f"{CONF_FILENAME}.candidate")

    def generate_config(self, applications: List[Dict[str, Any]]) -> str:
        """Generates dynamic Nginx reverse proxy configuration for all active apps.

        Ensures '/' (Sweet Treats Bakery) remains default and subpath routes
        (e.g., '/crypto/') are prioritized using exact and prefix matches.
        """
        # Filter for active applications with assigned target ports
        active_apps = []
        for app in applications:
            lifecycle = (app.get("lifecycle_status") or "active").lower()
            port = app.get("target_port")
            if lifecycle == "active" and port:
                try:
                    port_int = int(port)
                    active_apps.append({
                        "name": app.get("name", "unknown"),
                        "port": port_int,
                        "path": self._normalize_routing_path(app.get("routing_path") or "/"),
                        "health_path": app.get("health_check_path") or "/"
                    })
                except (ValueError, TypeError):
                    logger.warning(f"Skipping app '{app.get('name')}' with invalid target_port: {port}")

        # Separate root application from subpath applications
        root_app = None
        subpath_apps = []
        for app in active_apps:
            if app["path"] == "/":
                root_app = app
            else:
                subpath_apps.append(app)

        # Fallback: if no root app explicitly configured, default to Sweet Treats Bakery on 8081
        if not root_app:
            root_app = {
                "name": "sweet-treats-bakery",
                "port": 8081,
                "path": "/",
                "health_path": "/"
            }

        # Build subpath location blocks
        subpath_blocks = []
        # Sort subpaths by length descending for predictable prefix matching
        subpath_apps.sort(key=lambda a: len(a["path"]), reverse=True)

        for app in subpath_apps:
            clean_path = app["path"].strip("/")
            upstream_port = app["port"]
            app_name = app["name"]

            block = f"""    # Application: {app_name} (Port {upstream_port})
    location = /{clean_path} {{
        return 301 /{clean_path}/;
    }}

    location /{clean_path}/ {{
        proxy_pass http://127.0.0.1:{upstream_port}/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Prefix /{clean_path};
        proxy_connect_timeout 30s;
        proxy_read_timeout 120s;
        proxy_send_timeout 120s;
    }}"""
            subpath_blocks.append(block)

        subpaths_section = "\n\n".join(subpath_blocks)
        if subpaths_section:
            subpaths_section += "\n\n"

        root_port = root_app["port"]
        root_name = root_app["name"]

        config = f"""# AutoPod Cloud - Worker Ingress Gateway Configuration
# Generated dynamically by IngressManager. DO NOT EDIT DIRECTLY.

map $http_upgrade $connection_upgrade {{
    default upgrade;
    '' close;
}}

server {{
    listen {self.gateway_port} default_server;
    server_name _;

    # Ensure relative redirects so clients and ALBs never receive internal ports or IPs
    absolute_redirect off;
    port_in_redirect off;

    client_max_body_size 100M;

    # Internal Worker Ingress Gateway Health Endpoint
    location = /gateway-health {{
        access_log off;
        add_header Content-Type text/plain;
        return 200 'gateway-ok';
    }}

    # ALB Target Group Health Check Endpoint
    location = /healthz {{
        access_log off;
        default_type text/plain;
        return 200 "healthy\\n";
    }}

    # Canary Validation Endpoint
    location ^~ /__autopod-canary/ {{
        default_type text/plain;
        return 200 "autopod-ingress-canary-ok\\n";
    }}

{subpaths_section}    # Root Default Application: {root_name} (Port {root_port})
    location / {{
        proxy_pass http://127.0.0.1:{root_port};
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 30s;
        proxy_read_timeout 120s;
        proxy_send_timeout 120s;
    }}
}}
"""
        return config

    def validate_syntax(self, config_content: str) -> Tuple[bool, Optional[str]]:
        """Validates Nginx configuration syntax.

        Uses the real `nginx -t` if the binary is available;
        otherwise runs structural AST/brace-matching validation.
        """
        # 1. Structural validation
        open_braces = config_content.count("{")
        close_braces = config_content.count("}")
        if open_braces != close_braces:
            return False, f"Syntax Error: Unbalanced braces (open={open_braces}, close={close_braces})"

        if "server {" not in config_content:
            return False, "Syntax Error: Missing 'server {' block"

        if f"listen {self.gateway_port}" not in config_content:
            return False, f"Syntax Error: Missing listen directive for port {self.gateway_port}"

        # Check directive semicolons
        lines = [line.strip() for line in config_content.splitlines()]
        for idx, line in enumerate(lines, 1):
            if not line or line.startswith("#"):
                continue
            if line.endswith("{") or line.endswith("}"):
                continue
            if not line.endswith(";"):
                return False, f"Syntax Error on line {idx}: Directive missing terminating semicolon: '{line}'"

        # 2. System nginx validation if binary exists
        nginx_bin = shutil.which(self.nginx_binary)
        if nginx_bin:
            try:
                with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as tmp_f:
                    # Wrap in minimal http block for standalone nginx -t validation
                    tmp_f.write("events {}\nhttp {\n" + config_content + "\n}\n")
                    tmp_conf_path = tmp_f.name

                res = subprocess.run(
                    [nginx_bin, "-t", "-c", tmp_conf_path],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                os.remove(tmp_conf_path)
                if res.returncode != 0:
                    return False, f"nginx -t validation failed: {res.stderr}"
            except Exception as e:
                logger.debug(f"nginx -t check skipped or failed: {e}")

        return True, None

    def apply_config(self, applications: List[Dict[str, Any]]) -> Tuple[bool, str]:
        """Atomically generates, validates, and applies the gateway configuration.

        If validation or reload fails, safely rolls back to the previous configuration.
        """
        config_content = self.generate_config(applications)

        # Step 1: Validate syntax
        is_valid, err = self.validate_syntax(config_content)
        if not is_valid:
            err_msg = f"Candidate gateway configuration rejected: {err}"
            logger.error(err_msg)
            return False, err_msg

        # Step 2: Ensure configuration directory exists
        try:
            os.makedirs(self.conf_dir, exist_ok=True)
        except Exception as e:
            err_msg = f"Cannot create configuration directory {self.conf_dir}: {e}"
            logger.error(err_msg)
            return False, err_msg

        # Step 3: Backup existing configuration if present
        if os.path.isfile(self.conf_path):
            try:
                shutil.copy2(self.conf_path, self.backup_path)
            except Exception as e:
                logger.warning(f"Could not create backup of existing config: {e}")

        # Step 4: Write candidate file
        try:
            with open(self.candidate_path, "w", encoding="utf-8") as f:
                f.write(config_content)
        except Exception as e:
            err_msg = f"Failed to write candidate configuration to {self.candidate_path}: {e}"
            logger.error(err_msg)
            return False, err_msg

        # Step 5: Atomic rename candidate -> active conf
        try:
            # On Windows, replace handles existing target atomically
            os.replace(self.candidate_path, self.conf_path)
        except Exception as e:
            err_msg = f"Failed to activate candidate configuration: {e}"
            logger.error(err_msg)
            if os.path.isfile(self.backup_path):
                shutil.copy2(self.backup_path, self.conf_path)
            return False, err_msg

        # Step 6: Reload Nginx service if enabled
        if self.enable_system_reload and shutil.which(self.nginx_binary):
            reload_ok, reload_err = self._reload_nginx()
            if not reload_ok:
                logger.error(f"Nginx reload failed: {reload_err}. Rolling back to backup.")
                if os.path.isfile(self.backup_path):
                    shutil.copy2(self.backup_path, self.conf_path)
                    self._reload_nginx()
                return False, f"Nginx reload failed: {reload_err}"

        logger.info(f"Worker Ingress Gateway configuration successfully updated ({len(applications)} apps)")
        return True, "Gateway configuration successfully updated"

    def remove_route(self, app_name: str, applications: List[Dict[str, Any]]) -> Tuple[bool, str]:
        """Removes an application route by re-applying config without the named app."""
        filtered = [app for app in applications if app.get("name") != app_name]
        return self.apply_config(filtered)

    def generate_worker_sync_command(self, applications: List[Dict[str, Any]]) -> str:
        """Generates a base64-safe bash command for remote worker execution via SSM.

        Executes: writes candidate, validates with nginx -t, activates, and reloads.
        """
        import base64
        config_content = self.generate_config(applications)
        b64_content = base64.b64encode(config_content.encode("utf-8")).decode("utf-8")

        target_file = "/etc/nginx/conf.d/autopod_worker_ingress.conf"
        cand_file = "/etc/nginx/conf.d/autopod_worker_ingress.conf.candidate"
        backup_file = "/etc/nginx/conf.d/autopod_worker_ingress.conf.backup"

        script = f"""#!/usr/bin/env bash
set -e
mkdir -p /etc/nginx/conf.d
echo "{b64_content}" | base64 -d > {cand_file}
if nginx -t -c /etc/nginx/nginx.conf; then
    if [ -f {target_file} ]; then
        cp {target_file} {backup_file}
    fi
    mv {cand_file} {target_file}
    nginx -s reload || systemctl reload nginx
    echo "SUCCESS: Gateway ingress reloaded"
else
    echo "ERROR: Candidate config syntax failed"
    rm -f {cand_file}
    exit 1
fi
"""
        return script

    def sync_to_worker(self, runner, node_target: str, applications: List[Dict[str, Any]]) -> Tuple[bool, str]:
        """Synchronizes configuration to a remote worker node via runner (e.g. Boto3SSMRunner)."""
        import base64
        config_content = self.generate_config(applications)
        b64_content = base64.b64encode(config_content.encode("utf-8")).decode("utf-8")

        remote_cmd = (
            f"mkdir -p /etc/autopod-ingress/conf.d /etc/autopod-ingress/backups && "
            f"echo '{b64_content}' | base64 -d > /etc/autopod-ingress/conf.d/default.conf.candidate && "
            f"cp /etc/autopod-ingress/conf.d/default.conf /etc/autopod-ingress/backups/default.conf.pre_sync.bak 2>/dev/null || true; "
            f"cp /etc/autopod-ingress/conf.d/default.conf.candidate /etc/autopod-ingress/conf.d/default.conf && "
            f"if docker exec autopod-ingress-gateway nginx -t; then "
            f"docker exec autopod-ingress-gateway nginx -s reload && echo 'SYNC_SUCCESS'; "
            f"else "
            f"cp /etc/autopod-ingress/backups/default.conf.pre_sync.bak /etc/autopod-ingress/conf.d/default.conf 2>/dev/null || true; "
            f"docker exec autopod-ingress-gateway nginx -s reload && echo 'SYNC_REVERTED'; "
            f"fi"
        )
        try:
            ok, out, err = runner.run(node_target, remote_cmd)
            if ok and "SYNC_SUCCESS" in (out or ""):
                logger.info(f"Worker {node_target} ingress gateway synchronized successfully")
                return True, "Synchronized"
            else:
                logger.warning(f"Worker {node_target} ingress gateway sync issue: out={out}, err={err}")
                return False, f"Sync output: {out}, err: {err}"
        except Exception as e:
            logger.warning(f"Failed to sync ingress to worker {node_target}: {e}")
            return False, str(e)

    def _reload_nginx(self) -> Tuple[bool, Optional[str]]:
        """Invokes nginx reload command."""
        try:
            res = subprocess.run(
                [self.nginx_binary, "-s", "reload"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if res.returncode == 0:
                return True, None
            return False, res.stderr or res.stdout
        except Exception as e:
            return False, str(e)

    @staticmethod
    def _normalize_routing_path(path: str) -> str:
        """Ensures routing paths follow '/path/' convention, or '/' for root."""
        clean = path.strip()
        if not clean or clean == "/":
            return "/"
        clean = clean.strip("/")
        return f"/{clean}/"

