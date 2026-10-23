output "backups" {
  sensitive = true
  value = var.enable_backups ? {
    endpoint   = "https://s3.${var.region}.${data.aws_partition.current.dns_suffix}"
    bucket     = aws_s3_bucket.backups[0].bucket
    region     = var.region
    access_key = aws_iam_access_key.backups[0].id
    secret_key = aws_iam_access_key.backups[0].secret
  } : null
}

output "snapshot_archive" {
  sensitive = true
  value = var.enable_snapshot_archive ? {
    endpoint   = "https://s3.${var.region}.${data.aws_partition.current.dns_suffix}"
    bucket     = aws_s3_bucket.snapshot_archive[0].bucket
    region     = var.region
    access_key = aws_iam_access_key.snapshot_archive[0].id
    secret_key = aws_iam_access_key.snapshot_archive[0].secret
  } : null
}

output "nodes" {
  value = {
    for name, n in aws_instance.node : name => {
      internal_ip    = n.private_ip
      external_ip    = try(aws_eip.public[name].public_ip, null)
      instance_id    = n.id
      data_volume_id = try(aws_ebs_volume.data[name].id, null)
    }
  }
}
