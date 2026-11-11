# The network, the nodes' service accounts and the firewall. With
# create_cluster the VPC, its subnet and Cloud NAT are made here; otherwise
# the network is read from the existing cluster.

locals {
  create   = var.create_cluster
  regional = length(var.zones) > 1
  internal = var.ingress == "internal"

  cluster_name     = local.create ? google_container_cluster.boxd[0].name : var.existing_cluster_name
  cluster_location = local.create ? google_container_cluster.boxd[0].location : var.existing_cluster_location

  existing     = local.create ? null : data.google_container_cluster.existing[0]
  network_link = local.create ? google_compute_network.vpc[0].self_link : data.google_compute_subnetwork.existing[0].network
  subnet_link  = local.create ? google_compute_subnetwork.nodes[0].self_link : data.google_compute_subnetwork.existing[0].self_link

  node_cidr     = local.create ? var.vpc_cidr : data.google_compute_subnetwork.existing[0].ip_cidr_range
  pods_cidr     = local.create ? var.pods_cidr : local.existing.ip_allocation_policy[0].cluster_ipv4_cidr_block
  services_cidr = local.create ? var.services_cidr : local.existing.ip_allocation_policy[0].services_ipv4_cidr_block
  vpc_mtu       = local.create ? var.mtu : var.existing_vpc_mtu

  # A pool in a release channel must auto-upgrade (the hold below, or your
  # own maintenance exclusion, keeps it from doing so).
  auto_upgrade = local.create ? true : try(local.existing.release_channel[0].channel, "UNSPECIFIED") != "UNSPECIFIED"

  tags = {
    system = "${var.cluster_name}-system"
    edge   = "${var.cluster_name}-edge"
    worker = "${var.cluster_name}-worker"
  }
  # Google's health checkers (external and internal passthrough load balancers).
  health_check_ranges = ["35.191.0.0/16", "130.211.0.0/22", "209.85.152.0/22", "209.85.204.0/22"]
  # Between boxd's pools: Raft (control 7000, proxy 7001, dns 7002), the
  # proposal forwarder 8000, control's edge 7100, the worker's gRPC 9010 (also
  # live migration), the metrics ports, and Geneve (the routed network).
  boxd_tcp = ["7000-7002", "7100", "8000", "9010", "9101-9105"]
}

resource "google_project_service" "services" {
  for_each = toset(concat([
    "compute.googleapis.com",
    "container.googleapis.com",
    "iam.googleapis.com",
    "storage.googleapis.com",          # buckets, backups HMAC key
    "publicca.googleapis.com",         # ACME EAB minting for Google Trust Services
    "artifactregistry.googleapis.com", # images from Artifact Registry
    ], local.create ? ["cloudkms.googleapis.com"] : [],
    var.observability ? ["monitoring.googleapis.com", "logging.googleapis.com"] : []
  ))
  service            = each.value
  disable_on_destroy = false
}

data "google_project" "this" {}

# ---------------------------------------------------------------- network

resource "google_compute_network" "vpc" {
  count                   = local.create ? 1 : 0
  name                    = "${var.cluster_name}-vpc"
  auto_create_subnetworks = false
  mtu                     = var.mtu
  depends_on              = [google_project_service.services]
}

resource "google_compute_subnetwork" "nodes" {
  count                    = local.create ? 1 : 0
  name                     = "${var.cluster_name}-nodes"
  network                  = google_compute_network.vpc[0].id
  region                   = var.region
  ip_cidr_range            = var.vpc_cidr
  private_ip_google_access = true

  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = var.pods_cidr
  }
  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = var.services_cidr
  }
}

# Private nodes reach the internet (boxd's images, machines' egress) through NAT.
resource "google_compute_router" "router" {
  count   = local.create ? 1 : 0
  name    = "${var.cluster_name}-router"
  network = google_compute_network.vpc[0].id
  region  = var.region
}

resource "google_compute_router_nat" "nat" {
  count                              = local.create ? 1 : 0
  name                               = "${var.cluster_name}-nat"
  router                             = google_compute_router.router[0].name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"
}

data "google_container_cluster" "existing" {
  count    = local.create ? 0 : 1
  name     = var.existing_cluster_name
  location = var.existing_cluster_location
}

data "google_compute_subnetwork" "existing" {
  count  = local.create ? 0 : 1
  name   = reverse(split("/", data.google_container_cluster.existing[0].subnetwork))[0]
  region = var.region
}

# ------------------------------------------------------- service accounts

# Nodes of the system and edge pools.
resource "google_service_account" "node" {
  account_id   = "${var.cluster_name}-node"
  display_name = "boxd GKE nodes (system, edge)"
  depends_on   = [google_project_service.services]
}

# Worker nodes. Workers run on the host network, where Workload Identity
# doesn't apply: the storage engine reaches its bucket as this account,
# through the metadata server (buckets.tf grants it the bucket alone).
resource "google_service_account" "worker" {
  account_id   = "${var.cluster_name}-worker"
  display_name = "boxd GKE workers"
  depends_on   = [google_project_service.services]
}

locals {
  node_roles = ["roles/container.defaultNodeServiceAccount", "roles/artifactregistry.reader"]
  node_grants = {
    for pair in setproduct(["node", "worker"], local.node_roles) : "${pair[0]} ${pair[1]}" => pair
  }
}

resource "google_project_iam_member" "nodes" {
  for_each = local.node_grants
  project  = var.project
  role     = each.value[1]
  member   = "serviceAccount:${each.value[0] == "node" ? google_service_account.node.email : google_service_account.worker.email}"
}

# --------------------------------------------------------------- firewall

resource "google_compute_firewall" "edge_ingress" {
  name          = "${var.cluster_name}-edge-ingress"
  network       = local.network_link
  source_ranges = var.edge_source_ranges
  target_tags   = [local.tags.edge]
  allow {
    protocol = "tcp"
    ports    = var.edge_tcp_ports
  }
  allow {
    protocol = "udp"
    ports    = ["53"]
  }
}

resource "google_compute_firewall" "edge_health_checks" {
  name          = "${var.cluster_name}-edge-health-checks"
  network       = local.network_link
  source_ranges = local.health_check_ranges
  target_tags   = [local.tags.edge]
  allow {
    protocol = "tcp"
    ports    = ["443"]
  }
}

resource "google_compute_firewall" "between_pools" {
  name        = "${var.cluster_name}-between-pools"
  network     = local.network_link
  source_tags = values(local.tags)
  target_tags = values(local.tags)
  allow {
    protocol = "udp"
    ports    = ["6081"]
  }
  allow {
    protocol = "tcp"
    ports    = local.boxd_tcp
  }
  allow {
    protocol = "icmp"
  }
}

# Control and the console run on the pod network and dial the host-network
# roles (Raft, the proxy's API 9443).
resource "google_compute_firewall" "pods_to_hosts" {
  name          = "${var.cluster_name}-pods-to-hosts"
  network       = local.network_link
  source_ranges = [local.pods_cidr]
  target_tags   = [local.tags.edge, local.tags.worker]
  allow {
    protocol = "tcp"
    ports    = concat(local.boxd_tcp, ["9443"])
  }
}
