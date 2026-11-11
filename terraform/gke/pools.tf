# boxd's node pools, made in a cluster boxd made and in one of yours alike:
# dedicated to boxd (tainted boxd.sh/pool=<pool>:NoSchedule; every boxd pod
# tolerates it and selects its pool by the boxd.sh/pool label), sized only
# by apply (no autoscaling), never auto-repaired where a repair would
# recreate a worker.

locals {
  edges = { for i in range(var.edge_count) : tostring(i) => i }
}

resource "google_container_node_pool" "system" {
  name           = "${var.cluster_name}-system"
  cluster        = local.cluster_name
  location       = local.cluster_location
  node_locations = var.zones
  node_count     = var.system_nodes_per_zone

  management {
    auto_repair  = true
    auto_upgrade = local.auto_upgrade
  }

  node_config {
    machine_type    = var.system_machine_type
    image_type      = "COS_CONTAINERD"
    disk_type       = "pd-balanced"
    disk_size_gb    = 50
    service_account = google_service_account.node.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    labels          = { "boxd.sh/pool" = "system" }
    tags            = [local.tags.system]

    dynamic "taint" {
      for_each = var.system_tainted ? [1] : []
      content {
        key    = "boxd.sh/pool"
        value  = "system"
        effect = "NO_SCHEDULE"
      }
    }

    shielded_instance_config {
      enable_secure_boot          = true
      enable_integrity_monitoring = true
    }
  }

  depends_on = [google_project_iam_member.nodes]
}

# One pool of one node per edge, each in its own zone in turn: a proxy and a
# dns on the host network, behind that pool's own load balancer (edge.tf).
resource "google_container_node_pool" "edge" {
  for_each       = local.edges
  name           = "${var.cluster_name}-edge-${each.key}"
  cluster        = local.cluster_name
  location       = local.cluster_location
  node_locations = [var.zones[each.value % length(var.zones)]]
  node_count     = 1

  management {
    auto_repair  = true
    auto_upgrade = local.auto_upgrade
  }

  node_config {
    machine_type    = var.edge_machine_type
    image_type      = "UBUNTU_CONTAINERD"
    disk_type       = "pd-balanced"
    disk_size_gb    = 50
    service_account = google_service_account.node.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    labels = {
      "boxd.sh/pool" = "edge"
      "boxd.sh/edge" = each.key
    }
    tags = [local.tags.edge]

    taint {
      key    = "boxd.sh/pool"
      value  = "edge"
      effect = "NO_SCHEDULE"
    }

    shielded_instance_config {
      enable_secure_boot          = true
      enable_integrity_monitoring = true
    }
  }

  depends_on = [google_project_iam_member.nodes]
}

# Workers: nested virtualization, local NVMe SSDs (raw block: node-prep
# makes the RAID), Ubuntu (boxd's host kernel is an Ubuntu kernel), in the
# first zone. No auto-repair: a repaired node is a new one, without its
# machines. Only node_count may change on a running cluster (the installer
# refuses a plan that would recreate these nodes).
resource "google_container_node_pool" "worker" {
  name           = "${var.cluster_name}-worker"
  cluster        = local.cluster_name
  location       = local.cluster_location
  node_locations = [var.zones[0]]
  node_count     = var.worker_nodes

  management {
    auto_repair  = false
    auto_upgrade = local.auto_upgrade
  }

  node_config {
    machine_type    = var.worker_machine_type
    image_type      = "UBUNTU_CONTAINERD"
    disk_type       = "pd-balanced"
    disk_size_gb    = 100
    service_account = google_service_account.worker.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    labels = {
      "boxd.sh/pool" = "worker"
      "boxd.sh/disk" = var.worker_local_ssd > 0 ? "local-ssd" : "pd"
    }
    tags = [local.tags.worker]

    taint {
      key    = "boxd.sh/pool"
      value  = "worker"
      effect = "NO_SCHEDULE"
    }

    advanced_machine_features {
      threads_per_core             = 2
      enable_nested_virtualization = true
    }

    dynamic "local_nvme_ssd_block_config" {
      for_each = var.worker_local_ssd > 0 ? [1] : []
      content {
        local_ssd_count = var.worker_local_ssd
      }
    }

    shielded_instance_config {
      enable_secure_boot          = var.worker_secure_boot
      enable_integrity_monitoring = true
    }
  }

  depends_on = [google_project_iam_member.nodes]
}
