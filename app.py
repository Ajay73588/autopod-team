import os
import logging
from logging.handlers import RotatingFileHandler
import json
import hmac
import hashlib
from datetime import datetime
from flask import Flask, jsonify, request, g
from flask_cors import CORS  # Add this import
from webhook_handler import handle_webhook, verify_github_signature
from docker_manager import DockerManager
from database import init_db, get_container_logs, get_container_status
import database
import version_manager
import deployment_engine
import node_deployer
import aws_nodes
from auth import require_admin_key, check_admin_key
import atexit
from database import close_db_connection
import subprocess

app = Flask(__name__)

# CORS configuration for the React frontend.
#
# The allowed origins are configurable via the ALLOWED_ORIGINS environment
# variable (comma-separated) so the same image works for local dev
# (localhost:3000) and a production dashboard served from an EC2/reverse
# proxy origin, without editing code. Previously a separate `after_request`
# handler unconditionally overwrote this with a hardcoded
# 'http://localhost:3000', which silently broke CORS for any other origin
# (e.g. the production dashboard) -- that hardcoded handler has been removed;
# Flask-CORS is now the single source of truth for these headers.
_default_origins = "http://localhost,http://127.0.0.1,http://localhost:3000,http://127.0.0.1:3000,http://frontend:80,http://frontend:3000"
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", _default_origins).split(",") if o.strip()]

CORS(app,
     origins=ALLOWED_ORIGINS,
     methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
     allow_headers=["Content-Type", "Authorization", "Access-Control-Allow-Origin", "X-Admin-Key"],
     supports_credentials=True)

# Wrap WSGI app with ProxyFix to correctly handle X-Forwarded-For / X-Forwarded-Proto from Nginx
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Ensure logs folder exists
LOG_DIR = os.path.join(os.path.dirname(__file__), 'logs')
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, 'autopod.log')

# Configure rotating file logging
logger = logging.getLogger('autopod')
logger.setLevel(logging.INFO)

# Avoid adding multiple handlers if module reloaded
if not logger.handlers:
    handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    handler.setLevel(logging.INFO)
    logger.addHandler(handler)

# Also route Flask's logger to the same handlers
app.logger.handlers = logger.handlers
app.logger.setLevel(logger.level)

# Initialize Podman manager and database (safe init with logging)
try:
    docker_mgr = DockerManager()
    logger.info("DockerManager initialized successfully")
except Exception as e:
    logger.exception("Failed to initialize DockerManager")
    docker_mgr = None

try:
    init_db()
    logger.info("Database initialized successfully")
except Exception as e:
    logger.exception("Failed to initialize database")

try:
    import deployment_queue
    deployment_queue.get_queue_worker().start(docker_mgr=docker_mgr)
    logger.info("Deployment queue worker initialized successfully")
except Exception as e:
    logger.warning("Failed to initialize deployment queue worker: %s", e)

# Clean up database connections on app exit
@atexit.register
def cleanup():
    close_db_connection()
    logger.info("Database connections cleaned up")

@app.route('/')
def dashboard():
    """Render the main dashboard page."""
    return jsonify({
        "message": "AutoPod API is running! Use React frontend at http://localhost:3000",
        "status": "active",
        "version": "1.0.0"
    })

@app.route('/api/status')
def api_status():
    """API endpoint to get container statuses."""
    try:
        statuses = get_container_status()
        logger.info(f"Fetched {len(statuses)} container statuses")
        return jsonify({"success": True, "data": statuses})
    except Exception as e:
        logger.exception("Error fetching container statuses")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/logs', methods=['GET', 'OPTIONS'])
@require_admin_key
def api_logs():
    """API endpoint to get container logs.
    Admin-key protected: container logs may contain credentials, tokens, or sensitive application output."""
    try:
        logs = get_container_logs()
        logger.info(f"Fetched {len(logs)} log entries")
        return jsonify({"success": True, "data": logs})
    except Exception as e:
        logger.exception("Error fetching logs")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/sync', methods=['POST', 'OPTIONS'])
@require_admin_key
def api_sync():
    """API endpoint to manually sync Docker containers with database.
    Admin-key protected: this mutates the database and should not be
    callable anonymously."""
    if request.method == 'OPTIONS':
        return '', 200

    try:
        if docker_mgr is None:
            return jsonify({"success": False, "error": "DockerManager not initialized"}), 500

        docker_mgr.sync_with_db()
        logger.info("Manual sync completed successfully")
        return jsonify({"success": True, "message": "Sync completed successfully"})
    except Exception as e:
        logger.exception("Error syncing containers")
        return jsonify({"success": False, "error": str(e)}), 500
    
@app.route('/api/containers', methods=['GET'])
def api_containers():
    """API endpoint to get running Docker container list."""
    try:
        if docker_mgr is None:
            return jsonify({"success": False, "error": "DockerManager not initialized"}), 500

        containers = docker_mgr.get_containers()
        if containers is None:
            containers = []

        for container in containers:
            names = container.get("Names", [])
            primary_name = (names[0] if names else "").lstrip("/")
            is_infra = primary_name in {'autopod-frontend', 'autopod-backend', 'autopod-webhook-proxy'}
            container["is_protected"] = is_infra
            container["container_type"] = "infrastructure" if is_infra else "application"
            container["host_type"] = "control-plane" if (is_infra or not container.get("is_worker")) else "worker"
            if not container.get("target_node"):
                container["target_node"] = "control-plane" if is_infra else os.environ.get("AUTOPOD_WORKER_INSTANCE_ID", "i-086803910a4c52091")
            
            pub_url = database.resolve_container_public_url(primary_name)
            # P0.3: Ensure public_url is never localhost or 127.0.0.1
            if pub_url and ("localhost" in pub_url.lower() or "127.0.0.1" in pub_url):
                pub_url = None
            container["public_url"] = pub_url
            container["web_url"] = pub_url
            container["routing_status"] = "available" if pub_url else "unavailable"

        logger.info(f"Fetched {len(containers)} containers")
        return jsonify({
            "success": True,
            "data": containers,
            "count": len(containers),
            "message": f"Found {len(containers)} containers",
        })
    except Exception as e:
        logger.exception("Error fetching container data")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/debug/containers', methods=['GET'])
def api_debug_containers():
    """Deprecated: use /api/debug/docker-health instead.
    Kept as a backward-compatible alias; returns same payload as
    docker-health so old callers don't 404."""
    return debug_docker_health()

