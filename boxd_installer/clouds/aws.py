"""AWS: SSM-tunnelled ssh (no inbound 22 anywhere), one S3 bucket for
terraform + installer state, ZeroSSL as the default ACME CA (an AWS cluster
must not depend on a GCP project, which GTS key minting needs)."""
import base64
import ipaddress
import json
import os

from ..util import InstallError, log, run

REQUIRED = ("region", "availability_zone", "vpc_cidr", "public_subnet_cidr",
            "private_subnet_cidr", "state_bucket")
# [aws] vpc_id: an existing VPC and subnets instead of the ones terraform
# makes. Their CIDRs are read from AWS (existing_network), never written here.
NETWORK_CIDRS = ("vpc_cidr", "public_subnet_cidr", "private_subnet_cidr")
EXISTING_VPC_KEYS = ("vpc_id", "private_subnet_id", "public_subnet_id", "s3_endpoint_id")
# None: the NIC is read from each host (the device holding its internal_ip;
# render.IFACE_PLACEHOLDER). AWS names it ens5 on some instance types and
# enp39s0 on others (m8i), so no one name is right.
DEFAULT_INTERFACE = None
# [network] guest_mtu in routed mode when cluster.toml sets none: 0, the
# guest's own 1500. The VPC carries 9001, so Geneve's 50 bytes fit.
ROUTED_GUEST_MTU = 0
DEFAULT_CA = "zerossl"
INGRESS_MODES = ("public", "private")
BUCKET_ACCESS = ("anywhere", "vpc")
TF_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "terraform", "aws"))
SSH_PATH = "SSM"
SSH_HINT = ("ssm:StartSession on the instances and that their SSM agent is "
            "online (`aws ssm describe-instance-information`)")
AMI_PARAMETER = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
# S3 bucket names max out at 63 chars; ours are <name>-backups-<account-id>-<region>,
# <name>-archive-<account-id>-<region> and <name>-storage-<account-id>-<region>
# (all the same length).
BUCKET_NAME_MAX = 63
ACCOUNT_ID_LEN = 12

def _public(raw_node: dict, ingress: str) -> bool:
    """Same rule as tf.build_tfvars' public_ip: in public ingress, proxy/dns
    nodes get an Elastic IP and live in the public subnet. Everything else,
    and every node in private ingress, egresses via NAT."""
    return ingress == "public" and any(
        r.partition(":")[0] in ("proxy", "dns") for r in raw_node.get("roles", []))

def required(aws: dict) -> tuple[str, ...]:
    """With vpc_id, existing_network has filled in the CIDRs; a private
    cluster there has no public subnet."""
    if "vpc_id" in aws and aws.get("ingress", "public") == "private":
        return tuple(k for k in REQUIRED if k != "public_subnet_cidr")
    return REQUIRED

def _describe(region: str, *args: str) -> dict:
    res = run(["aws", "ec2", *args, "--region", region, "--output", "json"], capture=True)
    return json.loads(res.stdout)

