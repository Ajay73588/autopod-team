"""AutoPod Cloud -- Universal Technology Detector (Part A)

Inspects repository source code to deterministically identify application frameworks,
runtimes, build requirements, default ports, and health check endpoints.

Supported archetypes:
1. Custom Dockerfile / Containerfile
2. Static HTML / CSS / JavaScript
3. React with Vite
4. React with Create React App (CRA)
5. Node.js with Express
6. Generic Node.js Application
7. Python Flask
8. Python FastAPI
"""

import os
import re
import json
import logging
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List

logger = logging.getLogger("autopod.tech_detector")


@dataclass
class TechDetectionResult:
    """Structured representation of technology detection results."""
    framework: str
    runtime: str
    default_port: int
    health_check_path: str = "/"
    build_cmd: Optional[str] = None
    start_cmd: Optional[str] = None
    build_dir: Optional[str] = None
    has_custom_dockerfile: bool = False
    supported: bool = True
    error_message: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "framework": self.framework,
            "runtime": self.runtime,
            "default_port": self.default_port,
            "health_check_path": self.health_check_path,
            "build_cmd": self.build_cmd,
            "start_cmd": self.start_cmd,
            "build_dir": self.build_dir,
            "has_custom_dockerfile": self.has_custom_dockerfile,
            "supported": self.supported,
            "error_message": self.error_message,
            "metadata": self.metadata,
        }


