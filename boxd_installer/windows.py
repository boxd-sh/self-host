"""[windows]: Windows Server machines next to the Linux ones (docs/REFERENCE.md "Windows
machines"). The operator builds the image (win-img/README.md) and names it here; the license key
needs Windows guests. What the installer does with it, every apply:

- workers: the release's UEFI firmware at [paths] firmware's default, <data_dir>/uefi.fd (each
  worker hashes it at its start and reports it: `boxd-ctl node list`), and the Windows builds
  of the in-VM CLIs at <data_dir>/sdk/boxd.exe and run.exe when the release has them (the boxd
  agent in a Windows machine installs them from there). The worker role copies them from the
  node's own copy of the release and restarts the worker when one changed;
- the private registry's pull credentials, from the providers config's [windows_registry], as
  [worker] registries in every worker.toml (the worker writes them for `docker pull`);
- the proxies' security group or firewall: TCP 61000-65000, each Windows machine's RDP port
  (the proxy redirects the band to its RDP listener itself);
- `copies`, when set, as [windows] copies in every worker config (boxd_raft config.rs
  WindowsConfig: how a new snapshot or fork of a Windows machine is taken; the worker reads it at
  its start, no other role does). Unset, the table is left out and boxd's default (generalize)
  applies; a release that predates it ignores the table;
- after the converge: `boxd-ctl image set-windows`, `cluster-config set|unset windows_kms`,
  `node windows-host <worker> on|off` for every worker, then `cluster-config set
  windows_licensing` (marks first: a switch to per-host with no marked worker refuses creates).

Without [windows] none of this happens, and nothing set before is undone: a cluster that drops
it keeps its Windows image and settings until an operator unsets them (OPERATIONS.md).
"""
import ipaddress
import os
import re
import shlex

from . import ansible, assets, license
from .util import InstallError, log

KEYS = {"image", "kms", "licensing", "hosts", "copies"}
LICENSING = ("per-vm", "per-host")
# [windows] copies (boxd_raft config.rs WindowsCopies): "generalize" (boxd's default) or "fast".
COPIES = ("generalize", "fast")
# The node configs that read [windows]: the worker takes the copies.
COPIES_ROLES = ("worker",)
FEATURE = "windows-guests"
PROVIDER = "windows_registry"
PROVIDER_KEYS = ("host", "username", "password")
# What the worker role copies from the release when [windows] is set: bundle file, path under
# the worker's data dir, and whether a release may lack it. A release without the Windows CLIs
# (its CI couldn't cross-build them) still runs Windows machines: they lack `boxd` and `run`.
FILES = (("bin/uefi.fd", "uefi.fd", False),
         ("bin/boxd-sdk.exe", "sdk/boxd.exe", True),
         ("bin/boxd-run.exe", "sdk/run.exe", True))
# The cluster_config keys apply sets besides the image (boxd_core windows.rs KMS_KEY,
# LICENSING_KEY). The RDP band (61000-65000) is terraform's: var.windows_rdp.
KMS_KEY = "windows_kms"
LICENSING_KEY = "windows_licensing"

# A Docker image reference with a tag or a digest: [registry[:port]/]path[:tag][@digest].
_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_DOMAIN = r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*(?::\d+)?"
_REF = re.compile(rf"(?:(?P<domain>{_DOMAIN})/)?(?P<path>{_COMPONENT}(?:/{_COMPONENT})*)"
                  r"(?::(?P<tag>[\w][\w.-]{0,127}))?(?:@(?P<digest>sha256:[0-9a-f]{64}))?")
_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def on(doc: dict) -> bool:
    return isinstance(doc.get("windows"), dict)


def image_host(ref: str) -> str:
    """The registry host of an image reference, as docker reads it: the first component when it
    has a dot or a port or is localhost, else Docker Hub."""
    first, slash, _ = ref.partition("/")
    if slash and ("." in first or ":" in first or first == "localhost"):
        return first
    return "docker.io"


def image_error(ref) -> str | None:
    """Why `ref` is not an image reference boxd can pull for every new Windows machine: a string
    with a tag or a digest (`boxd-ctl image set-windows` keeps it as given)."""
    if not isinstance(ref, str) or not ref.strip():
        return "must be the image you pushed, e.g. \"registry.example.com/boxd/windows-server-2025:2026-11\""
    m = _REF.fullmatch(ref)
    if not m:
        return f"{ref!r} is not an image reference (registry/path:tag or registry/path@sha256:…)"
    if not (m.group("tag") or m.group("digest")):
        return (f"{ref!r} needs a tag or a digest: machines boot whatever the reference names, so "
                "a moving \"latest\" would give them different disks")
    return None


