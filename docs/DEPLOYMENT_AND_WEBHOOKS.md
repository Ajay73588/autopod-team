# AutoPod Cloud — Deployment, Webhooks, Environment & Testing Guide

---

## 1. Installation & Local Development Setup

### Requirements
- **Python 3.9+** (with `pip` and `venv`)
- **Node.js 18+ & npm**
- **Docker Engine** (running locally with socket access)
- **Git**

### Local Backend Setup
```bash
cp .env.example .env
# Edit .env with local development settings:
# Set ADMIN_API_KEY and GITHUB_WEBHOOK_SECRET for testing auth/webhooks,
# or leave blank in pure local dev (startup logs a warning and disables enforcement).

python -m venv .venv
source .venv/bin/activate        # Windows PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py                    # Listens on http://localhost:5000
```

### Local Frontend Setup
```bash
cd frontend
npm install
# By default package.json proxies unknown routes to http://localhost:5000
npm start                        # React dev server on http://localhost:3000
```

---

## 2. Docker & Production Deployment Workflow

### 2.1 Control Plane Compose Stack (`docker-compose.yml`)
`docker-compose.yml` provisions three interconnected services on `autopod-network`:
1. **`autopod-backend`**: Gunicorn/Flask backend on `127.0.0.1:5000` mounting `/var/run/docker.sock`, `./database`, and `./logs`. Uses non-root group access matched via `DOCKER_GID`.
2. **`autopod-frontend`**: React UI service on `127.0.0.1:3000` (`depends_on: autopod-backend: service_healthy`).
3. **`autopod-webhook-proxy`**: Hardened Nginx Alpine container on `127.0.0.1:8080` exposing solely `POST /webhook`.

Launch locally or on an EC2 host provisioned via `infra/ec2-setup.sh`:
```bash
cp .env.example .env
# Configure ADMIN_API_KEY, GITHUB_WEBHOOK_SECRET, DOCKER_REGISTRY, AWS_REGION, etc.
export DOCKER_GID=$(getent group docker | cut -d: -f3)

docker compose -f docker-compose.yml -f infra/docker-compose.prod.yml up -d --build
curl http://127.0.0.1:5000/api/debug/docker-health
```

### 2.2 CI/CD Pipeline Execution (`DeploymentEngine`)
Once a webhook push or dashboard deployment triggers:
1. Repository is shallow-cloned or pulled into a temporary workspace on the Control Plane.
2. `tech_detector.py` detects custom `Dockerfile`/`Containerfile` or framework archetype (Static, Vite, CRA, Express, Node, Flask, FastAPI).
3. `dockerfile_synthesizer.py` generates an optimized `Dockerfile` (and Nginx SPA routing config if applicable) when no custom Dockerfile exists.
4. Docker image is built and tagged with immutable tag `<app-name>:<short-sha>-<timestamp>`.
5. Image is pushed to **Amazon ECR** (and optionally Docker Hub if credentials exist).
6. `node_deployer.py` dispatches deployment via **AWS SSM** to Worker EC2: logs into ECR via IAM role, pulls the exact tag, starts candidate container on its target port, and verifies health via `health_probe.py`.
7. `ingress_manager.py` updates `/etc/autopod-ingress/conf.d/`, validates via `nginx -t`, reloads `autopod-ingress-gateway`, and records the version & deployment outcome in SQLite.

---

## 3. GitHub Webhook Configuration

In any target GitHub repository, navigate to **Settings → Webhooks → Add webhook**:

