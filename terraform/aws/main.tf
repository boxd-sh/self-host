locals {
  nodes_by_name = { for n in var.nodes : n.name => n }
  public_nodes  = { for n in var.nodes : n.name => n if n.public_ip }
  disk_nodes = {
    for n in var.nodes : n.name => n.data_disk_gb
    if n.data_disk_gb != null
  }
  instance_types = {
    for n in var.nodes : n.name => coalesce(n.machine_type, var.instance_type_defaults[n.primary_role])
  }
}

data "aws_partition" "current" {}

# [aws] vpc_id: the cluster goes into an existing VPC and its subnets, and
# none of the network below is created. Their CIDRs come from the VPC.
data "aws_vpc" "existing" {
  count = local.create_network ? 0 : 1
  id    = var.vpc_id
}

data "aws_subnet" "private" {
  count = local.create_network ? 0 : 1
  id    = var.private_subnet_id
}

data "aws_subnet" "public" {
  count = !local.create_network && var.public_subnet_id != null ? 1 : 0
  id    = var.public_subnet_id
}

locals {
  create_network    = var.vpc_id == null
  vpc_id            = local.create_network ? aws_vpc.vpc[0].id : data.aws_vpc.existing[0].id
  vpc_cidr          = local.create_network ? var.vpc_cidr : data.aws_vpc.existing[0].cidr_block
  private_subnet_id = local.create_network ? aws_subnet.private[0].id : data.aws_subnet.private[0].id
  # null in an existing VPC with private ingress: no node is public there.
  public_subnet_id = local.create_network ? aws_subnet.public[0].id : one(data.aws_subnet.public[*].id)
  # The installer's own route tables, for its S3 gateway endpoints.
  route_table_ids = local.create_network ? [aws_route_table.public[0].id, aws_route_table.private[0].id] : []
}

resource "aws_vpc" "vpc" {
  count                = local.create_network ? 1 : 0
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "${var.cluster_name}-vpc" }
}

resource "aws_internet_gateway" "igw" {
  count  = local.create_network ? 1 : 0
  vpc_id = aws_vpc.vpc[0].id
  tags   = { Name = "${var.cluster_name}-igw" }
}

resource "aws_subnet" "public" {
  count             = local.create_network ? 1 : 0
  vpc_id            = aws_vpc.vpc[0].id
  cidr_block        = var.public_subnet_cidr
  availability_zone = var.availability_zone
  tags              = { Name = "${var.cluster_name}-public" }
}

resource "aws_subnet" "private" {
  count             = local.create_network ? 1 : 0
  vpc_id            = aws_vpc.vpc[0].id
  cidr_block        = var.private_subnet_cidr
  availability_zone = var.availability_zone
  tags              = { Name = "${var.cluster_name}-private" }
}

resource "aws_eip" "nat" {
  count  = local.create_network ? 1 : 0
  domain = "vpc"
  tags   = { Name = "${var.cluster_name}-nat" }
}

resource "aws_nat_gateway" "nat" {
  count         = local.create_network ? 1 : 0
  allocation_id = aws_eip.nat[0].id
  subnet_id     = aws_subnet.public[0].id
  tags          = { Name = "${var.cluster_name}-nat" }
  depends_on    = [aws_internet_gateway.igw]
}

resource "aws_route_table" "public" {
  count  = local.create_network ? 1 : 0
  vpc_id = aws_vpc.vpc[0].id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.igw[0].id
  }
  tags = { Name = "${var.cluster_name}-public" }
}

resource "aws_route_table" "private" {
  count  = local.create_network ? 1 : 0
  vpc_id = aws_vpc.vpc[0].id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.nat[0].id
  }
  tags = { Name = "${var.cluster_name}-private" }
}

resource "aws_route_table_association" "public" {
  count          = local.create_network ? 1 : 0
  subnet_id      = aws_subnet.public[0].id
  route_table_id = aws_route_table.public[0].id
}

resource "aws_route_table_association" "private" {
  count          = local.create_network ? 1 : 0
  subnet_id      = aws_subnet.private[0].id
  route_table_id = aws_route_table.private[0].id
}

# Clusters from before vpc_id keep their network: the same resources, now counted.
moved {
  from = aws_vpc.vpc
  to   = aws_vpc.vpc[0]
}

moved {
  from = aws_internet_gateway.igw
  to   = aws_internet_gateway.igw[0]
}

moved {
  from = aws_subnet.public
  to   = aws_subnet.public[0]
}

moved {
  from = aws_subnet.private
  to   = aws_subnet.private[0]
}

moved {
  from = aws_eip.nat
  to   = aws_eip.nat[0]
}

moved {
  from = aws_nat_gateway.nat
  to   = aws_nat_gateway.nat[0]
}

