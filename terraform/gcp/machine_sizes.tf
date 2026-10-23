# Every machine size must fit every worker type. Specs come from Compute
# Engine itself (not parsed from the type name), read at plan time, so a bad
# size stops the plan before any instance is created or resized.

locals {
  worker_types = toset([
    for n in var.nodes : local.machine_types[n.name] if contains(n.roles, "worker")
  ])
  worker_specs = {
    for t, d in data.google_compute_machine_types.worker : t => {
      vcpu      = d.machine_types[0].guest_cpus
      memory_gb = floor(d.machine_types[0].memory_mb / 1024)
    }
  }
  # Workers overcommit memory (swap is the capacity tier), so a size bigger
  # than a worker's RAM is a warning, not an error.
  sizes_over_worker_memory = flatten([
    for t, w in local.worker_specs : [
      for s in var.machine_sizes : "${s.vcpu}x${s.memory_gb} on ${t} (${w.memory_gb} GiB)"
      if s.memory_gb > w.memory_gb
    ]
  ])
}

data "google_compute_machine_types" "worker" {
  for_each = local.worker_types
  zone     = var.zone
  filter   = "name = \"${each.value}\""

  lifecycle {
    postcondition {
      condition     = length(self.machine_types) == 1
      error_message = "worker machine type ${each.value} is not offered in ${var.zone}."
    }
    # A guest can't have more vCPUs than its host. (try: an unknown type is
    # reported by the postcondition above, not as an index error here.)
    postcondition {
      condition     = alltrue([for s in var.machine_sizes : s.vcpu <= try(self.machine_types[0].guest_cpus, s.vcpu)])
      error_message = "[templates].sizes ${join(", ", [for s in var.machine_sizes : "${s.vcpu}x${s.memory_gb}" if s.vcpu > try(self.machine_types[0].guest_cpus, s.vcpu)])} need more vCPUs than worker type ${each.value} has (${try(self.machine_types[0].guest_cpus, "?")}). Use a larger worker type or smaller sizes."
    }
  }

  depends_on = [google_project_service.services]
}

check "machine_sizes_fit_worker_memory" {
  assert {
    condition     = length(local.sizes_over_worker_memory) == 0
    error_message = "[templates].sizes exceed a worker's RAM and will rely on overcommit: ${join(", ", local.sizes_over_worker_memory)}. Use a memory-optimised worker type (n2-highmem-*) for memory-heavy sizes."
  }
}