@app.route('/webhook', methods=['POST', 'OPTIONS'], strict_slashes=False)
@app.route('/webhook/', methods=['POST', 'OPTIONS'], strict_slashes=False)
def webhook():
    """Handle GitHub webhook events.

    Security (ARCHITECTURE.md Section 15 / item 6.2): if GITHUB_WEBHOOK_SECRET
    is configured, every request must carry a valid HMAC-SHA256
    `X-Hub-Signature-256` header computed with that secret, verified against
    the RAW request body (read before Flask parses it as JSON, since
    GitHub signs the exact bytes it sent). Requests with a missing or
    invalid signature are rejected with 401 before any cloning/building
    happens. As a secondary path, a request carrying a valid X-Admin-Key
    is also accepted without a GitHub signature -- this is what lets the
    dashboard's own manual "deploy from URL" trigger (DeploymentSection.js)
    keep working without exposing the GitHub secret to the frontend.
    If GITHUB_WEBHOOK_SECRET is not set at all, verification is skipped
    entirely (local/dev convenience) and a warning is logged.
    """
    if request.method == 'OPTIONS':
        return '', 200

    try:
        raw_body = request.get_data()
        signature = request.headers.get("X-Hub-Signature-256", "")
        delivery_id = request.headers.get("X-GitHub-Delivery", "").strip()
        event_type = request.headers.get("X-GitHub-Event", "push").strip()
        webhook_secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "").strip()

        sig_exists = bool(signature)
        sig_format_valid = bool(signature and signature.startswith("sha256=") and len(signature) == 71)
        secret_present = bool(webhook_secret)

        admin_ok = check_admin_key(request)
        if webhook_secret:
            signature_ok = verify_github_signature(webhook_secret, raw_body, signature)

            if not (signature_ok or admin_ok):
                rejection_reason = "missing_signature" if not sig_exists else ("invalid_format" if not sig_format_valid else "signature_mismatch")
                logger.warning(
                    "Webhook request rejected: reason=%s, delivery_id=%s, event=%s, body_length=%d, "
                    "secret_present=%s, secret_len=%d, signature_present=%s, format_valid=%s",
                    rejection_reason, delivery_id or "none", event_type or "none", len(raw_body),
                    secret_present, len(webhook_secret), sig_exists, sig_format_valid
                )
                return jsonify({"status": "error", "message": "Invalid or missing webhook signature"}), 401
            else:
                logger.info(
                    "Webhook request authenticated: delivery_id=%s, event=%s, body_length=%d, "
                    "secret_present=%s, admin_auth=%s",
                    delivery_id or "none", event_type or "none", len(raw_body),
                    secret_present, admin_ok
                )
        else:
            logger.warning("GITHUB_WEBHOOK_SECRET not set -- webhook signature verification is DISABLED")

        # Deduplication & replay protection (Phase 8):
        # If an authenticated delivery ID was provided, atomically record it.
        # If record_webhook_delivery returns False, the exact delivery was already processed
        # or is currently being processed by a concurrent request.
        if delivery_id:
            if not database.record_webhook_delivery(delivery_id, event_type=event_type):
                logger.warning("Ignoring duplicate webhook delivery: %s", delivery_id)
                return jsonify({
                    "status": "ignored",
                    "message": f"Duplicate delivery '{delivery_id}' ignored (already processed)",
                    "data": {"delivery_id": delivery_id, "duplicate": True}
                }), 200

        data = request.get_json(silent=True)
        if data is None:
            logger.warning("Webhook received with no JSON body")
            return jsonify({"status": "error", "message": "No JSON data received"}), 400

        # Handle GitHub ping event:
        # GitHub automatically sends a ping event upon webhook creation/testing.
        # Respond with HTTP 200 OK without triggering deployment or creating records.
        event_lower = (event_type or "push").strip().lower()
        if event_lower == "ping":
            logger.info("GitHub ping event received (delivery_id=%s). Responding 200 OK.", delivery_id)
            zen = data.get("zen")
            hook_id = data.get("hook_id")
            return jsonify({
                "status": "ok",
                "message": "GitHub ping event received successfully",
                "data": {"event": "ping", "zen": zen, "hook_id": hook_id}
            }), 200

        # Handle unsupported GitHub event types:
        # AutoPod Cloud triggers deployments exclusively on 'push' events. Other events (pull_request,
        # issues, star, fork, etc.) are safely ignored with HTTP 200 without starting builds.
        if event_lower != "push":
            logger.info("Ignoring unsupported GitHub event type: %s", event_type)
            return jsonify({
                "status": "ignored",
                "message": f"Event type '{event_type}' ignored (only 'push' triggers deployments)",
                "data": {"event": event_type}
            }), 200

        # Handle branch/tag deletion pushes:
        # Deletion push events carry 'deleted': True or 40 zeros in 'after'. Do not deploy deleted refs.
        if data.get("deleted") is True or data.get("after") == "0000000000000000000000000000000000000000":
            logger.info("Ignoring branch deletion push event for ref: %s", data.get("ref"))
            return jsonify({
                "status": "ignored",
                "message": f"Branch deletion event for '{data.get('ref')}' ignored",
                "data": {"ref": data.get("ref"), "deleted": True}
            }), 200

        # Multi-application isolation (ARCHITECTURE.md Section 8): only an
        # authenticated admin action (the dashboard's manual "deploy from
        # URL") may register a brand-new application on the fly. A request
        # that only carries a valid GitHub signature -- i.e. a real push
        # from GitHub -- must target an ALREADY REGISTERED repo_url, or it
        # is rejected before any cloning/building happens.
        allow_auto_register = bool(admin_ok)

        repo_url = None
        if data and 'repository' in data:
            repo_url = data['repository'].get('clone_url') or data['repository'].get('html_url')
        elif data and 'repo_url' in data:
            repo_url = data.get('repo_url')

        app_record = database.find_application_by_repo_url(repo_url) if repo_url else None
        if repo_url and not app_record and not allow_auto_register:
            logger.warning("Rejected webhook for unregistered repo: %s", repo_url)
            return jsonify({
                "status": "rejected",
                "message": f"Repository '{repo_url}' is not registered. Please register the application in the dashboard first."
            }), 404

        if docker_mgr is None:
            logger.error("DockerManager not initialized — cannot process webhook")
            return jsonify({"status": "error", "message": "DockerManager not initialized"}), 500

        # In testing mode, explicit sync mode, or synthetic test payloads: execute synchronously
        if app.config.get('TESTING') or request.args.get('sync') == 'true' or data.get('test'):
            logger.info("Processing webhook payload synchronously")
            result = handle_webhook(docker_mgr, data, allow_auto_register=allow_auto_register)
            logger.info("Webhook processing completed: status=%s", result.get("status"))

            result_status = result.get("status", "success")
            if result_status in ("success", "ignored", "skipped"):
                status_code = 200
            elif result_status == "rejected":
                status_code = 404
            else:
                status_code = 400
            return jsonify({
                "status": result_status,
                "message": result.get("message", "Deployment processed"),
                "data": result
            }), status_code

        # Asynchronous Queue Processing (Production push-to-deploy)
        import deployment_queue
        worker = deployment_queue.get_queue_worker()
        worker.start(docker_mgr=docker_mgr)
        queued_job = deployment_queue.enqueue_webhook_deployment(
            application_id=app_record["id"],
            payload=data,
            triggered_by="webhook"
        )
        logger.info(
            "Webhook push for '%s' enqueued as job #%d (commit: %s)",
            app_record["name"], queued_job["id"], queued_job.get("commit_sha")
        )
        return jsonify({
            "status": "queued",
            "message": f"Deployment queued for '{app_record['name']}' (Job #{queued_job['id']})",
            "data": {
                "queue_id": queued_job["id"],
                "application_id": app_record["id"],
                "application_name": app_record["name"],
                "commit": queued_job.get("commit_sha"),
                "branch": queued_job.get("branch"),
                "status": "queued"
            }
        }), 202

    except Exception as e:
        logger.exception("Webhook handling error")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/webhook/deliveries', methods=['GET', 'OPTIONS'])
def api_list_webhook_deliveries():
    """List recent webhook deliveries recorded by AutoPod Cloud."""
    if request.method == 'OPTIONS':
        return '', 200
    try:
        limit = request.args.get('limit', default=10, type=int)
        deliveries = database.list_webhook_deliveries(limit=limit)
        return jsonify({"success": True, "data": deliveries})
    except Exception as e:
        logger.exception("Error listing webhook deliveries")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/deployments/queue', methods=['GET', 'OPTIONS'])
def api_list_deployment_queue():
    """List queued and recently processed background deployment jobs."""
    if request.method == 'OPTIONS':
        return '', 200
    try:
        limit = request.args.get('limit', default=20, type=int)
        jobs = database.list_queued_deployments(limit=limit)
        return jsonify({"success": True, "data": jobs}), 200
    except Exception as e:
        logger.exception("Error listing deployment queue")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    return jsonify({
        "status": "healthy",
        "docker_initialized": docker_mgr is not None,
        "database_initialized": True,
        "frontend_ready": True
    })

