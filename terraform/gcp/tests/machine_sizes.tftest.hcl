# terraform -chdir=terraform/gcp init -backend=false && terraform -chdir=terraform/gcp test
# Mocked providers: no GCP credentials needed. The worker type's specs are
# overridden to n2-standard-16's (16 vCPU / 64 GiB).

mock_provider "google" {
  override_data {
    target = data.google_compute_machine_types.worker
    values = { machine_types = [{ name = "n2-standard-16", guest_cpus = 16, memory_mb = 65536 }] }
  }
}
mock_provider "google-beta" {}

variables {
  project           = "tftest"
  region            = "europe-west4"
  zone              = "europe-west4-a"
  cluster_name      = "tftest"
  vpc_cidr          = "10.60.0.0/16"
  boot_image        = "projects/ubuntu-os-cloud/global/images/ubuntu-2404"
  deploy_ssh_pubkey = "ssh-ed25519 AAAA"
  enable_backups    = false
  nodes = [
    { name = "control-0", primary_role = "control", roles = ["control", "proxy"],
    internal_ip = "10.60.0.10", public_ip = true, nested_virt = false, boot_disk_gb = 50 },
    { name = "worker-0", primary_role = "worker", roles = ["worker"], machine_type = "n2-standard-16",
    internal_ip = "10.60.0.20", public_ip = false, nested_virt = true, data_disk_gb = 500, boot_disk_gb = 200 },
  ]
}

# Fresh project: the lookup waits for the Compute API, so it happens at
# apply time (a no-op against mocks). Later runs see the services in state
# and look up at plan time, like re-applies on an existing cluster.
run "sizes_that_fit_on_first_apply" {
  command = apply
  variables {
    machine_sizes = [{ vcpu = 1, memory_gb = 4 }, { vcpu = 16, memory_gb = 64 }]
  }
  assert {
    condition     = local.worker_types == toset(["n2-standard-16"])
    error_message = "only worker types are looked up"
  }
}

run "too_many_vcpus_fails_the_plan" {
  command = plan
  variables {
    machine_sizes = [{ vcpu = 1, memory_gb = 4 }, { vcpu = 32, memory_gb = 64 }]
  }
  expect_failures = [data.google_compute_machine_types.worker]
}

run "too_much_memory_only_warns" {
  command = plan
  variables {
    machine_sizes = [{ vcpu = 1, memory_gb = 128 }]
  }
  expect_failures = [check.machine_sizes_fit_worker_memory]
}

run "unknown_type_fails_the_plan" {
  command = plan
  override_data {
    target = data.google_compute_machine_types.worker
    values = { machine_types = [] }
  }
  variables {
    machine_sizes = [{ vcpu = 1, memory_gb = 4 }]
  }
  expect_failures = [data.google_compute_machine_types.worker]
}
