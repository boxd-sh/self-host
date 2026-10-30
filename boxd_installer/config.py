"""Load + validate cluster.toml. The validator is deliberately stricter than
the boxd binaries (whose serde structs ignore unknown fields)."""
import ipaddress
import os
import re
import ssl
import tomllib
import urllib.parse

from . import clouds
from .util import InstallError

KNOWN_ROLES = {"control", "worker", "proxy", "console", "dns", "metrics"}
SERVICE_TABLES = {
    "control", "worker", "proxy", "dns", "names", "acme",
    "github", "google", "oidc", "resend", "analytics", "backups",
    "automations", "templates", "snapshot_archive",
}
INSTALLER_TABLES = {"cluster", "network", "nodes", "license", "telemetry"} | set(clouds.CLOUDS)
ACME_CAS = ("gts", "zerossl", "local")
PRIVATE_NET = ipaddress.ip_network("10.0.0.0/8")
SHA_RE = r"[0-9a-f]{40}"
# A channel boxd publishes (e.g. `latest`); resolved to a sha at apply time by
# assets.resolve.
CHANNEL_RE = r"[a-z][a-z0-9-]{0,39}"
TOP_SCALARS: set[str] = set()

# The prewarmed template sizes when [templates].sizes is absent. Always
# pushed to the cluster too (`boxd-ctl machine-sizes set`), so even an
# unconfigured cluster's API accepts exactly the templates it seeds (not the
# binaries' built-in list).
DEFAULT_SIZES = ({"vcpu": 1, "memory_gb": 4}, {"vcpu": 2, "memory_gb": 8},
                 {"vcpu": 4, "memory_gb": 16})
TEMPLATE_KEYS = {"sizes", "image", "tag"}
# [worker] tc_filters (boxd `WorkerConfig::tc_filters`): "full" runs the
# LLVM-compiled per-VM TC filters; "anti-spoof" attaches hand-assembled ones
# (MAC/IP anti-spoof + infra-only :57073) and drops the tenant allow-list.
TC_FILTERS = ("full", "anti-spoof")

class Node:
    def __init__(self, raw: dict):
        self.name: str = raw["name"]
        self.raw = raw
        self.roles: list[str] = []
        self.qualifiers: dict[str, str] = {}
        for r in raw["roles"]:
            role, _, qual = r.partition(":")
            self.roles.append(role)
            if qual:
                self.qualifiers[role] = qual
        self.internal_ip: str = raw["internal_ip"]
        self.machine_type: str | None = raw.get("machine_type")
        self.data_disk_gb: int | None = raw.get("data_disk_gb")
        self.boot_disk_gb: int | None = raw.get("boot_disk_gb")

    def has(self, role: str) -> bool:
        return role in self.roles

    @property
    def primary_role(self) -> str:
        for r in ("worker", "control", "proxy", "dns", "metrics", "console"):
            if r in self.roles:
                return r
        return self.roles[0]

PROVIDER_TABLES = {"google", "github", "oidc", "resend", "analytics",
                   "backups", "automations", "snapshot_archive"}
# [oidc] (boxd `OidcConfig`): single sign-on through the company's OpenID
# Connect provider. Everything but the secret goes in cluster.toml; the
# secret goes in the providers config, which validate never reads.
OIDC_REQUIRED = ("issuer", "client_id", "display_name")
OIDC_KEYS = set(OIDC_REQUIRED) | {"ca_cert", "bootstrap_admin", "link_email_domains"}
# The object-store fields [snapshot_archive] takes from terraform (AWS) or
# the providers config; cluster.toml only names the folder.
ARCHIVE_STORE_KEYS = ("endpoint", "bucket", "region", "access_key", "secret_key")

def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out

