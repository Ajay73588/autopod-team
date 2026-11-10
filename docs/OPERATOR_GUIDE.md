# AutoPod Cloud — Operator Runbook & Administration Guide

**Audience:** Cloud Operators, DevOps Engineers, Platform Administrators  

---

## 1. Quick Reference Endpoints

| Resource | Endpoint Pattern | Purpose |
| :--- | :--- | :--- |
| **Management Dashboard** | `https://<dashboard-domain>/` | React SPA console protected by TLS + HTTP Basic Auth |
| **Canonical Webhook URL** | `https://<dashboard-domain>/webhook` | GitHub webhook push/ping ingestion (`HTTP 202 Accepted`) |
| **Public ALB Base URL** | `http://<alb-dns>/` | Public ingress routing via Worker Ingress Gateway |
| **Backend Health Check** | `https://<dashboard-domain>/health` | Control plane Flask liveness endpoint |
| **Worker Ingress Health** | `http://<alb-dns>/healthz` | Worker Nginx Ingress Gateway probe (`HTTP 200 OK`) |

---

## 2. Authentication Boundaries

1. **Dashboard Login:** Access `https://<dashboard-domain>/` using your Basic Auth operator username (e.g., `autopod-admin`) and password configured in `/etc/nginx/.htpasswd`.
2. **Backend API Key (`X-Admin-Key`):** Required for direct mutating API calls to port `5000` (Nginx injects `X-Admin-Key` automatically for authenticated dashboard traffic under `/api/`):
   ```bash
   -H "X-Admin-Key: <AUTOPOD_ADMIN_KEY>"
   ```
3. **GitHub Webhook Secret:** Shared 64-char hex secret configured in GitHub repository webhook settings and `/opt/autopod/.env` (`GITHUB_WEBHOOK_SECRET`).

---

## 3. Registering & Deploying Applications

### Option A: Dashboard UI
1. Log into the dashboard (`https://<dashboard-domain>/`).
2. Navigate to **Overview** or **Fleet** and register your GitHub repository URL, worker port (`8081+`), ingress routing path (`/` or `/<app-slug>/`), and health check path.
3. Trigger **Deploy Application** or push directly to the configured GitHub branch (`main`).
4. Monitor live transitions in **Deployments**: `queued` → `processing` → `pushing` → `deploying` → `success`.

### Option B: REST API
```bash
curl -X POST "https://<dashboard-domain>/api/deployments" \
  -u "<basic-auth-user>:<basic-auth-password>" \
  -H "Content-Type: application/json" \
  -d '{
    "repository": {
      "clone_url": "https://github.com/<owner>/<repo>.git",
      "name": "<app-name>"
    },
    "branch": "main"
  }'
```

---

## 4. Rollback Procedures

### Dashboard One-Click Rollback
1. Open the **Rollback** page.
2. Select the target application from the dropdown.
3. Inspect available historical versions (Version ID, Git Commit SHA, Timestamp, Active badge).
4. Click **Rollback** next to the desired previous version. AutoPod pulls the immutable ECR image tag directly onto the worker node without rebuilding source code.

### API Rollback
```bash
curl -X POST "https://<dashboard-domain>/api/deployments/rollback" \
  -u "<basic-auth-user>:<basic-auth-password>" \
  -H "Content-Type: application/json" \
  -d '{
    "application_id": 1,
    "version_id": 12
  }'
```

---

## 5. Container Lifecycle Management & Image Exports

- **Start / Stop / Restart Container:** Available directly on container cards in **Dashboard** / **Containers**, or via `POST /api/containers/{start|stop|restart}` with JSON payload `{"container_name": "<name>"}`.
- **Protected Infrastructure Safeguard:** Control Plane containers (`autopod-backend`, `autopod-frontend`, `autopod-webhook-proxy`) permanently reject removal via both UI and API (`HTTP 403 Forbidden`).
- **Export Docker Image TAR via S3:**
  Click **Download Image (.tar)** on a container card or execute:
  ```bash
  curl -X POST "https://<dashboard-domain>/api/containers/<container-name>/download-image" \
    -u "<basic-auth-user>:<basic-auth-password>"
  ```
  Returns a JSON payload containing a 15-minute AWS SigV4 presigned S3 download URL.

---

## 6. Diagnostics & Troubleshooting

- **Check Worker Ingress Gateway Health:**
  ```bash
  curl -i http://<alb-dns>/healthz
  ```
- **Inspect Worker Containers & Ingress Nginx via AWS SSM:**
  ```bash
  aws ssm send-command \
    --instance-ids "<worker-instance-id>" \
    --document-name "AWS-RunShellScript" \
    --parameters 'commands=["docker ps -a", "cat /etc/autopod-ingress/conf.d/default.conf", "docker exec autopod-ingress-gateway nginx -t"]'
  ```
- **Inspect Webhook Deliveries & Async Queue:**
  - `GET /api/webhook/deliveries` — confirms signature validity and `X-GitHub-Delivery` deduplication.
  - `GET /api/deployments/queue` — checks queued, processing, superseded, and completed build jobs.