def existing_network(aws: dict, describe=None) -> None:
    """[aws] vpc_id: check the existing VPC and subnets the cluster goes
    into, and put their CIDRs in `aws` (vpc_cidr, private_subnet_cidr,
    public_subnet_cidr), so validation and rendering work on them as on a VPC
    terraform makes. Called before validation; network_errors checks the
    routes, DNS and S3 endpoint."""
    errs = []
    errs += [f"[aws].{k} comes from vpc_id's VPC and subnets: remove it" for k in NETWORK_CIDRS
             if k in aws]
    errs += [f"[aws].{k} must be a quoted id string" for k in EXISTING_VPC_KEYS
             if k in aws and not (isinstance(aws[k], str) and aws[k])]
    errs += [f"[aws].vpc_id needs {k}" for k in ("region", "availability_zone") if k not in aws]
    public = aws.get("ingress", "public") == "public"
    if "private_subnet_id" not in aws:
        errs.append("[aws].vpc_id needs private_subnet_id")
    if public and "public_subnet_id" not in aws:
        errs.append('[aws].vpc_id needs public_subnet_id with ingress = "public" (proxy and '
                    "dns nodes get Elastic IPs there)")
    elif not public and "public_subnet_id" in aws:
        errs.append('[aws].public_subnet_id is only for ingress = "public": with private '
                    "ingress every node is in private_subnet_id")
    vpc_access = aws.get("bucket_access", "anywhere") == "vpc"
    if vpc_access and "s3_endpoint_id" not in aws:
        errs.append('[aws].bucket_access = "vpc" with vpc_id needs s3_endpoint_id: the VPC\'s '
                    "own S3 gateway endpoint (apply doesn't add a second one to its route tables)")
    elif not vpc_access and "s3_endpoint_id" in aws:
        errs.append('[aws].s3_endpoint_id is only for bucket_access = "vpc"')
    if errs:
        raise InstallError("cluster.toml invalid:\n  " + "\n  ".join(errs))

    describe = describe or _describe
    vpc_id = aws["vpc_id"]
    vpc = _lookup(describe, aws, "vpc_id", "describe-vpcs", "--vpc-ids", "Vpcs")
    subnet_ids = {"private": aws["private_subnet_id"]}
    if public:
        subnet_ids["public"] = aws["public_subnet_id"]
    aws["vpc_cidr"] = vpc["CidrBlock"]
    primary = ipaddress.ip_network(vpc["CidrBlock"])
    secondary = [ipaddress.ip_network(a["CidrBlock"]) for a in vpc.get("CidrBlockAssociationSet", [])
                 if a.get("CidrBlockState", {}).get("State") == "associated"
                 and a["CidrBlock"] != vpc["CidrBlock"]]
    for label, sid in subnet_ids.items():
        sub = _lookup(describe, aws, f"{label}_subnet_id", "describe-subnets", "--subnet-ids",
                      "Subnets")
        if sub["VpcId"] != vpc_id:
            errs.append(f"[aws].{label}_subnet_id {sid} is in {sub['VpcId']}, not vpc_id {vpc_id}")
        if sub["AvailabilityZone"] != aws["availability_zone"]:
            errs.append(f"[aws].{label}_subnet_id {sid} is in {sub['AvailabilityZone']}, not "
                        f"availability_zone {aws['availability_zone']} (where the data disks go)")
        aws[f"{label}_subnet_cidr"] = sub["CidrBlock"]
        cidr = ipaddress.ip_network(sub["CidrBlock"])
        if not cidr.subnet_of(primary):
            errs += [f"[aws].{label}_subnet_id {sid} ({cidr}) is in the VPC's secondary CIDR "
                     f"{sec}, not its primary {primary}: only subnets in the primary CIDR are "
                     "supported" for sec in secondary if cidr.subnet_of(sec)]
    if public and not errs:
        pub, priv = (ipaddress.ip_network(aws[f"{k}_subnet_cidr"]) for k in ("public", "private"))
        if aws["public_subnet_id"] == aws["private_subnet_id"] or pub.overlaps(priv):
            errs.append("[aws].public_subnet_id and private_subnet_id must be two different "
                        "subnets (their CIDRs overlap)")
    if errs:
        raise InstallError("cluster.toml invalid:\n  " + "\n  ".join(errs))
    log(f"[aws] vpc_id {vpc_id}: VPC {aws['vpc_cidr']} (resolver "
        f"{ipaddress.ip_network(aws['vpc_cidr']).network_address + 2}), "
        + ", ".join(f"{label} subnet {aws[f'{label}_subnet_cidr']}" for label in subnet_ids))

