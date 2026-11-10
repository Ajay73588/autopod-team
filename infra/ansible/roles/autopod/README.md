# AutoPod Application Ansible Role

Deploy and configure the AutoPod Cloud platform (backend Flask API and React frontend) on the control plane EC2 instance.

## Overview

Fixes **Defect D5** (control plane bootstrap gap):
- Terraform provisions the bare EC2 control plane instance and security groups.
- This Ansible role finishes the setup: creates install directory `/opt/autopod`, syncs or clones repository, renders `.env` from Ansible secrets (safe, not checked into source control), configures `docker-compose.prod.yml`, and installs/enables a `systemd` service (`autopod.service`) ensuring AutoPod starts automatically on boot.

## Required Variables

- `autopod_admin_api_key`: Shared secret for admin API calls
- `autopod_webhook_secret`: GitHub HMAC webhook secret

## Optional Variables

- `autopod_install_dir`: Default `/opt/autopod`
- `autopod_repo_url`: Git repository URL (optional)
- `autopod_git_branch`: Git branch (default `main`)
- `autopod_frontend_port`: Published port for frontend (default `80`)
- `autopod_backend_port`: Published port for backend API (default `5000`)
- `autopod_docker_username`: Docker Hub username
- `autopod_docker_password`: Docker Hub password
