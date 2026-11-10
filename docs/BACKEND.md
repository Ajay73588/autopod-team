# AutoPod Cloud — Backend Documentation

The backend service is a modular Python/Flask application orchestrated via Gunicorn in production (`Dockerfile`), interfacing with the Control Plane Docker daemon, SQLite WAL state engine, Amazon ECR, Amazon S3, and Worker EC2 fleets via AWS Systems Manager (SSM).

---

## 1. Python Module Inventory

| Module | Responsibility |
| :--- | :--- |
| [`app.py`](../app.py) | Flask entry point, ProxyFix & CORS middleware, rotating log handler, background queue worker startup, and REST/SSE HTTP endpoints. |
| [`auth.py`](../auth.py) | `X-Admin-Key` constant-time authentication decorator (`@require_admin_key`) guarding mutating endpoints. |
| [`database.py`](../database.py) | SQLite WAL schema initialization, connection management, CRUD operations for apps/versions/deployments/deliveries, and single-transaction `claim_next_queued_deployment()`. |
| [`webhook_handler.py`](../webhook_handler.py) | GitHub `ping`/`push` event parsing, constant-time HMAC-SHA256 verification (`X-Hub-Signature-256`), and delivery metadata normalization. |
| [`deployment_queue.py`](../deployment_queue.py) | Background `DeploymentQueueWorker` thread polling queued jobs, superseding older pending commits per application, and dispatching builds without blocking HTTP handlers. |
| [`deployment_engine.py`](../deployment_engine.py) | Executes the 12-stage CI/CD pipeline: clone, tech detection, Dockerfile synthesis, image build, ECR/Docker Hub tag & push, SSM rolling rollout, candidate health verification, and ingress sync. |
| [`tech_detector.py`](../tech_detector.py) | Deterministically inspects cloned repositories to identify framework archetypes (Custom Dockerfile, Static HTML/JS, Vite React, CRA React, Node/Express, Flask, FastAPI). |
| [`dockerfile_synthesizer.py`](../dockerfile_synthesizer.py) | Generates multi-stage or runtime-optimized `Dockerfile` + SPA Nginx config (`try_files $uri $uri/ /index.html =404;`) when a repository lacks a custom Dockerfile. |
| [`docker_manager.py`](../docker_manager.py) | Unified Control Plane local Docker & Worker EC2 remote container manager (single combined SSM `docker ps -a` + `docker inspect` for `StartedAt`/status, lifecycle controls, registry metadata, and S3 `.tar` archive export). |
| [`node_deployer.py`](../node_deployer.py) | Rolling deployment and rollback executor across registered worker `app_nodes`, supporting AWS SSM (`ssm:SendCommand`, default) and restricted SSH (`ssh` fallback). |
| [`ingress_manager.py`](../ingress_manager.py) | Dynamically builds `/etc/autopod-ingress/conf.d/` Nginx location blocks for active applications, validates via `nginx -t`, atomically reloads `autopod-ingress-gateway`, and enforces relative `301` subpath redirects. |
| [`health_probe.py`](../health_probe.py) | HTTP health checker polling candidate container ports until HTTP `< 500` or retry budget exhaustion. |
| [`version_manager.py`](../version_manager.py) | Generates `<image>:<short_sha>-<YYYYMMDD-HHMMSS>` tags and manages active `is_current` pointers. |
| [`aws_nodes.py`](../aws_nodes.py) | Discovers EC2 worker instances from the configured AWS Auto Scaling Group via `boto3` and syncs `app_nodes`. |

---

## 2. Asynchronous Queue & Concurrency Safety

When multiple Gunicorn workers handle webhook traffic concurrently:

1. `/webhook` verifies the HMAC-SHA256 signature on raw HTTP body bytes (`request.get_data()`), deduplicates against `webhook_deliveries` by `X-GitHub-Delivery`, enqueues a job in `deployment_queue`, and immediately responds `HTTP 202 Accepted`.
2. Background queue threads invoke `database.claim_next_queued_deployment()`, which executes an atomic `BEGIN IMMEDIATE` transaction in SQLite to transition one `queued` job to `processing`.
3. If newer commits were pushed for the same repository while a previous job was still waiting in `queued`, older queued commits are marked `superseded` automatically, preventing wasted builds.

---

## 3. REST API Reference

Mutating routes require `X-Admin-Key` (injected server-side by Control Plane Nginx after HTTP Basic Authentication).

