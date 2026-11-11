# What the installer reads back (tf.read_outputs): the cluster, its ranges
# and VPC MTU for the render, each edge's IP, the worker pool's instance
# groups (to delete a drained worker's node), the buckets.

output "gke" {
  value = {
    name          = local.cluster_name
    location      = local.cluster_location
    network       = local.network_link
    node_cidr     = local.node_cidr
    pods_cidr     = local.pods_cidr
    services_cidr = local.services_cidr
    vpc_mtu       = local.vpc_mtu
  }
}

output "edges" {
  value = [for i in range(var.edge_count) : { ip = google_compute_address.edge[tostring(i)].address }]
}

output "worker_instance_groups" {
  value = google_container_node_pool.worker.instance_group_urls
}

output "backups" {
  sensitive = true
  value = var.enable_backups ? {
    endpoint   = "https://storage.googleapis.com"
    bucket     = google_storage_bucket.backups[0].name
    region     = "auto"
    access_key = google_storage_hmac_key.backups[0].access_id
    secret_key = google_storage_hmac_key.backups[0].secret
  } : null
}

# No credentials: the workers reach the bucket as the worker nodes' account.
output "storage_bucket" {
  sensitive = true
  value = var.enable_storage_bucket ? {
    kind   = "gcs"
    bucket = google_storage_bucket.storage[0].name
  } : null
}
