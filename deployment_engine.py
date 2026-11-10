"""
deployment_engine.py

The real build -> tag -> push -> deploy -> health-check -> record pipeline
for AutoPod Cloud (ARCHITECTURE.md Sections 9/10).

Previously this entire sequence lived, incompletely, inside
webhook_handler.handle_webhook(): it built an image and ran a new container
in its place, but never tagged a version, never pushed to a registry, never
health-checked the result, and -- critically -- stopped the OLD container
before confirming the NEW one worked, which meant a bad deploy could leave
nothing running at all. This module fixes all of that and is now the single
place that knows how to perform (or roll back) a deployment.

Failure-safety contract (ARCHITECTURE.md Section 19 / Section 13):
    - The previously-running container is never touched until the new one
      has been started AND confirmed healthy.
    - If the new container never becomes healthy, the old one is left
      exactly as it was; the failed attempt is still recorded.
"""

import os
import shutil
import tempfile
import subprocess
import time
import logging

import database
import version_manager
import tech_detector
from dockerfile_synthesizer import DockerfileSynthesizer
from ingress_manager import IngressManager
from webhook_handler import (
    extract_repo_name_from_url,
    clone_repository,
    create_demo_project,
    find_available_port,
    validate_commit_sha,
    validate_git_ref,
)

logger = logging.getLogger('autopod')

HEALTH_CHECK_RETRIES = int(os.environ.get("HEALTH_CHECK_RETRIES", "8"))
HEALTH_CHECK_DELAY_SECONDS = float(os.environ.get("HEALTH_CHECK_DELAY_SECONDS", "1.5"))

DOCKER_REGISTRY = os.environ.get("DOCKER_REGISTRY", "docker.io")
DOCKER_USERNAME = os.environ.get("DOCKER_USERNAME")


