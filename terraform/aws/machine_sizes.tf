# Every machine size must fit every worker type. Specs come from EC2 itself
# (not parsed from the type name), read at plan time, so a bad size stops
# the plan before any instance is created or resized.

locals {
  worker_types = toset([
    for n in var.nodes : local.instance_types[n.name] if contains(n.roles, "worker")
  ])
  worker_specs = {
    for t, d in data.aws_ec2_instance_type.worker : t => {
      vcpu      = d.default_vcpus
      memory_gb = floor(d.memory_size / 1024) # MiB
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

data "aws_ec2_instance_type" "worker" {
  for_each      = local.worker_types
  instance_type = each.value

  lifecycle {
    # A guest can't have more vCPUs than its host.
    postcondition {
      condition     = alltrue([for s in var.machine_sizes : s.vcpu <= self.default_vcpus])
      error_message = "[templates].sizes ${join(", ", [for s in var.machine_sizes : "${s.vcpu}x${s.memory_gb}" if s.vcpu > self.default_vcpus])} need more vCPUs than worker type ${each.value} has (${self.default_vcpus}). Use a larger worker type or smaller sizes."
    }
  }
}

check "machine_sizes_fit_worker_memory" {
  assert {
    condition     = length(local.sizes_over_worker_memory) == 0
    error_message = "[templates].sizes exceed a worker's RAM and will rely on overcommit: ${join(", ", local.sizes_over_worker_memory)}. Use a memory-optimised worker type (R7i/R8i) for memory-heavy sizes."
  }
}
