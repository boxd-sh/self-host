# Every machine size must fit the worker type. Specs come from Compute
# Engine itself, read at plan time, so a bad size stops the plan before any
# pool is made or resized.

locals {
  worker_spec = {
    vcpu      = try(data.google_compute_machine_types.worker.machine_types[0].guest_cpus, 0)
    memory_gb = try(floor(data.google_compute_machine_types.worker.machine_types[0].memory_mb / 1024), 0)
  }
  # No swap on GKE nodes: a size bigger than a worker's RAM is a warning.
  sizes_over_worker_memory = [
    for s in var.machine_sizes : "${s.vcpu}x${s.memory_gb}" if s.memory_gb > local.worker_spec.memory_gb
  ]
}

data "google_compute_machine_types" "worker" {
  zone   = var.zones[0]
  filter = "name = \"${var.worker_machine_type}\""

  lifecycle {
    postcondition {
      condition     = length(self.machine_types) == 1
      error_message = "worker machine type ${var.worker_machine_type} is not offered in ${var.zones[0]}."
    }
    postcondition {
      condition     = alltrue([for s in var.machine_sizes : s.vcpu <= try(self.machine_types[0].guest_cpus, s.vcpu)])
      error_message = "[templates].sizes ${join(", ", [for s in var.machine_sizes : "${s.vcpu}x${s.memory_gb}" if s.vcpu > try(self.machine_types[0].guest_cpus, s.vcpu)])} need more vCPUs than worker type ${var.worker_machine_type} has (${try(self.machine_types[0].guest_cpus, "?")}). Use a larger worker type or smaller sizes."
    }
  }

  depends_on = [google_project_service.services]
}

check "machine_sizes_fit_worker_memory" {
  assert {
    condition     = length(local.sizes_over_worker_memory) == 0
    error_message = "[templates].sizes ${join(", ", local.sizes_over_worker_memory)} exceed a worker's RAM (${local.worker_spec.memory_gb} GiB on ${var.worker_machine_type}), and GKE nodes have no swap. Use a memory-optimised worker type (n2-highmem-*)."
  }
}