moved {
  from = aws_route_table.public
  to   = aws_route_table.public[0]
}

moved {
  from = aws_route_table.private
  to   = aws_route_table.private[0]
}

moved {
  from = aws_route_table_association.public
  to   = aws_route_table_association.public[0]
}

moved {
  from = aws_route_table_association.private
  to   = aws_route_table_association.private[0]
}

# Node to node. In an existing VPC only the cluster's own nodes (self): the
# VPC's other workloads reach the proxy through ingress_cidrs, nothing else.
resource "aws_security_group" "internal" {
  name   = "${var.cluster_name}-internal"
  vpc_id = local.vpc_id
  ingress {
    protocol    = "-1"
    from_port   = 0
    to_port     = 0
    cidr_blocks = local.create_network ? [local.vpc_cidr] : []
    self        = !local.create_network
  }
  egress {
    protocol    = "-1"
    from_port   = 0
    to_port     = 0
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "proxy_public" {
  name   = "${var.cluster_name}-proxy-public"
  vpc_id = local.vpc_id
  dynamic "ingress" {
    # With [windows], each Windows machine's RDP port too (61000-65000); the proxy
    # redirects the band to its own RDP listener, which stays closed from outside.
    for_each = concat([[80, 80], [443, 443], [9443, 9443], [10000, 30000], [40000, 60000]],
    var.windows_rdp ? [[61000, 65000]] : [])
    content {
      protocol    = "tcp"
      from_port   = ingress.value[0]
      to_port     = ingress.value[1]
      cidr_blocks = var.ingress_cidrs
    }
  }
}

resource "aws_security_group" "dns_public" {
  name   = "${var.cluster_name}-dns-public"
  vpc_id = local.vpc_id
  dynamic "ingress" {
    for_each = ["tcp", "udp"]
    content {
      protocol  = ingress.value
      from_port = 53
      to_port   = 53
      # Not ingress_cidrs: an authoritative nameserver is queried by
      # resolvers and ACME validators, not by the users ingress_cidrs names.
      # With private ingress the dns nodes have no public IP anyway.
      cidr_blocks = ["0.0.0.0/0"]
    }
  }
}

resource "aws_iam_role" "node" {
  name = "${var.cluster_name}-node"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "node" {
  name = "${var.cluster_name}-node"
  role = aws_iam_role.node.name
}

resource "aws_key_pair" "deploy" {
  key_name   = "${var.cluster_name}-deploy"
  public_key = var.deploy_ssh_pubkey
}

resource "aws_eip" "public" {
  for_each = local.public_nodes
  domain   = "vpc"
  tags     = { Name = "${var.cluster_name}-${each.key}" }
}

resource "aws_ebs_volume" "data" {
  for_each          = local.disk_nodes
  availability_zone = var.availability_zone
  type              = "gp3"
  size              = each.value
  iops              = var.data_disk_iops
  throughput        = var.data_disk_throughput
  tags              = { Name = "${var.cluster_name}-${each.key}-data" }
}

resource "aws_instance" "node" {
  for_each = local.nodes_by_name

  ami                  = var.ami
  instance_type        = local.instance_types[each.key]
  subnet_id            = each.value.public_ip ? local.public_subnet_id : local.private_subnet_id
  private_ip           = each.value.internal_ip
  key_name             = aws_key_pair.deploy.key_name
  iam_instance_profile = aws_iam_instance_profile.node.name
  source_dest_check    = false
  vpc_security_group_ids = concat(
    [aws_security_group.internal.id],
    contains(each.value.roles, "proxy") ? [aws_security_group.proxy_public.id] : [],
    contains(each.value.roles, "dns") ? [aws_security_group.dns_public.id] : [],
  )

  root_block_device {
    volume_type = "gp3"
    volume_size = each.value.boot_disk_gb
  }

  dynamic "cpu_options" {
    for_each = each.value.nested_virt && !strcontains(local.instance_types[each.key], ".metal") ? [1] : []
    content {
      nested_virtualization = "enabled"
    }
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  tags = { Name = each.key, "boxd:roles" = join(",", each.value.roles) }

  depends_on = [aws_route_table_association.private, aws_nat_gateway.nat]

  lifecycle {
    ignore_changes = [ami]
    precondition {
      condition     = !each.value.public_ip || local.public_subnet_id != null
      error_message = "A public proxy/dns node in an existing VPC needs public_subnet_id."
    }
  }
}

resource "aws_eip_association" "public" {
  for_each      = local.public_nodes
  allocation_id = aws_eip.public[each.key].id
  instance_id   = aws_instance.node[each.key].id
}

resource "aws_volume_attachment" "data" {
  for_each    = local.disk_nodes
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.data[each.key].id
  instance_id = aws_instance.node[each.key].id

  stop_instance_before_detaching = true
}
