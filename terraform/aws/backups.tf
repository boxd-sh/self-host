data "aws_caller_identity" "current" {}

# Region in the name: a deleted bucket's name stays blocked in every OTHER
# region for up to an hour (CreateBucket -> OperationAborted, which the
# provider retries silently until the create timeout).
resource "aws_s3_bucket" "backups" {
  count         = var.enable_backups ? 1 : 0
  bucket        = "${var.cluster_name}-backups-${data.aws_caller_identity.current.account_id}-${var.region}"
  force_destroy = false
}

resource "aws_s3_bucket_public_access_block" "backups" {
  count                   = var.enable_backups ? 1 : 0
  bucket                  = aws_s3_bucket.backups[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_iam_user" "backups" {
  count = var.enable_backups ? 1 : 0
  name  = "${var.cluster_name}-backups"
}

resource "aws_iam_user_policy" "backups" {
  count = var.enable_backups ? 1 : 0
  name  = "backups-bucket"
  user  = aws_iam_user.backups[0].name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:ListBucketMultipartUploads", "s3:GetBucketLocation"]
        Resource = aws_s3_bucket.backups[0].arn
      },
      {
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject",
        "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
        Resource = "${aws_s3_bucket.backups[0].arn}/*"
      },
    ]
  })
}

resource "aws_iam_access_key" "backups" {
  count = var.enable_backups ? 1 : 0
  user  = aws_iam_user.backups[0].name
}