def _lookup(describe, aws: dict, key: str, op: str, flag: str, field: str) -> dict:
    """The one VPC, subnet or endpoint [aws].<key> names. A missing id, or one
    this AWS login may not describe, stops here with the setting named."""
    ident = aws[key]
    try:
        found = describe(aws["region"], op, flag, ident)[field]
    except InstallError as e:
        why = str(e).strip().splitlines()[-1]
        raise InstallError(f"[aws].{key} {ident}: AWS can't find it in {aws['region']}, or "
                           f"this login may not describe it ({why})") from None
    if not found:
        raise InstallError(f"[aws].{key} {ident}: not found in {aws['region']}")
    return found[0]

def check_network_switch(cfg, mode: str | None, ids: dict | None = None) -> None:
    """Refuse to move a cluster that exists between a VPC terraform made and
    an existing one (vpc_id), or to another existing VPC or subnet: terraform
    would destroy the network its nodes are in, or replace every node. `mode`
    and `ids` are tf.state_network's and tf.state_network_ids'."""
    if mode == "created" and "vpc_id" in cfg.cloud:
        raise InstallError(
            "this cluster runs in the VPC apply made, and cluster.toml now sets [aws] vpc_id. "
            "Moving it would destroy that VPC under its nodes: remove vpc_id (and put the "
            "CIDRs back), or install the existing-VPC cluster as a new one (another cluster "
            "name or state bucket)")
    if mode == "existing" and "vpc_id" not in cfg.cloud:
        raise InstallError(
            "this cluster runs in an existing VPC ([aws] vpc_id), and cluster.toml no longer "
            "sets it. Making a VPC would replace every node: put vpc_id and the subnet ids "
            "back, or install the new-VPC cluster as a new one (another cluster name or "
            "state bucket)")
    # A subnet only one side has is an ingress switch, not a swap.
    swapped = [f"[aws].{k} {cfg.cloud[k]} (the cluster is in {was})"
               for k, was in (ids or {}).items() if cfg.cloud.get(k) not in (None, was)]
    if mode == "existing" and swapped:
        raise InstallError(
            "this cluster runs in another existing VPC or subnet than cluster.toml names: "
            + "; ".join(swapped) + ". Moving it would replace every node and security group "
            "(instance-store workers lose their data): put the ids back, or install it as a "
            "new cluster (another cluster name or state bucket)")

def network_errors(aws: dict, describe=None) -> list[str]:
    """[aws] vpc_id, after existing_network: what the nodes need from the
    existing network (a default route, the internet gateway for public
    ingress, VPC DNS, the S3 endpoint). Only validate and apply check this,
    so destroy and state still work on a network that changed under them."""
    describe = describe or _describe
    region, vpc_id = aws["region"], aws["vpc_id"]
    tables = _read(describe, region, f"{vpc_id}'s route tables", "describe-route-tables",
                   "--filters", f"Name=vpc-id,Values={vpc_id}")["RouteTables"]
    main = next((t for t in tables if any(a.get("Main") for a in t.get("Associations", []))), None)
    subnet_tables = {}
    for label in ("private", "public"):
        if sid := aws.get(f"{label}_subnet_id"):
            subnet_tables[sid] = next((t for t in tables if any(
                a.get("SubnetId") == sid for a in t.get("Associations", []))), main)
    errs = []
    for label, need_igw in (("private", False), ("public", True)):
        sid = aws.get(f"{label}_subnet_id")
        if sid is None:
            continue
        table = subnet_tables[sid]
        rtb = table["RouteTableId"] if table else "(none)"
        targets = [_target(r) for r in (table or {}).get("Routes", [])
                   if r.get("DestinationCidrBlock") == "0.0.0.0/0" and r.get("State") != "blackhole"]
        if not targets:
            need = ("needs a 0.0.0.0/0 route to an internet gateway (igw-…) so the proxy and dns "
                    "nodes' Elastic IPs can be reached" if need_igw else
                    "has no 0.0.0.0/0 route, so the nodes there can't download the release "
                    "(add one to a NAT gateway)")
            errs.append(f"[aws].{label}_subnet_id {sid}: its route table {rtb} {need}")
        elif need_igw and not any(t.startswith("igw-") for t in targets):
            errs.append(f"[aws].public_subnet_id {sid}: its route table {rtb} sends 0.0.0.0/0 to "
                        f"{', '.join(targets)}, not an internet gateway (igw-…), so the proxy "
                        "and dns nodes' Elastic IPs can't be reached")
    dns = _read(describe, region, f"{vpc_id}'s DNS support", "describe-vpc-attribute",
                "--vpc-id", vpc_id, "--attribute", "enableDnsSupport")
    if not dns.get("EnableDnsSupport", {}).get("Value"):
        errs.append(f"[aws].vpc_id {vpc_id} has DNS support (enableDnsSupport) off: the nodes "
                    "use the VPC's resolver")
    if aws.get("bucket_access") == "vpc":
        errs += _endpoint_errors(aws, subnet_tables, describe)
    return errs