class Config:
    def __init__(self, doc: dict, base_dir: str = "."):
        self.doc = doc
        # cluster.toml's directory: relative paths in it ([license].key_file)
        # start here.
        self.base_dir = base_dir
        self.overlay: dict = {}
        self.name: str = doc["cluster"]["name"]
        self.zone: str = doc["cluster"]["zone"]
        # A sha, or a channel name until assets.resolve swaps in the sha the
        # channel points at (and keeps the name in assets_channel).
        self.assets_version: str = doc["cluster"]["assets_version"]
        self.assets_channel: str | None = None
        self.env_group: str = self.name.replace("-", "_")
        self.cloud_name: str = _cloud_name(doc)
        self.cloud: dict = doc[self.cloud_name]
        self.vpc_cidr: str = self.cloud["vpc_cidr"]
        self.network: dict = doc["network"]
        self.nodes: list[Node] = [Node(n) for n in doc["nodes"]]
        mod = clouds.CLOUDS[self.cloud_name]
        self.interface: str = self.cloud.get("interface", mod.DEFAULT_INTERFACE)
        self.acme_ca: str = doc.get("acme", {}).get("ca", mod.DEFAULT_CA)
        # "private": proxy/dns get no public IP and advertise their internal
        # ones (AWS only; clients come in over Twingate or similar).
        self.ingress: str = self.cloud.get("ingress", "public")

    def apply_overlay(self, overlay: dict) -> None:
        """Secret provider tables from the encrypted state; they win over
        cluster.toml on conflicting leaves."""
        self.overlay = overlay

    def table(self, name: str) -> dict:
        base = self.doc.get(name, {})
        if name in self.overlay:
            return _deep_merge(base, self.overlay[name])
        return base

    def machine_sizes(self) -> list[dict]:
        """The effective size list: [templates].sizes, else DEFAULT_SIZES.
        seed_templates builds a template for each and then pushes the same
        list to the cluster, so the API only ever accepts sizes that have a
        template."""
        sizes = self.table("templates").get("sizes") or DEFAULT_SIZES
        return [{"vcpu": s["vcpu"], "memory_gb": s["memory_gb"]} for s in sizes]

    def snapshot_archive_declared(self) -> bool:
        """[snapshot_archive] in cluster.toml or the providers config."""
        return "snapshot_archive" in self.doc or "snapshot_archive" in self.overlay

    def nodes_with(self, role: str) -> list[Node]:
        return [n for n in self.nodes if n.has(role)]

def validate_providers(doc: dict) -> None:
    errs = []
    for key, val in doc.items():
        if key not in PROVIDER_TABLES:
            errs.append(f"[{key}]: not a provider table (allowed: "
                        f"{', '.join(sorted(PROVIDER_TABLES))})")
        elif not isinstance(val, dict):
            errs.append(f"[{key}]: must be a table")
        else:
            errs += [f"[{key}].{k} must be a quoted string, e.g. {k} = \"…\""
                     for k in ("client_id", "client_secret") if k in val and not isinstance(val[k], str)]
    oidc = doc.get("oidc") if isinstance(doc.get("oidc"), dict) else {}
    errs += [f"[oidc].{k} belongs in cluster.toml's [oidc]: only client_secret goes here"
             for k in oidc if k != "client_secret"]
    for k in ("auth_configs", "webhook_secret", "connect_domain"):
        if k in doc.get("automations", {}):
            errs.append(f"[automations].{k} is derived by the installer — remove it")
    if errs:
        raise InstallError("providers config invalid:\n  " + "\n  ".join(errs))

def inert_sign_in(doc: dict) -> str | None:
    """With [oidc], boxd-console turns Google and GitHub sign-in off, so their
    OAuth clients stay stored but do nothing; say so rather than leave them
    looking live. A GitHub App (app_* keys) keeps working and isn't named.
    `doc` is cluster.toml merged with the providers config."""
    if "oidc" not in doc:
        return None
    off = [f"[{t}]" for t in ("google", "github")
           if isinstance(doc.get(t), dict) and doc[t].get("client_id")]
    if not off:
        return None
    return (f"{' and '.join(off)} sign-in is off while [oidc] is set: single sign-on is "
            "the only way in. The client stays stored; remove it with `providers edit` "
            "if you won't switch back")

