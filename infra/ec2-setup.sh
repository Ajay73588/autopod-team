#!/usr/bin/env bash
#
# infra/ec2-setup.sh
#
# One-time setup script for a fresh AWS EC2 instance (Ubuntu 22.04/24.04
# assumed) that will host AutoPod Cloud. Run this once, manually, after
# SSH'ing into the instance:
#
#   scp -r . ubuntu@<ec2-public-ip>:~/autopod-cloud
#   ssh ubuntu@<ec2-public-ip>
#   cd autopod-cloud
#   chmod +x infra/ec2-setup.sh
#   ./infra/ec2-setup.sh
#
# This script only installs Docker and prepares the host. It does NOT
# create your AWS account, EC2 instance, or security group -- those are
# manual AWS Console/CLI steps requiring your own credentials and are
# documented in README.md's "AWS EC2 Deployment" section as
# REQUIRES MANUAL CONFIGURATION, not something a script run from inside
# the instance can do for you.

set -euo pipefail

echo "=== AutoPod Cloud: EC2 host setup ==="

echo "--- Updating apt and installing prerequisites ---"
sudo apt-get update -y
sudo apt-get install -y ca-certificates curl gnupg git

echo "--- Installing Docker Engine (official Docker repo) ---"
if ! command -v docker &> /dev/null; then
    sudo install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    sudo chmod a+r /etc/apt/keyrings/docker.gpg
    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
      $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
      sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
    sudo apt-get update -y
    sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
else
    echo "Docker already installed, skipping."
fi

echo "--- Adding current user to the docker group ---"
sudo usermod -aG docker "$USER"
echo "NOTE: log out and back in (or run 'newgrp docker') for the group change to take effect."

echo "--- Recording the host's docker group GID (needed for the backend Dockerfile's DOCKER_GID build arg) ---"
DOCKER_GID=$(getent group docker | cut -d: -f3)
echo "Host docker group GID: ${DOCKER_GID}"
echo "Set this as DOCKER_GID in your .env file (or export DOCKER_GID=${DOCKER_GID} before running docker compose build)."

echo ""
echo "=== Host setup complete ==="
echo ""
echo "Remaining manual steps (see README.md for full detail):"
echo "  1. Create/copy .env from .env.example and fill in real secrets."
echo "  2. Confirm your AWS security group allows inbound 22 (SSH, your IP only),"
echo "     80/443 (dashboard), and the port GitHub's webhook will reach (usually 5000"
echo "     directly, or 443 if you put a reverse proxy in front of it -- your choice)."
echo "  3. Run: DOCKER_GID=${DOCKER_GID} docker compose up -d --build"
echo "  4. Configure the GitHub webhook in your repository settings:"
echo "       Payload URL: http://<this-instance-public-ip>:5000/webhook"
echo "       Content type: application/json"
echo "       Secret: the same value as GITHUB_WEBHOOK_SECRET in your .env"
echo "  5. Verify: curl http://localhost:5000/api/debug/docker-health"
