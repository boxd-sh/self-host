# terraform -chdir=terraform/aws init -backend=false && terraform -chdir=terraform/aws test
# Private ingress: no Elastic IPs, proxy/dns in the private subnet, and the
# proxy SG open only to var.ingress_cidrs, dns 53 open to all. Mocked provider.

mock_provider "aws" {
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  override_data {
    target = data.aws_ec2_instance_type.worker
    values = { default_vcpus = 16, memory_size = 65536 }
  }
  # Known subnet ids at plan time, so the placement asserts can compare them.
  override_resource {
    target          = aws_subnet.public
    override_during = plan
    values          = { id = "subnet-public" }
  }
  override_resource {
    target          = aws_subnet.private
    override_during = plan
    values          = { id = "subnet-private" }
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
}

run "private" {
  command = plan
  variables {
    ingress_cidrs = ["10.70.0.0/16"]
    nodes = [
      { name = "control-0", primary_role = "control", roles = ["control", "dns", "proxy"],
      internal_ip = "10.70.1.11", public_ip = false, nested_virt = false, boot_disk_gb = 50 },
      { name = "worker-0", primary_role = "worker", roles = ["worker"], machine_type = "m8i.4xlarge",
      internal_ip = "10.70.1.10", public_ip = false, nested_virt = true, data_disk_gb = 500, boot_disk_gb = 200 },
    ]
  }
  assert {
    condition     = length(aws_eip.public) == 0 && length(aws_eip_association.public) == 0
    error_message = "private ingress must not allocate Elastic IPs"
  }
  assert {
    condition     = aws_instance.node["control-0"].subnet_id == aws_subnet.private.id
    error_message = "the proxy node must be in the private subnet"
  }
  assert {
    condition = alltrue([for r in aws_security_group.proxy_public.ingress :
    r.cidr_blocks == tolist(["10.70.0.0/16"])])
    error_message = "proxy_public must only admit ingress_cidrs"
  }
  assert {
    condition = alltrue([for r in aws_security_group.dns_public.ingress :
    r.cidr_blocks == tolist(["0.0.0.0/0"])])
    error_message = "dns_public must not be narrowed by ingress_cidrs"
  }
}

run "public" {
  command = plan
  variables {
    nodes = [
      { name = "control-0", primary_role = "control", roles = ["control", "dns", "proxy"],
      internal_ip = "10.70.0.10", public_ip = true, nested_virt = false, boot_disk_gb = 50 },
      { name = "worker-0", primary_role = "worker", roles = ["worker"], machine_type = "m8i.4xlarge",
      internal_ip = "10.70.1.10", public_ip = false, nested_virt = true, data_disk_gb = 500, boot_disk_gb = 200 },
    ]
  }
  assert {
    condition     = keys(aws_eip.public) == ["control-0"]
    error_message = "the public proxy/dns node gets an Elastic IP"
  }
  assert {
    condition     = aws_instance.node["control-0"].subnet_id == aws_subnet.public.id
    error_message = "the public proxy node must be in the public subnet"
  }
  assert {
    condition = alltrue([for r in aws_security_group.proxy_public.ingress :
    r.cidr_blocks == tolist(["0.0.0.0/0"])])
    error_message = "ingress_cidrs defaults to 0.0.0.0/0"
  }
}