def _extract_dockerfile_expose_port(dockerfile_path: str) -> int:
    """Best-effort extraction of EXPOSE port from Dockerfile, defaulting to 80."""
    try:
        with open(dockerfile_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if line.upper().startswith("EXPOSE"):
                    parts = line.split()
                    if len(parts) > 1:
                        port_str = parts[1].split("/")[0]
                        if port_str.isdigit():
                            return int(port_str)
    except Exception as e:
        logger.warning(f"Could not parse EXPOSE from {dockerfile_path}: {e}")
    return 80


def _parse_package_json(repo_path: str) -> Optional[Dict[str, Any]]:
    """Safe parser for package.json in repository root."""
    pkg_file = os.path.join(repo_path, "package.json")
    if not os.path.isfile(pkg_file):
        return None
    try:
        with open(pkg_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Malformed or unreadable package.json at {pkg_file}: {e}")
        return None


def _check_python_packages(repo_path: str) -> List[str]:
    """Inspect Python dependency specifications (requirements.txt, pyproject.toml, Pipfile)."""
    found_pkgs = []
    
    # 1. requirements.txt
    req_path = os.path.join(repo_path, "requirements.txt")
    if os.path.isfile(req_path):
        try:
            with open(req_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    clean = re.split(r"[><=~;]", line.strip())[0].strip().lower()
                    if clean:
                        found_pkgs.append(clean)
        except Exception as e:
            logger.warning(f"Error reading requirements.txt: {e}")

    # 2. pyproject.toml
    pyproject_path = os.path.join(repo_path, "pyproject.toml")
    if os.path.isfile(pyproject_path):
        try:
            with open(pyproject_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read().lower()
                if "flask" in content:
                    found_pkgs.append("flask")
                if "fastapi" in content:
                    found_pkgs.append("fastapi")
        except Exception:
            pass

    # 3. Pipfile
    pipfile_path = os.path.join(repo_path, "Pipfile")
    if os.path.isfile(pipfile_path):
        try:
            with open(pipfile_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read().lower()
                if "flask" in content:
                    found_pkgs.append("flask")
                if "fastapi" in content:
                    found_pkgs.append("fastapi")
        except Exception:
            pass

    return found_pkgs


def detect_technology(repo_path: str, config_override: Optional[Dict[str, Any]] = None) -> TechDetectionResult:
    """Deterministically detects the application technology in repo_path.

    Returns a TechDetectionResult object specifying framework, runtime,
    default port, build commands, and start commands.
    """
    if not os.path.isdir(repo_path):
        return TechDetectionResult(
            framework="unknown",
            runtime="unknown",
            default_port=8080,
            supported=False,
            error_message=f"Repository path does not exist: {repo_path}"
        )

    override = config_override or {}

    # Check for custom configuration file in repo if no override passed
    if not override:
        conf_file = os.path.join(repo_path, "autopod.json")
        if os.path.isfile(conf_file):
            try:
                with open(conf_file, "r", encoding="utf-8") as f:
                    override = json.load(f)
            except Exception as e:
                logger.warning(f"Failed to read autopod.json: {e}")

    # -------------------------------------------------------------------------
    # 1. Existing Custom Dockerfile / Containerfile
    # -------------------------------------------------------------------------
    dockerfile_candidates = ["Dockerfile", "Containerfile", "dockerfile"]
    existing_dockerfile = None
    for cand in dockerfile_candidates:
        cand_path = os.path.join(repo_path, cand)
        if os.path.isfile(cand_path):
            existing_dockerfile = cand_path
            break

    if existing_dockerfile:
        port = override.get("target_port") or _extract_dockerfile_expose_port(existing_dockerfile)
        return TechDetectionResult(
            framework="dockerfile",
            runtime="docker",
            default_port=int(port),
            health_check_path=override.get("health_check_path", "/"),
            has_custom_dockerfile=True,
            supported=True,
            metadata={"dockerfile_path": os.path.basename(existing_dockerfile)}
        )

    # -------------------------------------------------------------------------
    # 2. Node.js / JavaScript / TypeScript Ecosystem (package.json)
    # -------------------------------------------------------------------------
    pkg = _parse_package_json(repo_path)
    if pkg:
        deps = {k.lower(): v for k, v in (pkg.get("dependencies") or {}).items()}
        dev_deps = {k.lower(): v for k, v in (pkg.get("devDependencies") or {}).items()}
        all_deps = {**dev_deps, **deps}
        scripts = pkg.get("scripts") or {}

        # 2a. React with Vite
        if "vite" in all_deps:
            build_cmd = override.get("build_cmd") or "npm run build"
            return TechDetectionResult(
                framework="react-vite",
                runtime="node-nginx",
                default_port=int(override.get("target_port") or 80),
                health_check_path=override.get("health_check_path", "/"),
                build_cmd=build_cmd,
                start_cmd=override.get("start_cmd") or "nginx -g 'daemon off;'",
                build_dir="dist",
                supported=True,
                metadata={"package_name": pkg.get("name"), "version": pkg.get("version")}
            )

        # 2b. React with Create React App (react-scripts)
        if "react-scripts" in all_deps or "react" in deps and "build" in scripts:
            build_cmd = override.get("build_cmd") or "npm run build"
            return TechDetectionResult(
                framework="react-cra",
                runtime="node-nginx",
                default_port=int(override.get("target_port") or 80),
                health_check_path=override.get("health_check_path", "/"),
                build_cmd=build_cmd,
                start_cmd=override.get("start_cmd") or "nginx -g 'daemon off;'",
                build_dir="build",
                supported=True,
                metadata={"package_name": pkg.get("name"), "version": pkg.get("version")}
            )

        # 2c. Node.js Express Application
        if "express" in all_deps or "express" in deps:
            start_cmd = override.get("start_cmd")
            if not start_cmd:
                if "start" in scripts:
                    start_cmd = "npm start"
                elif os.path.isfile(os.path.join(repo_path, "server.js")):
                    start_cmd = "node server.js"
                elif os.path.isfile(os.path.join(repo_path, "app.js")):
                    start_cmd = "node app.js"
                elif pkg.get("main"):
                    start_cmd = f"node {pkg.get('main')}"
                else:
                    start_cmd = "npm start"

            port = int(override.get("target_port") or 3000)
            return TechDetectionResult(
                framework="node-express",
                runtime="node",
                default_port=port,
                health_check_path=override.get("health_check_path", "/"),
                build_cmd=override.get("build_cmd") or "npm ci --omit=dev || npm install --omit=dev",
                start_cmd=start_cmd,
                supported=True,
                metadata={"package_name": pkg.get("name"), "main": pkg.get("main")}
            )

        # 2d. Generic Node.js Application with start script
        if "start" in scripts or pkg.get("main"):
            start_cmd = override.get("start_cmd") or ("npm start" if "start" in scripts else f"node {pkg.get('main')}")
            port = int(override.get("target_port") or 3000)
            return TechDetectionResult(
                framework="nodejs",
                runtime="node",
                default_port=port,
                health_check_path=override.get("health_check_path", "/"),
                build_cmd=override.get("build_cmd") or "npm ci --omit=dev || npm install --omit=dev",
                start_cmd=start_cmd,
                supported=True,
                metadata={"package_name": pkg.get("name"), "main": pkg.get("main")}
            )

    # -------------------------------------------------------------------------
    # 3. Python Ecosystem (requirements.txt, pyproject.toml, Pipfile)
    # -------------------------------------------------------------------------
    py_pkgs = _check_python_packages(repo_path)
    py_files = [f for f in os.listdir(repo_path) if f.endswith(".py")] if os.path.isdir(repo_path) else []

    # 3a. FastAPI
    if "fastapi" in py_pkgs or any("fastapi" in open(os.path.join(repo_path, pf), "r", errors="ignore").read().lower() for pf in py_files[:3]):
        # Determine entry point
        main_module = "main" if os.path.isfile(os.path.join(repo_path, "main.py")) else ("app" if os.path.isfile(os.path.join(repo_path, "app.py")) else "main")
        start_cmd = override.get("start_cmd") or f"uvicorn {main_module}:app --host 0.0.0.0 --port 8000 --workers 2"
        return TechDetectionResult(
            framework="python-fastapi",
            runtime="python",
            default_port=int(override.get("target_port") or 8000),
            health_check_path=override.get("health_check_path", "/"),
            build_cmd=override.get("build_cmd") or "pip install --no-cache-dir -r requirements.txt",
            start_cmd=start_cmd,
            supported=True,
            metadata={"python_packages": py_pkgs, "entry_module": main_module}
        )

    # 3b. Flask
    if "flask" in py_pkgs or any("flask" in open(os.path.join(repo_path, pf), "r", errors="ignore").read().lower() for pf in py_files[:3]):
        flask_module = "app" if os.path.isfile(os.path.join(repo_path, "app.py")) else ("wsgi" if os.path.isfile(os.path.join(repo_path, "wsgi.py")) else "app")
        start_cmd = override.get("start_cmd") or f"gunicorn --bind 0.0.0.0:8000 --workers 2 --threads 4 {flask_module}:app"
        return TechDetectionResult(
            framework="python-flask",
            runtime="python",
            default_port=int(override.get("target_port") or 8000),
            health_check_path=override.get("health_check_path", "/"),
            build_cmd=override.get("build_cmd") or "pip install --no-cache-dir -r requirements.txt",
            start_cmd=start_cmd,
            supported=True,
            metadata={"python_packages": py_pkgs, "entry_module": flask_module}
        )

    # -------------------------------------------------------------------------
    # 4. Static HTML / CSS / JavaScript Application
    # -------------------------------------------------------------------------
    has_root_index = os.path.isfile(os.path.join(repo_path, "index.html"))
    has_public_index = os.path.isfile(os.path.join(repo_path, "public", "index.html"))
    if has_root_index or has_public_index:
        # Verify it is not a backend application with an index.html template
        has_backend_indicators = bool(
            pkg or
            os.path.isfile(os.path.join(repo_path, "requirements.txt")) or
            os.path.isfile(os.path.join(repo_path, "pyproject.toml")) or
            os.path.isfile(os.path.join(repo_path, "Pipfile")) or
            os.path.isfile(os.path.join(repo_path, "go.mod")) or
            os.path.isfile(os.path.join(repo_path, "pom.xml"))
        )
        if not has_backend_indicators:
            return TechDetectionResult(
                framework="static",
                runtime="nginx",
                default_port=int(override.get("target_port") or 80),
                health_check_path=override.get("health_check_path", "/"),
                build_cmd=override.get("build_cmd"),
                start_cmd=override.get("start_cmd") or "nginx -g 'daemon off;'",
                build_dir="public" if (has_public_index and not has_root_index) else ".",
                supported=True,
                metadata={"entry_file": "index.html"}
            )

    # -------------------------------------------------------------------------
    # 5. Unsupported or Ambiguous
    # -------------------------------------------------------------------------
    files_sample = os.listdir(repo_path)[:15] if os.path.isdir(repo_path) else []
    err_msg = (
        f"Unsupported application archetype in '{os.path.basename(repo_path)}'. "
        f"No recognized build specification found (checked for Dockerfile, package.json, "
        f"requirements.txt, pyproject.toml, Pipfile, index.html). "
        f"Root directory contains: {', '.join(files_sample)}"
    )
    logger.warning(err_msg)
    return TechDetectionResult(
        framework="unsupported",
        runtime="unknown",
        default_port=8080,
        supported=False,
        error_message=err_msg,
        metadata={"files": files_sample}
    )