def resolver_addr(value) -> str:
    """`IP` or `IP:port` (IPv6 as `[addr]:port`) → the `ip:port` the boxd
    binaries parse as a SocketAddr; port 53 when none is given."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"must be an IP address with an optional port, got {value!r}")
    host, port = value, "53"
    m = re.fullmatch(r"\[(.+)\](?::(\d*))?", value)
    if m:
        host, port = m.group(1), m.group(2) if m.group(2) is not None else "53"
    elif value.count(":") == 1:
        host, port = value.split(":")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError(f"{value!r} is not an IP address (a resolver hostname "
                         "would need resolving before it can resolve)") from None
    if not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"bad port in {value!r}")
    return f"[{ip}]:{port}" if ip.version == 6 else f"{ip}:{port}"

def _cloud_name(doc: dict) -> str:
    return next(k for k in clouds.CLOUDS if k in doc)

def load(path: str) -> Config:
    with open(path, "rb") as fh:
        doc = tomllib.load(fh)
    _validate(doc)
    return Config(doc, os.path.dirname(os.path.abspath(path)))

def _validate(doc: dict) -> None:
    errs: list[str] = []

    for key in doc:
        if key not in INSTALLER_TABLES | SERVICE_TABLES | TOP_SCALARS:
            errs.append(f"unknown top-level key: {key!r}")

    present = [k for k in clouds.CLOUDS if k in doc]
    if len(present) != 1:
        errs.append("exactly one cloud table required: "
                    + " or ".join(f"[{k}]" for k in clouds.CLOUDS)
                    + (f" (got {', '.join(present)})" if present else ""))
        raise InstallError("cluster.toml invalid:\n  " + "\n  ".join(errs))
    cloud = present[0]
    mod = clouds.CLOUDS[cloud]

    for tbl, keys in (("cluster", ("name", "zone", "assets_version")),
                      (cloud, mod.REQUIRED),
                      ("network", ("vni_range_start", "vni_range_end", "bridge_subnet_base"))):
        missing = [k for k in keys if k not in doc.get(tbl, {})]
        if missing:
            errs.append(f"[{tbl}] missing: {', '.join(missing)}")

    if errs:
        raise InstallError("cluster.toml invalid:\n  " + "\n  ".join(errs))

    if not re.fullmatch(r"[a-z][a-z0-9-]*", doc["cluster"]["name"]):
        errs.append("[cluster].name must be lowercase kebab-case (cloud resource naming)")
    av = doc["cluster"]["assets_version"]
    if not (isinstance(av, str) and (re.fullmatch(SHA_RE, av) or re.fullmatch(CHANNEL_RE, av))):
        errs.append("[cluster].assets_version must be a 40-hex git sha (pinned) or a "
                    'channel name like "dev" (the latest bundle published to it)')
    if "org_name" in doc["cluster"] and (why := org_name_error(doc["cluster"]["org_name"])):
        errs.append(f"[cluster].org_name {why}")

    try:
        vpc = ipaddress.ip_network(doc[cloud]["vpc_cidr"])
    except ValueError as e:
        errs.append(f"[{cloud}].vpc_cidr: {e}")
        vpc = None
    if vpc and (vpc.version != 4 or not vpc.subnet_of(PRIVATE_NET)):
        errs.append(f"[{cloud}].vpc_cidr must be inside 10.0.0.0/8 (boxd treats 10/8 "
                    "as the cluster-private network)")

    nodes = doc.get("nodes") or []
    if not nodes:
        errs.append("no [[nodes]] defined")
    names = [n.get("name") for n in nodes]
    if len(set(names)) != len(names):
        errs.append("duplicate node names")

    ns_slots: dict[str, str] = {}
    voters = 0
    for n in nodes:
        nm = n.get("name", "?")
        if not n.get("roles"):
            errs.append(f"{nm}: no roles")
            continue
        for r in n["roles"]:
            role, _, qual = r.partition(":")
            if role not in KNOWN_ROLES:
                errs.append(f"{nm}: unknown role {role!r}")
            if role == "control":
                pass
            if qual:
                if role != "dns":
                    errs.append(f"{nm}: qualifier on non-dns role {r!r}")
                elif not re.fullmatch(r"ns[0-9]+", qual):
                    errs.append(f"{nm}: dns qualifier must be nsN, got {qual!r}")
                elif qual in ns_slots:
                    errs.append(f"duplicate nameserver slot {qual} ({ns_slots[qual]} and {nm})")
                else:
                    ns_slots[qual] = nm
            if role == "dns" and not qual:
                errs.append(f"{nm}: dns role needs an nsN qualifier (e.g. \"dns:ns1\")")
        if "control" in [r.partition(":")[0] for r in n["roles"]]:
            voters += 1
        if "internal_ip" not in n:
            errs.append(f"{nm}: internal_ip required")
        elif vpc:
            try:
                if ipaddress.ip_address(n["internal_ip"]) not in vpc:
                    errs.append(f"{nm}: internal_ip {n['internal_ip']} outside vpc_cidr {vpc}")
            except ValueError as e:
                errs.append(f"{nm}: internal_ip: {e}")
        if "admin" in str(n.get("roles")):
            errs.append(f"{nm}: the admin role is not supported on a self-hosted cluster")
        is_worker = any(r.partition(":")[0] == "worker" for r in n.get("roles", []))
        # Given hosts (no terraform): the adapter checks their disks.
        if mod.TF_DIR is not None:
            if is_worker and not n.get("data_disk_gb"):
                errs.append(f"{nm}: worker nodes must set data_disk_gb (VM data lives "
                            "on the data disk; the 50G boot disk cannot hold it)")
            if not is_worker and n.get("data_disk_gb"):
                errs.append(f"{nm}: data_disk_gb is only valid on worker nodes")
        if "machine_type" in n and not (isinstance(n["machine_type"], str)
                                        and n["machine_type"]):
            errs.append(f"{nm}: machine_type must be a quoted string, e.g. "
                        '"m8i.4xlarge" or "n2-standard-16"')
        if "boot_disk_gb" in n and (not isinstance(n["boot_disk_gb"], int) or n["boot_disk_gb"] < 50):
            errs.append(f"{nm}: boot_disk_gb must be an integer >= 50")
        if "tc_filters" in n:
            if not is_worker:
                errs.append(f"{nm}: tc_filters is only valid on worker nodes")
            elif n["tc_filters"] not in TC_FILTERS:
                errs.append(f"{nm}: tc_filters must be one of {', '.join(TC_FILTERS)}, "
                            f"got {n['tc_filters']!r}")

    if voters == 0:
        errs.append("at least one control node required")
    elif voters % 2 == 0:
        errs.append(f"control (voter) count must be odd, got {voters}")

    if ns_slots:
        expected = {f"ns{i}" for i in range(1, len(ns_slots) + 1)}
        if set(ns_slots) != expected:
            errs.append(f"nameserver slots must be contiguous from ns1, got {sorted(ns_slots)}")

    net = doc["network"]
    if vpc:
        try:
            base = ipaddress.ip_network(net["bridge_subnet_base"] + "/16", strict=False)
            if base.overlaps(vpc):
                errs.append("[network].bridge_subnet_base overlaps vpc_cidr")
            if not base.subnet_of(PRIVATE_NET):
                errs.append("[network].bridge_subnet_base must be inside 10.0.0.0/8")
        except ValueError as e:
            errs.append(f"[network].bridge_subnet_base: {e}")
    if not net.get("vni_range_start", 0) < net.get("vni_range_end", 0):
        errs.append("[network] vni_range_start must be < vni_range_end")

    tcf = doc.get("worker", {}).get("tc_filters")
    if tcf is not None and tcf not in TC_FILTERS:
        errs.append(f"[worker].tc_filters must be one of {', '.join(TC_FILTERS)}, got {tcf!r}")

    if "acme" not in doc:
        errs.append('[acme] required: contact_email (public CAs), or ca = "local"')
    else:
        for k in doc["acme"]:
            if k in ("directory_url", "eab_kid", "eab_hmac_key", "eab_single_use",
                     "local_ca"):
                errs.append(f"[acme].{k} is forced by the installer — remove it")
        ca = doc["acme"].get("ca", mod.DEFAULT_CA)
        if ca not in ACME_CAS:
            errs.append(f"[acme].ca must be one of {', '.join(ACME_CAS)}, got {ca!r}")
        elif ca == "gts" and cloud != "gcp":
            errs.append('[acme].ca = "gts" needs a GCP project to mint its EAB key — '
                        f'use "zerossl" on [{cloud}] clusters')
        if ca != "local" and "contact_email" not in doc["acme"]:
            errs.append("[acme].contact_email required")
        if "dns_verify_resolver" in doc["acme"]:
            try:
                resolver_addr(doc["acme"]["dns_verify_resolver"])
            except ValueError as e:
                errs.append(f"[acme].dns_verify_resolver: {e}")
    if "automations" in doc and "composio_api_key" not in doc["automations"]:
        errs.append("[automations].composio_api_key required when [automations] is set")
    for k in ("auth_configs", "webhook_secret", "connect_domain"):
        if k in doc.get("automations", {}):
            errs.append(f"[automations].{k} is derived by the installer — remove it")

    mod.validate(doc, errs)
    _validate_templates(doc, errs)
    _validate_snapshot_archive(doc, errs)
    _validate_oidc(doc, errs)
    _validate_license(doc, errs)
    _validate_telemetry(doc, errs)

    consoles = [n["name"] for n in nodes if any(r.split(":")[0] == "console" for r in n.get("roles", []))]
    if len(consoles) > 1:
        errs.append(f"at most one console node supported, got {consoles}")

    if errs:
        raise InstallError("cluster.toml invalid:\n  " + "\n  ".join(errs))

# boxd-console trims [oidc] values with Rust's str::trim, which takes the
# Unicode White_Space characters. Python's str.strip() also takes
# U+001C-U+001F, which Rust keeps.
RUST_WHITESPACE = ("\t\n\x0b\x0c\r \x85\xa0       "
                   "         　")

def rust_trim(s: str) -> str:
    return s.strip(RUST_WHITESPACE)

_PEM_CERT = re.compile(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.S)

def pem_certificates(text: str) -> list[str]:
    """The CERTIFICATE blocks in text: the only ones boxd-console's PEM
    reader takes ("X509 CERTIFICATE" and "TRUSTED CERTIFICATE" aren't), with
    the text around them ignored, as there."""
    return _PEM_CERT.findall(text)

# What the URL parser boxd-console uses (the url crate, WHATWG rules)
# refuses in a host name; control characters and spaces are refused before.
_FORBIDDEN_HOST = set("#%/:<>?@[\\]^|\x7f")
_DIGITS = {8: "01234567", 10: "0123456789", 16: "0123456789abcdefABCDEF"}

def _ipv4_part(p: str) -> int | None:
    """One part of a WHATWG IPv4 host: decimal, 0x hex or 0-led octal."""
    if not p:
        return None
    base = 10
    if p[:2] in ("0x", "0X"):
        p, base = p[2:], 16
    elif len(p) > 1 and p[0] == "0":
        p, base = p[1:], 8
    if any(c not in _DIGITS[base] for c in p):
        return None
    return int(p, base) if p else 0

def _ends_in_a_number(host: str) -> bool:
    """Whether the WHATWG parser reads host as an IPv4 address."""
    parts = host.split(".")
    if parts[-1] == "" and len(parts) > 1:
        parts.pop()
    last = parts[-1]
    return bool(last) and (all(c in _DIGITS[10] for c in last) or _ipv4_part(last) is not None)

def _whatwg_ipv4(host: str) -> int | None:
    """host as the WHATWG parser reads an IPv4 host, shorthand such as
    "127.1" included; None where it fails, and the console refuses it."""
    parts = host.split(".")
    if parts[-1] == "" and len(parts) > 1:
        parts.pop()
    nums = [_ipv4_part(p) for p in parts]
    if len(nums) > 4 or None in nums or any(n > 255 for n in nums[:-1]) \
            or nums[-1] >= 256 ** (5 - len(nums)):
        return None
    return nums[-1] + sum(n * 256 ** (3 - i) for i, n in enumerate(nums[:-1]))

def org_name_error(name) -> str | None:
    """Why boxd-console would refuse `[cluster].org_name` (its
    `cluster_org_name`) at startup, or None. As a rename on its Org page:
    1-64 bytes once trimmed, empty meaning none, and not "personal"."""
    if not isinstance(name, str):
        return "must be a quoted string"
    n = rust_trim(name)
    if len(n.encode()) > 64:
        return f"{name!r} is longer than 64 bytes"
    if n.isascii() and n.lower() == "personal":
        return "can't be 'personal': the console reserves it"
    return None

def oidc_issuer_error(issuer) -> str | None:
    """Why boxd-console would refuse this issuer at startup, or why no
    sign-in could ever work with it; None when neither. As
    `Oidc::from_config`: a URL, https://, or http:// on a loopback address
    only, and no query or fragment. The console also keeps the issuer as
    written, for the discovery URL and the exact match with the provider's,
    so whitespace, control characters and backslashes, which a URL parser
    drops or rewrites, could never match."""
    if not isinstance(issuer, str) or not issuer:
        return "must be the provider's issuer URL, a quoted string"
    if any(c in RUST_WHITESPACE or c < " " or c in "\x7f\\" for c in issuer):
        return (f"{issuer!r} holds whitespace, a control character or a backslash: copy the "
                "provider's issuer exactly")
    if "?" in issuer or "#" in issuer:
        return f"{issuer!r} must have no query or fragment"
    try:
        u = urllib.parse.urlsplit(issuer)
        u.port  # raises for a port that isn't a number from 0 to 65535
    except ValueError as e:
        return f"{issuer!r} is not a URL ({e})"
    host = u.hostname or ""
    if u.scheme not in ("https", "http") or not host:
        return f"{issuer!r} must be an https:// URL"
    if u.netloc.rpartition("@")[2].startswith("["):
        try:
            loopback = ipaddress.IPv6Address(host).is_loopback
        except ValueError:
            return f"{issuer!r} is not a URL (the host {host!r} isn't an IPv6 address)"
    elif any(c in _FORBIDDEN_HOST for c in host):
        return f"{issuer!r} is not a URL (the host {host!r} holds a character a host can't)"
    elif _ends_in_a_number(host):
        ip = _whatwg_ipv4(host)
        if ip is None:
            return f"{issuer!r} is not a URL (the host {host!r} isn't an IPv4 address)"
        loopback = ip >> 24 == 127
    else:
        loopback = host == "localhost"
    if not (u.scheme == "https" or loopback):
        return f"{issuer!r} must be an https:// URL"
    return None

def _validate_oidc(doc: dict, errs: list[str]) -> None:
    """[oidc]: the checks boxd-console makes at startup, which would
    otherwise stop it after the apply. Its client_secret belongs in the
    providers config. The issuer's discovery document is checked by
    `validate` (oidc.check_discovery), which needs the network."""
    if "oidc" not in doc:
        return
    t = doc["oidc"]
    if not isinstance(t, dict):
        errs.append("[oidc] must be a table")
        return
    for k in t:
        if k == "client_secret":
            errs.append("[oidc].client_secret is a secret: set it with `providers edit`, "
                        "not in cluster.toml")
        elif k not in OIDC_KEYS:
            errs.append(f"[oidc].{k}: unknown key (allowed: {', '.join(sorted(OIDC_KEYS))})")
    for k in OIDC_REQUIRED:
        if not (isinstance(t.get(k), str) and rust_trim(t[k])):
            errs.append(f"[oidc].{k} required, a non-empty quoted string")
    if "issuer" in t and (why := oidc_issuer_error(t["issuer"])):
        errs.append(f"[oidc].issuer {why}")
    # Empty bootstrap_admin and ca_cert mean none, as in boxd-console.
    admin = t.get("bootstrap_admin")
    if admin is not None and not isinstance(admin, str):
        errs.append("[oidc].bootstrap_admin must be a quoted string, an email address")
    elif admin and (a := rust_trim(admin)):
        local, at, domain = a.partition("@")
        if not (at and local and domain) or any(c in RUST_WHITESPACE for c in a):
            errs.append(f"[oidc].bootstrap_admin {admin!r} is not an email address")
    domains = t.get("link_email_domains")
    if domains is not None:
        if not isinstance(domains, list):
            errs.append('[oidc].link_email_domains must be a list, e.g. ["example.com"]')
        else:
            for d in domains:
                norm = rust_trim(d).lstrip("@") if isinstance(d, str) else ""
                if not norm or any(c in norm for c in "@ \t"):
                    errs.append(f"[oidc].link_email_domains: {d!r} is not a domain")
    ca = t.get("ca_cert")
    if ca is not None and not isinstance(ca, str):
        errs.append("[oidc].ca_cert must be PEM text, in a multi-line string")
    elif ca and rust_trim(ca):
        if "PRIVATE KEY" in ca:
            errs.append("[oidc].ca_cert holds a private key: give only the CA's certificate, "
                        "and keep the key out of cluster.toml")
        elif not (certs := pem_certificates(ca)):
            errs.append("[oidc].ca_cert holds no PEM certificate (a -----BEGIN CERTIFICATE----- "
                        "block)")
        else:
            try:
                ssl.create_default_context().load_verify_locations(cadata="\n".join(certs))
            except (ssl.SSLError, ValueError) as e:
                errs.append(f"[oidc].ca_cert holds a certificate it can't read: {e}")

def _validate_license(doc: dict, errs: list[str]) -> None:
    """[license] key_file: a path relative to cluster.toml's directory (so it
    is inside the directory ./boxd-install mounts). Whether the file exists
    is a warning at validate, not an error."""
    if "license" not in doc:
        return
    t = doc["license"]
    if not isinstance(t, dict):
        errs.append("[license] must be a table")
        return
    for k in t:
        if k != "key_file":
            errs.append(f"[license].{k}: unknown key (allowed: key_file); the key itself goes "
                        "in the file, never in cluster.toml")
    f = t.get("key_file")
    if f is None:
        return
    parts = f.split("/") if isinstance(f, str) else []
    if not isinstance(f, str) or not f or f.startswith("/") or ".." in parts:
        errs.append(f"[license].key_file {f!r} must be a file name relative to cluster.toml's "
                    'directory, e.g. "license.key"')

def _key_file_set(doc: dict) -> bool:
    """[license] key_file is set. A [license] that is not a table is
    _validate_license's error, not a crash here."""
    lic = doc.get("license")
    return isinstance(lic, dict) and bool(lic.get("key_file"))

