# terraform -chdir=terraform/aws init -backend=false && terraform -chdir=terraform/aws test
# bucket_access = "vpc": an S3 gateway endpoint on both route tables and, on
# each installer-created bucket, a policy that denies data access from
# anywhere else. Also the snapshot archive bucket. Mocked provider.

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
    target          = aws_s3_bucket.snapshot_archive
    override_during = plan
    values          = { id = "tftest-archive", arn = "arn:aws:s3:::tftest-archive" }
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

run "vpc" {
  command = plan
  variables {
    bucket_access = "vpc"
  }
  assert {
    condition     = aws_vpc_endpoint.s3[0].service_name == "com.amazonaws.eu-central-1.s3" && aws_vpc_endpoint.s3[0].vpc_endpoint_type == "Gateway"
    error_message = "an S3 gateway endpoint for the region"
  }
  assert {
    condition     = aws_vpc_endpoint.s3[0].route_table_ids == toset(["rtb-public", "rtb-private"])
    error_message = "the endpoint must be on both route tables"
  }
  assert {
    condition = (jsondecode(aws_s3_bucket_policy.vpc_only["backups"].policy).Statement[0].Condition
    == { StringNotEquals = { "aws:SourceVpce" = "vpce-0123" } })
    error_message = "the policy must deny data access not through the endpoint"
  }
  assert {
    condition     = jsondecode(aws_s3_bucket_policy.vpc_only["backups"].policy).Statement[0].Effect == "Deny"
    error_message = "a deny, so no IAM grant can get around it"
  }
  assert {
    condition = !anytrue([for a in jsondecode(aws_s3_bucket_policy.vpc_only["backups"].policy).Statement[0].Action :
    a == "s3:ListBucket" || a == "s3:ListBucket*" || a == "s3:*"])
    error_message = "never deny s3:ListBucket: terraform's HeadBucket needs it, and a 403 there reads as a deleted bucket"
  }
  assert {
    condition     = keys(aws_s3_bucket_policy.vpc_only) == ["backups"]
    error_message = "no archive bucket, no archive policy"
  }
}

run "snapshot_archive" {
  command = plan
  variables {
    enable_snapshot_archive = true
  }
  assert {
    condition     = aws_s3_bucket.snapshot_archive[0].bucket == "tftest-archive-123456789012-eu-central-1"
    error_message = "the archive bucket is <cluster>-archive-<account>-<region>"
  }
  assert {
    condition = (one(one(aws_s3_bucket_lifecycle_configuration.snapshot_archive[0].rule).abort_incomplete_multipart_upload)
    .days_after_initiation == 1)
    error_message = "incomplete multipart uploads must be aborted"
  }
  assert {
    condition     = aws_iam_user.snapshot_archive[0].name == "tftest-snapshot-archive"
    error_message = "the archive has its own IAM user"
  }
  assert {
    condition = (jsondecode(aws_iam_user_policy.snapshot_archive[0].policy).Statement[1].Resource
    == "arn:aws:s3:::tftest-archive/*")
    error_message = "the archive key opens only the archive bucket"
  }
  assert {
    condition     = length(aws_vpc_endpoint.s3) == 0 && length(aws_s3_bucket_policy.vpc_only) == 0
    error_message = "bucket_access defaults to anywhere"
  }
}

run "snapshot_archive_vpc" {
  command = plan
  variables {
    enable_snapshot_archive = true
    bucket_access           = "vpc"
  }
  assert {
    condition     = toset(keys(aws_s3_bucket_policy.vpc_only)) == toset(["backups", "snapshot_archive"])
    error_message = "both buckets are VPC-only"
  }
  assert {
    condition     = length(aws_vpc_endpoint.s3) == 1
    error_message = "one endpoint serves both"
  }
  assert {
    condition = (jsondecode(aws_s3_bucket_policy.vpc_only["snapshot_archive"].policy).Statement[0].Resource
    == ["arn:aws:s3:::tftest-archive", "arn:aws:s3:::tftest-archive/*"])
    error_message = "the archive policy covers the archive bucket"
  }
}

run "snapshot_archive_only_vpc" {
  command = plan
  variables {
    enable_backups          = false
    enable_snapshot_archive = true
    bucket_access           = "vpc"
  }
  assert {
    condition     = keys(aws_s3_bucket_policy.vpc_only) == ["snapshot_archive"] && length(aws_vpc_endpoint.s3) == 1
    error_message = "the archive alone still gets the endpoint and its policy"
  }
}

run "anywhere" {
  command = plan
  assert {
    condition     = length(aws_vpc_endpoint.s3) == 0 && length(aws_s3_bucket_policy.vpc_only) == 0
    error_message = "the default adds no endpoint and no policy"
  }
}

run "vpc_without_backups" {
  command = plan
  variables {
    bucket_access  = "vpc"
    enable_backups = false
  }
  assert {
    condition     = length(aws_vpc_endpoint.s3) == 0 && length(aws_s3_bucket_policy.vpc_only) == 0
    error_message = "no bucket, nothing to restrict"
  }
}

run "bad_value" {
  command = plan
  variables {
    bucket_access = "private"
  }
  expect_failures = [var.bucket_access]
}
