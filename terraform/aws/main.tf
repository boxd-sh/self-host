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

resource "aws_vpc" "vpc" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "${var.cluster_name}-vpc" }
}

resource "aws_internet_gateway" "igw" {
  vpc_id = aws_vpc.vpc.id
  tags   = { Name = "${var.cluster_name}-igw" }
}

resource "aws_subnet" "public" {
  vpc_id            = aws_vpc.vpc.id
  cidr_block        = var.public_subnet_cidr
  availability_zone = var.availability_zone
  tags              = { Name = "${var.cluster_name}-public" }
}

resource "aws_subnet" "private" {
  vpc_id            = aws_vpc.vpc.id
  cidr_block        = var.private_subnet_cidr
  availability_zone = var.availability_zone
  tags              = { Name = "${var.cluster_name}-private" }
}

resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = { Name = "${var.cluster_name}-nat" }
}

resource "aws_nat_gateway" "nat" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public.id
  tags          = { Name = "${var.cluster_name}-nat" }
  depends_on    = [aws_internet_gateway.igw]
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.vpc.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.igw.id
  }
  tags = { Name = "${var.cluster_name}-public" }
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.vpc.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.nat.id
  }
  tags = { Name = "${var.cluster_name}-private" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "private" {
  subnet_id      = aws_subnet.private.id
  route_table_id = aws_route_table.private.id
}

resource "aws_security_group" "internal" {
  name   = "${var.cluster_name}-internal"
  vpc_id = aws_vpc.vpc.id
  ingress {
    protocol    = "-1"
    from_port   = 0
    to_port     = 0
    cidr_blocks = [var.vpc_cidr]
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
  vpc_id = aws_vpc.vpc.id
  dynamic "ingress" {
    for_each = [[80, 80], [443, 443], [9443, 9443], [10000, 30000], [40000, 60000]]
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
  vpc_id = aws_vpc.vpc.id
  dynamic "ingress" {
    for_each = ["tcp", "udp"]
    content {
      protocol    = ingress.value
      from_port   = 53
      to_port     = 53
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
  subnet_id            = each.value.public_ip ? aws_subnet.public.id : aws_subnet.private.id
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
