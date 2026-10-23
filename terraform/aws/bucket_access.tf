# bucket_access = "vpc": the data in the buckets the installer creates
# (backups, snapshot archive) is reachable only through this VPC's S3
# gateway endpoint. Every reader and writer is a node in the VPC (boxd signs
# its own requests; nothing hands out presigned URLs), so nothing else needs
# it. Bucket configuration stays manageable from anywhere with IAM, which is
# what terraform touches.
locals {
  vpc_only_buckets = var.bucket_access != "vpc" ? {} : merge(
    var.enable_backups ? { backups = aws_s3_bucket.backups[0] } : {},
    var.enable_snapshot_archive ? { snapshot_archive = aws_s3_bucket.snapshot_archive[0] } : {},
  )
}

# Routes S3 in this region through the VPC on both route tables. The
# provider leaves endpoint routes out of the inline route blocks, so they
# don't show up as drift on the route tables.
resource "aws_vpc_endpoint" "s3" {
  count             = length(local.vpc_only_buckets) > 0 ? 1 : 0
  vpc_id            = aws_vpc.vpc.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.public.id, aws_route_table.private.id]
  tags              = { Name = "${var.cluster_name}-s3" }
}

resource "aws_s3_bucket_policy" "vpc_only" {
  for_each = local.vpc_only_buckets
  bucket   = each.value.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DataOnlyThroughTheVpcEndpoint"
      Effect    = "Deny"
      Principal = "*"
      # Not s3:ListBucket: HeadBucket needs it, and the provider reads a 403
      # there as "bucket deleted", so the next plan from outside the VPC
      # would drop the bucket from state and tear down its policy, public
      # access block and lifecycle (hit on our AWS end-to-end cluster).
      # Object names stay listable with IAM; their contents don't.
      Action = ["s3:GetObject*", "s3:PutObject*", "s3:DeleteObject*",
        "s3:ListBucketVersions", "s3:ListBucketMultipartUploads",
      "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts", "s3:RestoreObject"]
      Resource  = [each.value.arn, "${each.value.arn}/*"]
      Condition = { StringNotEquals = { "aws:SourceVpce" = aws_vpc_endpoint.s3[0].id } }
    }]
  })
  # S3 rejects a bucket policy while the public access block is being written.
  depends_on = [aws_s3_bucket_public_access_block.backups, aws_s3_bucket_public_access_block.snapshot_archive]
}