def kms_error(value) -> str | None:
    """boxd's normalize_kms (boxd_core windows.rs), which refuses the value at apply otherwise:
    `host` or `host:port`, the host a DNS name or an IP address (IPv6 in brackets), no scheme,
    path or spaces, the port 1-65535."""
    if not isinstance(value, str) or not value.strip():
        return "must be your KMS host as host or host:port, e.g. \"kms.example.com:1688\""
    v = value.strip()
    if "://" in v or "/" in v or any(c.isspace() for c in v):
        return f"{value!r} must be host or host:port, with no scheme, path or spaces"
    if v.startswith("["):
        host, sep, port = v[1:].partition("]:")
        if not sep:
            if not v.endswith("]"):
                return f"{value!r}: an IPv6 address goes in brackets"
            host, port = v[1:-1], None
    elif v.count(":") > 1:
        return f"{value!r}: an IPv6 address goes in brackets"
    else:
        host, _, port = v.partition(":")
        port = port if ":" in v else None
    if port is not None and not (port.isdigit() and 0 < int(port) < 65536):
        return f"{value!r}: the port must be 1-65535"
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    if len(host) > 253 or not all(_LABEL.fullmatch(lbl) for lbl in host.split(".")):
        return f"{value!r}: the host must be a DNS name or an IP address"
    return None


def validate(doc: dict, errs: list[str], target: str | None) -> None:
    """[windows] in cluster.toml, and the registry credentials that don't belong there."""
    worker = doc.get("worker") if isinstance(doc.get("worker"), dict) else {}
    if "registries" in worker:
        errs.append("[worker].registries holds a registry's password: put it in the providers "
                    f"config instead ([{PROVIDER}] host, username, password; `providers edit`), "
                    "never in cluster.toml")
    if "windows" not in doc:
        return
    t = doc["windows"]
    if not isinstance(t, dict):
        errs.append("[windows] must be a table")
        return
    if target == "gke":
        errs.append("[windows] is not supported on [gke] yet: Windows machines run on clusters of "
                    "instances or hosts")
        return
    for k in t:
        if k not in KEYS:
            errs.append(f"[windows].{k}: unknown key (allowed: {', '.join(sorted(KEYS))})")
    if why := image_error(t.get("image")):
        errs.append(f"[windows].image {why}")
    if "kms" in t and (why := kms_error(t["kms"])):
        errs.append(f"[windows].kms {why}")
    licensing = t.get("licensing", "per-vm")
    if licensing not in LICENSING:
        errs.append(f"[windows].licensing must be one of {', '.join(LICENSING)}, got {licensing!r}")
    hosts = t.get("hosts")
    if hosts is not None:
        workers = {n.get("name") for n in doc.get("nodes") or []
                   if any(str(r).partition(":")[0] == "worker" for r in n.get("roles", []))}
        if not (isinstance(hosts, list) and all(isinstance(h, str) for h in hosts)):
            errs.append('[windows].hosts must be a list of worker names from [[nodes]], e.g. '
                        '["boxd-worker-0"]')
        else:
            for h in hosts:
                if h not in workers:
                    errs.append(f"[windows].hosts: {h!r} is not a worker in [[nodes]]")
            if len(set(hosts)) != len(hosts):
                errs.append("[windows].hosts names a worker twice")
            if licensing != "per-host":
                errs.append('[windows].hosts is only for licensing = "per-host" (per VM, Windows '
                            "machines go to any worker)")
    if "copies" in t and t["copies"] not in COPIES:
        errs.append(f"[windows].copies must be one of {', '.join(COPIES)}, got {t['copies']!r}")
    if licensing == "per-host" and not hosts:
        errs.append('[windows] licensing = "per-host" needs hosts: the workers you licensed for '
                    "Windows (with none, every Windows create is refused)")
    lic = doc.get("license")
    if not (isinstance(lic, dict) and lic.get("key_file")):
        errs.append(f"[windows] needs [license] key_file, with a key that includes Windows guests "
                    f"({FEATURE}): ask your boxd contact to add the feature")


def errors(cfg) -> list[str]:
    """The license key, read here (validate and apply): it must include Windows guests, or every
    Windows create is refused after a whole apply."""
    if not on(cfg.doc):
        return []
    path = license.key_file(cfg)
    if path is None or not os.path.isfile(path):
        return []  # license.key_path says what is wrong with the file itself
    features = license.key_features(cfg)
    if FEATURE in features:
        return []
    return [f"[windows] needs a license key that includes Windows guests ({FEATURE}), and "
            f"{cfg.doc['license']['key_file']} has {', '.join(sorted(features)) or 'no features'}: ask "
            "your boxd contact to add the feature"]


def check_bundle(cfg, assets_root: str) -> None:
    """Fail closed on a release without the firmware: its workers could run no Windows machine.
    The Windows CLIs are optional (a warning)."""
    if not on(cfg.doc):
        return
    for rel, _, optional in FILES:
        if assets.has_file(assets_root, rel):
            continue
        if not optional:
            raise InstallError(
                f"[windows] needs a release with Windows guests, and {cfg.assets_version[:12]} is "
                f"one without them (its manifest lists no {rel}, the UEFI firmware Windows "
                "machines boot): set [cluster].assets_version to a newer release, or remove "
                "[windows]")
    missing = [rel for rel, _, optional in FILES if optional and not assets.has_file(assets_root, rel)]
    if missing:
        log(f"warning: release {cfg.assets_version[:12]} has no {' or '.join(missing)}: Windows "
            "machines run, but without the in-VM `boxd` and `run` CLIs")


