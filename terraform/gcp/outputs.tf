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

# The storage engine's store, for the workers' bucket.toml (render.py). No
# credentials: the workers reach the bucket as the service account attached
# to them (storage_bucket.tf), through the metadata server.
output "storage_bucket" {
  sensitive = true
  value = var.enable_storage_bucket ? {
    kind   = "gcs"
    bucket = google_storage_bucket.storage[0].name
  } : null
}

output "nodes" {
  value = {
    for name, n in google_compute_instance.node : name => {
      internal_ip = n.network_interface[0].network_ip
      external_ip = try(google_compute_address.public[name].address, null)
    }
  }
}
