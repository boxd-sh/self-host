# terraform -chdir=terraform/gke init -backend=false && terraform -chdir=terraform/gke test
# The GKE target's cloud side, applied against mocked providers: the cluster
# and network only with create_cluster, boxd's pools and edges in both modes.

mock_provider "google" {
  override_data {
    target = data.google_compute_machine_types.worker
    values = { machine_types = [{ name = "n2-standard-16", guest_cpus = 16, memory_mb = 65536 }] }
  }
  override_data {
    target = data.google_project.this
    values = { number = "123456789" }
  }
  override_data {
    target = data.google_container_cluster.existing
    values = {
      subnetwork      = "projects/tftest/regions/europe-west4/subnetworks/platform-nodes"
      release_channel = [{ channel = "REGULAR" }]
      ip_allocation_policy = [{
        cluster_ipv4_cidr_block  = "172.30.0.0/16"
        services_ipv4_cidr_block = "172.31.0.0/20"
      }]
    }
  }
  override_data {
    target = data.google_compute_subnetwork.existing
    values = {
      ip_cidr_range = "10.80.0.0/20"
      network       = "https://www.googleapis.com/compute/v1/projects/tftest/global/networks/platform"
      self_link     = "https://www.googleapis.com/compute/v1/projects/tftest/regions/europe-west4/subnetworks/platform-nodes"
    }
  }
  override_resource {
    target = google_container_node_pool.edge
    values = { instance_group_urls = ["https://www.googleapis.com/compute/v1/projects/tftest/zones/europe-west4-a/instanceGroupManagers/gke-edge-grp"] }
  }
  override_resource {
    target = google_service_account.node
    values = { email = "tftest-node@tftest.iam.gserviceaccount.com" }
  }
  override_resource {
    target = google_service_account.worker
    values = { email = "tftest-worker@tftest.iam.gserviceaccount.com" }
  }
}
mock_provider "google-beta" {}

variables {
  project               = "tftest"
  region                = "europe-west4"
  cluster_name          = "tftest"
  create_cluster        = true
  zones                 = ["europe-west4-a"]
  vpc_cidr              = "10.60.0.0/20"
  admin_cidrs           = ["203.0.113.4/32"]
  system_machine_type   = "e2-standard-4"
  system_nodes_per_zone = 3
  system_tainted        = false
  edge_machine_type     = "n2-standard-4"
  edge_count            = 2
  worker_machine_type   = "n2-standard-16"
  worker_nodes          = 2
  worker_local_ssd      = 4
  worker_secure_boot    = false
  ingress               = "public"
  edge_tcp_ports        = ["53", "80", "443", "9443", "10000-30000", "40000-60000"]
  edge_source_ranges    = ["0.0.0.0/0"]
  observability         = true
  dashboard_json        = "{\"displayName\": \"boxd\"}"
  enable_backups        = true
  kms_suffix            = "abc123"
  hold_start            = "2026-11-10T00:00:00Z"
  machine_sizes         = [{ vcpu = 2, memory_gb = 8 }]
}