def _read(describe, region: str, what: str, *args: str) -> dict:
    try:
        return describe(region, *args)
    except InstallError as e:
        why = str(e).strip().splitlines()[-1]
        raise InstallError(f"can't read {what} (aws ec2 {args[0]}): {why}") from None

ROUTE_TARGETS = ("GatewayId", "NatGatewayId", "TransitGatewayId", "NetworkInterfaceId",
                 "InstanceId", "VpcPeeringConnectionId", "CarrierGatewayId", "LocalGatewayId")

def _target(route: dict) -> str:
    return next((route[k] for k in ROUTE_TARGETS if route.get(k)), "?")

def _endpoint_errors(aws: dict, subnet_tables: dict, describe) -> list[str]:
    """The S3 gateway endpoint the buckets' policy names must be available
    and on every cluster subnet's route table; otherwise the nodes' own
    requests are denied."""
    region, eid = aws["region"], aws["s3_endpoint_id"]
    ep = _lookup(describe, aws, "s3_endpoint_id", "describe-vpc-endpoints", "--vpc-endpoint-ids",
                 "VpcEndpoints")
    if (ep["VpcId"] != aws["vpc_id"] or ep["VpcEndpointType"] != "Gateway"
            or ep["ServiceName"] != f"com.amazonaws.{region}.s3"):
        return [f"[aws].s3_endpoint_id {eid} must be an S3 gateway endpoint "
                f"(com.amazonaws.{region}.s3) in {aws['vpc_id']}"]
    if str(ep.get("State", "")).lower() != "available":
        return [f"[aws].s3_endpoint_id {eid} is {ep.get('State')}, not available"]
    errs = []
    for sid, table in subnet_tables.items():
        rtb = table["RouteTableId"] if table else None
        if rtb not in ep.get("RouteTableIds", []):
            errs.append(f"[aws].s3_endpoint_id {eid} is not on {sid}'s route table {rtb}, "
                        "so the nodes there couldn't reach the buckets")
    return errs

def ingress_cidrs(aws: dict) -> list[str]:
    """Source CIDRs for the proxy security group (dns 53 stays open to all).
    Private ingress defaults to the VPC: the Twingate connector dials the
    proxy from inside it."""
    if "ingress_cidrs" in aws:
        return aws["ingress_cidrs"]
    return [aws["vpc_cidr"]] if aws.get("ingress", "public") == "private" else ["0.0.0.0/0"]

