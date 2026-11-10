#!/bin/bash
# AutoPod Cloud -- application node self-bootstrap user-data script.
# Runs once, at first boot, on every instance the Auto Scaling Group
# launches (both the initial fleet and any instance a scale-out event
# creates later -- ARCHITECTURE.md Section 10.2's exact sequence).
#
# This script intentionally does the MINIMUM needed to get a working
# container running and registered with the ALB target group: install
# Docker, ask the control plane what the current version is, pull and run
# it. Everything else (node_exporter, cAdvisor, the autopod-deploy SSH
# user) is Ansible's job for the INITIAL fleet (ARCHITECTURE.md Section
# 5.3); an Auto-Scaled node launched later only gets what this script
# does, which is a deliberate, documented limitation (it will serve
# traffic and be scrape-able once Ansible or a follow-up automation step
# reaches it -- not a blocking requirement for the node to join the ALB
# and serve the application, which is this script's actual job).

set -euo pipefail

# --- Install Docker Engine & SSM Agent (Amazon Linux 2023) ---------------------------
dnf install -y docker amazon-ssm-agent
systemctl enable --now docker
systemctl enable --now amazon-ssm-agent
usermod -aG docker ec2-user || true

# --- Fetch current versions for all applications or default application ---
# ARCHITECTURE.md Section 10.2 / Defect D4: Check if multi-app endpoint is available
CONTROL_PLANE_API_URL="${control_plane_api_url}"
APP_TARGET_PORT="${app_target_port}"

MULTI_DEPLOYED=0
ALL_VERSIONS_JSON=$(curl -fsS "$CONTROL_PLANE_API_URL/api/applications/current-versions" || echo "")
if [ -n "$ALL_VERSIONS_JSON" ]; then
  python3 -c '
import json, sys, subprocess

try:
    d = json.loads(sys.argv[1])
    region = sys.argv[2]
    apps = d.get("data") or []
    if not apps:
        sys.exit(1)
    for app in apps:
        img = app.get("image_tag")
        name = app.get("container_name")
        port = app.get("target_port")
        if not (img and name and port):
            continue
        if ".dkr.ecr." in img:
            ecr_reg = img.split("/")[0]
            print(f"AutoPod bootstrap: logging into ECR {ecr_reg}...")
            login_cmd = f"aws ecr get-login-password --region {region} | docker login --username AWS --password-stdin {ecr_reg}"
            res = subprocess.run(login_cmd, shell=True, check=False)
            if res.returncode != 0:
                sys.stderr.write(f"AutoPod bootstrap multi-app ECR login failed for {ecr_reg}\n")
                sys.exit(1)
        print(f"AutoPod bootstrap: deploying {name} ({img}) on port {port}...")
        pull_res = subprocess.run(["docker", "pull", img], check=False)
        if pull_res.returncode != 0:
            sys.stderr.write(f"AutoPod bootstrap multi-app docker pull failed for {img}\n")
            sys.exit(1)
        subprocess.run(["docker", "rm", "-f", name], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["docker", "run", "-d", "--name", name, "--restart", "unless-stopped", "-p", f"{port}:80", img], check=True)
    sys.exit(0)
except Exception as exc:
    sys.stderr.write(f"AutoPod bootstrap multi-app error: {exc}\n")
    sys.exit(1)
' "$ALL_VERSIONS_JSON" "${aws_region}" && MULTI_DEPLOYED=1 || MULTI_DEPLOYED=0
fi

if [ "$MULTI_DEPLOYED" -eq 1 ]; then
  echo "AutoPod bootstrap: all registered applications deployed successfully."
  exit 0
fi

echo "AutoPod bootstrap: multi-app endpoint not ready or empty; attempting single-application fallback..."

# Fallback: single application bootstrap (ARCHITECTURE.md Section 10.2)
VERSION_JSON=$(curl -fsS "$CONTROL_PLANE_API_URL/api/versions?current=true" || echo "")

if [ -z "$VERSION_JSON" ]; then
  echo "AutoPod bootstrap: could not reach control plane at $CONTROL_PLANE_API_URL -- nothing to deploy yet." >&2
  exit 0
fi

# Parse the response: image to run, the canonical container name and the
# port the control plane has on record for this application. The container
# name MUST match what node_deployer.py uses for later rolling deploys,
# otherwise a rolling deploy would not see (or replace) this container.
PARSED=$(echo "$VERSION_JSON" | python3 -c '
import json, sys
d = json.load(sys.stdin)
data = d.get("data") or []
image = data[0]["image_tag"] if data else ""
print(image)
print(d.get("container_name") or "")
print(d.get("target_port") or "")
' 2>/dev/null || echo "")

IMAGE_TAG=$(echo "$PARSED" | sed -n 1p)
CONTAINER_NAME=$(echo "$PARSED" | sed -n 2p)
PORT=$(echo "$PARSED" | sed -n 3p)
PORT="$${PORT:-$APP_TARGET_PORT}"

if [ -z "$IMAGE_TAG" ]; then
  echo "AutoPod bootstrap: no current version reported by control plane -- nothing to deploy yet." >&2
  exit 0
fi

if [ -z "$CONTAINER_NAME" ]; then
  echo "AutoPod bootstrap: control plane did not report a container name -- refusing to guess one." >&2
  exit 0
fi

# --- Authenticate to Amazon ECR if image is from ECR -----------------
if [[ "$IMAGE_TAG" == *".dkr.ecr."* ]]; then
  ECR_REGISTRY=$(echo "$IMAGE_TAG" | cut -d/ -f1)
  echo "AutoPod bootstrap: authenticating to Amazon ECR registry $ECR_REGISTRY in region ${aws_region}..."
  aws ecr get-login-password --region "${aws_region}" | docker login --username AWS --password-stdin "$ECR_REGISTRY" || {
    echo "AutoPod bootstrap: ECR authentication failed for $ECR_REGISTRY" >&2
    exit 1
  }
fi

# --- Pull and run -----------------------------------------------------
docker pull "$IMAGE_TAG" || {
  echo "AutoPod bootstrap: docker pull failed for $IMAGE_TAG -- registry may require auth or be unreachable." >&2
  exit 1
}

docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
docker run -d --name "$CONTAINER_NAME" --restart unless-stopped \
  -p "$PORT:80" \
  "$IMAGE_TAG"

echo "AutoPod bootstrap: $IMAGE_TAG running as $CONTAINER_NAME on port $PORT. ALB health checks will register this instance once it passes."
