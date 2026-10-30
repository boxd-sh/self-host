"""The boxd license key: [license] key_file in
cluster.toml, the free tier's worker vCPU limit, and the default worker type
without a key.

Without a valid license key a boxd cluster may have at most 16 worker vCPUs
(the logical CPUs of its worker hosts). It keeps working over the limit, but
refuses new machines while it is over. A key unlocks more; `apply` installs
it on the cluster with `boxd-ctl license install` every time.

boxd never stops a VM because of the license, so the license
never resizes a worker that exists: the free default type is only for
workers terraform has not created yet.
"""
import os
import re

from . import clouds
from .util import InstallError

FREE_TIER_MAX_WORKER_VCPUS = 16

# The worker type when a new [[nodes]] worker sets no machine_type and there
# is no license key: one of these is free. With a key, terraform's defaults
# apply (m8i.8xlarge, n2-standard-32: terraform/<cloud>/variables.tf).
FREE_WORKER_TYPES = {"aws": "m8i.4xlarge", "gcp": "n2-standard-16"}
PAID_WORKER_TYPES = {"aws": "m8i.8xlarge", "gcp": "n2-standard-32"}

# The text form of a license key: `boxd-lic<N>_` and
# base64url. Only the shape is checked here; control verifies the signature
# (InstallLicense) and gives the reason when it refuses one.
_KEY_RE = re.compile(r"boxd-lic[0-9]+_[A-Za-z0-9_-]+")
# The old download key (`BOXD_PRODUCT_LICENSE`) is 64 hex characters.
_DOWNLOAD_KEY_RE = re.compile(r"[0-9a-fA-F]{64}")

# vCPUs per EC2 size, the same for every x86 and Graviton family of the
# sizes the installer offers (the nested-virt C/M/R 7i and 8i families, and
# their .metal-Nxl types). Source: AWS, "Amazon EC2 instance type
# specifications" (docs.aws.amazon.com/ec2/latest/instancetypes/), checked
# 2026-10-21. A plain ".metal" differs by family, so it is not listed.
_AWS_SIZES = {"large": 2, "xlarge": 4}
_AWS_NXLARGE = re.compile(r"(\d+)xlarge")
_AWS_METAL = re.compile(r"metal-(\d+)xl")
# GCP predefined types end in their vCPU count (n2-standard-16 has 16).
# Source: Google Cloud, "Machine families resource and comparison guide"
# (cloud.google.com/compute/docs/machine-resource), checked 2026-10-21.
_GCP_PREDEFINED = re.compile(r"[a-z0-9]+-(?:standard|highmem|highcpu)-(\d+)")


def key_file(cfg) -> str | None:
    """The configured key file's path, relative paths from cluster.toml's
    directory; None without [license].key_file."""
    name = cfg.doc.get("license", {}).get("key_file")
    if not name:
        return None
    return os.path.join(cfg.base_dir, name)


def key_path(cfg) -> str | None:
    """The key file, checked; None without [license].key_file. A configured
    file that is missing or does not look like a license key is an error, so
    a typo never gives a cluster the free tier: `validate` and `apply` stop
    on it before terraform."""
    path = key_file(cfg)
    if path is None:
        return None
    shown = os.path.relpath(path, cfg.base_dir)
    if not os.path.isfile(path):
        raise InstallError(f"[license].key_file {shown} does not exist. Put the license key "
                           "from boxd in it, or remove [license] to install without a key "
                           "(the boxd free tier)")
    try:
        with open(path, errors="replace") as fh:
            lines = [line.strip() for line in fh.read().splitlines() if line.strip()]
    except OSError as e:
        raise InstallError(f"[license].key_file {shown} cannot be read ({e.strerror}): "
                           "check its permissions") from None
    if len(lines) == 1 and _DOWNLOAD_KEY_RE.fullmatch(lines[0]):
        raise InstallError(f"[license].key_file {shown} holds a download key (64 hex "
                           "characters), not a license key. A license key starts with "
                           "boxd-lic")
    if len(lines) != 1 or not _KEY_RE.fullmatch(lines[0]):
        raise InstallError(f"[license].key_file {shown} is not a license key: it must hold "
                           "one line that starts with boxd-lic")
    return path