# Docker Hub Authentication Endpoints - ADDED
@app.route('/api/docker/login', methods=['POST', 'OPTIONS'])
@require_admin_key
def docker_login():
    """Login to Docker Hub using provided credentials."""
    if request.method == 'OPTIONS':
        return '', 200
        
    try:
        data = request.get_json()
        username = data.get('username')
        password = data.get('password')
        registry = data.get('registry', 'docker.io')
        
        if not username or not password:
            return jsonify({
                'success': False,
                'error': 'Username and password are required'
            }), 400
        
        # Store credentials in environment variables
        os.environ['DOCKER_USERNAME'] = username
        os.environ['DOCKER_PASSWORD'] = password
        os.environ['DOCKER_REGISTRY'] = registry
        
        # Test the login (password piped via stdin, never placed on the command line)
        result = subprocess.run(
            ['docker', 'login', '-u', username, '--password-stdin', registry],
            input=password, capture_output=True, text=True
        )
        
        if result.returncode == 0:
            logger.info(f"Successfully logged in to Docker Hub as {username}")
            return jsonify({
                'success': True,
                'message': 'Successfully logged in to Docker Hub'
            })
        else:
            # Clear credentials if login fails
            os.environ.pop('DOCKER_USERNAME', None)
            os.environ.pop('DOCKER_PASSWORD', None)
            os.environ.pop('DOCKER_REGISTRY', None)
            logger.error(f"Docker Hub login failed: {result.stderr}")
            return jsonify({
                'success': False,
                'error': f'Login failed: {result.stderr}'
            }), 400
            
    except Exception as e:
        logger.exception("Error during Docker Hub login")
        return jsonify({
            'success': False,
            'error': f'Login error: {str(e)}'
        }), 500

@app.route('/api/docker/logout', methods=['POST', 'OPTIONS'])
@require_admin_key
def docker_logout():
    """Logout from Docker Hub and clear credentials."""
    if request.method == 'OPTIONS':
        return '', 200
        
    try:
        # Clear environment variables
        os.environ.pop('DOCKER_USERNAME', None)
        os.environ.pop('DOCKER_PASSWORD', None)
        os.environ.pop('DOCKER_REGISTRY', None)
        
        # Logout from Podman
        result = subprocess.run(['docker', 'logout', 'docker.io'],
                              capture_output=True, text=True)
        
        logger.info("Successfully logged out from Docker Hub")
        return jsonify({
            'success': True,
            'message': 'Successfully logged out from Docker Hub'
        })
        
    except Exception as e:
        logger.exception("Error during Docker Hub logout")
        return jsonify({
            'success': False,
            'error': f'Logout error: {str(e)}'
        }), 500

@app.route('/api/docker/check-login', methods=['GET'])
@require_admin_key
def check_docker_login():
    """Check if currently logged in to Docker Hub.
    Admin-key protected: returns the configured Docker Hub username,
    which is operational information that must not be publicly visible."""
    try:
        username = os.environ.get('DOCKER_USERNAME')

        if username:
            # Verify login is still valid by checking the docker config, not
            # by running docker search (which makes a network call and leaks
            # the username in the command arguments).
            import shutil
            docker_available = bool(shutil.which('docker'))
            logger.info("Docker Hub login check: logged_in=%s", bool(username))
            return jsonify({
                'success': True,
                'logged_in': True,
                'username': username,
                'docker_available': docker_available,
            })
        else:
            logger.info("Docker Hub login check: not logged in")
            return jsonify({
                'success': True,
                'logged_in': False,
                'username': None
            })

    except Exception as e:
        logger.exception("Error checking Docker Hub login status")
        return jsonify({
            'success': False,
            'error': f'Error checking login status: {str(e)}'
        }), 500

@app.route('/api/docker/push', methods=['POST', 'OPTIONS'])
@require_admin_key
def docker_push_image():
    """Push container image to Docker Hub."""
    if request.method == 'OPTIONS':
        return '', 200
        
    try:
        data = request.get_json()
        image_name = data.get('image_name')
        username = data.get('username')
        registry = data.get('registry', 'docker.io')
        
        if not image_name or not username:
            return jsonify({
                'success': False,
                'error': 'Image name and username are required'
            }), 400
        
        # Tag the image
        tagged_name = f'{registry}/{username}/{image_name}:latest'
        
        # Tag the image
        tag_result = subprocess.run([
            'docker', 'tag', image_name, tagged_name
        ], capture_output=True, text=True)
        
        if tag_result.returncode != 0:
            logger.error(f"Failed to tag image {image_name}: {tag_result.stderr}")
            return jsonify({
                'success': False,
                'error': f'Failed to tag image: {tag_result.stderr}'
            }), 400
        
        # Push the image
        push_result = subprocess.run([
            'docker', 'push', tagged_name
        ], capture_output=True, text=True)
        
        if push_result.returncode == 0:
            logger.info(f"Successfully pushed image {tagged_name} to Docker Hub")
            return jsonify({
                'success': True,
                'data': {
                    'tagged_name': tagged_name,
                    'registry_url': f'https://hub.docker.com/r/{username}/{image_name}',
                    'pull_command': f'docker pull {tagged_name}'
                }
            })
        else:
            logger.error(f"Failed to push image {tagged_name}: {push_result.stderr}")
            return jsonify({
                'success': False,
                'error': f'Push failed: {push_result.stderr}'
            }), 400
            
    except Exception as e:
        logger.exception("Error pushing image to Docker Hub")
        return jsonify({
            'success': False,
            'error': f'Push error: {str(e)}'
        }), 500

# Container Management Endpoints
@app.route('/api/containers/start', methods=['POST', 'OPTIONS'])
@require_admin_key
def api_start_container():
    """API endpoint to start a container."""
    if request.method == 'OPTIONS':
        return '', 200
        
    try:
        data = request.get_json() or {}
        container_name = data.get('container_name')
        target_node = data.get('target_node')
        
        if not container_name:
            return jsonify({"success": False, "error": "Container name is required"}), 400
        
        if docker_mgr is None:
            return jsonify({"success": False, "error": "DockerManager not initialized"}), 500
        
        success = docker_mgr.start_container(container_name, target_node=target_node)
        if success:
            docker_mgr.sync_with_db()  # Update the database
            app = database.get_application_by_container_name(container_name)
            if app:
                database.update_application(app["id"], lifecycle_status="active")
            logger.info(f"Container {container_name} started successfully")
            return jsonify({"success": True, "message": f"Container {container_name} started"})
        else:
            logger.error(f"Failed to start container {container_name}")
            return jsonify({"success": False, "error": f"Failed to start container {container_name}"}), 500
            
    except Exception as e:
        logger.exception("Error starting container")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/containers/stop', methods=['POST', 'OPTIONS'])
@require_admin_key
def api_stop_container():
    """API endpoint to stop a container."""
    if request.method == 'OPTIONS':
        return '', 200
        
    try:
        data = request.get_json() or {}
        container_name = data.get('container_name')
        target_node = data.get('target_node')
        
        if not container_name:
            return jsonify({"success": False, "error": "Container name is required"}), 400
        
        if docker_mgr is None:
            return jsonify({"success": False, "error": "DockerManager not initialized"}), 500
        
        success = docker_mgr.stop_container(container_name, target_node=target_node)
        if success:
            docker_mgr.sync_with_db()  # Update the database
            app = database.get_application_by_container_name(container_name)
            if app:
                database.update_application(app["id"], health_status="stopped")
            logger.info(f"Container {container_name} stopped successfully")
            return jsonify({"success": True, "message": f"Container {container_name} stopped"})
        else:
            logger.error(f"Failed to stop container {container_name}")
            return jsonify({"success": False, "error": f"Failed to stop container {container_name}"}), 500
            
    except Exception as e:
        logger.exception("Error stopping container")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/containers/restart', methods=['POST', 'OPTIONS'])
