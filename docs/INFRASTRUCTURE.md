# AutoPod Cloud — AWS Infrastructure & Ingress Overview

AutoPod Cloud's infrastructure is defined declaratively using **Terraform** (`infra/terraform/`) and configured via **Ansible** (`infra/ansible/`), combined with runtime **Nginx** ingress configs (`infra/nginx/` and dynamic worker gateway generation).

---

## 1. AWS Topology Summary (`us-east-1`)

```text
Internet
  ├── HTTPS (:443) ──▶ Control Plane EC2 (Nginx TLS + Basic Auth)
  │                        ├── /*       ──▶ React Dashboard (:3000)
  │                        ├── /api/*   ──▶ Flask Backend (:5000, injects X-Admin-Key)
  │                        └── /webhook ──▶ Flask Backend (:5000, raw HMAC check)
  │
  └── HTTP (:80)   ──▶ AWS Application Load Balancer (autopod-alb)
                           ├── Rule /webhook ──▶ Control Plane EC2
                           └── Default /*    ──▶ Worker Target Group (:8080 /healthz)
                                                     └── Worker EC2 (autopod-ingress-gateway)
                                                             ├── /             ──▶ App 1 (:8081)
                                                             ├── /voltride.../ ──▶ App 2 (:8082)
                                                             └── /crypto.../   ──▶ App 3 (:8086)
```

---

## 2. Terraform Modules (`infra/terraform/`)

| File | Provisioned AWS Resources |
| :--- | :--- |
| `providers.tf` & `variables.tf` | AWS provider (`us-east-1`), region, key pair, `admin_cidr` restriction, ASG sizing defaults. |
| `vpc.tf` | Dedicated VPC, public subnets across availability zones, Internet Gateway, and route tables. |
| `security.tf` | Least-privilege Security Groups for ALB (`80`), Control Plane (`22` from `admin_cidr`, `80`/`443` for dashboard/webhook), and Worker fleet (ALB ingress -> `8080`, restricted outbound). |
| `iam.tf` | Instance profiles for Control Plane and Worker EC2 nodes granting Amazon SSM management, ECR push/pull (`ecr:GetAuthorizationToken`, repository push/pull), and S3 image export permissions (`autopod-images-*`). |
| `ecr.tf` | Private Amazon ECR repositories with `scan_on_push = true` and lifecycle retention policies. |
| `ec2.tf` | Control Plane EC2 host (`t3.small` / Amazon Linux 2023) running Docker & Compose services. |
| `autoscaling.tf` & `templates/` | Worker node Launch Template (with `app_node_user_data.sh.tpl` bootstrapping Docker & SSM Agent) and Auto Scaling Group (`autopod-app-nodes-asg`). |
| `alb.tf` | Public Application Load Balancer (`autopod-alb`), listener rules, and target groups (`autopod-worker-ingress-tg` polling `/healthz` on port `8080`, `autopod-control-plane-tg`). |
| `cloudwatch.tf` | CPU utilization CloudWatch alarms driving ASG scaling policies. |
| `outputs.tf` | Outputs ALB DNS name, Control Plane public IP, ECR URLs, and ASG identifiers. |

> Never commit `terraform.tfvars`, `*.tfstate*`, `.terraform/`, or `tfplan` — all are excluded by `.gitignore`.

---

## 3. Ansible Host Automation (`infra/ansible/`)

| Role | Purpose |
| :--- | :--- |
| `docker` | Installs Docker Engine, Docker Compose plugin, and configures non-root socket group membership. |
| `autopod` | Deploys Control Plane environment templates, `systemd` units, and starts Compose services. |
| `deployment` | Configures restricted fallback deployment user (`autopod-deploy`) with forced SSH command wrapper (`authorized_keys` command pinning) when SSH transport is selected instead of SSM. |
| `monitoring` | Configures `node_exporter`, `cAdvisor`, `Prometheus` (EC2 dynamic service discovery), and `Grafana` bound strictly to `127.0.0.1` for SSH-tunneled access. |

---

## 4. Control Plane & Worker Nginx Architecture

### 4.1 Control Plane Reverse Proxy (`infra/nginx/autopod.conf`)
- Terminates **TLS 1.2 / 1.3** (`443`) using Let's Encrypt certificates and redirects plain HTTP dashboard traffic (`80`) to HTTPS (`301`).
- Keeps `location = /webhook` and `location = /health` open without HTTP Basic Auth so GitHub webhooks and ALB health probes succeed immediately.
- Enforces `auth_basic` (`/etc/nginx/.htpasswd`) on `/` (React SPA) and `/api/*`, injecting `proxy_set_header X-Admin-Key "${ADMIN_API_KEY}";` only after operator authentication succeeds.

### 4.2 Dedicated Webhook Ingress Proxy (`infra/nginx/webhook_ingress.conf`)
- Lightweight container (`autopod-webhook-proxy`) listening on port `8080` exposing **strictly `location = /webhook`** (`POST`/`OPTIONS` only; all other paths return `404` and disallowed HTTP methods return `403`).

### 4.3 Worker Ingress Gateway (`autopod-ingress-gateway`)
Managed programmatically via [`ingress_manager.py`](../ingress_manager.py) on Worker EC2 port `8080`:
- Dynamically renders Nginx reverse-proxy location blocks for every `active` application in SQLite.
- Validates candidate configs with `nginx -t` prior to swapping and atomically rolls back if syntax validation fails.
- Sets `absolute_redirect off;` and emits explicit **relative `301` redirects** (`return 301 /<app-slug>/;`) when clients omit trailing slashes, preserving the public ALB host and stripping internal worker ports (`127.0.0.1:8080`).
- Rewrites `/<app-slug>/(.*)` upstream to `/$1` so static SPAs and backend apps serve cleanly under subpath URLs.
