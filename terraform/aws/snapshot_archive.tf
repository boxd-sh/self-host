# [snapshot_archive] in cluster.toml: the bucket `boxd-ctl snapshot archive`
# moves snapshots into, with its own IAM user so the archive's key opens
# nothing else. Only control and worker nodes get the key (render.py).

resource "aws_s3_bucket" "snapshot_archive" {
  count         = var.enable_snapshot_archive ? 1 : 0
  bucket        = "${var.cluster_name}-archive-${data.aws_caller_identity.current.account_id}-${var.region}"
  force_destroy = false
}

resource "aws_s3_bucket_public_access_block" "snapshot_archive" {
  count                   = var.enable_snapshot_archive ? 1 : 0
  bucket                  = aws_s3_bucket.snapshot_archive[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# boxd never sends an SSE header for the archive: encryption at rest is the
# bucket default. S3 applies SSE-S3 anyway; this makes it explicit.
resource "aws_s3_bucket_server_side_encryption_configuration" "snapshot_archive" {
  count  = var.enable_snapshot_archive ? 1 : 0
  bucket = aws_s3_bucket.snapshot_archive[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# An interrupted upload leaves parts no listing shows and boxd can't clean
# up; they'd be billed forever without this.
resource "aws_s3_bucket_lifecycle_configuration" "snapshot_archive" {
  count  = var.enable_snapshot_archive ? 1 : 0
  bucket = aws_s3_bucket.snapshot_archive[0].id
  rule {
    id     = "abort-incomplete-multipart-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

resource "aws_iam_user" "snapshot_archive" {
  count = var.enable_snapshot_archive ? 1 : 0
  name  = "${var.cluster_name}-snapshot-archive"
}

resource "aws_iam_user_policy" "snapshot_archive" {
  count = var.enable_snapshot_archive ? 1 : 0
  name  = "snapshot-archive-bucket"
  user  = aws_iam_user.snapshot_archive[0].name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:ListBucketMultipartUploads", "s3:GetBucketLocation"]
        Resource = aws_s3_bucket.snapshot_archive[0].arn
      },
      {
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject",
        "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
        Resource = "${aws_s3_bucket.snapshot_archive[0].arn}/*"
      },
    ]
  })
}

resource "aws_iam_access_key" "snapshot_archive" {
  count = var.enable_snapshot_archive ? 1 : 0
  user  = aws_iam_user.snapshot_archive[0].name
}
