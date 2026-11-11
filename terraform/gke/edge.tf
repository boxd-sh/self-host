# Each edge's address and passthrough network load balancer: every protocol
# and port (L3_DEFAULT) to that edge pool's one node, where the host-network
# proxy and dns take it. A Kubernetes Service can't express the proxy's port
# ranges, and kube-proxy would rewrite the destination SO_ORIGINAL_DST reads;
# a passthrough load balancer keeps the client's source and its own address.
# The GCE guest agent on the node routes that address locally.
#
# ingress = "internal": internal passthrough load balancers with addresses
# in the nodes' subnet, for clients on your VPN or interconnect.

locals {
  lb_scheme = local.internal ? "INTERNAL" : "EXTERNAL"
}

resource "google_compute_address" "edge" {
  for_each     = local.edges
  name         = "${var.cluster_name}-edge-${each.key}"
  region       = var.region
  address_type = local.internal ? "INTERNAL" : "EXTERNAL"
  subnetwork   = local.internal ? local.subnet_link : null
  address      = local.internal && length(var.internal_ips) > each.value ? var.internal_ips[each.value] : null
  depends_on   = [google_project_service.services]
}

resource "google_compute_region_health_check" "edge" {
  name   = "${var.cluster_name}-edge"
  region = var.region
  tcp_health_check {
    port = 443
  }
  depends_on = [google_project_service.services]
}

resource "google_compute_region_backend_service" "edge" {
  for_each              = local.edges
  name                  = "${var.cluster_name}-edge-${each.key}"
  region                = var.region
  load_balancing_scheme = local.lb_scheme
  protocol              = "UNSPECIFIED"
  health_checks         = [google_compute_region_health_check.edge.id]
  network               = local.internal ? local.network_link : null

  backend {
    # The node pool's managed instance group, as an instance group.
    group          = replace(google_container_node_pool.edge[each.key].instance_group_urls[0], "instanceGroupManagers", "instanceGroups")
    balancing_mode = "CONNECTION"
  }
}

resource "google_compute_forwarding_rule" "edge" {
  for_each              = local.edges
  name                  = "${var.cluster_name}-edge-${each.key}"
  region                = var.region
  load_balancing_scheme = local.lb_scheme
  ip_protocol           = "L3_DEFAULT"
  all_ports             = true
  ip_address            = google_compute_address.edge[each.key].address
  backend_service       = google_compute_region_backend_service.edge[each.key].id
  network               = local.internal ? local.network_link : null
  subnetwork            = local.internal ? local.subnet_link : null
}