def _validate_telemetry(doc: dict, errs: list[str]) -> None:
    """[telemetry] enabled: a bool. Unset, apply leaves the cluster's setting
    as it is. false needs a license key (control refuses `off` without one
    that includes telemetry opt-out), so it is an error here, before
    terraform. The cloud and region that control reports come from the
    provider table, not from here."""
    if "telemetry" not in doc:
        return
    t = doc["telemetry"]
    if not isinstance(t, dict):
        errs.append("[telemetry] must be a table")
        return
    for k in t:
        if k != "enabled":
            errs.append(f"[telemetry].{k}: unknown key (allowed: enabled)")
    if "enabled" in t and not isinstance(t["enabled"], bool):
        errs.append("[telemetry].enabled must be true or false")
    elif t.get("enabled") is False and not _key_file_set(doc):
        errs.append("[telemetry] enabled = false needs [license] key_file: only a license key "
                    "that includes telemetry opt-out can turn telemetry off")

def _validate_snapshot_archive(doc: dict, errs: list[str]) -> None:
    """[snapshot_archive] turns the archive on. In cluster.toml it takes only
    archive_dir: the store comes from terraform (AWS) or `providers edit`.
    Same archive_dir rules as boxd's SnapshotArchiveConfig::validate."""
    if "snapshot_archive" not in doc:
        return
    t = doc["snapshot_archive"]
    if not isinstance(t, dict):
        errs.append("[snapshot_archive] must be a table")
        return
    for k in t:
        if k == "sse":
            errs.append("[snapshot_archive].sse is not supported: the archive relies on the "
                        "bucket's default encryption")
        elif k in ARCHIVE_STORE_KEYS:
            errs.append(f"[snapshot_archive].{k} doesn't belong in cluster.toml: the installer "
                        "creates the bucket on AWS; for your own bucket set the store fields "
                        "with `providers edit`")
        elif k != "archive_dir":
            errs.append(f"[snapshot_archive].{k}: unknown key (allowed: archive_dir)")
    if "archive_dir" in t:
        d = t["archive_dir"]
        parts = d.split("/") if isinstance(d, str) else []
        if (not isinstance(d, str) or not d or d.startswith("/") or d.endswith("/")
                or "" in parts or "." in parts or ".." in parts or parts[0] == "org"):
            errs.append(f"[snapshot_archive].archive_dir {d!r} must be a relative folder "
                        "without '..', not under org/ (DR backups live there)")

