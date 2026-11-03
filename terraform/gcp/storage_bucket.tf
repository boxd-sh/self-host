# [storage_engine] in cluster.toml: the bucket boxd's storage engine keeps
# machines' disks in, and a service account with access to it alone, attached
# to the worker instances (main.tf). No key exists: the workers' bucket.toml
# names no service_account, so boxd uses the metadata server. Kept on destroy
# while it holds objects (force_destroy = false).

resource "google_storage_bucket" "storage" {
  count    = var.enable_storage_bucket ? 1 : 0
  name     = "${var.project}-${var.cluster_name}-storage"
  location = var.region
  project  = var.project

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  # The engine uploads big layers in parts (GCS's XML multipart API through
  # boxd's client); an interrupted upload's parts would be billed forever.
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

resource "google_service_account" "storage" {
  count        = var.enable_storage_bucket ? 1 : 0
  account_id   = "${var.cluster_name}-storage"
  display_name = "boxd storage engine (the workers' bucket)"
  depends_on   = [google_project_service.services]
}

resource "google_storage_bucket_iam_member" "storage" {
  count  = var.enable_storage_bucket ? 1 : 0
  bucket = google_storage_bucket.storage[0].name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.storage[0].email}"
}