def _get_git_commit(repo_path):
    """Best-effort short commit hash for the cloned repo. Returns None if
    unavailable (e.g. a generated demo project with no git history)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_path, capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _wait_for_health(docker_mgr, container_name, port=None, health_check_path=None):
    """Poll the container's health a few times before giving up. Returns
    True the moment it looks healthy; False if it never does.

    Two layers are checked, both must pass:
      1. Docker-level: the container exists and is running/healthy
         (`is_container_healthy`).
      2. Application-level (only if `port` and `health_check_path` are
         both given, i.e. the application being deployed is registered
         with a `health_check_path` -- ARCHITECTURE.md Section 6.3): an
         HTTP GET against that path must not fail/5xx
         (`http_health_check`). This catches a container that Docker
         reports as "running" but whose application process never
         actually came up or is crash-looping behind something that
         keeps the container alive.
    A demo/unregistered-path deployment (no health_check_path) falls back
    to layer 1 only, preserving the previous behavior exactly.
    """
    for attempt in range(HEALTH_CHECK_RETRIES):
        container_ok = docker_mgr.container_exists(container_name) and docker_mgr.is_container_healthy(container_name)
        if container_ok:
            if port and health_check_path:
                if docker_mgr.http_health_check(port, health_check_path):
                    return True
                # Container is up, but the app isn't answering on its health
                # path yet -- keep polling instead of failing immediately.
            else:
                return True
        time.sleep(HEALTH_CHECK_DELAY_SECONDS)
    return False


def _fail(deployment_id, message, version_id=None):
    database.update_deployment(deployment_id, status="failed", version_id=version_id, message=message, finished=True)
    logger.error(f"Deployment {deployment_id} failed: {message}")
    return {"status": "error", "message": message, "deployment_id": deployment_id}


def _succeed(deployment_id, version_id, container_name, image_tag, port, repo_name, application_id=None):
    database.update_deployment(deployment_id, status="success", version_id=version_id, finished=True)
    pub_url = None
    routing_path = "/"
    framework = "auto-detect"
    if application_id:
        try:
            ingress_mgr = IngressManager()
            ingress_mgr.apply_config(database.list_applications())
        except Exception:
            pass

        app_row = database.get_application(application_id)
        if app_row:
            routing_path = app_row.get("routing_path") or "/"
            framework = app_row.get("framework") or "auto-detect"
            if app_row.get("public_url"):
                pub_url = app_row["public_url"]
            elif os.environ.get("AUTOPOD_ALB_URL"):
                alb_url = os.environ.get("AUTOPOD_ALB_URL", "").strip()
                if alb_url and "localhost" not in alb_url.lower() and "127.0.0.1" not in alb_url:
                    if routing_path == "/":
                        pub_url = f"{alb_url.rstrip('/')}/"
                    else:
                        clean_sub = routing_path.strip("/")
                        pub_url = f"{alb_url.rstrip('/')}/{clean_sub}/"
                    database.update_application(application_id, public_url=pub_url)
    if not pub_url:
        pub_url = database.resolve_container_public_url(container_name)
    if pub_url and ("localhost" in pub_url.lower() or "127.0.0.1" in pub_url):
        pub_url = None
    return {
        "status": "success",
        "message": f"Container {container_name} deployed successfully on port {port}",
        "container_name": container_name,
        "image_name": image_tag,
        "port": port,
        "public_url": pub_url,
        "routing_path": routing_path,
        "framework": framework,
        "repo_name": repo_name,
        "deployment_id": deployment_id,
        "version_id": version_id,
    }


def deploy(docker_mgr, payload, triggered_by="webhook", allow_auto_register=True, node_deployer_instance=None):
    """Standard (non-rollback) deployment: clone -> build -> tag -> push ->
    deploy -> health check -> record.

    `allow_auto_register` controls what happens when the payload's repo_url
    does not match any already-registered application:
      - True  (default; used for synthetic/demo payloads and authenticated
        admin-triggered "deploy from URL" requests): the application is
        created on the fly, same as before.
      - False (used for real GitHub webhook pushes authenticated only by
        HMAC signature, not an admin key): the deployment is REJECTED
        before any cloning/building happens, and no deployment row is
        created. This is the multi-application isolation boundary --
        ARCHITECTURE.md Section 8 requires that a push to a repo nobody
        registered cannot trigger a build on this host. Register the repo
        first via POST /api/applications.
    """
    repo_url = None
    repo_name = "webhook-app"

    if payload and 'repository' in payload:
        repo_url = payload['repository'].get('clone_url') or payload['repository'].get('html_url')
        repo_name = payload['repository'].get('name', 'webhook-app')
    elif payload and 'test' in payload:
        repo_name = payload.get('name', 'test-app')
    elif payload and 'repo_url' in payload:
        repo_url = payload.get('repo_url')
        repo_name = payload.get('name') or payload.get('container_name') or 'manual-app'
    else:
        raise ValueError("Invalid deployment payload: missing 'repository' or 'test'")

    if repo_url:
        extracted = extract_repo_name_from_url(repo_url)
        if extracted and extracted != "webhook-app":
            repo_name = extracted

    # Extract branch / ref from push event
    raw_ref = payload.get('ref') if payload else None
    branch = None
    if raw_ref and isinstance(raw_ref, str):
        cleaned_ref = validate_git_ref(raw_ref)
        if cleaned_ref is None:
            logger.warning("Malformed git ref received in payload: %s", raw_ref)
            return {
                "status": "error",
                "message": f"Malformed or invalid git ref: '{raw_ref}'",
            }
        branch = cleaned_ref
    elif payload and payload.get('branch'):
        branch = validate_git_ref(payload.get('branch'))
        if branch is None and payload.get('branch'):
            return {
                "status": "error",
                "message": f"Malformed or invalid branch: '{payload.get('branch')}'",
            }

    # For webhook triggers, only deploy the deployment branch ('main')
    if triggered_by == "webhook" and branch and branch != "main":
        logger.info("Ignoring webhook push to non-deployment branch '%s' for repo '%s'", branch, repo_url)
        return {
            "status": "ignored",
            "message": f"Push event on branch '{branch}' ignored. Only 'main' triggers automatic deployment.",
            "branch": branch
        }

    # Extract commit SHA from push event
    commit_sha = None
    raw_sha = None
    if payload:
        raw_sha = payload.get('after')
        if not raw_sha or raw_sha == '0' * len(raw_sha):
            head_commit = payload.get('head_commit')
            if isinstance(head_commit, dict):
                raw_sha = head_commit.get('id')
        if not raw_sha:
            raw_sha = payload.get('commit_sha') or payload.get('commit')

    if raw_sha:
        commit_sha = validate_commit_sha(raw_sha)
        if commit_sha is None:
            logger.warning("Malformed commit SHA received in payload: %s", raw_sha)
            return {
                "status": "error",
                "message": f"Malformed or invalid commit SHA: '{raw_sha}'",
            }

    application = None
    if repo_url:
        application = database.find_application_by_repo_url(repo_url)
        if application is None:
            if not allow_auto_register:
                logger.warning(f"Rejected webhook push for unregistered repository: {repo_url}")
                return {
                    "status": "rejected",
                    "message": (
                        f"Repository '{repo_url}' is not registered as an AutoPod application. "
                        "Register it first via POST /api/applications before pushing."
                    ),
                }
            application = database.get_or_create_application(repo_name, repo_url)
    else:
        # No repo_url at all (synthetic/demo/test payload) -- always allowed,
        # there is nothing a registration check could meaningfully reject.
        application = database.get_or_create_application(repo_name, repo_url)

    application_id = application["id"]
    # Use the registered application's own name for image/container naming
    # so a webhook push matched to an existing app doesn't drift from it.
    repo_name = application["name"]
    image_name = f"autopod-{repo_name.lower().replace(' ', '-')}"
    container_name = f"{image_name}-container"
    preferred_port = None
    if application.get("target_port"):
        try:
            preferred_port = int(application["target_port"])
        except (TypeError, ValueError):
            preferred_port = None

    # Check payload overrides
    if not preferred_port and payload and ('target_port' in payload or 'port' in payload):
        try:
            preferred_port = int(payload.get('target_port') or payload.get('port'))
            database.update_application(application_id, target_port=str(preferred_port))
        except (TypeError, ValueError):
            pass

    # If still not set, automatically allocate next worker target port (8081-8099)
    if preferred_port is None:
        allocated = database.allocate_next_target_port()
        if allocated:
            preferred_port = allocated
            database.update_application(application_id, target_port=str(allocated))
            logger.info("Allocated worker target port %d for application %s", allocated, repo_name)
    health_check_path = application.get("health_check_path")

    deployment = database.create_deployment(
        application_id=application_id, status="in_progress",
        is_rollback=False, triggered_by=triggered_by
    )
    deployment_id = deployment["id"]

    temp_dir = tempfile.mkdtemp(prefix="autopod_")
    repo_path = os.path.join(temp_dir, repo_name)

    detected_framework = "dockerfile"
    current_routing_path = (application.get("routing_path") if application else None) or "/"

    try:
        is_real_repo = bool(
            repo_url and (
                "github.com" in repo_url or
                repo_url.startswith("git@") or
                repo_url.endswith(".git")
            )
        )
        if is_real_repo:
            clone_ok = clone_repository(repo_url, repo_path, branch=branch, commit_sha=commit_sha)
            if not clone_ok:
                err_msg = f"Failed to checkout revision (branch={branch}, commit={commit_sha}) from '{repo_url}'. Previous version was left running untouched."
                return _fail(deployment_id, err_msg)

            # P1: Universal Framework Detection & Smart Dockerfile Synthesis
            import json as _json
            app_build_cfg = {}
            if application and application.get("build_config"):
                try:
                    raw_cfg = application["build_config"]
                    app_build_cfg = _json.loads(raw_cfg) if isinstance(raw_cfg, str) else raw_cfg
                except Exception:
                    pass

            tech_spec = tech_detector.detect_technology(repo_path, config_override=app_build_cfg)
            if not tech_spec.supported:
                err_msg = tech_spec.error_message or f"Unsupported application archetype in '{repo_name}'"
                logger.error(err_msg)
                return _fail(deployment_id, err_msg)

            # Determine routing path
            if not current_routing_path or current_routing_path == "/":
                app_n = (application.get("name") if application else None) or repo_name
                if app_n.lower() != "sweet-treats-bakery":
                    current_routing_path = database.allocate_next_routing_path(app_n)
                else:
                    current_routing_path = "/"

            synth_ok, _bdir, synth_err = DockerfileSynthesizer.synthesize(
                repo_path,
                tech_spec,
                target_port=preferred_port,
                routing_path=current_routing_path
            )
            if not synth_ok:
                err_msg = f"Build configuration error: {synth_err}"
                logger.error(err_msg)
                return _fail(deployment_id, err_msg)

            detected_framework = tech_spec.framework
            if not health_check_path or health_check_path == "/":
                health_check_path = tech_spec.health_check_path or "/"

            database.update_application(
                application_id,
                framework=detected_framework,
                health_check_path=health_check_path,
                routing_path=current_routing_path
            )
        else:
            # Synthetic / test mode without a real git repository URL
            create_demo_project(repo_path, repo_name)
            detected_framework = "static"
            current_routing_path = (application.get("routing_path") if application else None) or "/"

        git_commit = _get_git_commit(repo_path)
        if not git_commit and commit_sha:
            git_commit = commit_sha[:7]

        image_tag = version_manager.generate_tag(image_name, git_commit)

        if not docker_mgr.build_image(repo_path, image_tag):
            return _fail(deployment_id, f"Docker build failed for {image_tag}")

        recorded_image_tag = image_tag

        docker_registry = os.environ.get("DOCKER_REGISTRY", DOCKER_REGISTRY)
        docker_username = os.environ.get("DOCKER_USERNAME", DOCKER_USERNAME)
        is_ecr = bool(docker_registry and ".dkr.ecr." in docker_registry)

        # P0.1: Enforce registry authentication and fail-fast image push
        if is_ecr:
            clean_name = image_tag.replace("localhost/", "")
            target_ecr_tag = f"{docker_registry}/{clean_name}"
            repo_part = clean_name.split(':')[0]
            if hasattr(docker_mgr, "ensure_ecr_repository"):
                if not docker_mgr.ensure_ecr_repository(repo_part):
                    ecr_err = getattr(docker_mgr, "_last_ecr_error", None) or f"Failed to prepare ECR repository '{repo_part}'"
                    logger.error(f"ECR repository preparation failed: {ecr_err}")
                    database.update_deployment(deployment_id, message=f"ECR Error: {ecr_err}")
                    return _fail(deployment_id, f"ECR Repository Error: {ecr_err}")
            if hasattr(docker_mgr, "login_ecr"):
                if not docker_mgr.login_ecr(registry=docker_registry):
                    login_err = getattr(docker_mgr, "_last_ecr_error", None) or "Failed to authenticate with Amazon ECR"
                    logger.error(f"ECR login failed: {login_err}")
                    database.update_deployment(deployment_id, message=f"ECR Login Error: {login_err}")
                    return _fail(deployment_id, f"ECR Authentication Error: {login_err}")
            if docker_mgr.push_image(image_tag, registry=docker_registry):
                recorded_image_tag = target_ecr_tag
                database.update_deployment(deployment_id, message=f"Pushed to ECR: {target_ecr_tag}")
            else:
                push_err = getattr(docker_mgr, "_last_push_error", None) or f"Failed to push image to ECR registry ({target_ecr_tag})"
                logger.error(push_err)
                database.update_deployment(deployment_id, message=f"Error: {push_err}")
                return _fail(deployment_id, f"ECR Push Failed: {push_err}")
        elif docker_username:
            if docker_mgr.push_image(image_tag, registry=docker_registry, username=docker_username):
                recorded_image_tag = f"{docker_registry}/{image_tag}" if docker_registry != "docker.io" else image_tag
                database.update_deployment(deployment_id, message=f"Pushed to registry: {recorded_image_tag}")
            else:
                push_err = getattr(docker_mgr, "_last_push_error", None) or "Registry push failed"
                logger.error(push_err)
                database.update_deployment(deployment_id, message=f"Error: {push_err}")
                return _fail(deployment_id, f"Registry Push Failed: {push_err}")
        else:
            database.update_deployment(deployment_id, message="DOCKER_REGISTRY not configured; registry push skipped")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    version = version_manager.record_version(application_id, recorded_image_tag, git_commit, framework=detected_framework)
    version_id = version["id"]

    # P0.2: Enforce worker-only rollout -- eliminate control-plane container fallback
    in_service_nodes = database.list_app_nodes(status="in_service")
    allow_local_fallback = os.environ.get("AUTOPOD_ALLOW_LOCAL_FALLBACK", "false").lower() in ("true", "1")

    if not in_service_nodes:
        if not allow_local_fallback:
            fail_msg = (
                "Worker fleet unavailable: No application worker nodes are currently registered and in_service. "
                "AutoPod requires at least one healthy worker node to deploy user workloads. "
                "Control-plane local deployment fallback has been disabled."
            )
            logger.error(fail_msg)
            database.update_deployment(
                deployment_id,
                status="failed",
                version_id=version_id,
                message=fail_msg,
                finished=True,
            )
            return _fail(deployment_id, fail_msg, version_id=version_id)
        else:
            logger.warning("AUTOPOD_ALLOW_LOCAL_FALLBACK enabled: Deploying locally on control plane (non-production test mode)")
            return _deploy_image(
                docker_mgr, application_id, deployment_id, version_id,
                image_tag, container_name, repo_name,
                preferred_port=preferred_port, health_check_path=health_check_path
            )

    if not preferred_port:
        return _fail(deployment_id, "No worker target_port could be allocated for worker fleet rollout", version_id=version_id)

    if node_deployer_instance is None:
        import node_deployer
        deployer = node_deployer.NodeDeployer()
    else:
        deployer = node_deployer_instance

    rolling_kwargs = {
        "application_id": application_id,
        "image_tag": recorded_image_tag,
        "container_name": container_name,
        "target_port": preferred_port,
        "health_check_path": health_check_path,
        "version_id": version_id,
        "triggered_by": triggered_by,
        "is_rollback": False,
        "nodes": in_service_nodes,
    }
    try:
        rolling_res = deployer.rolling_deploy(**rolling_kwargs, existing_deployment_id=deployment_id)
    except TypeError:
        rolling_res = deployer.rolling_deploy(**rolling_kwargs)
    if rolling_res["status"] == "success":
        version_manager.set_current(application_id, version_id)

        # Compute public URL from ALB URL and routing path
        alb_url = os.environ.get("AUTOPOD_ALB_URL", "").strip() or "http://autopod-alb-513586547.us-east-1.elb.amazonaws.com"
        if alb_url and "localhost" not in alb_url.lower() and "127.0.0.1" not in alb_url:
            if current_routing_path == "/":
                pub_url = f"{alb_url.rstrip('/')}/"
            else:
                clean_sub = current_routing_path.strip("/")
                pub_url = f"{alb_url.rstrip('/')}/{clean_sub}/"
        else:
            app_record = database.get_application(application_id)
            pub_url = (app_record.get("public_url") if app_record else None) or database.resolve_container_public_url(container_name)

        # P0.3: Ensure public URL is never localhost or 127.0.0.1
        if pub_url and ("localhost" in pub_url.lower() or "127.0.0.1" in pub_url):
            pub_url = None

        database.update_application(
            application_id,
            public_url=pub_url,
            health_status="healthy",
            lifecycle_status="active",
            target_port=str(preferred_port),
            routing_path=current_routing_path,
            container_name=container_name,
            image_tag=recorded_image_tag,
            framework=detected_framework
        )

        # Synchronize Worker Ingress Gateway dynamically AFTER database is updated with active status and port
        try:
            ingress_mgr = IngressManager()
            all_apps = database.list_applications()
            ingress_mgr.apply_config(all_apps)
            for node in in_service_nodes:
                target = node.get("instance_id") or node.get("ip_address") or node.get("private_ip")
                if target and hasattr(deployer, "runner"):
                    ingress_mgr.sync_to_worker(deployer.runner, target, all_apps)
        except Exception as e:
            logger.warning(f"Worker ingress gateway synchronization failed: {e}")

        database.update_deployment(
            deployment_id,
            status="success",
            version_id=version_id,
            message=rolling_res.get("message") or f"Successfully deployed {container_name} to {len(in_service_nodes)} node(s)",
            finished=True,
            container_name=container_name,
            port=preferred_port,
            image_tag=recorded_image_tag
        )

        return {
            "status": "success",
            "message": rolling_res.get("message") or f"Successfully deployed {container_name}",
            "deployment_id": deployment_id,
            "version_id": version_id,
            "port": preferred_port,
            "public_url": pub_url,
            "routing_path": current_routing_path,
            "framework": detected_framework,
            "container_name": container_name,
            "image_tag": recorded_image_tag,
            "rolling_results": rolling_res.get("results", []),
        }
    else:
        fail_msg = rolling_res.get("message") or "Rolling deployment to worker nodes failed"
        logger.error(f"Worker rollout failed for deployment {deployment_id}: {fail_msg}")
        database.update_deployment(
            deployment_id,
            status="failed",
            version_id=version_id,
            message=fail_msg,
            finished=True,
        )
        return {
            "status": "error",
            "message": fail_msg,
            "deployment_id": deployment_id,
            "version_id": version_id,
            "image_tag": recorded_image_tag,
            "rolling_results": rolling_res.get("results", []),
        }


def rollback(docker_mgr, application_id, target_version_id, triggered_by="dashboard", node_deployer_instance=None):
    """Redeploy a previously-built, already-recorded version. Does not
    rebuild anything -- it (re)pulls the stored image tag and deploys it,
    exactly like a normal deployment's final stage."""
    version = version_manager.get_version(target_version_id)
    if not version or version["application_id"] != application_id:
        raise ValueError(f"Version {target_version_id} not found for application {application_id}")

    application = database.get_application(application_id)
    if not application:
        raise ValueError(f"Application {application_id} not found")
    repo_name = application["name"]
    image_name = f"autopod-{repo_name.lower().replace(' ', '-')}"
    container_name = f"{image_name}-container"
    image_tag = version["image_tag"]
    preferred_port = None
    if application.get("target_port"):
        try:
            preferred_port = int(application["target_port"])
        except (TypeError, ValueError):
            preferred_port = None

    # If still not set, automatically allocate next worker target port (8081-8099)
    if preferred_port is None:
        allocated = database.allocate_next_target_port()
        if allocated:
            preferred_port = allocated
            database.update_application(application_id, target_port=str(allocated))
            logger.info("Allocated worker target port %d for application %s", allocated, repo_name)
    health_check_path = application.get("health_check_path")

    deployment = database.create_deployment(
        application_id=application_id, version_id=target_version_id, status="in_progress",
        is_rollback=True, triggered_by=triggered_by
    )
    deployment_id = deployment["id"]

    # Multi-node worker rollback (Section 6.3 / P0.2: Enforce worker-only rollout):
    in_service_nodes = database.list_app_nodes(status="in_service")
    allow_local_fallback = os.environ.get("AUTOPOD_ALLOW_LOCAL_FALLBACK", "false").lower() in ("true", "1")

    if not in_service_nodes:
        if not allow_local_fallback:
            fail_msg = (
                "Worker fleet unavailable: No application worker nodes are currently registered and in_service. "
                "AutoPod cannot execute rollback without an available worker node."
            )
            logger.error(fail_msg)
            database.update_deployment(
                deployment_id,
                status="failed",
                version_id=target_version_id,
                message=fail_msg,
                finished=True,
            )
            return _fail(deployment_id, fail_msg, version_id=target_version_id)
        else:
            docker_mgr.pull_image(image_tag)
            return _deploy_image(
                docker_mgr, application_id, deployment_id, target_version_id,
                image_tag, container_name, repo_name,
                preferred_port=preferred_port, health_check_path=health_check_path
            )

    if not preferred_port:
        return _fail(deployment_id, "No worker target_port could be allocated for worker fleet rollback", version_id=target_version_id)

    if node_deployer_instance is None:
        import node_deployer
        deployer = node_deployer.NodeDeployer()
    else:
        deployer = node_deployer_instance

    rolling_kwargs = {
        "application_id": application_id,
        "image_tag": image_tag,
        "container_name": container_name,
        "target_port": preferred_port,
        "health_check_path": health_check_path,
        "version_id": target_version_id,
        "triggered_by": triggered_by,
        "is_rollback": True,
        "nodes": in_service_nodes,
    }
    try:
        rolling_res = deployer.rolling_deploy(**rolling_kwargs, existing_deployment_id=deployment_id)
    except TypeError:
        rolling_res = deployer.rolling_deploy(**rolling_kwargs)
    if rolling_res["status"] == "success":
        version_manager.set_current(application_id, target_version_id)
        database.update_deployment(
            deployment_id,
            status="success",
            version_id=target_version_id,
            message=rolling_res.get("message") or f"Successfully rolled back {container_name} on {len(in_service_nodes)} node(s)",
            finished=True,
        )
        return {
            "status": "success",
            "message": rolling_res.get("message") or f"Successfully rolled back {container_name}",
            "deployment_id": deployment_id,
            "version_id": target_version_id,
            "port": preferred_port,
            "container_name": container_name,
            "image_tag": image_tag,
            "rolling_results": rolling_res.get("results", []),
        }
    else:
        fail_msg = rolling_res.get("message") or "Rolling rollback to worker nodes failed"
        database.update_deployment(
            deployment_id,
            status="failed",
            version_id=target_version_id,
            message=fail_msg,
            finished=True,
        )
        return {
            "status": "error",
            "message": fail_msg,
            "deployment_id": deployment_id,
            "version_id": target_version_id,
            "image_tag": image_tag,
            "rolling_results": rolling_res.get("results", []),
        }


