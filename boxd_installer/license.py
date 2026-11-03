"""The boxd license key: [license] key_file in
cluster.toml, the free tier's worker vCPU limit, and the default worker type
for the free tier.

Every cluster has a license key: boxd releases are downloaded with it
(assets.base_url). A key without the unlimited-worker-vcpus feature is the
free tier: the cluster may have at most 16 worker vCPUs (the logical CPUs of
its worker hosts). It keeps working over the limit, but refuses new machines
while it is over. `apply` installs the key on the cluster with
`boxd-ctl license install` every time.

boxd never stops a VM because of the license, so the license
never resizes a worker that exists: the free default type is only for
workers terraform has not created yet.
"""
import base64
import os
import re

from . import clouds
from .util import InstallError

FREE_TIER_MAX_WORKER_VCPUS = 16

# The worker type when a new [[nodes]] worker sets no machine_type on the
# free tier: one of these is within its limit. With unlimited worker vCPUs,
# terraform's defaults apply (m8i.8xlarge, n2-standard-32:
# terraform/<cloud>/variables.tf).
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
                           "from boxd in it")
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


def key_text(cfg) -> str:
    """The configured license key, one line. Every cluster needs one: boxd
    releases are downloaded with it."""
    path = key_path(cfg)
    if path is None:
        raise InstallError(NO_KEY)
    with open(path, errors="replace") as fh:
        return next(line.strip() for line in fh.read().splitlines() if line.strip())


NO_KEY = ("cluster.toml has no license key. boxd releases are downloaded with your license "
          "key: put the key from boxd in a file next to cluster.toml and name it in "
          '[license] key_file = "license.key". No key yet? Ask your boxd contact')


# The features a key can carry, by their number in the key. The numbers are
# fixed by boxd's license format and never reused.
FEATURE_NAMES = {
    1: "unlimited-worker-vcpus",
    2: "telemetry-opt-out",
    3: "bucket-storage",
    4: "usage-billing",
}
USAGE_BILLING = "usage-billing"
UNLIMITED_WORKER_VCPUS = "unlimited-worker-vcpus"


def _varint(data: bytes, i: int) -> tuple[int, int]:
    value, shift = 0, 0
    while True:
        if i >= len(data) or shift > 63:
            raise ValueError("truncated varint")
        b = data[i]
        i += 1
        value |= (b & 0x7F) << shift
        if b < 0x80:
            return value, i
        shift += 7


def _fields(data: bytes):
    """The (number, wire type, value) of each protobuf field in `data`:
    varints as int, length-delimited as bytes. Other wire types are skipped."""
    i = 0
    while i < len(data):
        tag, i = _varint(data, i)
        number, wire = tag >> 3, tag & 7
        if wire == 0:
            value, i = _varint(data, i)
        elif wire == 2:
            n, i = _varint(data, i)
            if i + n > len(data):
                raise ValueError("truncated field")
            value, i = data[i:i + n], i + n
        elif wire == 1:
            value, i = None, i + 8
        elif wire == 5:
            value, i = None, i + 4
        else:
            raise ValueError(f"wire type {wire}")
        yield number, wire, value


def key_payload(text: str) -> dict | None:
    """What a key's text says about itself, NOT verified (control checks the
    signature): its format version, its feature names, and whether it carries
    a usage-billing report token. None when the text doesn't decode."""
    text = text.strip()
    if not _KEY_RE.fullmatch(text):
        return None
    body = text.split("_", 1)[1]
    try:
        signed = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        payload = next((v for n, w, v in _fields(signed) if n == 2 and w == 2), None)
        if payload is None:
            return None
        out = {"format": 0, "features": set(), "report_token": False}
        for number, wire, value in _fields(payload):
            if number == 1 and wire == 0:
                out["format"] = value
            elif number == 10 and wire == 0:
                out["features"].add(FEATURE_NAMES.get(value, f"feature-{value}"))
            elif number == 10 and wire == 2:  # packed
                j = 0
                while j < len(value):
                    f, j = _varint(value, j)
                    out["features"].add(FEATURE_NAMES.get(f, f"feature-{f}"))
            elif number == 11 and wire == 2:
                out["report_token"] = len(value) == 32
        return out
    except (ValueError, TypeError):
        return None