def vcpus(cloud: str, machine_type: str) -> int | None:
    """The published vCPU count of a machine type; None when unknown."""
    if cloud == "aws":
        _, _, size = machine_type.partition(".")
        if size in _AWS_SIZES:
            return _AWS_SIZES[size]
        if m := _AWS_NXLARGE.fullmatch(size):
            return int(m.group(1)) * 4
        if m := _AWS_METAL.fullmatch(size):
            return int(m.group(1)) * 4
        return None
    if cloud == "gcp" and (m := _GCP_PREDEFINED.fullmatch(machine_type)):
        return int(m.group(1))
    return None


def default_worker_type(cfg) -> str:
    """A new worker's type when its [[nodes]] entry sets none."""
    table = PAID_WORKER_TYPES if key_path(cfg) else FREE_WORKER_TYPES
    return table[cfg.cloud_name]


def worker_type(cfg, node, existing: dict[str, str] | None = None) -> str:
    """The type a worker gets: its machine_type, else the type it has now,
    else the default for a new worker."""
    return node.machine_type or (existing or {}).get(node.name) or default_worker_type(cfg)


def tfvars_machine_type(cfg, node, existing: dict[str, str] | None = None) -> str | None:
    """What tfvars passes for a node. Its own machine_type always wins. A
    worker that exists keeps the type it has (`existing`, from the terraform
    state), with or without a key. A new worker without a key gets the free
    type. None lets terraform's default apply."""
    if node.machine_type:
        return node.machine_type
    if not node.has("worker"):
        return None
    if current := (existing or {}).get(node.name):
        return current
    if not key_path(cfg):
        return FREE_WORKER_TYPES[cfg.cloud_name]
    return None


def free_tier_warnings(cfg, existing: dict[str, str] | None = None,
                       assume_new: bool = False,
                       counted: dict[str, int] | None = None) -> list[str]:
    """Without a key: the workers over the free limit, and worker types whose
    vCPUs are unknown. Never an error: the cluster installs and runs, and
    refuses new machines while it is over. Raises when [license].key_file is
    set but not usable (key_path). `assume_new` (validate, which has no
    terraform state): an over-limit warning adds that workers without
    machine_type were counted as new ones and that apply counts the types
    that exist. Under the limit validate says nothing; apply checks the real
    types anyway. `counted`: each worker's logical CPUs, read on the host
    (metal, which has no machine types); without it metal says nothing."""
    if key_path(cfg):
        return []
    if counted is None and not clouds.provisions(cfg):
        return []
    out = []
    total, unknown = 0, []
    for n in cfg.nodes_with("worker"):
        if counted is not None:
            total += counted[n.name]
            continue
        t = worker_type(cfg, n, existing)
        v = vcpus(cfg.cloud_name, t)
        if v is None:
            unknown.append(f"{n.name} ({t})")
        else:
            total += v
    if total > FREE_TIER_MAX_WORKER_VCPUS:
        out.append(f"the workers have {total} vCPUs and there is no license key; the boxd free "
                   f"tier allows {FREE_TIER_MAX_WORKER_VCPUS}. The cluster installs and works, "
                   "but it refuses new machines while it is over the limit. Add a license key "
                   "([license] key_file) or use fewer or smaller workers."
                   + (_ASSUMED_NEW if assume_new and _untyped_workers(cfg) else ""))
    if unknown:
        out.append(f"cannot count the vCPUs of {', '.join(unknown)}: the free tier allows "
                   f"{FREE_TIER_MAX_WORKER_VCPUS} worker vCPUs without a license key")
    return out


_ASSUMED_NEW = (" This count takes the workers without machine_type as new ones; `apply` "
                "counts the types that exist.")


def _untyped_workers(cfg) -> list[str]:
    return [n.name for n in cfg.nodes_with("worker") if not n.machine_type]
