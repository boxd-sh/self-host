# terraform -chdir=terraform/gcp init -backend=false && terraform -chdir=terraform/gcp test
# enable_storage_bucket: the storage engine's bucket, a service account with
# access to it alone, attached to worker instances only (no key). Mocked.

mock_provider "google" {
  override_data {
    target = data.google_compute_machine_types.worker
    values = { machine_types = [{ name = "n2-standard-16", guest_cpus = 16, memory_mb = 65536 }] }
  }
  override_resource {
    target          = google_service_account.storage
    override_during = plan
    values          = { email = "tftest-storage@tftest.iam.gserviceaccount.com" }
  }
}
mock_provider "google-beta" {}

variables {
  project           = "tftest"
  region            = "europe-west4"
  zone              = "europe-west4-a"
  cluster_name      = "tftest"
  vpc_cidr          = "10.60.0.0/16"
  boot_image        = "projects/ubuntu-os-cloud/global/images/ubuntu-2404"
  deploy_ssh_pubkey = "ssh-ed25519 AAAA"
  enable_backups    = false
  nodes = [
    { name = "control-0", primary_role = "control", roles = ["control", "proxy"],
    internal_ip = "10.60.0.10", public_ip = true, nested_virt = false, boot_disk_gb = 50 },
    { name = "worker-0", primary_role = "worker", roles = ["worker"], machine_type = "n2-standard-16",
    internal_ip = "10.60.0.20", public_ip = false, nested_virt = true, data_disk_gb = 500, boot_disk_gb = 200 },
  ]
}

run "off_by_default" {
  command = plan
  assert {
    condition     = length(google_storage_bucket.storage) == 0 && length(google_service_account.storage) == 0
    error_message = "no [storage_engine], no bucket and no account"
  }
  assert {
    condition     = length(google_compute_instance.node["worker-0"].service_account) == 0
    error_message = "without the bucket, nodes keep no service account"
  }
}

run "storage_bucket" {
  command = plan
  variables {
    enable_storage_bucket = true
  }
  assert {
    condition     = google_storage_bucket.storage[0].name == "tftest-tftest-storage"
    error_message = "the bucket is <project>-<cluster>-storage"
  }
  assert {
    condition     = google_storage_bucket.storage[0].public_access_prevention == "enforced"
    error_message = "the bucket is never public"
  }
  assert {
    condition     = google_storage_bucket.storage[0].force_destroy == false
    error_message = "destroy never empties the storage bucket"
  }
  assert {
    condition = (one(one(google_storage_bucket.storage[0].lifecycle_rule).action).type
    == "AbortIncompleteMultipartUpload")
    error_message = "incomplete multipart uploads must be aborted"
  }
  assert {
    condition     = google_storage_bucket_iam_member.storage[0].role == "roles/storage.objectAdmin"
    error_message = "the account administers the bucket's objects only"
  }
  assert {
    condition = (one(google_compute_instance.node["worker-0"].service_account).email
    == "tftest-storage@tftest.iam.gserviceaccount.com")
    error_message = "workers run as the storage account"
  }
  assert {
    condition     = length(google_compute_instance.node["control-0"].service_account) == 0
    error_message = "only workers get the storage account"
  }
}