def _validate_ingress(doc: dict, errs: list[str]) -> str:
    """Returns the mode the subnet checks should follow."""
    aws = doc["aws"]
    ingress = aws.get("ingress", "public")
    if ingress not in INGRESS_MODES:
        errs.append(f"[aws].ingress must be one of {', '.join(INGRESS_MODES)}, got {ingress!r}")
        ingress = "public"
    if ingress == "private":
        ca = doc.get("acme", {}).get("ca", DEFAULT_CA)
        if ca != "local":
            errs.append(f'[aws].ingress = "private" requires [acme] ca = "local" (got {ca!r}): '
                        "public CAs validate by querying boxd-dns from the internet, "
                        "which a private-only cluster cannot answer")
    cidrs = aws.get("ingress_cidrs")
    if cidrs is not None:
        if not isinstance(cidrs, list) or not cidrs:
            errs.append("[aws].ingress_cidrs must be a non-empty list of IPv4 CIDRs")
        else:
            for c in cidrs:
                try:
                    if not isinstance(c, str) or ipaddress.ip_network(c).version != 4:
                        raise ValueError(f"{c!r} is not an IPv4 CIDR")
                except ValueError as e:
                    errs.append(f"[aws].ingress_cidrs: {e}")
    return ingress

def _auto_archive(doc: dict) -> bool:
    """[snapshot_archive] declared in cluster.toml makes terraform create its
    bucket and key, unless [aws] auto_snapshot_archive = false (your own
    bucket, from `providers edit`)."""
    return "snapshot_archive" in doc and doc["aws"].get("auto_snapshot_archive", True) is not False

def _auto_storage(doc: dict) -> bool:
    """[storage_engine] declared in cluster.toml makes terraform create the
    cluster's storage bucket and its key, unless [aws] auto_storage_bucket =
    false (your own bucket, from `providers edit`)."""
    return isinstance(doc.get("storage_engine"), dict) and \
        doc["aws"].get("auto_storage_bucket", True) is not False

# [aws] storage_bucket_type: "standard" (an S3 general purpose bucket in the
# region) or "express" (an S3 Express One Zone directory bucket in the
# cluster's availability zone: lower latency, but one AZ holds the only copy).
STORAGE_BUCKET_TYPES = ("standard", "express")

def storage_express(doc: dict) -> bool:
    """The installer's storage bucket is an S3 Express directory bucket."""
    return _auto_storage(doc) and doc["aws"].get("storage_bucket_type") == "express"

def validate(doc: dict, errs: list[str]) -> None:
    aws = doc["aws"]
    ingress = _validate_ingress(doc, errs)
    if "vpc_id" not in aws:
        errs += [f"[aws].{k} is for an existing VPC: it needs vpc_id"
                 for k in EXISTING_VPC_KEYS[1:] if k in aws]
    access = aws.get("bucket_access", "anywhere")
    if access not in BUCKET_ACCESS:
        errs.append(f"[aws].bucket_access must be one of {', '.join(BUCKET_ACCESS)}, got {access!r}")
    elif access == "vpc" and not (aws.get("auto_backups", True) or _auto_archive(doc)
                                  or _auto_storage(doc)):
        errs.append('[aws].bucket_access = "vpc" restricts the buckets the installer '
                    "creates; with auto_backups = false and no auto-created snapshot "
                    "archive or storage bucket there are none")
    if "auto_snapshot_archive" in aws and not isinstance(aws["auto_snapshot_archive"], bool):
        errs.append("[aws].auto_snapshot_archive must be true or false")
    if "storage_bucket_type" in aws:
        kind = aws["storage_bucket_type"]
        if kind not in STORAGE_BUCKET_TYPES:
            errs.append(f"[aws].storage_bucket_type must be one of "
                        f"{', '.join(STORAGE_BUCKET_TYPES)}, got {kind!r}")
        elif kind == "express" and not _auto_storage(doc):
            errs.append('[aws].storage_bucket_type = "express" is for the bucket the installer '
                        "makes: it needs [storage_engine], and auto_storage_bucket not false "
                        "(your own directory bucket goes in `providers edit` with s3_express = true)")
    max_name = BUCKET_NAME_MAX - len(f"-backups-{'0' * ACCOUNT_ID_LEN}-{aws['region']}")
    if len(doc["cluster"]["name"]) > max_name:
        errs.append(f"[cluster].name must be <= {max_name} chars in {aws['region']} (the "
                    "buckets are <name>-backups-/-archive-/-storage-<account-id>-<region>, "
                    "max 63)")
    try:
        vpc = ipaddress.ip_network(aws["vpc_cidr"])
        # None: private ingress in an existing VPC, where no node is public.
        pub = ipaddress.ip_network(aws["public_subnet_cidr"]) if "public_subnet_cidr" in aws else None
        priv = ipaddress.ip_network(aws["private_subnet_cidr"])
    except ValueError as e:
        errs.append(f"[aws] subnet: {e}")
        return
    for label, sub in (("public_subnet_cidr", pub), ("private_subnet_cidr", priv)):
        if sub and (sub.version != vpc.version or not sub.subnet_of(vpc)):
            errs.append(f"[aws].{label} {sub} is outside vpc_cidr {vpc}")
    if pub and pub.version == priv.version and pub.overlaps(priv):
        errs.append("[aws] public_subnet_cidr and private_subnet_cidr overlap")
    for n in doc.get("nodes") or []:
        if "internal_ip" not in n:
            continue
        try:
            addr = ipaddress.ip_address(n["internal_ip"])
        except ValueError:
            continue
        sub, label = (pub, "public") if _public(n, ingress) else (priv, "private")
        nm = n.get("name", "?")
        if addr.version != sub.version or addr not in sub:
            why = ("proxy/dns nodes are public; the rest are private" if ingress == "public"
                   else 'every node is private with ingress = "private"')
            errs.append(f"{nm}: internal_ip {addr} must be in the {label} subnet {sub} ({why})")
        elif int(addr) - int(sub.network_address) < 4 or addr == sub.broadcast_address:
            errs.append(f"{nm}: internal_ip {addr} is AWS-reserved (first four and last "
                        f"address of {sub})")

