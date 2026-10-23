resource "google_storage_bucket" "backups" {
  count = var.enable_backups ? 1 : 0
  name     = "${var.project}-backups"
  location = var.region
  project  = var.project

  uniform_bucket_level_access = true
  force_destroy = false

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
