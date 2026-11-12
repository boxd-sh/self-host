# [storage_engine] in cluster.toml: the bucket boxd's storage engine keeps
# machines' disks in (layers, indexes, manifests, saved RAM), with its own IAM
# user so the engine's key opens nothing else. Only workers get the key
# (render.py, a 0600 /etc/boxd/bucket.toml). Kept on destroy: force_destroy
# is false, and a non-empty bucket stays (apply/destroy report it).

# [aws] storage_bucket_type = "express": an S3 Express One Zone directory
# bucket in the cluster's availability zone instead (lower latency, one AZ).
data "aws_availability_zone" "cluster" {
  name = var.availability_zone
}

locals {
  storage_standard = var.enable_storage_bucket && !var.storage_bucket_express
  storage_express  = var.enable_storage_bucket && var.storage_bucket_express
  # The one the engine uses, whichever kind: name and ARN.
  storage_bucket_name = local.storage_express ? aws_s3_directory_bucket.storage[0].bucket : (
  local.storage_standard ? aws_s3_bucket.storage[0].bucket : null)
  storage_bucket_arn = local.storage_express ? aws_s3_directory_bucket.storage[0].arn : (
  local.storage_standard ? aws_s3_bucket.storage[0].arn : null)
}

resource "aws_s3_directory_bucket" "storage" {
  count  = local.storage_express ? 1 : 0
  bucket = "${var.cluster_name}-storage-${data.aws_caller_identity.current.account_id}--${data.aws_availability_zone.cluster.zone_id}--x-s3"
  location {
    name = data.aws_availability_zone.cluster.zone_id
  }
  force_destroy = false
}

resource "aws_s3_bucket" "storage" {
  count         = local.storage_standard ? 1 : 0
  bucket        = "${var.cluster_name}-storage-${data.aws_caller_identity.current.account_id}-${var.region}"
  force_destroy = false
}

# A directory bucket blocks public access and encrypts with SSE-S3 always.
resource "aws_s3_bucket_public_access_block" "storage" {
  count                   = local.storage_standard ? 1 : 0
  bucket                  = aws_s3_bucket.storage[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# boxd sends no SSE header: encryption at rest is the bucket default. S3
# applies SSE-S3 anyway; this makes it explicit.
resource "aws_s3_bucket_server_side_encryption_configuration" "storage" {
  count  = local.storage_standard ? 1 : 0
  bucket = aws_s3_bucket.storage[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# The engine uploads big layers in parts; an interrupted upload leaves parts
# no listing shows, billed forever without this.
resource "aws_s3_bucket_lifecycle_configuration" "storage" {
  count  = var.enable_storage_bucket ? 1 : 0
  bucket = local.storage_bucket_name
  rule {
    id     = "abort-incomplete-multipart-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

resource "aws_iam_user" "storage" {
  count = var.enable_storage_bucket ? 1 : 0
  name  = "${var.cluster_name}-storage-engine"
}

# What the engine does there: conditional creates, multipart uploads,
# deletes and listing. A directory bucket authorizes every object request
# through a CreateSession session instead, whose ReadWrite mode covers them.
resource "aws_iam_user_policy" "storage" {
  count = var.enable_storage_bucket ? 1 : 0
  name  = "storage-engine-bucket"
  user  = aws_iam_user.storage[0].name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = local.storage_express ? [
      {
        Effect   = "Allow"
        Action   = ["s3express:CreateSession"]
        Resource = local.storage_bucket_arn
      },
      ] : [
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:ListBucketMultipartUploads", "s3:GetBucketLocation"]
        Resource = local.storage_bucket_arn
      },
      {
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject",
        "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
        Resource = "${local.storage_bucket_arn}/*"
      },
    ]
  })
}

# Workers reach the zonal endpoint through the VPC, not the NAT gateway. Not
# in an existing VPC: its route tables aren't the installer's to change.
resource "aws_vpc_endpoint" "s3express" {
  count             = local.storage_express && local.create_network ? 1 : 0
  vpc_id            = local.vpc_id
  service_name      = "com.amazonaws.${var.region}.s3express"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = local.route_table_ids
  tags              = { Name = "${var.cluster_name}-s3express" }
}

resource "aws_iam_access_key" "storage" {
  count = var.enable_storage_bucket ? 1 : 0
  user  = aws_iam_user.storage[0].name
}
