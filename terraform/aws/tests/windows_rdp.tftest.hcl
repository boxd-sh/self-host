# terraform -chdir=terraform/aws init -backend=false && terraform -chdir=terraform/aws test
# [windows]: the proxy SG takes each Windows machine's RDP port (TCP 61000-65000) from
# ingress_cidrs, and never the proxy's own RDP listener (3399). Without it, no RDP band. Mocked
# provider.

mock_provider "aws" {
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  override_data {
    target = data.aws_ec2_instance_type.worker
    values = { default_vcpus = 16, memory_size = 65536 }
  }
}

variables {
  region              = "us-east-1"
  availability_zone   = "us-east-1a"
  cluster_name        = "tftest"
  vpc_cidr            = "10.70.0.0/16"
  public_subnet_cidr  = "10.70.0.0/24"
  private_subnet_cidr = "10.70.1.0/24"
  ami                 = "ami-0123456789abcdef0"
  deploy_ssh_pubkey   = "ssh-ed25519 AAAA"
  enable_backups      = false
  nodes = [
    { name = "control-0", primary_role = "control", roles = ["control", "dns", "proxy"],
    internal_ip = "10.70.0.10", public_ip = true, nested_virt = false, boot_disk_gb = 50 },
    { name = "worker-0", primary_role = "worker", roles = ["worker"], machine_type = "m8i.4xlarge",
    internal_ip = "10.70.1.10", public_ip = false, nested_virt = true, data_disk_gb = 500, boot_disk_gb = 200 },
  ]
}

run "windows" {
  command = plan
  variables {
    windows_rdp   = true
    ingress_cidrs = ["203.0.113.0/24"]
  }
  assert {
    condition = length([for r in aws_security_group.proxy_public.ingress : r
    if r.from_port == 61000 && r.to_port == 65000 && r.protocol == "tcp"]) == 1
    error_message = "with windows_rdp, proxy_public admits TCP 61000-65000"
  }
  assert {
    condition = alltrue([for r in aws_security_group.proxy_public.ingress :
    r.cidr_blocks == tolist(["203.0.113.0/24"])])
    error_message = "the RDP band comes from ingress_cidrs, like every proxy port"
  }
  assert {
    condition = length([for r in aws_security_group.proxy_public.ingress : r
    if r.from_port <= 3399 && r.to_port >= 3399]) == 0
    error_message = "the proxy's RDP listener (3399) stays closed from outside"
  }
  assert {
    condition     = length(aws_security_group.proxy_public.ingress) == 6
    error_message = "the five usual ranges and the RDP band"
  }
}

run "linux_only" {
  command = plan
  assert {
    condition = length([for r in aws_security_group.proxy_public.ingress : r
    if r.to_port >= 61000]) == 0
    error_message = "without windows_rdp, no RDP band"
  }
  assert {
    condition     = length(aws_security_group.proxy_public.ingress) == 5
    error_message = "the five usual ranges"
  }
}
