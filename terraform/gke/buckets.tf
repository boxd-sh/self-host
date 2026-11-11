# The storage engine's bucket ([storage_engine], as terraform/gcp makes it)
# with the worker nodes' account on it alone, and the VM backups bucket
# with an HMAC key for boxd's S3 client. Kept on destroy while they hold
# objects (force_destroy = false).

resource "google_storage_bucket" "storage" {
  count    = var.enable_storage_bucket ? 1 : 0
  name     = "${var.project}-${var.cluster_name}-storage"
  location = var.region
  project  = var.project

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  # The engine uploads big layers in parts; an interrupted upload's parts
  # would be billed forever.
  lifecycle_rule {
    condition {
      age = 1
    }
    action {
      type = "AbortIncompleteMultipartUpload"
    }
  }

  depends_on = [google_project_service.services]
}

# No key anywhere: the workers' bucket.toml names none, and boxd reaches the
# bucket through the metadata server as the worker nodes' account (guests
# can't: the worker drops their traffic to 169.254.169.254).
resource "google_storage_bucket_iam_member" "storage" {
  count  = var.enable_storage_bucket ? 1 : 0
  bucket = google_storage_bucket.storage[0].name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_storage_bucket" "backups" {
  count    = var.enable_backups ? 1 : 0
  name     = "${var.project}-${var.cluster_name}-backups"
  location = var.region
  project  = var.project

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  depends_on = [google_project_service.services]
}

resource "google_service_account" "backups" {
  count        = var.enable_backups ? 1 : 0
  account_id   = "${var.cluster_name}-backups"
  display_name = "boxd backups writer (S3-interop HMAC)"
  depends_on   = [google_project_service.services]
}

resource "google_storage_bucket_iam_member" "backups_writer" {
  count  = var.enable_backups ? 1 : 0
  bucket = google_storage_bucket.backups[0].name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.backups[0].email}"
}

resource "google_storage_hmac_key" "backups" {
  count                 = var.enable_backups ? 1 : 0
  service_account_email = google_service_account.backups[0].email
}
