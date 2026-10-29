"""AWS: SSM-tunnelled ssh (no inbound 22 anywhere), one S3 bucket for
terraform + installer state, ZeroSSL as the default ACME CA (an AWS cluster
must not depend on a GCP project, which GTS key minting needs)."""
import base64
import ipaddress
import os

from ..util import InstallError, run

REQUIRED = ("region", "availability_zone", "vpc_cidr", "public_subnet_cidr",
            "private_subnet_cidr", "state_bucket")
DEFAULT_INTERFACE = "ens5"
DEFAULT_CA = "zerossl"
INGRESS_MODES = ("public", "private")
BUCKET_ACCESS = ("anywhere", "vpc")
TF_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "terraform", "aws"))
SSH_PATH = "SSM"
SSH_HINT = ("ssm:StartSession on the instances and that their SSM agent is "
            "online (`aws ssm describe-instance-information`)")
AMI_PARAMETER = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
# S3 bucket names max out at 63 chars; ours are <name>-backups-<account-id>-<region>
# and <name>-archive-<account-id>-<region> (same length).
BUCKET_NAME_MAX = 63
ACCOUNT_ID_LEN = 12

def _public(raw_node: dict, ingress: str) -> bool:
    """Same rule as tf.build_tfvars' public_ip: in public ingress, proxy/dns
    nodes get an Elastic IP and live in the public subnet. Everything else,
    and every node in private ingress, egresses via NAT."""
    return ingress == "public" and any(
        r.partition(":")[0] in ("proxy", "dns") for r in raw_node.get("roles", []))

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

def validate(doc: dict, errs: list[str]) -> None:
    aws = doc["aws"]
    ingress = _validate_ingress(doc, errs)
    access = aws.get("bucket_access", "anywhere")
    if access not in BUCKET_ACCESS:
        errs.append(f"[aws].bucket_access must be one of {', '.join(BUCKET_ACCESS)}, got {access!r}")
    elif access == "vpc" and not (aws.get("auto_backups", True) or _auto_archive(doc)):
        errs.append('[aws].bucket_access = "vpc" restricts the buckets the installer '
                    "creates; with auto_backups = false and no auto-created snapshot "
                    "archive there are none")
    if "auto_snapshot_archive" in aws and not isinstance(aws["auto_snapshot_archive"], bool):
        errs.append("[aws].auto_snapshot_archive must be true or false")
    max_name = BUCKET_NAME_MAX - len(f"-backups-{'0' * ACCOUNT_ID_LEN}-{aws['region']}")
    if len(doc["cluster"]["name"]) > max_name:
        errs.append(f"[cluster].name must be <= {max_name} chars in {aws['region']} (the "
                    "buckets are <name>-backups-/<name>-archive-<account-id>-<region>, max 63)")
    try:
        vpc = ipaddress.ip_network(aws["vpc_cidr"])
        pub = ipaddress.ip_network(aws["public_subnet_cidr"])
        priv = ipaddress.ip_network(aws["private_subnet_cidr"])
    except ValueError as e:
        errs.append(f"[aws] subnet: {e}")
        return
    for label, sub in (("public_subnet_cidr", pub), ("private_subnet_cidr", priv)):
        if sub.version != vpc.version or not sub.subnet_of(vpc):
            errs.append(f"[aws].{label} {sub} is outside vpc_cidr {vpc}")
    if pub.version == priv.version and pub.overlaps(priv):
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

def tfvars(cfg, state, nodes: list[dict]) -> dict:
    return {
        "region": cfg.cloud["region"],
        "availability_zone": cfg.cloud["availability_zone"],
        "cluster_name": cfg.name,
        "vpc_cidr": cfg.vpc_cidr,
        "public_subnet_cidr": cfg.cloud["public_subnet_cidr"],
        "private_subnet_cidr": cfg.cloud["private_subnet_cidr"],
        "ami": cfg.cloud.get("ami") or _resolve_ami(cfg),
        "deploy_ssh_pubkey": state.deploy_pubkey(),
        "enable_backups": cfg.cloud.get("auto_backups", True),
        "ingress_cidrs": ingress_cidrs(cfg.cloud),
        "bucket_access": cfg.cloud.get("bucket_access", "anywhere"),
        "enable_snapshot_archive": _auto_archive(cfg.doc),
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

def host_vars(node, tf_out: dict) -> dict:
    """ansible_host = instance id (the SSM target). The data disk is pinned
    by EBS volume serial: instance-store families expose extra NVMe disks
    that boxd-volume's first-non-root-disk detection would pick instead."""
    hv = {"ansible_host": ssh_target(node, tf_out)}
    vol = tf_out["nodes"][node.name].get("data_volume_id")
    if vol:
        hv["boxd_volume_device"] = "/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_" + vol.replace("-", "")
    return hv