@require_admin_key
def api_restart_container():
    """API endpoint to restart a container."""
    if request.method == 'OPTIONS':
        return '', 200
        
    try:
        data = request.get_json() or {}
        container_name = data.get('container_name')
        target_node = data.get('target_node')
        
        if not container_name:
            return jsonify({"success": False, "error": "Container name is required"}), 400
        
        if docker_mgr is None:
            return jsonify({"success": False, "error": "DockerManager not initialized"}), 500
        
        success = docker_mgr.restart_container(container_name, target_node=target_node)
        if success:
            docker_mgr.sync_with_db()  # Update the database
            app = database.get_application_by_container_name(container_name)
            if app:
                database.update_application(app["id"], lifecycle_status="active")
            logger.info(f"Container {container_name} restarted successfully")
            return jsonify({"success": True, "message": f"Container {container_name} restarted"})
        else:
            logger.error(f"Failed to restart container {container_name}")
            return jsonify({"success": False, "error": f"Failed to restart container {container_name}"}), 500
            
    except Exception as e:
        logger.exception("Error restarting container")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/containers/remove', methods=['POST', 'OPTIONS'])
@require_admin_key
def api_remove_container():
    """API endpoint to remove a container."""
    if request.method == 'OPTIONS':
        return '', 200
        
    try:
        data = request.get_json() or {}
        container_name = data.get('container_name')
        target_node = data.get('target_node')
        
        if not container_name:
            return jsonify({"success": False, "error": "Container name is required"}), 400
        
        clean_name = container_name.strip().lstrip("/")
        PROTECTED_INFRASTRUCTURE_CONTAINERS = {'autopod-frontend', 'autopod-backend', 'autopod-webhook-proxy'}
        if clean_name in PROTECTED_INFRASTRUCTURE_CONTAINERS:
            logger.warning(f"Rejected attempt to remove protected infrastructure container: {clean_name}")
            return jsonify({
                "success": False,
                "error": f"Cannot remove protected AutoPod infrastructure container '{clean_name}'. Infrastructure containers are required for the platform to operate."
            }), 403
        
        if docker_mgr is None:
            return jsonify({"success": False, "error": "DockerManager not initialized"}), 500
        
        success = docker_mgr.remove_container(container_name, target_node=target_node)
        if success:
            docker_mgr.sync_with_db()  # Update the database
            # Clean up application lifecycle status and ingress routes
            app = database.get_application_by_container_name(container_name)
            if app:
                database.update_application(
                    app["id"],
                    lifecycle_status="removed",
                    health_status="stopped"
                )
                try:
                    from ingress_manager import IngressManager
                    ingress_mgr = IngressManager()
                    all_apps = database.list_applications()
                    active_apps = [a for a in all_apps if a.get("lifecycle_status") == "active" and a.get("id") != app["id"]]
                    ingress_mgr.apply_config(active_apps)
                    
                    import node_deployer
                    runner = node_deployer.Boto3SSMRunner()
                    workers = database.list_app_nodes(status="in_service")
                    for w in workers:
                        target = w.get("instance_id") or w.get("ip_address")
                        ingress_mgr.sync_to_worker(runner, target, active_apps)
                except Exception as ex:
                    logger.warning(f"Failed to clean up ingress route on container removal: {ex}")

            logger.info(f"Container {container_name} removed successfully")
            return jsonify({"success": True, "message": f"Container {container_name} removed"})
        else:
            logger.error(f"Failed to remove container {container_name}")
            return jsonify({"success": False, "error": f"Failed to remove container {container_name}"}), 500
            
    except Exception as e:
        logger.exception("Error removing container")
        return jsonify({"success": False, "error": str(e)}), 500

