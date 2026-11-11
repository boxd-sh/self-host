# terraform/gke: the cloud side of boxd on Google Kubernetes Engine. The
# installer writes every variable (clouds/gke.py tfvars) and renders the
# Kubernetes objects itself (kube_render.py); nothing here touches the
# cluster's API.

variable "project" { type = string }
variable "region" { type = string }
# [cluster].name: prefixes every resource, and names the GKE cluster with
# create_cluster.
variable "cluster_name" { type = string }

# [gke] cluster = "create": the network, the cluster and its Secrets key are
# made here. Otherwise the cluster existing_cluster_name in
# existing_cluster_location is read, and only boxd's own pieces are made.
variable "create_cluster" { type = bool }
variable "existing_cluster_name" {
  type    = string
  default = ""
}
variable "existing_cluster_location" {
  type    = string
  default = ""
}
# An existing cluster's VPC MTU, read by the installer (passed through to
# the render, which sets guests' MTU from it).
variable "existing_vpc_mtu" {
  type    = number
  default = 1460
}

# The zones of var.region the pools go in. One: a zonal cluster. Several: a
# regional cluster; the system pool and the edges spread over them, the
# worker pool stays in the first.
variable "zones" { type = list(string) }

# create_cluster only: the nodes' subnet, GKE's pod and service ranges
# (secondary ranges of that subnet), and who may reach the control plane.
variable "vpc_cidr" {
  type    = string
  default = ""
}
variable "pods_cidr" {
  type    = string
  default = "172.20.0.0/16"
}
variable "services_cidr" {
  type    = string
  default = "172.24.0.0/20"
}
variable "admin_cidrs" {
  type    = list(string)
  default = []
}
# Geneve takes 50 bytes: 8896 leaves guests their own 1500.
variable "mtu" {
  type    = number
  default = 8896
}

variable "system_machine_type" { type = string }
variable "system_nodes_per_zone" { type = number }
# In a cluster of yours the system pool is tainted like the others (GKE's own
# system pods run on your pools); in one boxd made it isn't (they run here).
variable "system_tainted" { type = bool }

variable "edge_machine_type" { type = string }
# One node pool of one node per edge, each behind its own load balancer IP:
# every proxy needs its own address (machines' SSH ports are per proxy).
variable "edge_count" { type = number }

variable "worker_machine_type" { type = string }
# Never below the workers running now (the installer passes the larger
# number): a smaller pool would lose a node GKE picks, and every machine on
# it. The installer drains a worker and deletes its node itself.
variable "worker_nodes" { type = number }
variable "worker_local_ssd" { type = number }
# host_kernel = "stock": Secure Boot on. boxd's host kernel is not signed by
# Google, so with host_kernel = "boxd" it is off.
variable "worker_secure_boot" { type = bool }

variable "ingress" {
  type = string
  validation {
    condition     = contains(["public", "internal"], var.ingress)
    error_message = "ingress is public or internal."
  }
}
variable "internal_ips" {
  type    = list(string)
  default = []
}
variable "edge_tcp_ports" { type = list(string) }
variable "edge_source_ranges" { type = list(string) }

variable "observability" { type = bool }
# The Cloud Monitoring dashboard (gke_dashboard.py); "" makes none.
variable "dashboard_json" {
  type    = string
  default = ""
}

variable "enable_backups" {
  type    = bool
  default = true
}
variable "enable_storage_bucket" {
  type    = bool
  default = false
}

# create_cluster: the Secrets key ring's suffix (a key ring outlives
# destroy) and when the hold on node upgrades starts.
variable "kms_suffix" {
  type    = string
  default = ""
}
variable "hold_start" {
  type    = string
  default = ""
}

variable "machine_sizes" {
  type = list(object({
    vcpu      = number
    memory_gb = number
  }))
  default = []
}
