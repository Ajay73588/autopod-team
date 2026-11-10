from flask import request
import os
import logging
import subprocess
import datetime
import hmac
import hashlib
import re
import shutil

logger = logging.getLogger('autopod')


# ---------------------------------------------------------------------------
# GitHub webhook signature verification (HMAC-SHA256)
#
# Previously this module performed NO signature verification at all, despite
# the project report describing it as implemented. Any POST to /webhook
# would trigger a full build+deploy. Now: if GITHUB_WEBHOOK_SECRET is set,
# every request MUST carry a valid X-Hub-Signature-256 header computed with
# that secret, or it is rejected before any cloning/building happens.
# ---------------------------------------------------------------------------

def verify_github_signature(secret, payload_body, signature_header):
    """Verify a GitHub webhook's HMAC-SHA256 signature.

    `payload_body` must be the raw request bytes (not re-serialized JSON --
    GitHub signs the exact bytes it sent, and re-encoding can change them).
    Returns True if valid, False otherwise.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), payload_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def handle_webhook(docker_mgr, data=None, allow_auto_register=True, triggered_by="webhook"):
    """Backward-compatible entry point used by app.py's /webhook route and
    by the dashboard's manual "deploy from URL" trigger.

    This now delegates the actual build/tag/push/deploy/health-check/record
    pipeline to `deployment_engine.deploy()` instead of doing a bare
    build+run itself. Kept as a thin wrapper (rather than inlining
    deployment_engine's import at module load time) to avoid a circular
    import, since deployment_engine imports the repo/demo-project helpers
    defined later in this file.

    `allow_auto_register` is forwarded to `deployment_engine.deploy()`: the
    app.py /webhook route sets this to False for requests authenticated only
    by a GitHub HMAC signature (a real push from an unregistered repo must
    be rejected, not silently turned into a new application), and leaves it
    True for admin-key-authenticated requests (the dashboard's manual
    "deploy from URL" button, where a human operator is deliberately naming
    a new app).
    """
    if data is None:
        payload = request.get_json()
    else:
        payload = data

    print(f"[DEBUG] Webhook payload received: {payload}")

    if not payload or ('repository' not in payload and 'test' not in payload and 'repo_url' not in payload):
        print("[DEBUG] Invalid webhook payload format")
        return {"status": "error", "message": "Invalid webhook payload: missing 'repository' or 'test' field"}

    from deployment_engine import deploy  # local import breaks the circular dependency
    result = deploy(docker_mgr, payload, triggered_by=triggered_by, allow_auto_register=allow_auto_register)
    return result


def extract_repo_name_from_url(repo_url):
    """Extract repository name from GitHub URL with better formatting."""
    try:
        if not repo_url:
            return "webhook-app"
            
        # Remove .git suffix and trailing slashes
        clean_url = repo_url.rstrip('/').replace('.git', '')
        
        # Extract repository name from different GitHub URL formats
        if 'github.com' in clean_url:
            parts = clean_url.split('/')
            # Find the part after github.com/username/
            if len(parts) >= 5:  # https://github.com/username/repo
                repo_name = parts[4]
            elif len(parts) >= 2:  # github.com/username/repo
                repo_name = parts[-1]
            else:
                repo_name = "webhook-app"
            
            # Validate and clean repository name
            if repo_name and repo_name != "webhook-app":
                # Clean up the name for container naming
                repo_name = repo_name.lower().replace(' ', '-').replace('_', '-')
                # Remove any invalid characters
                repo_name = ''.join(c for c in repo_name if c.isalnum() or c == '-')
                return repo_name
                
    except Exception as e:
        logger.warning(f"⚠ Could not extract repo name from URL: {e}")
    
    return "webhook-app"


def validate_commit_sha(sha):
    """Validate a git commit SHA.
    Must be a 7-64 character hexadecimal string and cannot be the null hash.
    Returns the normalized lowercase SHA if valid, None otherwise.
    """
    if not sha or not isinstance(sha, str):
        return None
    cleaned = sha.strip()
    if not cleaned or cleaned == "0" * len(cleaned):
        return None
    if re.fullmatch(r"^[0-9a-fA-F]{7,64}$", cleaned):
        return cleaned.lower()
    return None


def validate_git_ref(ref_name):
    """Validate a git branch or tag reference name according to git ref rules.
    Disallows shell metacharacters, directory traversal ('..'), control chars,
    and invalid git ref characters.
    Returns cleaned ref string if valid, None otherwise.
    """
    if not ref_name or not isinstance(ref_name, str):
        return None
    cleaned = ref_name.strip()
    if not cleaned:
        return None
    # Strip leading refs/heads/ or refs/tags/ if passed
    if cleaned.startswith("refs/heads/"):
        cleaned = cleaned[len("refs/heads/"):]
    elif cleaned.startswith("refs/tags/"):
        cleaned = cleaned[len("refs/tags/"):]

    # Check for invalid git ref characters: spaces, ~, ^, :, ?, *, [, \, @{, .., //
    if any(c in cleaned for c in [" ", "~", "^", ":", "?", "*", "[", "\\", "@", "\t", "\n", "\r"]):
        return None
    if ".." in cleaned or "//" in cleaned:
        return None
    if cleaned.startswith("/") or cleaned.endswith("/") or cleaned.endswith(".lock") or cleaned.endswith("."):
        return None
    if not re.fullmatch(r"^[a-zA-Z0-9_\-\./]+$", cleaned):
        return None
    return cleaned


def clone_repository(repo_url, repo_path, branch=None, commit_sha=None):
    """Clone a Git repository and check out the intended branch/commit.

    All git invocations use argument lists with subprocess.run (no shell execution).
    Validates branch and commit_sha formats.
    Returns True if successfully cloned and checked out; False otherwise.
    """
    try:
        clean_branch = validate_git_ref(branch) if branch else None
        clean_sha = validate_commit_sha(commit_sha) if commit_sha else None

        cmd = ["git", "clone"]
        if clean_branch and not clean_sha:
            cmd += ["--depth", "1", "--branch", clean_branch]
        elif clean_sha:
            cmd += ["--depth", "50"]
            if clean_branch:
                cmd += ["--branch", clean_branch]
        else:
            cmd += ["--depth", "1"]

        cmd += [repo_url, repo_path]
        logger.info(f"🔗 Cloning: {' '.join(cmd)}")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            check=False
        )

        if result.returncode != 0:
            logger.error(f"❌ Git clone failed: {result.stderr}")
            # If clone with --branch failed, attempt fallback clone without --branch restriction
            if clean_branch:
                logger.info("Retrying clone without branch restriction...")
                shutil.rmtree(repo_path, ignore_errors=True)
                retry_cmd = ["git", "clone", "--depth", "50" if clean_sha else "1", repo_url, repo_path]
                result = subprocess.run(retry_cmd, capture_output=True, text=True, timeout=120, check=False)
                if result.returncode != 0:
                    logger.error(f"❌ Fallback git clone failed: {result.stderr}")
                    return False
            else:
                return False

        # If a specific commit SHA was requested, checkout that exact commit
        if clean_sha:
            logger.info(f"Checking out exact commit: {clean_sha}")
            checkout_res = subprocess.run(
                ["git", "checkout", clean_sha],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=30,
                check=False
            )
            if checkout_res.returncode != 0:
                logger.warning(f"Initial checkout of {clean_sha} failed, fetching commit from remote...")
                fetch_res = subprocess.run(
                    ["git", "fetch", "--depth", "1", "origin", clean_sha],
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False
                )
                if fetch_res.returncode == 0:
                    checkout_res = subprocess.run(
                        ["git", "checkout", clean_sha],
                        cwd=repo_path,
                        capture_output=True,
                        text=True,
                        timeout=30,
                        check=False
                    )

            if checkout_res.returncode != 0:
                logger.error(f"❌ Failed to checkout commit {clean_sha}: {checkout_res.stderr}")
                return False
            logger.info(f"✅ Successfully checked out commit {clean_sha}")

        elif clean_branch:
            # Explicitly ensure branch is checked out
            checkout_res = subprocess.run(
                ["git", "checkout", clean_branch],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=30,
                check=False
            )
            if checkout_res.returncode != 0:
                logger.warning(f"Could not explicitly checkout branch {clean_branch}: {checkout_res.stderr}")

        logger.info("✅ Git clone and revision checkout successful")
        return True

    except subprocess.TimeoutExpired:
        logger.error("⏰ Git clone timeout - repository too large or network issue")
        return False
    except Exception as e:
        logger.error(f"❌ Unexpected error during git clone: {e}")
        return False

def create_demo_project(repo_path, repo_name):
    """Create a demo or wrapper project structure with a Dockerfile (also written as Containerfile for backward compatibility).
    P0.4: Safe creation only. Never destructively overwrites existing repository source files."""
    os.makedirs(repo_path, exist_ok=True)
    existing_user_files = [f for f in os.listdir(repo_path) if f not in (".git", "Dockerfile", "Containerfile", "default.conf", "index.html", "build-info.txt")]
    if existing_user_files:
        logger.warning(
            f"create_demo_project refused to overwrite existing files in '{repo_path}': "
            f"{existing_user_files[:5]}. Original repository source code preserved untouched."
        )
        return False
    
    # Get current timestamp for the demo page
    current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    app_prefix = repo_name.lower().replace(' ', '-').split('-')[0]
    
    # Create a Dockerfile (also saved as Containerfile below for Podman compatibility)
    containerfile_content = f"""FROM nginx:alpine

# Copy custom Nginx configuration supporting path-based routing & health checks
COPY default.conf /etc/nginx/conf.d/default.conf

# Copy all application files
COPY . /usr/share/nginx/html/

# Symlinks to support subpath prefix routing
RUN ln -s /usr/share/nginx/html /usr/share/nginx/html/{app_prefix} 2>/dev/null || true

# Create build info file
RUN echo "AutoPod Build - $(date)" > /usr/share/nginx/html/build-info.txt

# Expose port 80
EXPOSE 80

# Start Nginx
CMD ["nginx", "-g", "daemon off;"]
"""

    nginx_conf_content = f"""server {{
    listen 80;
    server_name _;
    port_in_redirect off;
    absolute_redirect off;

    root /usr/share/nginx/html;
    index index.html;

    location / {{
        try_files $uri $uri/ /index.html =404;
    }}

    location /{app_prefix}/ {{
        alias /usr/share/nginx/html/;
        index index.html;
        try_files $uri $uri/ /index.html =404;
    }}

    location = /health-fail {{
        return 500 "Health check simulated failure";
    }}
}}
"""
    
    # Create a custom HTML file
    html_content = f"""<!DOCTYPE html>
<html>
<head>
    <title>AutoPod Demo - {repo_name}</title>
    <style>
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            margin: 0;
            padding: 0;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
        }}
        .container {{
            max-width: 800px;
            margin: 20px;
            padding: 40px;
            background: rgba(255,255,255,0.1);
            border-radius: 20px;
            backdrop-filter: blur(10px);
            box-shadow: 0 8px 32px rgba(0,0,0,0.1);
            text-align: center;
        }}
        h1 {{
            font-size: 3em;
            margin-bottom: 20px;
            color: #66fcf1;
        }}
        h2 {{
            font-size: 1.5em;
            margin-bottom: 30px;
            opacity: 0.9;
        }}
        .status {{
            background: rgba(46, 204, 113, 0.2);
            padding: 15px;
            border-radius: 10px;
            margin: 20px 0;
            border: 1px solid #2ecc71;
        }}
        .info {{
            background: rgba(52, 152, 219, 0.2);
            padding: 15px;
            border-radius: 10px;
            margin: 20px 0;
            border: 1px solid #3498db;
        }}
        .tech-info {{
            background: rgba(155, 89, 182, 0.2);
            padding: 15px;
            border-radius: 10px;
            margin: 20px 0;
            border: 1px solid #9b59b6;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🚀 AutoPod</h1>
        <h2>Intelligent Container Deployment</h2>
        
        <div class="status">
            <h3>✅ Deployment Successful!</h3>
            <p>Repository: <strong>{repo_name}</strong></p>
        </div>
        
        <div class="info">
            <p>🕒 Deployed at: {current_time}</p>
            <p>🐳 Powered by Docker</p>
            <p>🤖 Automated by AutoPod</p>
        </div>
        
        <div class="tech-info">
            <h4>🛠️ Technical Details</h4>
            <p>This container was automatically:</p>
            <ul style="text-align: left; display: inline-block;">
                <li>Built from Dockerfile</li>
                <li>Deployed via webhook trigger</li>
                <li>Running on Docker runtime</li>
                <li>Managed by AutoPod system</li>
            </ul>
        </div>
        
        <p>Refresh the AutoPod dashboard to see this container in the list!</p>
    </div>
</body>
</html>
"""
    
    # Also write Containerfile for Podman compatibility, in case it's ever used
    with open(os.path.join(repo_path, "Containerfile"), "w", encoding='utf-8') as f:
        f.write(containerfile_content)
    
    # Also create Dockerfile for compatibility
    with open(os.path.join(repo_path, "Dockerfile"), "w", encoding='utf-8') as f:
        f.write(containerfile_content)
    
    # Write HTML file ONLY if index.html doesn't already exist
    index_file = os.path.join(repo_path, "index.html")
    if not os.path.exists(index_file):
        with open(index_file, "w", encoding='utf-8') as f:
            f.write(html_content)

    # Write Nginx configuration file
    with open(os.path.join(repo_path, "default.conf"), "w", encoding='utf-8') as f:
        f.write(nginx_conf_content)

    logger.info(f"📄 Demo project created at: {repo_path} with Containerfile and custom Nginx conf")

def find_available_port(start_port=8081):
    """Find an available port starting from start_port.
    When running inside Docker, verifies that candidate port is free on
    the Docker host as well as container-local loopback."""
    import socket
    port = start_port
    max_port = start_port + 50  # Search up to 50 ports

    probe_host = os.environ.get("DOCKER_HOST_PROBE_IP")
    if not probe_host and (os.path.exists("/.dockerenv") or os.environ.get("FLASK_ENV") == "production"):
        probe_host = "host.docker.internal"

    while port <= max_port:
        try:
            # 1. Check container-local or host-local bind
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(('0.0.0.0', port))

            # 2. If running containerized, verify port is not already bound on the Docker host
            if probe_host:
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s_host:
                        s_host.settimeout(0.3)
                        if s_host.connect_ex((probe_host, port)) == 0:
                            port += 1
                            continue
                except Exception:
                    pass

            return port
        except OSError:
            port += 1

    # If no port found, return the original (might fail, but we try)
    logger.warning(f"⚠  No available ports found, using {start_port}")
    return start_port