def worker_files(cfg, assets_root: str | None) -> list[dict]:
    """The worker role's boxd_windows_files: each file of FILES the release has, empty without
    [windows] (or before a release is known: a render without one)."""
    if not on(cfg.doc) or not assets_root:
        return []
    return [{"src": rel, "dest": dest} for rel, dest, _ in FILES if assets.has_file(assets_root, rel)]


def node_table(cfg, role: str) -> dict:
    """[windows] for a worker config: `copies` as cluster.toml says, nothing when it doesn't
    (boxd's default then, generalize) or for another role."""
    if not on(cfg.doc) or role not in COPIES_ROLES or "copies" not in cfg.doc["windows"]:
        return {}
    return {"copies": cfg.doc["windows"]["copies"]}


def registries(cfg) -> list[dict]:
    """[worker] registries for every worker.toml: the providers config's [windows_registry], with
    [windows] set. boxd-worker refuses to start on an entry with an empty field."""
    if not on(cfg.doc):
        return []
    reg = cfg.table(PROVIDER)
    if not reg:
        return []
    missing = [k for k in PROVIDER_KEYS if not (isinstance(reg.get(k), str) and reg[k].strip())]
    if missing:
        raise InstallError(f"the providers config's [{PROVIDER}] has no {', '.join(missing)} "
                           "(`providers edit`)")
    return [{k: reg[k].strip() for k in PROVIDER_KEYS}]


def registry_warnings(cfg) -> list[str]:
    """The image's registry and the credentials don't go together: workers then pull it without
    credentials, which a private registry refuses at the first Windows create."""
    if not on(cfg.doc):
        return []
    host = image_host(cfg.doc["windows"]["image"])
    reg = cfg.table(PROVIDER)
    if not reg:
        return [f"[windows] image is on {host}, and the providers config has no [{PROVIDER}]: "
                "workers pull it without credentials, which works only from a public registry "
                f"(set [{PROVIDER}] host, username and password with `providers edit`)"]
    if reg.get("host", "").strip() != host:
        return [f"[windows] image is on {host}, but [{PROVIDER}] has credentials for "
                f"{reg.get('host')!r}: workers pull the image without them"]
    return []


def provider_errors(doc: dict) -> list[str]:
    """The providers config's [windows_registry]: host, username, password, quoted strings."""
    t = doc.get(PROVIDER)
    if t is None:
        return []
    if not isinstance(t, dict):
        return [f"[{PROVIDER}] must be a table"]
    errs = [f"[{PROVIDER}].{k}: unknown key (allowed: {', '.join(PROVIDER_KEYS)})"
            for k in t if k not in PROVIDER_KEYS]
    errs += [f'[{PROVIDER}].{k} required, a non-empty quoted string, e.g. {k} = "…"'
             for k in PROVIDER_KEYS if not (isinstance(t.get(k), str) and t[k].strip())]
    host = t.get("host")
    if isinstance(host, str) and ("/" in host or "://" in host):
        errs.append(f"[{PROVIDER}].host is the registry's host name as the image reference names "
                    f"it (e.g. \"registry.example.com\"), not a URL or a path: got {host!r}")
    return errs


def worker_id(alloc, node) -> str:
    """The worker's id in boxd (`node list`, `node windows-host`): worker-<its node id>."""
    return f"worker-{alloc.node_id(node.name, 'worker')}"


def configure(cfg, alloc) -> None:
    """After the converge, on a cluster whose workers all run the new release: what [windows]
    says, set again on every apply (an operator's hand change lasts until the next apply). One
    boxd-ctl call at a time (the control socket rate-limits)."""
    if not on(cfg.doc):
        return
    t = cfg.doc["windows"]
    _ctl(cfg, f"image set-windows {shlex.quote(t['image'])}")
    log(f"Windows image -> {t['image']}")
    if t.get("kms"):
        _ctl(cfg, f"cluster-config set {KMS_KEY} {shlex.quote(t['kms'].strip())}")
        log(f"Windows KMS host -> {t['kms'].strip()}")
    else:
        # Deleting a key that isn't there succeeds: the agent clears what it set at the next boot.
        _ctl(cfg, f"cluster-config unset {KMS_KEY}")
    licensing = t.get("licensing", "per-vm")
    hosts = set(t.get("hosts") or [])
    marked = []
    for node in cfg.nodes_with("worker"):
        wid = worker_id(alloc, node)
        state = "on" if node.name in hosts else "off"
        _ctl(cfg, f"node windows-host {wid} {state}")
        if state == "on":
            marked.append(f"{node.name} ({wid})")
    _ctl(cfg, f"cluster-config set {LICENSING_KEY} {licensing}")
    log(f"Windows licensing -> {licensing}"
        + (f", licensed hosts: {', '.join(marked)}" if licensing == "per-host" else ""))


def _ctl(cfg, command: str) -> None:
    res = ansible.ctl(cfg, command, check=False)
    if res.returncode != 0:
        raise InstallError(f"boxd-ctl {command} failed: {_err(res)}")


def _err(res) -> str:
    return ((res.stderr or "") + (res.stdout or "")).strip()[-2000:]
