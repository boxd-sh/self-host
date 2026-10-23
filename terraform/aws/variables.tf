variable "region" { type = string }
variable "availability_zone" { type = string }
variable "cluster_name" { type = string }
variable "vpc_cidr" { type = string }
variable "public_subnet_cidr" { type = string }
variable "private_subnet_cidr" { type = string }
variable "ami" { type = string }

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

# Source CIDRs for the proxy_public SG (dns_public stays open: resolvers,
# not users, query it). The installer passes the VPC in private ingress
# (Twingate connector).
variable "ingress_cidrs" {
  type    = list(string)
  default = ["0.0.0.0/0"]
}

variable "instance_type_defaults" {
  type = map(string)
  default = {
    control    = "m7i.2xlarge"
    worker     = "m8i.8xlarge" # nested virt (C/M/R 7i+8i) or any *.metal
    proxy      = "m7i.xlarge"
    dns        = "m7i.xlarge"
    console    = "m7i.xlarge"
    metrics    = "m7i.2xlarge"
    monitoring = "m7i.xlarge"
  }
}

variable "data_disk_iops" {
  type    = number
  default = 12000
}

variable "data_disk_throughput" {
  type    = number
  default = 500
}

variable "enable_backups" {
  type    = bool
  default = true
}

# [snapshot_archive] declared in cluster.toml (and [aws]
# auto_snapshot_archive not false): snapshot_archive.tf.
variable "enable_snapshot_archive" {
  type    = bool
  default = false
}

# "vpc": the installer-created buckets' data only through the VPC's S3
# gateway endpoint (bucket_access.tf). "anywhere": IAM alone decides.
variable "bucket_access" {
  type    = string
  default = "anywhere"
  validation {
    condition     = contains(["anywhere", "vpc"], var.bucket_access)
    error_message = "bucket_access must be \"anywhere\" or \"vpc\"."
  }
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