def _deploy_image(docker_mgr, application_id, deployment_id, version_id, image_tag, container_name, repo_name, preferred_port=None, health_check_path=None):
    """Shared final stage for both fresh deployments and rollbacks:
    start the new image under a candidate name, health-check it, and only
    on success replace whatever was running under the real container name.
    The previously-running container is never touched until the candidate
    is confirmed healthy (ARCHITECTURE.md Section 19 / bug 7.6 fix)."""
    previous_existed = docker_mgr.container_exists(container_name)
    candidate_name = f"{container_name}-candidate"

    # Clean up any stale candidate from a previous failed attempt.
    docker_mgr.stop_container(candidate_name)
    docker_mgr.remove_container(candidate_name)

    port_search_start = preferred_port if preferred_port else 8081
    port = find_available_port(port_search_start)
    run_ok = docker_mgr.run_container(image_tag, candidate_name, ports=f"{port}:80")
    healthy = run_ok and _wait_for_health(docker_mgr, candidate_name, port=port, health_check_path=health_check_path)

    if not healthy:
        docker_mgr.stop_container(candidate_name)
        docker_mgr.remove_container(candidate_name)
        reason = "failed to start" if not run_ok else "failed its post-deploy health check"
        msg = f"New container ({image_tag}) {reason}."
        if previous_existed:
            msg += " Previous version was left running untouched."
        return _fail(deployment_id, msg, version_id=version_id)

    # Candidate is healthy -- safe to replace the old container now.
    if previous_existed:
        docker_mgr.stop_container(container_name)
        docker_mgr.remove_container(container_name)

    docker_mgr.stop_container(candidate_name)
    docker_mgr.remove_container(candidate_name)

    final_port = find_available_port(port_search_start)
    final_ok = docker_mgr.run_container(image_tag, container_name, ports=f"{final_port}:80")
    if not final_ok:
        # Extremely unlikely given the candidate just proved this image
        # works, but handle it rather than silently reporting success.
        return _fail(deployment_id, f"Candidate for {image_tag} was healthy, but promoting it to {container_name} failed.", version_id=version_id)

    docker_mgr.sync_with_db()
    version_manager.set_current(application_id, version_id)

    return _succeed(deployment_id, version_id, container_name, image_tag, final_port, repo_name, application_id=application_id)
