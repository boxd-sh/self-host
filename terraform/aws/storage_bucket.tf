# [storage_engine] in cluster.toml: the bucket boxd's storage engine keeps
# machines' disks in (layers, indexes, manifests, saved RAM), with its own IAM
# user so the engine's key opens nothing else. Only workers get the key
# (render.py, a 0600 /etc/boxd/bucket.toml). Kept on destroy: force_destroy
# is false, and a non-empty bucket stays (apply/destroy report it).

resource "aws_s3_bucket" "storage" {
  count         = var.enable_storage_bucket ? 1 : 0
  bucket        = "${var.cluster_name}-storage-${data.aws_caller_identity.current.account_id}-${var.region}"
  force_destroy = false
}

resource "aws_s3_bucket_public_access_block" "storage" {
  count                   = var.enable_storage_bucket ? 1 : 0
  bucket                  = aws_s3_bucket.storage[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# boxd sends no SSE header: encryption at rest is the bucket default. S3
# applies SSE-S3 anyway; this makes it explicit.
resource "aws_s3_bucket_server_side_encryption_configuration" "storage" {
  count  = var.enable_storage_bucket ? 1 : 0
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
  bucket = aws_s3_bucket.storage[0].id
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
# deletes and listing.
resource "aws_iam_user_policy" "storage" {
  count = var.enable_storage_bucket ? 1 : 0
  name  = "storage-engine-bucket"
  user  = aws_iam_user.storage[0].name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:ListBucketMultipartUploads", "s3:GetBucketLocation"]
        Resource = aws_s3_bucket.storage[0].arn
      },
      {
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject",
        "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
        Resource = "${aws_s3_bucket.storage[0].arn}/*"
      },
    ]
  })
}

resource "aws_iam_access_key" "storage" {
  count = var.enable_storage_bucket ? 1 : 0
  user  = aws_iam_user.storage[0].name
}
