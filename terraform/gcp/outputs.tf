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

output "nodes" {
  value = {
    for name, n in google_compute_instance.node : name => {
      internal_ip = n.network_interface[0].network_ip
      external_ip = try(google_compute_address.public[name].address, null)
    }
  }
}