def _network_tfvars(aws: dict) -> dict:
    """The VPC terraform makes, or with vpc_id the existing one's ids."""
    if "vpc_id" in aws:
        return {k: aws.get(k) for k in EXISTING_VPC_KEYS}
    return {k: aws[k] for k in NETWORK_CIDRS}

def tfvars(cfg, state, nodes: list[dict]) -> dict:
    return {
        "region": cfg.cloud["region"],
        "availability_zone": cfg.cloud["availability_zone"],
        "cluster_name": cfg.name,
        **_network_tfvars(cfg.cloud),
        "ami": cfg.cloud.get("ami") or _resolve_ami(cfg),
        "deploy_ssh_pubkey": state.deploy_pubkey(),
        "enable_backups": cfg.cloud.get("auto_backups", True),
        "ingress_cidrs": ingress_cidrs(cfg.cloud),
        "bucket_access": cfg.cloud.get("bucket_access", "anywhere"),
        "enable_snapshot_archive": _auto_archive(cfg.doc),
        "enable_storage_bucket": _auto_storage(cfg.doc),
        "storage_bucket_express": storage_express(cfg.doc),
        "nodes": nodes,
    }

def _resolve_ami(cfg) -> str:
    """Canonical's current Ubuntu 24.04 AMI for the region, resolved here
    for the same reason GCP resolves its image family: the plan never sees
    an unknown create-only value (main.tf ignores later AMI changes)."""
    res = run(["aws", "ssm", "get-parameter", "--region", cfg.cloud["region"],
               "--name", AMI_PARAMETER, "--query", "Parameter.Value", "--output", "text"],
              capture=True)
    ami = res.stdout.strip()
    if not ami.startswith("ami-"):
        raise InstallError(f"could not resolve the Ubuntu 24.04 AMI in {cfg.cloud['region']}")
    return ami

def tf_key_env(key: bytes) -> dict:
    """SSE-C for the S3 state object. Only applied with backend encrypt=true
    (see backend_args), and the bucket must not block SSE-C (docs/INSTALL.md)."""
    return {"AWS_SSE_CUSTOMER_KEY": base64.b64encode(key).decode()}