def key_features(cfg) -> set[str]:
    """The feature names of the configured key, unverified; empty without
    [license].key_file or when the file can't be read or decoded (key_path
    reports a bad file on its own)."""
    path = key_file(cfg)
    if path is None or not os.path.isfile(path):
        return set()
    try:
        with open(path, errors="replace") as fh:
            payload = key_payload(fh.read())
    except OSError:
        return set()
    return payload["features"] if payload else set()


def usage_billing(cfg) -> bool:
    """The configured key bills the cluster on its usage: its telemetry
    reports are what boxd bills from, so telemetry can't be turned off."""
    return USAGE_BILLING in key_features(cfg)


def unlimited_vcpus(cfg) -> bool:
    """The configured key lifts the free tier's worker vCPU limit."""
    return UNLIMITED_WORKER_VCPUS in key_features(cfg)


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
    table = PAID_WORKER_TYPES if unlimited_vcpus(cfg) else FREE_WORKER_TYPES
    return table[cfg.cloud_name]


def worker_type(cfg, node, existing: dict[str, str] | None = None) -> str:
    """The type a worker gets: its machine_type, else the type it has now,
    else the default for a new worker."""
    return node.machine_type or (existing or {}).get(node.name) or default_worker_type(cfg)


def tfvars_machine_type(cfg, node, existing: dict[str, str] | None = None) -> str | None:
    """What tfvars passes for a node. Its own machine_type always wins. A
    worker that exists keeps the type it has (`existing`, from the terraform
    state), whatever the key allows. A new worker on the free tier gets the
    free type. None lets terraform's default apply."""
    if node.machine_type:
        return node.machine_type
    if not node.has("worker"):
        return None
    if current := (existing or {}).get(node.name):
        return current
    if not unlimited_vcpus(cfg):
        return FREE_WORKER_TYPES[cfg.cloud_name]
    return None


def free_tier_warnings(cfg, existing: dict[str, str] | None = None,
                       assume_new: bool = False,
                       counted: dict[str, int] | None = None) -> list[str]:
    """On the free tier: the workers over its limit, and worker types whose
    vCPUs are unknown. Never an error: the cluster installs and runs, and
    refuses new machines while it is over. Raises when [license].key_file is
    set but not usable (key_path). `assume_new` (validate, which has no
    terraform state): an over-limit warning adds that workers without
    machine_type were counted as new ones and that apply counts the types
    that exist. Under the limit validate says nothing; apply checks the real
    types anyway. `counted`: each worker's logical CPUs, read on the host
    (metal, which has no machine types); without it metal says nothing."""
    key_path(cfg)
    if unlimited_vcpus(cfg):
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
        out.append(f"the workers have {total} vCPUs, and the boxd free tier allows "
                   f"{FREE_TIER_MAX_WORKER_VCPUS}. The cluster installs and works, but it "
                   "refuses new machines while it is over the limit. Ask boxd for a license "
                   "key with unlimited worker vCPUs, or use fewer or smaller workers."
                   + (_ASSUMED_NEW if assume_new and _untyped_workers(cfg) else ""))
    if unknown:
        out.append(f"cannot count the vCPUs of {', '.join(unknown)}: the free tier allows "
                   f"{FREE_TIER_MAX_WORKER_VCPUS} worker vCPUs")
    return out


_ASSUMED_NEW = (" This count takes the workers without machine_type as new ones; `apply` "
                "counts the types that exist.")


def _untyped_workers(cfg) -> list[str]:
    return [n.name for n in cfg.nodes_with("worker") if not n.machine_type]