def _validate_templates(doc: dict, errs: list[str]) -> None:
    """Shape only. Whether each size fits the worker types is checked by
    terraform against the cloud's real specs (terraform/<cloud>/machine_sizes.tf)."""
    if "templates" not in doc:
        return
    t = doc["templates"]
    if not isinstance(t, dict):
        errs.append("[templates] must be a table")
        return
    for k in t:
        if k not in TEMPLATE_KEYS:
            errs.append(f"[templates].{k}: unknown key (allowed: {', '.join(sorted(TEMPLATE_KEYS))})")
    for k in ("image", "tag"):
        if k in t and not (isinstance(t[k], str) and t[k]):
            errs.append(f"[templates].{k} must be a non-empty string")
    if "sizes" not in t:
        return
    sizes = t["sizes"]
    if not isinstance(sizes, list) or not sizes:
        errs.append("[templates].sizes must be a non-empty list of "
                    "{ vcpu = N, memory_gb = N } tables")
        return
    seen: set[tuple[int, int]] = set()
    for i, s in enumerate(sizes):
        where = f"[templates].sizes[{i}]"
        if not isinstance(s, dict):
            errs.append(f"{where} must be a {{ vcpu = N, memory_gb = N }} table")
            continue
        extra = set(s) - {"vcpu", "memory_gb"}
        if extra:
            errs.append(f"{where}: unknown key(s) {', '.join(sorted(extra))}")
        ok = True
        for k in ("vcpu", "memory_gb"):
            v = s.get(k)
            # bool is an int subclass; `vcpu = true` is a typo, not a size.
            if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
                errs.append(f"{where}.{k} must be a positive integer, got {v!r}")
                ok = False
        if not ok:
            continue
        pair = (s["vcpu"], s["memory_gb"])
        if pair in seen:
            errs.append(f"{where}: duplicate size {pair[0]}x{pair[1]}")
            continue
        seen.add(pair)