run "create" {
  command = apply
  assert {
    condition     = length(google_container_cluster.boxd) == 1 && length(google_compute_network.vpc) == 1
    error_message = "create_cluster makes the cluster and its network"
  }
  assert {
    condition     = google_container_cluster.boxd[0].datapath_provider == "LEGACY_DATAPATH"
    error_message = "the legacy dataplane, not Dataplane V2"
  }
  assert {
    condition     = google_container_cluster.boxd[0].database_encryption[0].state == "ENCRYPTED"
    error_message = "Secrets encrypted with the KMS key"
  }
  assert {
    condition     = google_compute_network.vpc[0].mtu == 8896
    error_message = "VPC MTU 8896, so guests keep 1500 under Geneve"
  }
  assert {
    condition     = google_container_node_pool.worker.node_count == 2 && google_container_node_pool.worker.management[0].auto_repair == false
    error_message = "workers: the size given, never auto-repaired"
  }
  assert {
    condition     = google_container_node_pool.worker.node_config[0].advanced_machine_features[0].enable_nested_virtualization
    error_message = "nested virtualization on workers"
  }
  assert {
    condition     = google_container_node_pool.worker.node_config[0].local_nvme_ssd_block_config[0].local_ssd_count == 4
    error_message = "local NVMe SSDs as raw block"
  }
  assert {
    condition     = google_container_node_pool.worker.node_config[0].shielded_instance_config[0].enable_secure_boot == false
    error_message = "boxd's host kernel: Secure Boot off"
  }
  assert {
    condition     = length(google_container_node_pool.edge) == 2 && length(google_compute_forwarding_rule.edge) == 2
    error_message = "one pool and one load balancer per edge"
  }
  assert {
    condition     = google_compute_forwarding_rule.edge["0"].ip_protocol == "L3_DEFAULT" && google_compute_forwarding_rule.edge["0"].load_balancing_scheme == "EXTERNAL"
    error_message = "public edges: external passthrough, every protocol"
  }
  assert {
    condition     = length(google_container_node_pool.system.node_config[0].taint) == 0
    error_message = "the system pool of a cluster boxd made is not tainted (GKE's own pods run there)"
  }
  assert {
    condition     = length(google_monitoring_dashboard.boxd) == 1
    error_message = "observability makes the dashboard"
  }
  assert {
    condition     = length(google_storage_bucket.storage) == 0 && length(google_storage_bucket.backups) == 1
    error_message = "backups bucket by default, storage bucket only with the engine"
  }
}

run "existing" {
  command = apply
  variables {
    create_cluster            = false
    existing_cluster_name     = "platform"
    existing_cluster_location = "europe-west4"
    existing_vpc_mtu          = 1460
    vpc_cidr                  = ""
    admin_cidrs               = []
    system_nodes_per_zone     = 1
    system_tainted            = true
    kms_suffix                = ""
    hold_start                = ""
  }
  assert {
    condition     = length(google_container_cluster.boxd) == 0 && length(google_compute_network.vpc) == 0 && length(google_kms_key_ring.secrets) == 0
    error_message = "an existing cluster: no cluster, network or key made"
  }
  assert {
    condition     = google_container_node_pool.worker.cluster == "platform" && google_container_node_pool.worker.location == "europe-west4"
    error_message = "boxd's pools go in the existing cluster"
  }
  assert {
    condition     = output.gke.node_cidr == "10.80.0.0/20" && output.gke.pods_cidr == "172.30.0.0/16" && output.gke.vpc_mtu == 1460
    error_message = "the ranges and MTU come from the existing cluster"
  }
  assert {
    condition     = google_container_node_pool.system.node_config[0].taint[0].key == "boxd.sh/pool"
    error_message = "in your cluster every boxd pool is tainted"
  }
  assert {
    condition     = one(google_compute_firewall.pods_to_hosts.source_ranges) == "172.30.0.0/16"
    error_message = "the pod range reaches the host-network roles"
  }
}

run "internal_stock_kernel" {
  command = apply
  variables {
    ingress            = "internal"
    internal_ips       = ["10.60.0.250"]
    edge_source_ranges = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
    worker_secure_boot = true
    worker_local_ssd   = 0
    observability      = false
    dashboard_json     = ""
  }
  assert {
    condition     = google_compute_forwarding_rule.edge["0"].load_balancing_scheme == "INTERNAL" && google_compute_address.edge["0"].address_type == "INTERNAL"
    error_message = "internal edges: internal passthrough load balancers"
  }
  assert {
    condition     = google_compute_address.edge["0"].address == "10.60.0.250"
    error_message = "internal_ips pins an edge's address"
  }
  assert {
    condition     = google_container_node_pool.worker.node_config[0].shielded_instance_config[0].enable_secure_boot
    error_message = "the stock kernel keeps Secure Boot"
  }
  assert {
    condition     = length(google_container_node_pool.worker.node_config[0].local_nvme_ssd_block_config) == 0
    error_message = "local_ssd = 0: no local SSDs"
  }
  assert {
    condition     = length(google_monitoring_dashboard.boxd) == 0 && google_container_cluster.boxd[0].monitoring_config[0].managed_prometheus[0].enabled == false
    error_message = "observability off: no dashboard, no managed Prometheus"
  }
}