### Health & System
| Method & Path | Auth | Description |
| :--- | :--- | :--- |
| `GET /` | Public | Basic service metadata JSON. |
| `GET /health` | Public | ALB / Compose liveness check. |
| `GET /api/debug/docker-health` | Public | Verifies Docker socket reachability on Control Plane. |
| `GET /api/status` | Read | Aggregated container status counts (`running`, `stopped`, `total`). |
| `POST /api/sync` | Admin | Synchronizes container state and worker ingress gateway routes. |

### GitHub Webhooks & Queue
| Method & Path | Auth | Description |
| :--- | :--- | :--- |
| `POST /webhook` | HMAC / Admin | Handles `ping` and `push` events (`X-Hub-Signature-256`). Returns `202` on valid enqueue. |
| `GET /api/webhook/deliveries` | Admin / Read | Lists recorded GitHub delivery events and signature outcomes. |
| `GET /api/deployments/queue` | Read | Inspects active and historical asynchronous queue jobs. |

### Applications & Worker Fleet
| Method & Path | Auth | Description |
| :--- | :--- | :--- |
| `GET /api/applications` | Read | Lists registered applications with ports, routes, and public URLs. |
| `POST /api/applications` | Admin | Registers a new application (repo URL, port, health check path, routing path). |
| `GET / PATCH / DELETE /api/applications/<id>` | Admin | Reads, updates, or soft-deactivates an application record. |
| `GET /api/app-nodes` | Read | Lists worker EC2 nodes tracked in SQLite. |
| `POST /api/app-nodes/sync` | Admin | Polls AWS Auto Scaling Group to discover and register worker instances. |

### Deployments & Rollback
| Method & Path | Auth | Description |
| :--- | :--- | :--- |
| `GET /api/deployments` | Read | Lists deployment history across all applications. |
| `POST /api/deployments` | Admin | Triggers an asynchronous or direct deployment for a repository & branch. |
| `GET /api/deployments/<id>` | Read | Retrieves build logs and state for a single deployment. |
| `GET /api/versions` | Read | Lists recorded immutable image versions per application. |
| `GET /api/applications/current-versions` | Read | Returns the active running version pointer per application. |
| `POST /api/deployments/rollback` | Admin | Rolls an application back to a specific historical `version_id` without rebuilding source. |
| `POST /api/deployments/rolling` | Admin | Triggers multi-node rolling rollout for a version. |
| `POST /api/deployments/rolling-rollback` | Admin | Executes rolling rollback across worker nodes. |

### Container & Image Management
| Method & Path | Auth | Description |
| :--- | :--- | :--- |
| `GET /api/containers` | Read | Returns unified Control Plane + live SSM Worker containers (`started_at`, `worker_port`, `target_node`, `lifecycle_status`). |
| `POST /api/containers/start` | Admin | Starts a stopped container on Control Plane or Worker node. |
| `POST /api/containers/stop` | Admin | Stops a running container without disrupting peer applications. |
| `POST /api/containers/restart` | Admin | Restarts a target container via Docker/SSM. |
| `POST /api/containers/remove` | Admin | Removes disposable containers; returns `HTTP 403` for protected infrastructure containers (`autopod-frontend`, `autopod-backend`, `autopod-webhook-proxy`). |
| `POST /api/containers/<name>/open` | Admin | Resolves verified public ALB/ingress URL (`HTTP 409` for internal-only services). |
| `POST /api/containers/<name>/download-image` | Admin | Exports container image via `docker save` to S3 and returns a 15-minute presigned SigV4 URL. |
| `GET /api/containers/<name>/stats` | Read | Live CPU/Memory resource stats for a container. |
| `GET /api/images` | Read | Lists local & AWS ECR images with `is_ecr` and `registry_type` badges. |
| `POST /api/images/pull` / `push` / `remove` / `tag` | Admin | Docker registry and local image lifecycle actions. |
| `POST /api/docker/login` / `logout` / `GET /api/docker/check-login` | Admin | Optional Docker Hub credentials management. |

### Logs & Networking
| Method & Path | Auth | Description |
| :--- | :--- | :--- |
| `GET /api/logs` / `/api/logs/advanced` | Read | Filtered deployment and container logs. |
| `GET /api/logs/stream` | Read | Server-Sent Events (SSE) live log tailing. |
| `GET /api/logs/export` | Read | Downloads structured logs. |
| `GET /api/containers/network/all` / `/api/network/stats` | Read | Network inspection across Docker bridge networks. |
