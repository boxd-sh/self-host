locals {
  nodes_by_name = { for n in var.nodes : n.name => n }
  public_nodes  = { for n in var.nodes : n.name => n if n.public_ip }
  disk_nodes = {
    for n in var.nodes : n.name => n.data_disk_gb
    if n.data_disk_gb != null
  }
  machine_types = {
    for n in var.nodes : n.name => coalesce(n.machine_type, var.machine_type_defaults[n.primary_role])
  }
}

resource "google_project_service" "services" {
  for_each = toset([
    "compute.googleapis.com",
    "iam.googleapis.com",      # backups service account
    "iap.googleapis.com",      # ssh tunneling (the only ssh path)
    "publicca.googleapis.com", # ACME EAB minting for Google Trust Services
    "storage.googleapis.com"   # backups bucket + HMAC key
  ])
  service            = each.value
  disable_on_destroy = false
}

resource "google_compute_network" "vpc" {
  name                    = "${var.cluster_name}-vpc"
  auto_create_subnetworks = false
  mtu                     = var.mtu
  depends_on              = [google_project_service.services]
}

resource "google_compute_subnetwork" "subnet" {
  name          = "${var.cluster_name}-subnet"
  network       = google_compute_network.vpc.id
  ip_cidr_range = var.vpc_cidr
  region        = var.region
}

resource "google_compute_firewall" "internal" {
  name          = "${var.cluster_name}-internal"
  network       = google_compute_network.vpc.id
  source_ranges = [var.vpc_cidr]
  allow { protocol = "tcp" }
  allow { protocol = "udp" }
  allow { protocol = "icmp" }
}

resource "google_compute_router" "router" {
  name    = "${var.cluster_name}-router"
  network = google_compute_network.vpc.id
  region  = var.region
}

resource "google_compute_router_nat" "nat" {
  name                               = "${var.cluster_name}-nat"
  router                             = google_compute_router.router.name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"
}

resource "google_compute_firewall" "iap_ssh" {
  name          = "${var.cluster_name}-iap-ssh"
  network       = google_compute_network.vpc.id
  source_ranges = [var.iap_range]
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

resource "google_compute_firewall" "proxy_public" {
  name          = "${var.cluster_name}-proxy-public"
  network       = google_compute_network.vpc.id
  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["proxy"]
  allow {
    protocol = "tcp"
    ports    = ["80", "443", "9443", "10000-30000", "40000-60000"]
  }
}

resource "google_compute_firewall" "dns_public" {
  name          = "${var.cluster_name}-dns-public"
  network       = google_compute_network.vpc.id
  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["dns"]
  allow {
    protocol = "tcp"
    ports    = ["53"]
  }
  allow {
    protocol = "udp"
    ports    = ["53"]
  }
}

resource "google_compute_address" "public" {
  for_each   = local.public_nodes
  name       = "${var.cluster_name}-${each.key}"
  region     = var.region
  depends_on = [google_project_service.services]
}

resource "google_compute_disk" "data" {
  for_each   = local.disk_nodes
  name       = "${var.cluster_name}-${each.key}-data"
  type       = "pd-ssd"
  size       = each.value
  zone       = var.zone
  depends_on = [google_project_service.services]
}

resource "google_compute_instance" "node" {
  for_each = local.nodes_by_name

  name         = each.key
  machine_type = local.machine_types[each.key]
  zone         = var.zone
  tags         = each.value.roles

  boot_disk {
    initialize_params {
      image = var.boot_image
      size  = each.value.boot_disk_gb
      type  = "pd-ssd"
    }
  }

  dynamic "attached_disk" {
    for_each = contains(keys(local.disk_nodes), each.key) ? [1] : []
    content {
      source      = google_compute_disk.data[each.key].id
      device_name = "boxd-data"
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.subnet.id
    network_ip = each.value.internal_ip
    dynamic "access_config" {
      for_each = each.value.public_ip ? [1] : []
      content {
        nat_ip = google_compute_address.public[each.key].address
      }
    }
  }

  dynamic "advanced_machine_features" {
    for_each = each.value.nested_virt ? [1] : []
    content {
      enable_nested_virtualization = true
    }
  }

  can_ip_forward = true

  metadata = {
    ssh-keys       = "ubuntu:${var.deploy_ssh_pubkey}"
    enable-oslogin = "FALSE"
  }

  allow_stopping_for_update = true

  # On a first apply the machine-type lookup waits for the Compute API, so
  # it runs at apply time; this keeps its vCPU check ahead of the instances.
  depends_on = [data.google_compute_machine_types.worker]

  lifecycle {
    ignore_changes = [
      metadata["ssh-keys"],
      boot_disk[0].initialize_params[0].image,
    ]
  }
}

