# terraform -chdir=terraform/aws init -backend=false && terraform -chdir=terraform/aws test
# enable_storage_bucket: the storage engine's bucket, its own IAM user and
# key, and its place among the VPC-only buckets. Mocked provider.

mock_provider "aws" {
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  override_data {
    target = data.aws_ec2_instance_type.worker
    values = { default_vcpus = 16, memory_size = 65536 }
  }
  override_resource {
    target          = aws_route_table.public
    override_during = plan
    values          = { id = "rtb-public" }
  }
  override_resource {
    target          = aws_route_table.private
    override_during = plan
    values          = { id = "rtb-private" }
  }
  override_resource {
    target          = aws_vpc_endpoint.s3
    override_during = plan
    values          = { id = "vpce-0123" }
  }
  override_resource {
    target          = aws_s3_bucket.backups
    override_during = plan
    values          = { id = "tftest-backups", arn = "arn:aws:s3:::tftest-backups" }
  }
  override_resource {
    target          = aws_s3_bucket.storage
    override_during = plan
    values          = { id = "tftest-storage", arn = "arn:aws:s3:::tftest-storage" }
  }
}

variables {
  region              = "eu-central-1"
  availability_zone   = "eu-central-1a"
  cluster_name        = "tftest"
  vpc_cidr            = "10.70.0.0/16"
  public_subnet_cidr  = "10.70.0.0/24"
  private_subnet_cidr = "10.70.1.0/24"
  ami                 = "ami-0123456789abcdef0"
  deploy_ssh_pubkey   = "ssh-ed25519 AAAA"
  nodes = [
    { name = "control-0", primary_role = "control", roles = ["control", "dns", "proxy"],
    internal_ip = "10.70.1.11", public_ip = false, nested_virt = false, boot_disk_gb = 50 },
    { name = "worker-0", primary_role = "worker", roles = ["worker"], machine_type = "m8i.4xlarge",
    internal_ip = "10.70.1.10", public_ip = false, nested_virt = true, data_disk_gb = 500, boot_disk_gb = 200 },
  ]
}

run "off_by_default" {
  command = plan
  assert {
    condition     = length(aws_s3_bucket.storage) == 0 && length(aws_iam_user.storage) == 0
    error_message = "no [storage_engine], no bucket and no user"
  }
}

run "storage_bucket" {
  command = plan
  variables {
    enable_storage_bucket = true
  }
  assert {
    condition     = aws_s3_bucket.storage[0].bucket == "tftest-storage-123456789012-eu-central-1"
    error_message = "the bucket is <cluster>-storage-<account>-<region>"
  }
  assert {
    condition     = aws_s3_bucket.storage[0].force_destroy == false
    error_message = "destroy never empties the storage bucket"
  }
  assert {
    condition = (one(one(aws_s3_bucket_lifecycle_configuration.storage[0].rule).abort_incomplete_multipart_upload)
    .days_after_initiation == 1)
    error_message = "incomplete multipart uploads must be aborted"
  }
  assert {
    condition     = aws_s3_bucket_public_access_block.storage[0].restrict_public_buckets
    error_message = "the bucket is never public"
  }
  assert {
    condition     = aws_iam_user.storage[0].name == "tftest-storage-engine"
    error_message = "the engine has its own IAM user"
  }
  assert {
    condition = (jsondecode(aws_iam_user_policy.storage[0].policy).Statement[1].Resource
    == "arn:aws:s3:::tftest-storage/*")
    error_message = "the engine's key opens only the storage bucket"
  }
  assert {
    condition = contains(jsondecode(aws_iam_user_policy.storage[0].policy).Statement[1].Action,
    "s3:AbortMultipartUpload")
    error_message = "the engine aborts the multipart uploads it gives up on"
  }
}

run "storage_vpc" {
  command = plan
  variables {
    enable_storage_bucket = true
    bucket_access         = "vpc"
  }
  assert {
    condition     = toset(keys(aws_s3_bucket_policy.vpc_only)) == toset(["backups", "storage"])
    error_message = "the storage bucket is VPC-only too"
  }
  assert {
    condition = (jsondecode(aws_s3_bucket_policy.vpc_only["storage"].policy).Statement[0].Resource
    == ["arn:aws:s3:::tftest-storage", "arn:aws:s3:::tftest-storage/*"])
    error_message = "the storage policy covers the storage bucket"
  }
}

run "storage_only_vpc" {
  command = plan
  variables {
    enable_backups        = false
    enable_storage_bucket = true
    bucket_access         = "vpc"
  }
  assert {
    condition     = keys(aws_s3_bucket_policy.vpc_only) == ["storage"] && length(aws_vpc_endpoint.s3) == 1
    error_message = "the storage bucket alone still gets the endpoint and its policy"
  }
}
