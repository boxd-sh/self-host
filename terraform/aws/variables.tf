variable "region" { type = string }
variable "availability_zone" { type = string }
variable "cluster_name" { type = string }
# The VPC the installer creates. null with vpc_id.
variable "vpc_cidr" {
  type    = string
  default = null
}
variable "public_subnet_cidr" {
  type    = string
  default = null
}
variable "private_subnet_cidr" {
  type    = string
  default = null
}

# [aws] vpc_id: an existing VPC and its subnets instead (main.tf). The public
# subnet only with public ingress; s3_endpoint_id only with bucket_access = "vpc".
variable "vpc_id" {
  type    = string
  default = null
  validation {
    condition = var.vpc_id != null || (var.vpc_cidr != null && var.public_subnet_cidr != null
    && var.private_subnet_cidr != null)
    error_message = "Without vpc_id, vpc_cidr, public_subnet_cidr and private_subnet_cidr are required."
  }
  validation {
    condition     = var.vpc_id == null || var.private_subnet_id != null
    error_message = "vpc_id needs private_subnet_id."
  }
  validation {
    condition = var.vpc_id == null || (var.vpc_cidr == null && var.public_subnet_cidr == null
    && var.private_subnet_cidr == null)
    error_message = "With vpc_id the CIDRs come from the existing VPC: leave vpc_cidr and the subnet CIDRs unset."
  }
  validation {
    condition = var.vpc_id != null || (var.private_subnet_id == null && var.public_subnet_id == null
    && var.s3_endpoint_id == null)
    error_message = "private_subnet_id, public_subnet_id and s3_endpoint_id are only for an existing VPC (vpc_id)."
  }
}
variable "private_subnet_id" {
  type    = string
  default = null
}
variable "public_subnet_id" {
  type    = string
  default = null
}
variable "s3_endpoint_id" {
  type    = string
  default = null
}
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

# [storage_engine] declared in cluster.toml (and [aws] auto_storage_bucket
# not false): storage_bucket.tf.
variable "enable_storage_bucket" {
  type    = bool
  default = false
}

# [aws] storage_bucket_type = "express": the storage bucket is an S3 Express
# One Zone directory bucket in availability_zone (storage_bucket.tf).
variable "storage_bucket_express" {
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
