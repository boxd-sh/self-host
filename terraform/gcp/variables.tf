variable "project" { type = string }
variable "region" { type = string }
variable "zone" { type = string }
variable "cluster_name" { type = string }
variable "vpc_cidr" { type = string }
variable "boot_image" { type = string }

variable "deploy_ssh_pubkey" { type = string }

variable "nodes" {
  type = list(object({
    name         = string
    primary_role = string
    roles        = list(string)
    internal_ip  = string
    machine_type = optional(string)
    public_ip    = bool
    nested_virt  = bool
    data_disk_gb = optional(number)
    boot_disk_gb = number
  }))
}

variable "mtu" {
  type    = number
  default = 8896
}

variable "machine_type_defaults" {
  type = map(string)
  default = {
    control    = "n2-standard-8"
    worker     = "n2-standard-32" # must support nested virtualization
    proxy      = "n2-standard-4"
    dns        = "n2-standard-4"
    console    = "n2-standard-4"
    metrics    = "n2-standard-8"
    monitoring = "n2-standard-4"
  }
}

variable "enable_backups" {
  type    = bool
  default = true
}

variable "iap_range" {
  type    = string
  default = "35.235.240.0/20"
}

# The installer's effective [templates].sizes (configured or default) —
# checked against the real worker types in machine_sizes.tf.
variable "machine_sizes" {
  type = list(object({
    vcpu      = number
    memory_gb = number
  }))
  default = []
}
