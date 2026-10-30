"""Bare metal, or any hosts you already run: no terraform. cluster.toml lists
the hosts and their addresses; the installer reaches them over plain ssh as
`ubuntu` with the deploy key, and keeps its encrypted state in a local file
instead of a bucket. ZeroSSL as the default ACME CA (GTS needs a GCP project)."""
import ipaddress
import os
import shutil

from ..util import InstallError

REQUIRED = ("vpc_cidr", "interface")
TABLE_KEYS = set(REQUIRED) | {"state_dir"}
DEFAULT_INTERFACE = None
DEFAULT_CA = "zerossl"
# No terraform: the hosts exist already, and nothing is created or destroyed.
TF_DIR = None
SSH_PATH = "ssh"
SSH_HINT = ("that each node's ssh address is reachable from here and that the user "
            "ubuntu there has state/keys/boxd-deploy.pub in ~/.ssh/authorized_keys "
            "and passwordless sudo")
DEFAULT_STATE_DIR = "metal-state"
# Keys only the cloud adapters use: on metal the hosts are given, not sized.
CLOUD_NODE_KEYS = ("machine_type", "data_disk_gb", "boot_disk_gb")

def _ip_error(value) -> str | None:
    try:
        if not isinstance(value, str) or ipaddress.ip_address(value).version != 4:
            return f"{value!r} is not an IPv4 address"
    except ValueError:
        return f"{value!r} is not an IPv4 address"
    return None

def validate(doc: dict, errs: list[str]) -> None:
    metal = doc["metal"]
    for k in metal:
        if k not in TABLE_KEYS:
            errs.append(f"[metal].{k} is not a [metal] key (known: {', '.join(sorted(TABLE_KEYS))})")
    if "interface" in metal and not (isinstance(metal["interface"], str) and metal["interface"]):
        errs.append('[metal].interface must be the name of the NIC on the private network, e.g. "eno2"')
    if "state_dir" in metal and not (isinstance(metal["state_dir"], str) and metal["state_dir"]):
        errs.append("[metal].state_dir must be a directory path")
    for n in doc.get("nodes") or []:
        nm = n.get("name", "?")
        roles = [r.partition(":")[0] for r in n.get("roles", [])]
        for k in CLOUD_NODE_KEYS:
            if k in n:
                errs.append(f"{nm}: {k} is not used on [metal] (the hosts exist already)")
        if "public_ip" in n and (why := _ip_error(n["public_ip"])):
            errs.append(f"{nm}: public_ip: {why}")
        if "ssh_host" in n and not (isinstance(n["ssh_host"], str) and n["ssh_host"]):
            errs.append(f"{nm}: ssh_host must be a host name or address")
        if ("proxy" in roles or "dns" in roles) and "public_ip" not in n:
            errs.append(f"{nm}: proxy and dns nodes need public_ip, the address clients "
                        "and the DNS glue records point at")
        if "worker" in roles:
            disk = n.get("data_disk")
            if not (isinstance(disk, str) and disk.startswith("/dev/")):
                errs.append(f"{nm}: worker nodes must set data_disk, the device VM data "
                            'lives on (e.g. "/dev/nvme1n1"). apply formats it as XFS '
                            "unless it is XFS already: everything on it is lost")
        elif "data_disk" in n:
            errs.append(f"{nm}: data_disk is only valid on worker nodes")

def outputs(cfg) -> dict:
    """What terraform outputs on a cloud, from cluster.toml."""
    return {"nodes": {n.name: {"internal_ip": n.internal_ip,
                               "external_ip": n.raw.get("public_ip")} for n in cfg.nodes},
            "backups": None}

def state_url(cfg) -> str:
    """A local file, relative to cluster.toml's directory."""
    base = os.path.join(cfg.base_dir, cfg.cloud.get("state_dir", DEFAULT_STATE_DIR))
    return os.path.join(base, cfg.name, "installer-state.enc")

def object_size(cfg, url: str) -> int | None:
    return os.path.getsize(url) if os.path.isfile(url) else None

def download(cfg, url: str, path: str) -> None:
    shutil.copyfile(url, path)

def upload(cfg, path: str, url: str) -> None:
    os.makedirs(os.path.dirname(url), mode=0o700, exist_ok=True)
    tmp = url + ".tmp"
    shutil.copyfile(path, tmp)
    os.chmod(tmp, 0o600)
    os.replace(tmp, url)

def copy(cfg, src: str, dst: str) -> None:
    if os.path.isfile(src):
        shutil.copyfile(src, dst)
        os.chmod(dst, 0o600)

def preflight(cfg) -> None:
    pass

def ssh_proxy_command(cfg) -> None:
    """Direct ssh: no tunnel."""
    return None

def ssh_target(node, tf_out: dict) -> str:
    target = node.raw.get("ssh_host") or node.raw.get("public_ip") or node.internal_ip
    if not target:
        raise InstallError(f"{node.name}: no ssh address")
    return target

def host_vars(node, tf_out: dict) -> dict:
    """The data disk is always named: boxd-volume's first-non-root-disk
    detection would format whichever disk a server lists first."""
    hv = {"ansible_host": ssh_target(node, tf_out)}
    if node.has("worker"):
        hv["boxd_volume_device"] = node.raw["data_disk"]
    return hv
