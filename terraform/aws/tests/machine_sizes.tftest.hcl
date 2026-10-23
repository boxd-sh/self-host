# terraform -chdir=terraform/aws init -backend=false && terraform -chdir=terraform/aws test
# Mocked provider: no AWS credentials needed. The worker type's specs are
# overridden to m8i.4xlarge's (16 vCPU / 64 GiB).

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
    { name = "control-0", primary_role = "control", roles = ["control", "proxy"],
    internal_ip = "10.70.0.10", public_ip = true, nested_virt = false, boot_disk_gb = 50 },
    { name = "worker-0", primary_role = "worker", roles = ["worker"], machine_type = "m8i.4xlarge",
    internal_ip = "10.70.1.10", public_ip = false, nested_virt = true, data_disk_gb = 500, boot_disk_gb = 200 },
  ]
}

run "sizes_that_fit" {
  command = plan
  variables {
    machine_sizes = [{ vcpu = 1, memory_gb = 4 }, { vcpu = 16, memory_gb = 64 }]
  }
  assert {
    condition     = local.worker_types == toset(["m8i.4xlarge"])
    error_message = "only worker types are looked up"
  }
}

run "too_many_vcpus_fails_the_plan" {
  command = plan
  variables {
    machine_sizes = [{ vcpu = 1, memory_gb = 4 }, { vcpu = 32, memory_gb = 64 }]
  }
  expect_failures = [data.aws_ec2_instance_type.worker]
}

run "too_much_memory_only_warns" {
  command = plan
  variables {
    machine_sizes = [{ vcpu = 1, memory_gb = 128 }]
  }
  expect_failures = [check.machine_sizes_fit_worker_memory]
}
