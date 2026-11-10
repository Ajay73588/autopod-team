# AutoPod Cloud — Infrastructure

This directory holds the Terraform (AWS resources), Ansible (host configuration), and Nginx reverse-proxy templates implementing AutoPod Cloud's multi-node AWS topology. See `../docs/INFRASTRUCTURE.md` and `../ARCHITECTURE.md` for full architecture details.

## Order of operations

1. **Terraform first** — creates the VPC, subnets, security groups, the
   control-plane EC2 instance, the Launch Template + Auto Scaling Group
   for app nodes, the ALB, IAM role, and CloudWatch alarms.
2. **Ansible second**, targeting the IPs Terraform's outputs give you —
   installs Docker on every host, sets up monitoring (node_exporter on
   every host, cAdvisor on app nodes, Prometheus+Grafana on the control
   plane only), creates the restricted `autopod-deploy` SSH user on
   app nodes, and deploys AutoPod itself (`autopod` role: backend + frontend
   via Docker Compose wrapped in a systemd service) on the control plane.

```
infra/
  terraform/        # AWS resources (see terraform/ directory for file-by-file breakdown)
  ansible/           # Host configuration (see ansible/ directory)
  ec2-setup.sh        # Standalone single-host setup script (pre-existing, for a
                        # simpler single-EC2 demo that doesn't use the full
                        # multi-node topology below)
  docker-compose.prod.yml  # Production compose override (pre-existing)
```

## How to actually run this (requires a real AWS account + credentials)

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars
# edit terraform.tfvars: key_pair_name, admin_cidr (your own IP, never 0.0.0.0/0)

terraform init
terraform validate
terraform plan -out=tfplan        # REVIEW this output carefully
# terraform apply tfplan          # <-- only run this with the team's explicit
                                    #     go-ahead; this creates real, billable
                                    #     AWS resources.

terraform output                   # control_plane_public_ip, alb_dns_name, etc.
```

Then, with those IPs filled into `infra/ansible/inventory.ini`:

```bash
cd ../ansible
ansible-galaxy collection install -r requirements.yml
ansible-playbook site.yml --check --diff   # dry run first
ansible-playbook site.yml                   # apply
```

## Cost control

- The Auto Scaling Group's `max_size` is capped at 4 (`variables.tf`) —
  a runaway scaling event can never exceed 4 app-node instances' worth of
  EC2 time.
- No NAT Gateway, no RDS, no always-on managed service beyond the EC2
  instances and the ALB itself (ARCHITECTURE.md Section 3.2/15).
- **`terraform destroy` is the primary cost-control mechanism.** Run it
  between working sessions and especially after the final demo:
  ```bash
  cd infra/terraform
  terraform destroy
  ```

## Security notes

- `terraform.tfvars` and `*.tfstate` are gitignored (`infra/terraform/.gitignore`)
  — state can contain sensitive values, and `.tfvars` holds your admin CIDR.
- `admin_cidr` must be your team's own IP in `/32` form, never `0.0.0.0/0`.
- Prometheus and Grafana are bound to `127.0.0.1` only on the control
  plane (see `infra/ansible/roles/monitoring/tasks/main.yml`) — reach them
  via an SSH tunnel (`ssh -L 3001:localhost:3001 ec2-user@<control-plane-ip>`
  then open `http://localhost:3001`), never by opening those ports publicly.
- The `autopod-deploy` SSH key on app nodes is restricted with a forced
  `command=` in `authorized_keys` to a whitelisted wrapper script — it can
  never run an arbitrary shell command, even if the key were misused.

## Verification Summary

| Check | Status |
|---|---|
| Terraform HCL syntax & structural validation (`tests/test_infra_terraform.py`) | **Passed** |
| Terraform live AWS provisioning (`vpc`, `alb`, `ec2`, `ecr`, `iam`, `security`, `asg`) | **Live Deployed** |
| Ansible syntax & lint (`ansible-lint`, `site.yml`) | **Passed** |
| Reverse Proxy Nginx & Worker Ingress Gateway (`autopod.conf`, `webhook_ingress.conf`) | **Live Verified** |