| Field | Value |
| :--- | :--- |
| **Payload URL** | `https://<your-control-plane-domain>/webhook` *(recommended)* or `http://<your-alb-dns>/webhook` |
| **Content type** | `application/json` |
| **Secret** | `<GITHUB_WEBHOOK_SECRET>` (must exactly match Control Plane `.env`) |
| **SSL verification** | **Enable SSL verification** (`insecure_ssl: "0"` — recommended with TLS/Let's Encrypt) |
| **Trigger events** | **Just the `push` event** |
| **Active** | Checked |

### Expected Responses
- **GitHub `ping`:** `HTTP 200 OK` (`{"status": "ok", "message": "GitHub ping event received successfully"}`).
- **Valid `push`:** `HTTP 202 Accepted` in `<100ms` (job queued for background build).
- **Duplicate `X-GitHub-Delivery`:** `HTTP 200 OK` (`{"status": "ignored", "data": {"duplicate": true}}`).
- **Missing/Invalid `X-Hub-Signature-256`:** `HTTP 401 Unauthorized`.

---

## 4. Environment Variable Reference

All variables are defined in [`.env.example`](../.env.example). Never commit real secret values.

| Variable | Component | Purpose & Default |
| :--- | :--- | :--- |
| `ADMIN_API_KEY` | Backend (`auth.py`) | Shared secret verified against `X-Admin-Key` on mutating endpoints. Generate 64-char hex (`openssl rand -hex 32`). |
| `GITHUB_WEBHOOK_SECRET` | Backend (`app.py`) | Shared secret used for constant-time `X-Hub-Signature-256` HMAC validation. |
| `ALLOWED_ORIGINS` | Backend (`app.py`) | Comma-separated CORS allowlist (include your dashboard HTTPS origin). |
| `FLASK_DEBUG` / `FLASK_ENV` / `FLASK_RUN_HOST` | Backend (`app.py`) | Flask runtime settings (`false`, `production`, `0.0.0.0`). |
| `AUTOPOD_DB_PATH` | Backend (`database.py`) | Path to SQLite WAL database (`autopod.db` or `/app/database/autopod.db`). |
| `AWS_REGION` | Backend (`boto3`) | AWS region (default `us-east-1`). Credentials come from IAM instance profile / environment chain. |
| `DOCKER_REGISTRY` | Engine / Compose | Target container registry (e.g., `<account-id>.dkr.ecr.us-east-1.amazonaws.com` or `docker.io`). |
| `DOCKER_USERNAME` / `DOCKER_PASSWORD` | Backend (Optional) | Optional credentials for external Docker Hub mirror pulls/pushes; not required for AWS ECR. |
| `AUTOPOD_S3_IMAGES_BUCKET` | DockerMgr | Amazon S3 bucket storing exported container `.tar` archives. |
| `AUTOSCALING_GROUP_NAME` / `AUTOPOD_NODE_IP_ATTRIBUTE` | `aws_nodes.py` | Target ASG (`autopod-app-nodes-asg`) and IP mode (`private` or `public`). |
| `AUTOPOD_WORKER_INSTANCE_ID` | DockerMgr / App | Primary EC2 worker instance ID (`i-...`) targeted by SSM queries and controls. |
| `AUTOPOD_DEPLOY_METHOD` / `AUTOPOD_SSM_TIMEOUT` | `node_deployer.py` | Remote orchestration transport (`ssm` recommended, or `ssh`; timeout seconds e.g. `180`). |
| `AUTOPOD_ALLOW_LOCAL_FALLBACK` | `deployment_engine.py` | Set `false` in production so failed remote worker rollouts do not fall back to Control Plane Docker. |
| `HEALTH_CHECK_RETRIES` / `HEALTH_CHECK_DELAY_SECONDS` | `deployment_engine.py` | Candidate health probe attempts (`8`) and interval (`1.5s`). |
| `NODE_HEALTH_CHECK_RETRIES` / `NODE_HEALTH_CHECK_DELAY_SECONDS` | `node_deployer.py` | Worker candidate health verification timing. |
| `AUTOPOD_DEPLOY_SSH_USER` / `_KEY_PATH` / `_TIMEOUT` | `node_deployer.py` | Restricted SSH fallback identity (`autopod-deploy`, path only). |
| `INGRESS_CONF_DIR` / `INGRESS_GATEWAY_PORT` | `ingress_manager.py` | Worker ingress Nginx config dir (`/etc/nginx/conf.d` or `/etc/autopod-ingress/conf.d`) and port (`8080`). |
| `AUTOPOD_ALB_URL` / `APP_PUBLIC_URL_*` | `database.py` | Public ALB base URL used by dashboard "Open" resolution. |
| `REACT_APP_API_URL` | Frontend (`api.js`) | Backend URL (`http://localhost:5000` in dev; `""` empty string in Nginx production proxy). |

---

## 5. Testing Instructions

### Backend Tests (`pytest`)
Includes unit & integration tests across API auth, SQLite atomic claiming, tech detection, Dockerfile synthesis, SSM deployer mocks, ingress generation, Terraform HCL syntax, and live probe tests (automatically skipped if local Docker/ingress proxy is absent):
```bash
pip install -r requirements.txt
pytest tests/ -v
```
*Offline verified result:* **218 passed, 41 skipped, 0 failed**.

### Frontend Tests (`Jest`)
Includes React Testing Library tests across dashboard components, container grid public URL guards, deployments, fleet, networking, health monitoring, and API client error wrapping:
```bash
cd frontend
npm install
CI=true npm test -- --watchAll=false
```
*Verified result:* **8 test suites passed, 15 tests passed, 0 failed**.

---

## 6. Known Limitations

1. **Single Control Plane Database:** SQLite WAL handles multi-worker Gunicorn threads cleanly via atomic transactions, but scaling to multiple independent Control Plane EC2 instances would require migrating `database.py` to Amazon RDS PostgreSQL.
2. **Docker Socket Root Equivalence:** Mounting `/var/run/docker.sock` inside `autopod-backend` provides Docker control on the Control Plane host; strict host network & IAM boundaries must protect the Control Plane.
3. **Worker Node Requirements:** Worker nodes rely on AWS Systems Manager (`amazon-ssm-agent`) and IAM instance profile permissions (`ecr:GetAuthorizationToken`, ECR read) to accept builds without SSH keys.
4. **Live Log Tail Scope:** SSE streaming (`/api/logs/stream`) attaches to local Control Plane Docker containers, whereas Worker containers report telemetry via batched SSM commands.
5. **TLS Certificate Binding:** HTTPS access (`https://<public-ip-sslip.io>`) uses `sslip.io` + Let's Encrypt ACME HTTP-01 challenge and expects the Control Plane public IP to match the active Elastic IP / instance IP.