def backend_args(cfg) -> list[str]:
    return [f"-backend-config=bucket={cfg.cloud['state_bucket']}",
            f"-backend-config=key={cfg.name}/terraform.tfstate",
            f"-backend-config=region={cfg.cloud['region']}",
            "-backend-config=encrypt=true",
            "-backend-config=use_lockfile=true"]

def state_url(cfg) -> str:
    return f"s3://{cfg.cloud['state_bucket']}/{cfg.name}/installer-state.enc"

def _split(url: str) -> tuple[str, str]:
    bucket, _, key = url.removeprefix("s3://").partition("/")
    return bucket, key

def object_size(cfg, url: str) -> int | None:
    bucket, key = _split(url)
    res = run(["aws", "s3api", "head-object", "--region", cfg.cloud["region"],
               "--bucket", bucket, "--key", key,
               "--query", "ContentLength", "--output", "text"], capture=True, check=False)
    return int(res.stdout.strip()) if res.returncode == 0 else None

def _cp(cfg, src: str, dst: str, check: bool = True) -> None:
    run(["aws", "s3", "cp", "--only-show-errors", "--region", cfg.cloud["region"], src, dst],
        capture=True, check=check)

def download(cfg, url: str, path: str) -> None:
    _cp(cfg, url, path)

def upload(cfg, path: str, url: str) -> None:
    _cp(cfg, path, url)

def copy(cfg, src: str, dst: str) -> None:
    _cp(cfg, src, dst, check=False)

def preflight(cfg) -> None:
    run(["aws", "sts", "get-caller-identity", "--region", cfg.cloud["region"]], capture=True)
    run(["session-manager-plugin", "--version"], capture=True)

def ssh_proxy_command(cfg) -> str:
    return (f"aws ssm start-session --region {cfg.cloud['region']} --target %h "
            "--document-name AWS-StartSSHSession --parameters portNumber=%p")

def ssh_target(node, tf_out: dict) -> str:
    """SSM addresses instances by id, not name."""
    iid = tf_out.get("nodes", {}).get(node.name, {}).get("instance_id")
    if not iid:
        raise InstallError(f"{node.name}: no instance_id in terraform outputs — run `apply`")
    return iid

def check_data_disk_switch(cfg, instances: set[str], ebs: set[str]) -> None:
    """Refuse to move a worker that exists between an EBS data disk and
    instance store, before terraform plans it: to instance store, terraform
    would destroy the EBS volume the worker's machines live on; to EBS, the
    worker would format a new volume and leave its machines behind on the
    instance store. `instances` and `ebs` are the nodes with an instance and
    with an EBS data volume in the terraform state."""
    bad = []
    for n in cfg.nodes_with("worker"):
        if n.name not in instances:
            continue
        if n.instance_store and n.name in ebs:
            bad.append(f"{n.name} runs on an EBS data disk, and cluster.toml now says "
                       'data_disk = "instance-store"')
        elif not n.instance_store and n.name not in ebs:
            bad.append(f"{n.name} runs on instance store, and cluster.toml now gives it an "
                       "EBS data disk (data_disk_gb)")
    if bad:
        raise InstallError(
            "; ".join(bad) + ". A worker's data disk holds its machines, so it can't change "
            "under them: put the worker's data disk setting back, or move its machines off, "
            "remove the worker from cluster.toml and apply, then add it back with the new "
            "setting under a new name")

def host_vars(node, tf_out: dict) -> dict:
    """ansible_host = instance id (the SSM target). The data disk is pinned
    by EBS volume serial: instance-store families expose extra NVMe disks
    that boxd-volume's first-non-root-disk detection would pick instead.
    With data_disk = "instance-store" those NVMe disks are the data disk."""
    hv = {"ansible_host": ssh_target(node, tf_out)}
    vol = tf_out["nodes"][node.name].get("data_volume_id")
    if node.instance_store:
        hv["boxd_volume_instance_store"] = True
    elif vol:
        hv["boxd_volume_device"] = "/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_" + vol.replace("-", "")
    return hv