# Health Monitoring Endpoints
@app.route('/api/containers/<container_name>/health', methods=['GET'])
def api_container_health(container_name):
    """Get detailed health information for a container."""
    try:
        if docker_mgr is None:
            return jsonify({"success": False, "error": "DockerManager not initialized"}), 500
        
        health_data = {
            'health': docker_mgr.get_container_health(container_name),
            'resources': docker_mgr.get_container_resources(container_name)
        }
        
        return jsonify({"success": True, "data": health_data})
    except Exception as e:
        logger.exception(f"Error getting health for {container_name}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/containers/health', methods=['GET'])
def api_all_containers_health():
    """Get health status for all containers."""
    try:
        if docker_mgr is None:
            return jsonify({"success": False, "error": "DockerManager not initialized"}), 500
        
        health_data = docker_mgr.get_all_containers_health()
        return jsonify({"success": True, "data": health_data})
    except Exception as e:
        logger.exception("Error getting all containers health")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/containers/<container_name>/stats', methods=['GET'])
def api_container_stats(container_name):
    """Get real-time statistics for a container."""
    try:
        if docker_mgr is None:
            return jsonify({"success": False, "error": "DockerManager not initialized"}), 500
        
        stats_data = docker_mgr.get_container_stats(container_name)
        return jsonify({"success": True, "data": stats_data})
    except Exception as e:
        logger.exception(f"Error getting stats for {container_name}")
        return jsonify({"success": False, "error": str(e)}), 500
    
@app.route('/api/containers/<container_name>/weburl', methods=['GET'])
def api_container_weburl(container_name):
    """Get the web URL for a container."""
    try:
        web_url = database.resolve_container_public_url(container_name)
        container_status = (
            docker_mgr.get_container_status_for_ui(container_name)
            if docker_mgr and hasattr(docker_mgr, 'get_container_status_for_ui')
            else {'ports': [], 'status': 'unknown'}
        )
        
        return jsonify({
            "success": True, 
            "data": {
                "web_url": web_url,
                "has_web_interface": web_url is not None,
                "ports": container_status.get('ports', []),
                "status": container_status.get('status', 'unknown')
            }
        })
    except Exception as e:
        logger.exception(f"Error getting web URL for {container_name}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/containers/<container_name>/open', methods=['POST', 'OPTIONS'])
@require_admin_key
def api_open_container_web(container_name):
    """Open container's web interface."""
    if request.method == 'OPTIONS':
        return '', 200
        
    try:
        web_url = database.resolve_container_public_url(container_name)
        
        # P0.3: Never return localhost/127.0.0.1 and return HTTP 409 if public routing is unconfigured
        if not web_url or "localhost" in web_url.lower() or "127.0.0.1" in web_url:
            c_name = container_name.strip().lstrip('/').lower()
            internal_services = {'autopod-backend', 'autopod-frontend', 'autopod-webhook-proxy', 'autopod-db'}
            if any(c_name == s or c_name.startswith(f"{s}-") for s in internal_services):
                err = f"Container '{container_name}' is an internal service without a public web interface (HTTP 409 Routing Unavailable)."
            else:
                err = f"Public routing unavailable for container '{container_name}'. No verified public ingress route is configured (HTTP 409 Routing Unavailable)."
            return jsonify({
                "success": False, 
                "error": err,
                "status_code": 409
            }), 409
        
        # Return the verified public URL for the frontend to open
        return jsonify({
            "success": True, 
            "data": {
                "web_url": web_url,
                "message": f"Web interface available at: {web_url}"
            }
        })
    except Exception as e:
        logger.exception(f"Error opening web interface for {container_name}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/containers/<container_name>/download-image', methods=['POST', 'GET', 'OPTIONS'])
@require_admin_key
def api_download_container_image(container_name):
    """Export container's Docker image as a .tar archive via S3 and return a presigned download URL."""
    if request.method == 'OPTIONS':
        return '', 200

    try:
        if docker_mgr is None:
            return jsonify({"success": False, "error": "DockerManager not initialized"}), 500

        result = docker_mgr.export_container_image(container_name)
        if not result.get("success"):
            return jsonify({
                "success": False,
                "error": result.get("error", "Failed to export image")
            }), 400

        return jsonify({
            "success": True,
            "data": result
        }), 200
    except Exception as e:
        logger.exception(f"Error downloading image for {container_name}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/containers/<container_name>/network', methods=['GET'])
def api_container_network(container_name):
    """Get detailed network information for a container."""
    try:
        if docker_mgr is None:
            return jsonify({"success": False, "error": "DockerManager not initialized"}), 500
        
        network_data = docker_mgr.get_container_network_info(container_name)
        return jsonify({"success": True, "data": network_data})
    except Exception as e:
        logger.exception(f"Error getting network info for {container_name}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/containers/network/all', methods=['GET'])
def api_all_containers_network():
    """Get network information for all containers."""
    try:
        if docker_mgr is None:
            return jsonify({"success": False, "error": "DockerManager not initialized"}), 500
        
        containers = docker_mgr.get_containers()
        network_data = {}
        
        for container in containers:
            container_name = container.get('Names', ['unknown'])[0]
            try:
                network_data[container_name] = docker_mgr.get_container_network_info(container_name)
            except Exception as e:
                logger.warning(f"Could not get network info for {container_name}: {e}")
                network_data[container_name] = {
                    'ports': [],
                    'networks': [],
                    'ip_address': 'N/A',
                    'gateway': 'N/A',
                    'error': str(e)
                }
        
        logger.info(f"Fetched network info for {len(network_data)} containers")
        return jsonify({"success": True, "data": network_data})
    except Exception as e:
        logger.exception("Error getting all containers network info")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/network/stats', methods=['GET'])
def api_network_stats():
    """Get network statistics and summary."""
    try:
        if docker_mgr is None:
            return jsonify({"success": False, "error": "DockerManager not initialized"}), 500
        
        containers = docker_mgr.get_containers()
        total_containers = len(containers)
        containers_with_ports = 0
        total_ports = 0
        networks_list = set()
        
        for container in containers:
            container_name = container.get('Names', ['unknown'])[0]
            try:
                network_info = docker_mgr.get_container_network_info(container_name)
                if network_info.get('ports'):
                    containers_with_ports += 1
                    total_ports += len(network_info['ports'])
                if network_info.get('networks'):
                    networks_list.update(network_info['networks'])
            except:
                pass
        
        stats = {
            'total_containers': total_containers,
            'containers_with_ports': containers_with_ports,
            'total_ports_exposed': total_ports,
            'unique_networks': len(networks_list),
            'networks': list(networks_list)
        }
        
        logger.info("Fetched network statistics")
        return jsonify({"success": True, "data": stats})
    except Exception as e:
        logger.exception("Error getting network stats")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/logs/advanced', methods=['GET'])
@require_admin_key
def api_logs_advanced():
    """Advanced logging endpoint with filtering, searching, and pagination.
    Admin-key protected: log content can contain secrets, tokens, or
    command output that must not be publicly accessible."""
    try:
        # Get query parameters
        container_name = request.args.get('container', None)
        search_query = request.args.get('search', None)
        log_type = request.args.get('type', None)  # 'info', 'error', 'warning'
        limit = int(request.args.get('limit', 100))
        offset = int(request.args.get('offset', 0))
        
        logs = get_container_logs()
        
        # Filter by container name
        if container_name:
            logs = [log for log in logs if log.get('container_name') == container_name]
        
        # Filter by log type
        if log_type:
            logs = [log for log in logs if log_type.lower() in log.get('log', '').lower()]
        
        # Search in logs
        if search_query:
            logs = [log for log in logs if search_query.lower() in log.get('log', '').lower()]
        
        # Pagination
        total = len(logs)
        logs = logs[offset:offset + limit]
        
        logger.info(f"Advanced logs fetched: {len(logs)} entries from {total} total")
        return jsonify({
            "success": True,
            "data": logs,
            "total": total,
            "limit": limit,
            "offset": offset
        })
    except Exception as e:
        logger.exception("Error fetching advanced logs")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/logs/export', methods=['GET'])
@require_admin_key
def api_logs_export():
    """Export logs in JSON or CSV format.
    Admin-key protected: same sensitivity as /api/logs/advanced."""
    try:
        format_type = request.args.get('format', 'json')  # 'json' or 'csv'
        container_name = request.args.get('container', None)
        
        logs = get_container_logs()
        
        if container_name:
            logs = [log for log in logs if log.get('container_name') == container_name]
        
        if format_type == 'csv':
            import csv
            import io
            
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=['timestamp', 'container_name', 'log'])
            writer.writeheader()
            writer.writerows(logs)
            
            logger.info(f"Exported {len(logs)} logs as CSV")
            return output.getvalue(), 200, {
                'Content-Disposition': 'attachment; filename=logs.csv',
                'Content-Type': 'text/csv'
            }
        else:
            logger.info(f"Exported {len(logs)} logs as JSON")
            return jsonify({
                "success": True,
                "data": logs,
                "count": len(logs)
            })
    except Exception as e:
        logger.exception("Error exporting logs")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/logs/stream', methods=['GET'])
@require_admin_key
def api_logs_stream():
    """Stream logs in real-time (Server-Sent Events).
    Admin-key protected: same sensitivity as /api/logs/advanced.
    Note: SSE keeps the connection open; the admin key must be passed
    as a query parameter ?admin_key=... or X-Admin-Key header since
    EventSource API does not support custom headers in all browsers.
    auth.py's check_admin_key() already checks both locations."""
    try:
        def generate():
            last_count = 0
            while True:
                logs = get_container_logs()
                if len(logs) > last_count:
                    new_logs = logs[last_count:]
                    for log in new_logs:
                        yield f"data: {json.dumps(log)}\n\n"
                    last_count = len(logs)
                import time
                time.sleep(1)
        
        return generate(), 200, {
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive'
        }
    except Exception as e:
        logger.exception("Error streaming logs")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/images', methods=['GET'])
def api_get_images():
    """Get list of all local images."""
    try:
        if docker_mgr is None:
            return jsonify({"success": False, "error": "DockerManager not initialized"}), 500
        
        images = docker_mgr.get_all_images_info()
        logger.info(f"Fetched {len(images)} local images")
        return jsonify({"success": True, "data": images})
    except Exception as e:
        logger.exception("Error fetching images")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/images/search', methods=['GET'])
def api_search_images():
    """Search for images in registries."""
    try:
        query = request.args.get('q', '')
        limit = int(request.args.get('limit', 25))
        
        if not query:
            return jsonify({"success": False, "error": "Search query is required"}), 400
        
        if docker_mgr is None:
            return jsonify({"success": False, "error": "DockerManager not initialized"}), 500
        
        results = docker_mgr.search_images(query, limit)
        logger.info(f"Searched for images: {query} - Found {len(results)} results")
        return jsonify({"success": True, "data": results})
    except Exception as e:
        logger.exception("Error searching images")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/images/pull', methods=['POST', 'OPTIONS'])
@require_admin_key
def api_pull_image():
    """Pull an image from a registry."""
    if request.method == 'OPTIONS':
        return '', 200
        
    try:
        data = request.get_json()
        image_name = data.get('image_name')
        
        if not image_name:
            return jsonify({"success": False, "error": "Image name is required"}), 400
        
        if docker_mgr is None:
            return jsonify({"success": False, "error": "DockerManager not initialized"}), 500
        
        success = docker_mgr.pull_image(image_name)
        if success:
            logger.info(f"Image pulled successfully: {image_name}")
            return jsonify({"success": True, "message": f"Image {image_name} pulled successfully"})
        else:
            return jsonify({"success": False, "error": f"Failed to pull image {image_name}"}), 500
    except Exception as e:
        logger.exception("Error pulling image")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/images/push', methods=['POST', 'OPTIONS'])
@require_admin_key
def api_push_image():
    """Push an image to a registry."""
    if request.method == 'OPTIONS':
        return '', 200
        
    try:
        data = request.get_json()
        image_name = data.get('image_name')
        registry = data.get('registry', None)
        
        if not image_name:
            return jsonify({"success": False, "error": "Image name is required"}), 400
        
        if docker_mgr is None:
            return jsonify({"success": False, "error": "DockerManager not initialized"}), 500
        
        success = docker_mgr.push_image(image_name, registry)
        if success:
            logger.info(f"Image pushed successfully: {image_name}")
            return jsonify({"success": True, "message": f"Image {image_name} pushed successfully"})
        else:
            return jsonify({"success": False, "error": f"Failed to push image {image_name}"}), 500
    except Exception as e:
        logger.exception("Error pushing image")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/images/remove', methods=['POST', 'OPTIONS'])
@require_admin_key
def api_remove_image():
    """Remove a local image."""
    if request.method == 'OPTIONS':
        return '', 200
        
    try:
        data = request.get_json()
        image_name = data.get('image_name')
        
        if not image_name:
            return jsonify({"success": False, "error": "Image name is required"}), 400
        
        if docker_mgr is None:
            return jsonify({"success": False, "error": "DockerManager not initialized"}), 500
        
        success = docker_mgr.remove_image(image_name)
        if success:
            logger.info(f"Image removed successfully: {image_name}")
            return jsonify({"success": True, "message": f"Image {image_name} removed successfully"})
        else:
            return jsonify({"success": False, "error": f"Failed to remove image {image_name}"}), 500
    except Exception as e:
        logger.exception("Error removing image")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/images/<image_name>/details', methods=['GET'])
def api_image_details(image_name):
    """Get detailed information about an image."""
    try:
        if docker_mgr is None:
            return jsonify({"success": False, "error": "DockerManager not initialized"}), 500
        
        details = docker_mgr.get_image_details(image_name)
        if details:
            logger.info(f"Fetched details for image: {image_name}")
            return jsonify({"success": True, "data": details})
        else:
            return jsonify({"success": False, "error": f"Image {image_name} not found"}), 404
    except Exception as e:
        logger.exception(f"Error getting image details for {image_name}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/images/<image_name>/history', methods=['GET'])
def api_image_history(image_name):
    """Get the history/layers of an image."""
    try:
        if docker_mgr is None:
            return jsonify({"success": False, "error": "DockerManager not initialized"}), 500
        
        history = docker_mgr.get_image_history(image_name)
        logger.info(f"Fetched history for image: {image_name}")
        return jsonify({"success": True, "data": history})
    except Exception as e:
        logger.exception(f"Error getting image history for {image_name}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/images/tag', methods=['POST', 'OPTIONS'])
@require_admin_key
def api_tag_image():
    """Tag an image with a new name."""
    if request.method == 'OPTIONS':
        return '', 200
        
    try:
        data = request.get_json()
        source_image = data.get('source_image')
        target_image = data.get('target_image')
        
        if not source_image or not target_image:
            return jsonify({"success": False, "error": "Source and target image names are required"}), 400
        
        if docker_mgr is None:
            return jsonify({"success": False, "error": "DockerManager not initialized"}), 500
        
        success = docker_mgr.tag_image(source_image, target_image)
        if success:
            logger.info(f"Image tagged: {source_image} -> {target_image}")
            return jsonify({"success": True, "message": f"Image tagged successfully"})
        else:
            return jsonify({"success": False, "error": "Failed to tag image"}), 500
    except Exception as e:
        logger.exception("Error tagging image")
        return jsonify({"success": False, "error": str(e)}), 500

# Add missing method implementations to handle undefined methods
@app.route('/api/containers/<container_name>/resources', methods=['GET'])
def api_container_resources(container_name):
    """Get container resource usage and limits."""
    try:
        if docker_mgr is None:
            return jsonify({"success": False, "error": "DockerManager not initialized"}), 500
        
        # Create a simple resources response if method doesn't exist
        try:
            resources = docker_mgr.get_container_resources(container_name)
        except AttributeError:
            # Fallback if method doesn't exist
            resources = {
                'cpu_percent': '0%',
                'memory_usage': '0B',
                'memory_limit': 'N/A',
                'network_io': '0B / 0B',
                'block_io': '0B / 0B',
                'pids': '0',
                'restart_count': 0,
                'created_at': 'unknown',
                'container_name': container_name
            }
        
        return jsonify({"success": True, "data": resources})
    except Exception as e:
        logger.exception(f"Error getting resources for {container_name}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/debug/docker-health')
def debug_docker_health():
    """Debug endpoint to check Docker daemon connectivity.

    Previously this called `podman.health_check()`, a method that did not
    exist anywhere on the manager class, so this endpoint always threw
    AttributeError and returned 500 -- which in turn meant docker-compose's
    backend health check (which targeted this exact URL) could never pass.
    `DockerManager.health_check()` now genuinely exists (see docker_manager.py),
    so this endpoint is meaningful again. The old `/api/debug/podman-health`
    path is preserved as an alias for backward compatibility.
    """
    try:
        if docker_mgr is None:
            return jsonify({'error': 'DockerManager not initialized'}), 500
        health = docker_mgr.health_check()
        containers = docker_mgr.get_containers()
        status_code = 200 if health.get('docker_available') else 503
        return jsonify({
            'docker_health': health,
            'container_count': len(containers),
            'in_docker': os.path.exists('/.dockerenv'),
            'timestamp': datetime.now().isoformat()
        }), status_code
    except Exception as e:
        logger.exception("Error in docker health debug endpoint")
        return jsonify({'error': str(e)}), 500


@app.route('/api/debug/podman-health')
def debug_podman_health_alias():
    """Backward-compatible alias for the renamed health debug endpoint."""
    return debug_docker_health()


# ---------------------------------------------------------------------------
# Deployments / Versions / Rollback API (ARCHITECTURE.md Section 17)
#
# All of this is new -- the previous implementation had no concept of a
# deployment or version record anywhere in the database. GET endpoints are
# intentionally left unauthenticated (read-only, matches the rest of the
# monitoring API); the rollback endpoint is a state-changing action and is
# protected the same way container/image mutations are.
# ---------------------------------------------------------------------------

@app.route('/api/applications', methods=['GET', 'POST', 'OPTIONS'])
@app.route('/api/applications/', methods=['GET', 'POST', 'OPTIONS'])
def api_applications():
    """GET: list known applications. An application can get into this table
    two ways: explicit registration below, or (for synthetic/demo/admin
    "deploy from URL" payloads only) being auto-created on first deploy.

    POST (admin-key protected): explicitly register a new application with
    a repo_url, target_port and health_check_path. This is required before
    a real GitHub webhook push for that repo will be accepted -- see
    deployment_engine.deploy()'s allow_auto_register parameter.
    """
    if request.method == 'OPTIONS':
        return '', 200

    if request.method == 'GET':
        try:
            apps = database.list_applications()
            return jsonify({"success": True, "data": apps})
        except Exception as e:
            logger.exception("Error listing applications")
            return jsonify({"success": False, "error": str(e)}), 500

    # POST
    if not check_admin_key(request):
        return jsonify({"success": False, "error": "Unauthorized: missing or invalid X-Admin-Key"}), 401
    try:
        body = request.get_json(silent=True) or {}
        name = (body.get("name") or "").strip()
        repo_url = (body.get("repo_url") or "").strip() or None
        target_port = body.get("target_port")
        health_check_path = body.get("health_check_path") or "/"
        public_url = (body.get("public_url") or "").strip() or None
        framework = body.get("framework") or "auto-detect"
        build_config = body.get("build_config")
        env_vars = body.get("env_vars") or body.get("env_vars_json")
        routing_path = body.get("routing_path")
        health_status = body.get("health_status") or "unknown"
        lifecycle_status = body.get("lifecycle_status") or "active"
        target_node = body.get("target_node")
        container_name = body.get("container_name")
        image_tag = body.get("image_tag")

        if not name:
            return jsonify({"success": False, "error": "'name' is required"}), 400

        application = database.register_application(
            name=name, repo_url=repo_url,
            target_port=target_port, health_check_path=health_check_path,
            public_url=public_url, framework=framework,
            build_config=build_config, env_vars=env_vars,
            routing_path=routing_path, health_status=health_status,
            lifecycle_status=lifecycle_status, target_node=target_node,
            container_name=container_name, image_tag=image_tag
        )
        return jsonify({"success": True, "data": application}), 201
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 409
    except Exception as e:
        logger.exception("Error registering application")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/applications/<int:application_id>', methods=['GET', 'PATCH', 'DELETE', 'OPTIONS'])
def api_application_detail(application_id):
    """Detail view, update, and deletion of a single registered application."""
    if request.method == 'OPTIONS':
        return '', 200

    if request.method == 'GET':
        try:
            application = database.get_application(application_id)
            if not application:
                return jsonify({"success": False, "error": "Application not found"}), 404
            return jsonify({"success": True, "data": application})
        except Exception as e:
            logger.exception(f"Error getting application {application_id}")
            return jsonify({"success": False, "error": str(e)}), 500

    # DELETE (admin-key protected)
    if request.method == 'DELETE':
        if not check_admin_key(request):
            return jsonify({"success": False, "error": "Unauthorized: missing or invalid X-Admin-Key"}), 401
        try:
            deleted = database.delete_application(application_id)
            if not deleted:
                return jsonify({"success": False, "error": "Application not found"}), 404
            return jsonify({"success": True, "message": f"Application {application_id} deleted successfully"})
        except Exception as e:
            logger.exception(f"Error deleting application {application_id}")
            return jsonify({"success": False, "error": str(e)}), 500

    # PATCH (admin-key protected)
    if not check_admin_key(request):
        return jsonify({"success": False, "error": "Unauthorized: missing or invalid X-Admin-Key"}), 401
    try:
        body = request.get_json(silent=True) or {}
        updated = database.update_application(
            application_id,
            name=body.get("name"),
            repo_url=body.get("repo_url"),
            target_port=body.get("target_port"),
            health_check_path=body.get("health_check_path"),
            public_url=body.get("public_url"),
            framework=body.get("framework"),
            build_config=body.get("build_config"),
            env_vars=body.get("env_vars") or body.get("env_vars_json"),
            routing_path=body.get("routing_path"),
            health_status=body.get("health_status"),
            lifecycle_status=body.get("lifecycle_status"),
            target_node=body.get("target_node"),
            container_name=body.get("container_name"),
            image_tag=body.get("image_tag")
        )
        if not updated:
            return jsonify({"success": False, "error": "Application not found"}), 404
        return jsonify({"success": True, "data": updated})
    except Exception as e:
        logger.exception(f"Error updating application {application_id}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/app-nodes', methods=['GET'])
def api_list_app_nodes():
    """List known Auto-Scaling-managed app nodes (ARCHITECTURE.md Section
    6.3). Populated by POST /api/app-nodes/sync; on a fresh install (or if
    that's never been called) this returns an empty list."""
    try:
        status = request.args.get('status')
        nodes = database.list_app_nodes(status=status)
        return jsonify({"success": True, "data": nodes})
    except Exception as e:
        logger.exception("Error listing app nodes")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/app-nodes/sync', methods=['POST', 'OPTIONS'])
@require_admin_key
def api_sync_app_nodes():
    """Poll the AWS Auto Scaling Group (aws_nodes.sync_app_nodes_from_asg)
    and reconcile `app_nodes` to match. Read-only against AWS (describe-*
    calls only). Requires boto3 installed and AWS credentials configured
    in this process's environment (standard boto3 credential chain --
    never pass credentials in the request body). If either is missing,
    this reports the real error rather than a fake empty success."""
    if request.method == 'OPTIONS':
        return '', 200
    try:
        body = request.get_json(silent=True) or {}
        summary = aws_nodes.sync_app_nodes_from_asg(
            asg_name=body.get('asg_name'),
            ip_attribute=body.get('ip_attribute'),
        )
        return jsonify({"success": True, "data": summary})
    except RuntimeError as e:
        # boto3 not installed -- a clear, actionable 501, not a 500.
        return jsonify({"success": False, "error": str(e)}), 501
    except Exception as e:
        logger.exception("Error syncing app nodes from AWS")
        return jsonify({"success": False, "error": str(e)}), 502


@app.route('/api/deployments', methods=['GET'])
def api_list_deployments():
    """List deployment history, optionally filtered by application_id."""
    try:
        application_id = request.args.get('application_id', type=int)
        limit = request.args.get('limit', default=50, type=int)
        offset = request.args.get('offset', default=0, type=int)
        deployments = database.list_deployments(application_id=application_id, limit=limit, offset=offset)
        return jsonify({"success": True, "data": deployments})
    except Exception as e:
        logger.exception("Error listing deployments")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/deployments', methods=['POST', 'OPTIONS'])
@require_admin_key
def api_create_deployment():
    """Manual deployment triggered by an authenticated administrator (e.g. from the dashboard Quick Deploy form).
    Protected by @require_admin_key (X-Admin-Key header).
    Accepts:
      - {"repository": {"clone_url": "...", "name": "..."}}
      - {"repo_url": "...", "name": "..."}
      - {"test": True, "name": "..."}
    """
    if request.method == 'OPTIONS':
        return '', 200

    try:
        if docker_mgr is None:
            logger.error("DockerManager not initialized — cannot process deployment")
            return jsonify({"status": "error", "message": "DockerManager not initialized"}), 500

        data = request.get_json(silent=True)
        if not data:
            logger.warning("Manual deployment received with no JSON body")
            return jsonify({"status": "error", "message": "No JSON data received"}), 400

        # Normalization: if top-level repo_url was passed, wrap it in repository object
        if 'repository' not in data and 'repo_url' in data:
            data['repository'] = {
                'clone_url': data['repo_url'],
                'name': data.get('name') or data.get('app_name', 'webhook-app')
            }

        logger.info("Manual deployment requested via API")
        result = handle_webhook(docker_mgr, data, allow_auto_register=True, triggered_by="dashboard")
        logger.info("Manual deployment completed: status=%s", result.get("status"))

        result_status = result.get("status", "success")
        if result_status in ("success", "ignored", "skipped"):
            status_code = 200
        elif result_status == "rejected":
            status_code = 404
        else:
            status_code = 400

        return jsonify({
            "status": result_status,
            "message": result.get("message", "Deployment processed"),
            "data": result
        }), status_code

    except Exception as e:
        logger.exception("Manual deployment error")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/deployments/<int:deployment_id>', methods=['GET'])
def api_get_deployment(deployment_id):
    """Detail view of a single deployment attempt."""
    try:
        deployment = database.get_deployment(deployment_id)
        if not deployment:
            return jsonify({"success": False, "error": "Deployment not found"}), 404
        return jsonify({"success": True, "data": deployment})
    except Exception as e:
        logger.exception(f"Error getting deployment {deployment_id}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/versions', methods=['GET'])
def api_list_versions():
    """List stored versions for an application (required query param:
    application_id) -- these are the images available to roll back to."""
    try:
        application_id = request.args.get('application_id', type=int)
        if not application_id:
            apps = database.list_applications()
            if not apps:
                return jsonify({"success": True, "data": []})
            application_id = apps[0]['id']  # single-application scope (ARCHITECTURE.md Section 4)
        limit = request.args.get('limit', default=50, type=int)
        versions = version_manager.list_versions(application_id, limit=limit)
        current = version_manager.get_current_version(application_id)
        current_id = current['id'] if current else None
        for v in versions:
            v['is_current'] = bool(v.get('is_current')) or (v['id'] == current_id)
        if request.args.get('current', '').lower() in ('1', 'true', 'yes'):
            # Used by the ASG user-data bootstrap: a newly-scaled node must
            # run the *current* version (which after a rollback is not the
            # newest one), so return only that.
            versions = [v for v in versions if v['is_current']]
        body = {"success": True, "data": versions, "application_id": application_id}
        application = database.get_application(application_id)
        if application:
            # Everything a freshly-scaled node needs to start the app exactly
            # as node_deployer.py would (same container name and port), so a
            # later rolling deploy replaces it instead of colliding with it.
            body["container_name"] = _container_name_for_application(application)
            body["target_port"] = application.get("target_port")
            body["health_check_path"] = application.get("health_check_path")
        return jsonify(body)
    except Exception as e:
        logger.exception("Error listing versions")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/applications/current-versions', methods=['GET'])
def api_applications_current_versions():
    """List current active versions for all registered applications.
    Used by the ASG multi-application bootstrap process so scale-out instances
    pull and run all active registered applications on their designated ports."""
    try:
        apps = database.list_applications()
        result = []
        for app_item in apps:
            app_id = app_item["id"]
            current = version_manager.get_current_version(app_id)
            if current:
                result.append({
                    "application_id": app_id,
                    "application_name": app_item.get("name"),
                    "container_name": _container_name_for_application(app_item),
                    "target_port": app_item.get("target_port") or 8081,
                    "health_check_path": app_item.get("health_check_path") or "/",
                    "image_tag": current.get("image_tag"),
                    "version_id": current.get("id"),
                })
        return jsonify({"success": True, "data": result})
    except Exception as e:
        logger.exception("Error listing current versions across applications")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/deployments/rollback', methods=['POST', 'OPTIONS'])
@require_admin_key
def api_rollback():
    """Roll back the current application to a previously-recorded version.

    Body: {"version_id": <int>} or {"application_id": <int>, "version_id": <int>}
    """
    if request.method == 'OPTIONS':
        return '', 200
    try:
        if docker_mgr is None:
            return jsonify({"success": False, "error": "DockerManager not initialized"}), 500

        body = request.get_json(silent=True) or {}
        version_id = body.get('version_id')
        if not version_id:
            return jsonify({"success": False, "error": "version_id is required"}), 400

        version = version_manager.get_version(version_id)
        if not version:
            return jsonify({"success": False, "error": f"Version {version_id} not found"}), 404

        application_id = body.get('application_id') or version['application_id']

        result = deployment_engine.rollback(docker_mgr, application_id, version_id, triggered_by="dashboard_rollback")

        status_code = 200 if result.get("status") == "success" else 400
        return jsonify({
            "success": result.get("status") == "success",
            "deployment": result
        }), status_code
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    except Exception as e:
        logger.exception("Error during rollback")
        return jsonify({"success": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# Multi-node rolling deployment / rollback over SSH (ARCHITECTURE.md
# Section 8/14). Deploys an ALREADY-BUILT image (identified by a stored
# version_id -- see /api/versions) to every in-service app node, one at a
# time. Unlike the single-node deploy/rollback above (which builds from a
# webhook payload and runs locally), this only ever redeploys something
# already recorded -- a node pulls from the registry, so the image must
# have been pushed there (DOCKER_USERNAME configured) for this to work
# against real nodes.
# ---------------------------------------------------------------------------

def _container_name_for_application(application):
    repo_name = application["name"]
    image_name = f"autopod-{repo_name.lower().replace(' ', '-')}"
    return f"{image_name}-container"


def _run_rolling(application_id, version_id, triggered_by, is_rollback):
    application = database.get_application(application_id)
    if not application:
        return jsonify({"success": False, "error": f"Application {application_id} not found"}), 404

    version = version_manager.get_version(version_id)
    if not version or version["application_id"] != application_id:
        return jsonify({"success": False, "error": f"Version {version_id} not found for application {application_id}"}), 404

    target_port = application.get("target_port")
    if not target_port:
        return jsonify({
            "success": False,
            "error": "This application has no target_port registered. Set one via POST /api/applications "
                     "(or re-register it) before rolling out to multiple nodes -- every node needs to know "
                     "which port to publish the container on."
        }), 400

    deployer = node_deployer.NodeDeployer()
    result = deployer.rolling_deploy(
        application_id=application_id,
        image_tag=version["image_tag"],
        container_name=_container_name_for_application(application),
        target_port=int(target_port),
        health_check_path=application.get("health_check_path"),
        version_id=version_id,
        triggered_by=triggered_by,
        is_rollback=is_rollback,
    )

    status_code = {"success": 200, "partial_failure": 207}.get(result["status"], 400)
    body = {"success": result["status"] == "success", "data": result}
    if status_code == 400:
        # Surface the reason at the top level so API clients (the dashboard)
        # show it instead of a generic "Server error".
        body["error"] = result.get("message") or "Rolling deployment failed on the first node."
    return jsonify(body), status_code


@app.route('/api/deployments/rolling', methods=['POST', 'OPTIONS'])
@require_admin_key
def api_rolling_deploy():
    """Roll out an already-built, already-recorded version to every
    in-service app node. Body: {"application_id": <int>, "version_id": <int>}.
    Populate app_nodes first via POST /api/app-nodes/sync."""
    if request.method == 'OPTIONS':
        return '', 200
    try:
        body = request.get_json(silent=True) or {}
        application_id = body.get('application_id')
        version_id = body.get('version_id')
        if not application_id or not version_id:
            return jsonify({"success": False, "error": "application_id and version_id are required"}), 400
        return _run_rolling(application_id, version_id, triggered_by="rolling_deploy_api", is_rollback=False)
    except Exception as e:
        logger.exception("Error during rolling deployment")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/deployments/rolling-rollback', methods=['POST', 'OPTIONS'])
@require_admin_key
def api_rolling_rollback():
    """Roll EVERY in-service app node back to a previously-recorded
    version, one node at a time, stopping at the first failure. Body:
    {"application_id": <int>, "version_id": <int>}."""
    if request.method == 'OPTIONS':
        return '', 200
    try:
        body = request.get_json(silent=True) or {}
        application_id = body.get('application_id')
        version_id = body.get('version_id')
        if not application_id or not version_id:
            return jsonify({"success": False, "error": "application_id and version_id are required"}), 400
        return _run_rolling(application_id, version_id, triggered_by="rolling_rollback_api", is_rollback=True)
    except Exception as e:
        logger.exception("Error during rolling rollback")
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == '__main__':
    # Perform initial sync when starting the application
    if docker_mgr:
        try:
            logger.info("Performing initial sync on startup...")
            docker_mgr.sync_with_db()
            logger.info("Initial sync completed on startup")
        except Exception as e:
            logger.error(f"Initial sync failed: {e}")
    
    # DEBUG_MODE must be explicitly opted into for local development.
    # Previously this was hardcoded `debug=True`, which also enables
    # Werkzeug's interactive debugger -- a serious remote-code-execution
    # risk if this process is ever reachable from outside the developer's
    # own machine (e.g. once deployed to EC2). For any non-local deployment,
    # run via Gunicorn instead (see Dockerfile / README.md), which never
    # hits this `__main__` block at all.
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() == "true"

    logger.info("AutoPod Cloud starting — Server URL: http://0.0.0.0:5000")
    logger.info("Debug mode: %s", 'ON (development only!)' if debug_mode else 'OFF')
    logger.info("CORS allowed origins: %s", ALLOWED_ORIGINS)

    app.run(host='0.0.0.0', port=5000, debug=debug_mode)