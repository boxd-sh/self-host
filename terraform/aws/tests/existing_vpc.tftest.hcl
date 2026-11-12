# terraform -chdir=terraform/aws init -backend=false && terraform -chdir=terraform/aws test
# vpc_id: an existing VPC and its subnets. Nothing network-level is created,
# nodes go in the given subnets, the internal SG admits only the nodes, and
# bucket_access = "vpc" uses the VPC's S3 endpoint. Mocked provider.

mock_provider "aws" {
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_availability_zone" {
    defaults = { zone_id = "euc1-az2" }
  }
  override_data {
    target = data.aws_ec2_instance_type.worker
    values = { default_vcpus = 16, memory_size = 65536 }
  }
  override_data {
    target = data.aws_vpc.existing
    values = { id = "vpc-existing", cidr_block = "10.60.0.0/16" }
  }
  override_data {
    target = data.aws_subnet.private
    values = { id = "subnet-private" }
  }
  override_data {
    target = data.aws_subnet.public
    values = { id = "subnet-public" }
  }
  override_resource {
    target          = aws_s3_bucket.backups
    override_during = plan
    values          = { id = "tftest-backups", arn = "arn:aws:s3:::tftest-backups" }
  }
}

variables {
  region            = "eu-central-1"
  availability_zone = "eu-central-1a"
  cluster_name      = "tftest"
  vpc_id            = "vpc-existing"
  private_subnet_id = "subnet-private"
  ami               = "ami-0123456789abcdef0"
  deploy_ssh_pubkey = "ssh-ed25519 AAAA"
  ingress_cidrs     = ["10.60.0.0/16"]
  nodes = [
    { name = "control-0", primary_role = "control", roles = ["control", "dns", "proxy"],
    internal_ip = "10.60.7.250", public_ip = false, nested_virt = false, boot_disk_gb = 50 },
    { name = "worker-0", primary_role = "worker", roles = ["worker"], machine_type = "m8i.4xlarge",
    internal_ip = "10.60.7.251", public_ip = false, nested_virt = true, data_disk_gb = 500, boot_disk_gb = 200 },
  ]
}

run "private" {
  command = plan
  assert {
    condition = (length(aws_vpc.vpc) + length(aws_internet_gateway.igw) + length(aws_subnet.public)
      + length(aws_subnet.private) + length(aws_eip.nat) + length(aws_nat_gateway.nat)
      + length(aws_route_table.public) + length(aws_route_table.private)
    + length(aws_route_table_association.public) + length(aws_route_table_association.private)) == 0
    error_message = "an existing VPC gets no VPC, gateway, NAT, subnet or route table"
  }
  assert {
    condition     = length(data.aws_subnet.public) == 0
    error_message = "private ingress needs no public subnet"
  }
  assert {
    condition     = alltrue([for n in aws_instance.node : n.subnet_id == "subnet-private"])
    error_message = "every node is in the given private subnet"
  }
  assert {
    condition = alltrue([for sg in [aws_security_group.internal, aws_security_group.proxy_public,
    aws_security_group.dns_public] : sg.vpc_id == "vpc-existing"])
    error_message = "the security groups are in the existing VPC"
  }
  assert {
    condition = alltrue([for r in aws_security_group.internal.ingress :
    r.self && length(r.cidr_blocks) == 0])
    error_message = "in an existing VPC the internal SG admits only the cluster's own nodes"
  }
  assert {
    condition = alltrue([for r in aws_security_group.proxy_public.ingress :
    r.cidr_blocks == tolist(["10.60.0.0/16"])])
    error_message = "clients still reach the proxy through ingress_cidrs"
  }
  assert {
    condition     = length(aws_eip.public) == 0
    error_message = "private ingress must not allocate Elastic IPs"
  }
}

run "public" {
  command = plan
  variables {
    public_subnet_id = "subnet-public"
    ingress_cidrs    = ["0.0.0.0/0"]
    nodes = [
      { name = "control-0", primary_role = "control", roles = ["control", "dns", "proxy"],
      internal_ip = "10.60.0.250", public_ip = true, nested_virt = false, boot_disk_gb = 50 },
      { name = "worker-0", primary_role = "worker", roles = ["worker"], machine_type = "m8i.4xlarge",
      internal_ip = "10.60.7.251", public_ip = false, nested_virt = true, data_disk_gb = 500, boot_disk_gb = 200 },
    ]
  }
  assert {
    condition     = aws_instance.node["control-0"].subnet_id == "subnet-public"
    error_message = "the public proxy node goes in the given public subnet"
  }
  assert {
    condition     = aws_instance.node["worker-0"].subnet_id == "subnet-private"
    error_message = "the worker goes in the given private subnet"
  }
  assert {
    condition     = keys(aws_eip.public) == ["control-0"] && length(aws_nat_gateway.nat) == 0
    error_message = "an Elastic IP for the public node, and still no NAT gateway"
  }
}

run "bucket_access_vpc" {
  command = plan
  variables {
    bucket_access  = "vpc"
    s3_endpoint_id = "vpce-existing"
  }
  assert {
    condition     = length(aws_vpc_endpoint.s3) == 0
    error_message = "no second S3 gateway endpoint in an existing VPC"
  }
  assert {
    condition = (jsondecode(aws_s3_bucket_policy.vpc_only["backups"].policy).Statement[0].Condition
    == { StringNotEquals = { "aws:SourceVpce" = "vpce-existing" } })
    error_message = "the policy names the VPC's own endpoint"
  }
}

run "storage_express" {
  command = plan
  variables {
    enable_backups         = false
    enable_storage_bucket  = true
    storage_bucket_express = true
  }
  assert {
    condition     = length(aws_vpc_endpoint.s3express) == 0
    error_message = "no s3express endpoint on route tables that aren't the installer's"
  }
}

run "public_node_needs_public_subnet" {
  command = plan
  variables {
    nodes = [
      { name = "control-0", primary_role = "control", roles = ["control", "dns", "proxy"],
      internal_ip = "10.60.0.250", public_ip = true, nested_virt = false, boot_disk_gb = 50 },
    ]
  }
  expect_failures = [aws_instance.node]
}

run "needs_private_subnet" {
  command = plan
  variables {
    private_subnet_id = null
  }
  expect_failures = [var.vpc_id]
}

run "no_cidrs_with_vpc_id" {
  command = plan
  variables {
    vpc_cidr = "10.60.0.0/16"
  }
  expect_failures = [var.vpc_id]
}

run "cidrs_without_vpc_id" {
  command = plan
  variables {
    vpc_id            = null
    private_subnet_id = null
  }
  expect_failures = [var.vpc_id]
}

run "ids_without_vpc_id" {
  command = plan
  variables {
    vpc_id              = null
    vpc_cidr            = "10.70.0.0/16"
    public_subnet_cidr  = "10.70.0.0/24"
    private_subnet_cidr = "10.70.1.0/24"
  }
  expect_failures = [var.vpc_id]
}
