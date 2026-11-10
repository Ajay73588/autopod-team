# AutoPod Cloud -- Terraform outputs.
# These are exactly what infra/ansible/inventory.ini needs filled in
# (ARCHITECTURE.md Section 5.1's "Ansible -> EC2 relationship" diagram:
# `terraform output` -> inventory.ini). After `terraform apply`, run
# `terraform output -json` and use it to populate the inventory rather
# than hand-typing IPs.

output "control_plane_public_ip" {
  description = "Public IP of EC2-1 (control plane). Put this under [control_plane] in infra/ansible/inventory.ini."
  value       = aws_instance.control_plane.public_ip
}

output "control_plane_private_ip" {
  description = "Private IP of the control plane, used by app nodes' bootstrap script to reach the AutoPod API."
  value       = aws_instance.control_plane.private_ip
}

output "alb_dns_name" {
  description = "Public DNS name of the Application Load Balancer -- this is the URL to demo the deployed application through."
  value       = aws_lb.autopod.dns_name
}

output "autoscaling_group_name" {
  description = "Name of the app-node Auto Scaling Group, for `aws autoscaling describe-auto-scaling-groups` / manual scaling demos."
  value       = aws_autoscaling_group.app_nodes.name
}

output "vpc_id" {
  description = "VPC ID, useful for sanity-checking resources in the AWS Console."
  value       = aws_vpc.autopod.id
}

output "app_node_security_group_id" {
  description = "Security group ID for app nodes (useful if adding resources outside this Terraform config later)."
  value       = aws_security_group.app_node.id
}
