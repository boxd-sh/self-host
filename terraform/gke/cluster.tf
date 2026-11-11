# create_cluster: a GKE Standard cluster for boxd. VPC-native, private nodes
# behind Cloud NAT, the control plane reachable from admin_cidrs only, the
# legacy dataplane (Dataplane V2's eBPF would sit beside boxd's own TC
# programs on the workers), the Regular channel (mandatory for new
# projects) with node upgrades held until the end of support: a recreated
# worker node loses every machine on it, so nodes change only when apply
# changes them. Secrets are encrypted with a Cloud KMS key.

resource "google_kms_key_ring" "secrets" {
  count      = local.create ? 1 : 0
  name       = "${var.cluster_name}-${var.kms_suffix}"
  location   = var.region
  depends_on = [google_project_service.services]
  # A replacement (a new kms_suffix) makes the new one first: the cluster moves its
  # Secrets to it before the old one goes. Destroyed first, the cluster's Secrets were
  # left under a key scheduled for destruction, and the move hung.
  lifecycle {
    create_before_destroy = true
  }
}

resource "google_kms_crypto_key" "secrets" {
  count           = local.create ? 1 : 0
  name            = "gke-secrets"
  key_ring        = google_kms_key_ring.secrets[0].id
  rotation_period = "7776000s" # 90 days
  lifecycle {
    create_before_destroy = true
  }
}

# GKE's service agent encrypts and decrypts the cluster's Secrets with it.
resource "google_project_service_identity" "container" {
  count      = local.create ? 1 : 0
  provider   = google-beta
  project    = var.project
  service    = "container.googleapis.com"
  depends_on = [google_project_service.services]
}

resource "google_kms_crypto_key_iam_member" "gke_agent" {
  count         = local.create ? 1 : 0
  crypto_key_id = google_kms_crypto_key.secrets[0].id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:service-${data.google_project.this.number}@container-engine-robot.iam.gserviceaccount.com"
  depends_on    = [google_project_service_identity.container]
  lifecycle {
    create_before_destroy = true
  }
}

resource "google_container_cluster" "boxd" {
  count    = local.create ? 1 : 0
  name     = var.cluster_name
  location = local.regional ? var.region : var.zones[0]
  # Regional: the pools' zones.
  node_locations = local.regional ? var.zones : null

  network    = google_compute_network.vpc[0].id
  subnetwork = google_compute_subnetwork.nodes[0].id
  # boxd's pools only (pools.tf).
  remove_default_node_pool = true
  initial_node_count       = 1
  deletion_protection      = false

  networking_mode = "VPC_NATIVE"
  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }
  datapath_provider = "LEGACY_DATAPATH"

  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = false
  }
  master_authorized_networks_config {
    dynamic "cidr_blocks" {
      for_each = var.admin_cidrs
      content {
        cidr_block   = cidr_blocks.value
        display_name = "boxd admin ${cidr_blocks.key}"
      }
    }
  }

  workload_identity_config {
    workload_pool = "${var.project}.svc.id.goog"
  }

  release_channel {
    channel = "REGULAR"
  }
  maintenance_policy {
    # Control-plane patches land at weekends; node upgrades don't (the hold).
    recurring_window {
      start_time = "2026-01-03T02:00:00Z"
      end_time   = "2026-01-03T06:00:00Z"
      recurrence = "FREQ=WEEKLY;BYDAY=SA,SU"
    }
    maintenance_exclusion {
      exclusion_name = "boxd-hold"
      start_time     = var.hold_start
      exclusion_options {
        scope             = "NO_MINOR_OR_NODE_UPGRADES"
        end_time_behavior = "UNTIL_END_OF_SUPPORT"
      }
    }
  }

  database_encryption {
    state    = "ENCRYPTED"
    key_name = google_kms_crypto_key.secrets[0].id
  }

  logging_config {
    enable_components = var.observability ? ["SYSTEM_COMPONENTS", "WORKLOADS"] : ["SYSTEM_COMPONENTS"]
  }
  monitoring_config {
    enable_components = ["SYSTEM_COMPONENTS"]
    managed_prometheus {
      enabled = var.observability
    }
  }

  depends_on = [
    google_project_service.services,
    google_kms_crypto_key_iam_member.gke_agent,
    google_compute_router_nat.nat,
  ]

  lifecycle {
    # GKE fills in the exclusion's end (the version's end of support) and
    # moves it with upgrades: the hold is set once, at create.
    ignore_changes = [maintenance_policy[0].maintenance_exclusion]
  }
}
