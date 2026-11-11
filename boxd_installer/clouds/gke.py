"""GKE: boxd's roles as pods on Google Kubernetes Engine.

cluster.toml carries [gke] next to [gcp]: [gcp] still names the project, the
region and the state bucket (the state object, terraform's backend and the
ACME default are GCP's, as for the Compute Engine target), and [gke]
replaces [gcp]'s instance fields and the [[nodes]] list:

    control    StatefulSet, 1 or 3 Raft voters, pod network, a disk each   (pool "system")
    console    StatefulSet, pod network                                   (pool "system")
    proxy+dns  one pair per edge index, host network, each on its own
               one-node pool behind its own passthrough load balancer     (pools "edge-<i>")
    worker     privileged, host network, one per node                     (pool "worker")

Terraform (terraform/gke) makes the pools, the load balancers, the firewall
rules, the buckets and, with `cluster = "create"`, the cluster and its
network; the installer then renders every Kubernetes object itself and
applies it (kube_render.py, kube.py).

This module is the cloud side: [gke] validation, the topology the renderer
and the node-id allocation see (one synthetic node per pod), the license
check on the worker pool, the checks of an existing cluster and of the org
policy, the terraform variables and the plan guard. The state bucket is
[gcp]'s, so its functions are gcp's."""
import ipaddress
import json
import re
import secrets as pysecrets
import os
import time

from ..util import InstallError, log, run
from .gcp import (backend_args, copy, download, object_size, state_url,  # noqa: F401
                  tf_key_env, upload)

TF_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "terraform", "gke"))
DEFAULT_CA = "gts"
SSH_PATH = "kubectl"
# The registry boxd publishes its Kubernetes images in (boxd, boxd-node-prep,
# boxd-dockerd), tagged with the release id. [images] registry points the
# cluster at a mirror instead.
DEFAULT_REGISTRY = "europe-docker.pkg.dev/boxd-prod-491613/releases"
# The NIC of GKE's Ubuntu nodes (no gVNIC): the vrack interface and the
# proxy's public interface in every rendered config.
IFACE = "ens4"
# Where node-prep mounts the local SSD RAID on a worker node, the worker's data dir.
DATA_MOUNT = "/mnt/disks/boxd-data"
# The cluster's [gcp] table on GKE: the instance fields belong to [gke].
CLOUD_REQUIRED = ("project", "region", "state_bucket")
GCP_KEYS = set(CLOUD_REQUIRED) | {"auto_backups", "auto_storage_bucket"}
GCP_MOVED = {"zone": "[gke] zones", "vpc_cidr": "[gke] vpc_cidr",
             "image_family": "nothing (GKE picks the node images)",
             "interface": "nothing (GKE's Ubuntu nodes name it ens4)"}
KEYS = {"cluster", "namespace", "zones", "admin_cidrs", "host_kernel", "ingress",
        "internal_ips", "vpc_cidr", "pods_cidr", "services_cidr", "observability",
    "analytics", "analytics_disk_gb",
        "control_replicas", "pools"}
CREATE_ONLY = ("admin_cidrs", "vpc_cidr", "pods_cidr", "services_cidr")
POOLS = ("system", "edge", "worker")
POOL_KEYS = {"system": {"machine_type", "nodes"}, "edge": {"machine_type", "nodes"},
             "worker": {"machine_type", "nodes", "local_ssd", "data_disk_gb"}}
AUTOSCALING_KEYS = {"autoscaling", "min_nodes", "max_nodes", "min_node_count",
                    "max_node_count", "total_min_nodes", "total_max_nodes"}
HOST_KERNELS = ("boxd", "stock")
INGRESS = ("public", "internal")
DEFAULT_PODS = "172.20.0.0/16"
DEFAULT_SERVICES = "172.24.0.0/20"
# Local NVMe SSD counts GCE accepts on N2 (by vCPU count; terraform and GCE
# refuse a count the type doesn't take).
LOCAL_SSD_COUNTS = (0, 1, 2, 4, 8, 16, 24)
# Families without nested virtualization on GCE: AMD, Arm and E2.
NO_NESTED_VIRT = ("e2", "n2d", "c2d", "c3d", "c4d", "n4d", "t2d", "t2a", "c4a", "n4a")
# Nested virtualization in GKE node pools.
MIN_VERSION = (1, 28, 4)
DOCKER_BRIDGE = ipaddress.ip_network("172.17.0.0/16")
PRIVATE_NET = ipaddress.ip_network("10.0.0.0/8")
NAMESPACE_RE = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")
CLUSTER_RE = re.compile(r"projects/([a-z][-a-z0-9]{4,28}[a-z0-9])/locations/([a-z0-9-]+)"
                        r"/clusters/([a-z]([-a-z0-9]{0,38}[a-z0-9])?)")
REGISTRY_RE = re.compile(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]+)?(/[a-z0-9._-]+)*")
SECRET_NAME_RE = re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?")
# Service account ids are 6-30 chars: <name>-backups, <name>-worker, <name>-node.
SA_ID_MAX = 30
BUCKET_NAME_MAX = 63
# Free tier without a key that lifts it (license.py).
UNLIMITED = "unlimited-worker-vcpus"
SHIELDED_VM_POLICY = "compute.requireShieldedVm"
EDGE_UDP_PORTS = ("53",)
SSH_RANGE, EXPOSED_RANGE = "10000-30000", "40000-60000"
RFC1918 = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")

def settings(doc: dict) -> dict:
    """[gke] with every default filled in. Pools: system defaults to the
    smallest multiple of the zone count that holds every control replica;
    edge to 2 nodes (two proxies, two nameservers); worker has no default size."""
    g = doc.get("gke") or {}
    zones = list(g.get("zones") or [])
    s = {"namespace": "boxd", "host_kernel": "boxd", "ingress": "public",
         "observability": True, "control_replicas": 3, "internal_ips": [],
         "analytics": True, "analytics_disk_gb": 100,
         "pods_cidr": DEFAULT_PODS, "services_cidr": DEFAULT_SERVICES,
         **{k: v for k, v in g.items() if k != "pools"}}
    s["zones"] = zones
    s["create"] = g.get("cluster") == "create"
    pools = g.get("pools") if isinstance(g.get("pools"), dict) else {}
    nz = max(len(zones), 1)
    control = s["control_replicas"] if isinstance(s["control_replicas"], int) else 1
    system = {"machine_type": "e2-standard-4", **(pools.get("system") or {})}
    system.setdefault("nodes", nz * max(1, -(-control // nz)))
    edge = {"machine_type": "n2-standard-4", "nodes": 2, **(pools.get("edge") or {})}
    worker = {"local_ssd": 4, **(pools.get("worker") or {})}
    s["pools"] = {"system": system, "edge": edge, "worker": worker}
    return s

def parse_cluster(value) -> tuple[str, str, str] | None:
    """projects/<p>/locations/<l>/clusters/<c> -> (p, l, c)."""
    m = CLUSTER_RE.fullmatch(value) if isinstance(value, str) else None
    return (m.group(1), m.group(2), m.group(3)) if m else None

def pod_dns(namespace: str, sts: str, ordinal: int) -> str:
    """A StatefulSet pod's stable DNS name (its headless Service is the
    StatefulSet's own name). Host-network pods resolve to their node's IP."""
    return f"{sts}-{ordinal}.{sts}.{namespace}.svc.cluster.local"

def topology_nodes(doc: dict) -> list[dict]:
    """One [[nodes]]-shaped entry per pod, for the renderer and the node-id
    allocation (state.allocate keys ids by `<name>/<role>`). internal_ip is
    the pod's DNS name: what the Raft peers and learners dial."""
    s = settings(doc)
    ns = s["namespace"]
    out = [{"name": f"control-{i}", "roles": ["control"],
            "internal_ip": pod_dns(ns, "boxd-control", i)}
           for i in range(s["control_replicas"])]
    out.append({"name": "console", "roles": ["console"],
                "internal_ip": pod_dns(ns, "boxd-console", 0)})
    for i in range(s["pools"]["edge"]["nodes"]):
        out.append({"name": f"proxy-{i}", "roles": ["proxy"],
                    "internal_ip": pod_dns(ns, f"boxd-proxy-{i}", 0)})
        out.append({"name": f"dns-{i}", "roles": [f"dns:ns{i + 1}"],
                    "internal_ip": pod_dns(ns, f"boxd-dns-{i}", 0)})
    w = s["pools"]["worker"]
    for i in range(w["nodes"]):
        out.append({"name": f"worker-{i}", "roles": ["worker"],
                    "internal_ip": pod_dns(ns, "boxd-worker", i),
                    "machine_type": w["machine_type"]})
    return out

def ordinal(node_name: str) -> int:
    """worker-3 / proxy-3 / dns-3 / control-3 -> 3."""
    return int(node_name.rsplit("-", 1)[1])

def registry(doc: dict) -> str:
    return (doc.get("images") or {}).get("registry") or DEFAULT_REGISTRY

def image(doc: dict, name: str, version: str) -> str:
    return f"{registry(doc)}/{name}:{version}"

def computer_image(doc: dict, bundle_image: str) -> str:
    """The VM image templates and `image set` use: <registry>/computer when
    [images] registry is set (a mirror), else the bundle's (boxd's)."""
    own = (doc.get("images") or {}).get("registry")
    return f"{own}/computer" if own else bundle_image

def guest_mtu(vpc_mtu: int) -> int:
    """[network] guest_mtu for a VPC MTU: Geneve takes 50 bytes, so 1500 for
    the guest needs 1550 (8896 -> 0, the guest's own 1500; 1460 -> 1410;
    1500 -> 1450)."""
    return 0 if vpc_mtu - 50 >= 1500 else vpc_mtu - 50

def console_cluster_ip(services_cidr: str) -> str:
    """The console Service's pinned ClusterIP, which the proxies'
    internal_services name (they take an IP). Offset 80 sits in the range's
    lower band, which Kubernetes keeps for static assignments."""
    net = ipaddress.ip_network(services_cidr)
    offset = 80 if net.num_addresses > 160 else net.num_addresses // 2
    return str(net.network_address + offset)

def _cidr(value, what: str, errs: list[str]):
    try:
        net = ipaddress.ip_network(value)
    except (ValueError, TypeError) as e:
        errs.append(f"{what}: {e}")
        return None
    if net.version != 4:
        errs.append(f"{what} must be an IPv4 range, got {value!r}")
        return None
    return net

def _pos_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0

def validate(doc: dict, errs: list[str]) -> None:
    """[gke] and what it changes in the rest of cluster.toml: no [[nodes]],
    [gcp] without its instance fields, the routed network only. The checks
    that need the cluster or the project (an existing cluster's version,
    dataplane, ranges, MTU; the org policy) are live_checks."""
    from .. import config as _config
    g = doc["gke"]
    if not isinstance(g, dict):
        errs.append("[gke] must be a table")
        return
    if doc.get("nodes"):
        errs.append("[[nodes]] is not used with [gke]: the installer runs boxd's roles as pods; "
                    "size them with control_replicas and [gke.pools.*] instead")
    gcp = doc.get("gcp") or {}
    for k in gcp:
        if k in GCP_MOVED:
            errs.append(f"[gcp].{k} is not used with [gke]: set {GCP_MOVED[k]}")
        elif k not in GCP_KEYS:
            errs.append(f"[gcp].{k} is not a [gcp] key with [gke] (known: "
                        f"{', '.join(sorted(GCP_KEYS))})")
    for k in g:
        if k not in KEYS:
            errs.append(f"[gke].{k}: unknown key (allowed: {', '.join(sorted(KEYS))})")
    s = settings(doc)
    region = str(gcp.get("region", ""))
    name = doc["cluster"]["name"]

    cluster = g.get("cluster")
    parsed = parse_cluster(cluster)
    if cluster is None:
        errs.append('[gke].cluster required: "create", or an existing cluster as '
                    '"projects/<project>/locations/<location>/clusters/<name>"')
    elif cluster != "create" and parsed is None:
        errs.append(f'[gke].cluster must be "create" or '
                    f'"projects/<project>/locations/<location>/clusters/<name>", got {cluster!r}')
    elif parsed:
        project, location, _ = parsed
        if project != gcp.get("project"):
            errs.append(f"[gke].cluster is in project {project}, but [gcp].project is "
                        f"{gcp.get('project')!r}: the installer works in one project")
        if location != region and not location.startswith(f"{region}-"):
            errs.append(f"[gke].cluster is in {location}, outside [gcp].region {region}")
        for k in CREATE_ONLY:
            if k in g:
                errs.append(f"[gke].{k} is only for cluster = \"create\": an existing cluster's "
                            "network and control-plane access are read from it")
    if s["create"]:
        if len(name) > 40:
            errs.append("[cluster].name is the GKE cluster's name with cluster = \"create\": "
                        "at most 40 characters")
        cidrs = g.get("admin_cidrs")
        if not (isinstance(cidrs, list) and cidrs):
            errs.append("[gke].admin_cidrs required with cluster = \"create\": the addresses that "
                        "may reach the cluster's control plane, the installer's among them "
                        '(e.g. ["203.0.113.4/32"])')
        else:
            for c in cidrs:
                _cidr(c, "[gke].admin_cidrs", errs)

    ns = s["namespace"]
    if not (isinstance(ns, str) and NAMESPACE_RE.fullmatch(ns)) or ns.startswith("kube-"):
        errs.append(f"[gke].namespace must be a Kubernetes namespace name (lowercase letters, "
                    f"digits, dashes; not kube-*), got {ns!r}")

    zones = g.get("zones")
    if not (isinstance(zones, list) and zones and all(isinstance(z, str) for z in zones)):
        errs.append(f'[gke].zones required: the zones of {region or "[gcp].region"} the pools '
                    'go in, e.g. ["europe-west4-a"] (one) or three for a regional cluster')
        zones = []
    else:
        for z in zones:
            if not re.fullmatch(re.escape(region) + r"-[a-z]", z):
                errs.append(f"[gke].zones: {z!r} is not a zone of [gcp].region {region}")
        if len(set(zones)) != len(zones):
            errs.append("[gke].zones has duplicates")

    for k, allowed in (("host_kernel", HOST_KERNELS), ("ingress", INGRESS)):
        if s[k] not in allowed:
            errs.append(f"[gke].{k} must be one of {', '.join(allowed)}, got {s[k]!r}")
    if not isinstance(s["analytics"], bool):
        errs.append("[gke].analytics must be true or false: ClickHouse in the cluster for the "
                    "console's usage graphs (false: none, or [analytics] names your own)")
    d = s["analytics_disk_gb"]
    if type(d) is not int or not 10 <= d <= 65536:
        errs.append(f"[gke].analytics_disk_gb must be a whole number of GB from 10 to 65536, "
                    f"got {d!r}")
    if not isinstance(s["observability"], bool):
        errs.append("[gke].observability must be true or false")
    control = s["control_replicas"]
    if not _pos_int(control):
        errs.append(f"[gke].control_replicas must be 1 or 3, got {control!r}")
    elif control % 2 == 0:
        errs.append(f"[gke].control_replicas must be odd (Raft voters need a majority), got {control}")
    elif control > 3:
        errs.append(f"[gke].control_replicas: more than 3 is not supported yet, got {control}")

    if s["ingress"] == "internal":
        ca = doc.get("acme", {}).get("ca", DEFAULT_CA)
        if ca != "local":
            errs.append(f'[gke].ingress = "internal" requires [acme] ca = "local" (got {ca!r}): '
                        "public CAs validate by querying boxd-dns from the internet, which an "
                        "internal load balancer can't answer")
    ips = g.get("internal_ips")
    if ips is not None:
        if s["ingress"] != "internal":
            errs.append('[gke].internal_ips is only for ingress = "internal"')
        elif not isinstance(ips, list):
            errs.append("[gke].internal_ips must be a list of IPv4 addresses, one per edge")
        else:
            for ip in ips:
                try:
                    ipaddress.IPv4Address(ip)
                except (ValueError, TypeError):
                    errs.append(f"[gke].internal_ips: {ip!r} is not an IPv4 address")

    _validate_pools(g, s, zones, errs)

    # The ranges: the VM pool must stay clear of every one GKE uses.
    net = doc.get("network") or {}
    for k in net:
        if k not in _config.NETWORK_KEYS:
            errs.append(f"[network].{k} is not a key the installer knows "
                        f"({', '.join(sorted(_config.NETWORK_KEYS))})")
    if net.get("mode", "routed") != "routed":
        errs.append('[network].mode must be "routed" with [gke] (Geneve between nodes; GKE '
                    "has no L2 between nodes for bridge mode)")
    if "bridge_subnet_base" in net:
        errs.append('[network].bridge_subnet_base is for mode = "bridge", which GKE does not run')
    vpc = None
    if s["create"]:
        vpc = _cidr(g.get("vpc_cidr"), "[gke].vpc_cidr", errs) if "vpc_cidr" in g else None
        if "vpc_cidr" not in g:
            errs.append('[gke].vpc_cidr required with cluster = "create": the nodes\' subnet, '
                        'inside 10.0.0.0/8 (e.g. "10.60.0.0/20")')
        elif vpc and not vpc.subnet_of(PRIVATE_NET):
            errs.append("[gke].vpc_cidr must be inside 10.0.0.0/8 (boxd treats 10/8 as the "
                        "cluster-private network)")
        ranges = {"[gke].vpc_cidr": vpc,
                  "[gke].pods_cidr": _cidr(s["pods_cidr"], "[gke].pods_cidr", errs),
                  "[gke].services_cidr": _cidr(s["services_cidr"], "[gke].services_cidr", errs)}
        if ranges["[gke].services_cidr"] and ranges["[gke].services_cidr"].prefixlen > 24:
            errs.append("[gke].services_cidr must be a /24 or larger")
        named = [(k, v) for k, v in ranges.items() if v]
        for i, (ka, a) in enumerate(named):
            for kb, b in named[i + 1:]:
                if a.overlaps(b):
                    errs.append(f"{ka} {a} overlaps {kb} {b}")
            if a.overlaps(DOCKER_BRIDGE):
                errs.append(f"{ka} {a} overlaps {DOCKER_BRIDGE}, Docker's default bridge on the "
                            "workers")
        if "pool" in net:
            pool = _net_or_none(net["pool"])
            for k, v in named:
                if pool and v != vpc and pool.overlaps(v):
                    errs.append(f"[network].pool {pool} overlaps {k} {v}")
    if "pool" in net:
        errs.extend(_config.pool_errors(net["pool"], vpc))
    if "guest_mtu" in net:
        m = net["guest_mtu"]
        if isinstance(m, bool) or not isinstance(m, int) or not (m == 0 or 1280 <= m <= 1500):
            errs.append(f"[network].guest_mtu must be 0 (the guest's own 1500) or 1280 to 1500, "
                        f"got {m!r}")
    if ("vni_range_start" in net or "vni_range_end" in net) and \
            not net.get("vni_range_start", 100) < net.get("vni_range_end", 10000):
        errs.append("[network] vni_range_start must be < vni_range_end")

    # Tables the installer sets itself on GKE, or that GKE doesn't have.
    if doc.get("proxy", {}).get("ssh_port") == 22:
        errs.append("[proxy].ssh_port can't be 22 on GKE: the edge node's own sshd holds it "
                    "(per-VM SSH reaches the proxy through a redirect of 10000-30000; use 2222)")
    if "listen" in doc.get("dns", {}):
        errs.append("[dns].listen is set by the installer on GKE: each dns listens on its "
                    "edge's load balancer IP")
    for k in ("ksm", "swap_gb"):
        if k in doc.get("worker", {}):
            errs.append(f"[worker].{k} is not supported on GKE yet (GKE nodes have no swap)")

    images = doc.get("images")
    if images is not None:
        if not isinstance(images, dict):
            errs.append("[images] must be a table")
        else:
            for k, v in images.items():
                if k == "registry":
                    if not (isinstance(v, str) and REGISTRY_RE.fullmatch(v)):
                        errs.append(f"[images].registry must be a registry path without a scheme "
                                    f"or a trailing slash, e.g. "
                                    f'"europe-west4-docker.pkg.dev/my-project/boxd", got {v!r}')
                elif k == "pull_secret":
                    if not (isinstance(v, str) and SECRET_NAME_RE.fullmatch(v)):
                        errs.append(f"[images].pull_secret must be the name of a Secret in the "
                                    f"namespace, got {v!r}")
                else:
                    errs.append(f"[images].{k}: unknown key (allowed: pull_secret, registry)")

    # Names terraform derives from [cluster].name.
    accounts = ["-node", "-worker"] + (["-backups"] if gcp.get("auto_backups", True) else [])
    if len(name) + max(len(a) for a in accounts) > SA_ID_MAX:
        errs.append(f"[cluster].name must be <= {SA_ID_MAX - len('-backups')} chars on GKE: the "
                    "service accounts <name>-worker and <name>-backups are at most "
                    f"{SA_ID_MAX} chars")
    bucket = f"{gcp.get('project', '')}-{name}-storage"
    if len(bucket) > BUCKET_NAME_MAX:
        errs.append(f"the bucket name {bucket} is {len(bucket)} chars, over GCS's "
                    f"{BUCKET_NAME_MAX}: shorten [cluster].name")

def _net_or_none(value):
    try:
        return ipaddress.ip_network(value)
    except (ValueError, TypeError):
        return None

def _validate_pools(g: dict, s: dict, zones: list, errs: list[str]) -> None:
    pools = g.get("pools", {})
    if not isinstance(pools, dict):
        errs.append("[gke.pools] must be a table of system, edge and worker")
        return
    for name, t in pools.items():
        if name not in POOLS:
            errs.append(f"[gke.pools.{name}]: unknown pool (boxd's pools are "
                        f"{', '.join(POOLS)})")
            continue
        if not isinstance(t, dict):
            errs.append(f"[gke.pools.{name}] must be a table")
            continue
        for k in t:
            if k in AUTOSCALING_KEYS:
                errs.append(f"[gke.pools.{name}].{k}: boxd's pools don't autoscale; their size is "
                            "nodes, and apply changes it (a worker leaves only through the "
                            "installer's drain)")
            elif k not in POOL_KEYS[name]:
                errs.append(f"[gke.pools.{name}].{k}: unknown key (allowed: "
                            f"{', '.join(sorted(POOL_KEYS[name]))})")
    for name in POOLS:
        p = s["pools"][name]
        if not (isinstance(p.get("machine_type"), str) and p.get("machine_type")):
            if name == "worker":
                errs.append('[gke.pools.worker].machine_type required: an Intel type with nested '
                            'virtualization, e.g. "n2-standard-16"')
            else:
                errs.append(f"[gke.pools.{name}].machine_type must be a quoted string")
        if name == "worker" and "nodes" not in p:
            errs.append("[gke.pools.worker].nodes required: how many workers (one per node)")
        elif not _pos_int(p.get("nodes")):
            errs.append(f"[gke.pools.{name}].nodes must be a positive integer, got "
                        f"{p.get('nodes')!r}")
    system, worker = s["pools"]["system"], s["pools"]["worker"]
    if zones and _pos_int(system.get("nodes")):
        if system["nodes"] % len(zones):
            errs.append(f"[gke.pools.system].nodes ({system['nodes']}) must be a multiple of the "
                        f"{len(zones)} zones: a pool has the same number of nodes in each zone")
        control = s["control_replicas"]
        if _pos_int(control) and system["nodes"] < control:
            errs.append(f"[gke.pools.system].nodes ({system['nodes']}) must be at least "
                        f"control_replicas ({control}): each control replica gets its own node")
    mt = worker.get("machine_type")
    if isinstance(mt, str) and mt.split("-")[0] in NO_NESTED_VIRT:
        errs.append(f"[gke.pools.worker].machine_type {mt}: AMD, Arm and E2 machines have no "
                    "nested virtualization, so they can't boot VMs. Use Intel N2 (recommended), "
                    "N1, C2 or C3")
    lssd = worker.get("local_ssd")
    if lssd not in LOCAL_SSD_COUNTS or isinstance(lssd, bool):
        errs.append(f"[gke.pools.worker].local_ssd must be one of "
                    f"{', '.join(map(str, LOCAL_SSD_COUNTS))}, got {lssd!r}")
    elif lssd == 0:
        if not (_pos_int(worker.get("data_disk_gb")) and worker["data_disk_gb"] >= 100):
            errs.append("[gke.pools.worker].data_disk_gb required with local_ssd = 0: each "
                        "worker's pd-balanced data disk, in GB (at least 100; VM data and its "
                        "cache live there)")
    elif "data_disk_gb" in worker:
        errs.append("[gke.pools.worker].data_disk_gb is only for local_ssd = 0: with local SSDs "
                    "the worker's data dir is their RAID")

def warnings(cfg) -> list[str]:
    """validate's and apply's warnings: the recommended topology, and ranges
    in 10/8 (boxd's routed datapath and guest firewall treat 10/8 as its own)."""
    s = settings(cfg.doc)
    out = []
    if s["control_replicas"] == 1:
        out.append("[gke].control_replicas = 1: one control replica holds the cluster's state "
                   "alone, so losing its node stops the cluster until it is back. 3 is "
                   "recommended for anything people rely on (it can't be changed after the "
                   "first apply)")
    if s["pools"]["edge"]["nodes"] == 1:
        out.append("[gke.pools.edge].nodes = 1: one proxy and one nameserver; while that node is "
                   "down nothing reaches the cluster. 2 is recommended")
    if s["create"]:
        for k in ("pods_cidr", "services_cidr"):
            net = _net_or_none(s[k])
            if net and net.overlaps(PRIVATE_NET):
                out.append(f"[gke].{k} {net} is inside 10.0.0.0/8, which boxd treats as its own "
                           "(machines' pool, routed infrastructure addresses); a range outside "
                           f"it, like the default {DEFAULT_PODS if k == 'pods_cidr' else DEFAULT_SERVICES}, "
                           "is safer")
    return out

def license_errors(cfg) -> list[str]:
    """The worker pool's vCPUs (nodes x the machine type's) against the
    license: 16 without a key that lifts the limit (the free tier). On GKE a
    pool over the limit is refused before anything is made, rather than
    installed and then refusing new machines."""
    from .. import license
    s = settings(cfg.doc)
    w = s["pools"]["worker"]
    if license.key_path(cfg) and UNLIMITED in license.key_features(cfg):
        return []
    per = license.vcpus("gcp", w["machine_type"])
    limit = license.FREE_TIER_MAX_WORKER_VCPUS
    if per is None:
        return [f"can't count the vCPUs of [gke.pools.worker].machine_type {w['machine_type']}: "
                f"without a license key that lifts the {limit}-vCPU limit, use a predefined type "
                "(n2-standard-16, n2-highmem-8, ...)"]
    total = per * w["nodes"]
    if total > limit:
        return [f"the worker pool has {total} vCPUs ({w['nodes']} x {w['machine_type']}), and "
                f"without a license key that lifts it boxd allows {limit} (the free tier). Add "
                "[license] key_file with a key from boxd that includes unlimited worker vCPUs, "
                "or use fewer or smaller workers"]
    return []

# ------------------------------------------------------------- live checks

HOST_KERNEL_POLICY = (
    'host_kernel = "boxd" needs Secure Boot off on the worker pool (boxd\'s host kernel is not '
    "signed by Google), and this project enforces the org policy "
    "constraints/compute.requireShieldedVm, which refuses such nodes. Set [gke] host_kernel = "
    '"stock" (GKE\'s kernel, Secure Boot on: forks of running machines then pause the source '
    "longer), or have the policy lifted for this project")

def shielded_vm_enforced(policy: dict) -> bool:
    """`gcloud resource-manager org-policies describe --effective` of
    compute.requireShieldedVm: a boolean policy, enforced or not."""
    return bool((policy.get("booleanPolicy") or {}).get("enforced"))

def _version(v: str) -> tuple[int, int, int] | None:
    m = re.match(r"(\d+)\.(\d+)\.(\d+)", v or "")
    return tuple(int(x) for x in m.groups()) if m else None

def _ar_registry(doc: dict) -> bool:
    host = registry(doc).split("/")[0]
    return host.endswith("-docker.pkg.dev") or host.endswith("gcr.io")

def existing_cluster_problems(cfg, c: dict, network: dict, subnet: dict) -> tuple[list[str], list[str]]:
    """What stops boxd on an existing cluster (`gcloud container clusters
    describe`, its network and subnet as `gcloud compute ... describe` give
    them), each error with its remedy; and the warnings."""
    errs, warns = [], []
    s = settings(cfg.doc)
    name = c.get("name", "the cluster")
    if (c.get("autopilot") or {}).get("enabled"):
        errs.append(f"{name} is an Autopilot cluster, which can't run boxd's workers (privileged, "
                    "host network, nested virtualization): use a Standard cluster")
    v = _version(c.get("currentMasterVersion", ""))
    if v is None or v < MIN_VERSION:
        errs.append(f"{name} runs GKE {c.get('currentMasterVersion', '?')}; boxd needs "
                    f"{'.'.join(map(str, MIN_VERSION))} or later (nested virtualization in node "
                    "pools). Upgrade the control plane first")
    nc = c.get("networkConfig") or {}
    if nc.get("datapathProvider") == "ADVANCED_DATAPATH":
        errs.append(f"{name} runs Dataplane V2, which boxd is not tested with (its eBPF sits "
                    "beside boxd's own TC programs on the workers). Use a cluster on the "
                    "legacy dataplane")
    ip = c.get("ipAllocationPolicy") or {}
    if not ip.get("useIpAliases"):
        errs.append(f"{name} is not VPC-native (alias IPs): boxd needs a VPC-native cluster")
    region = cfg.cloud.get("region", "")
    locations = c.get("locations") or []
    for z in s["zones"]:
        if locations and z not in locations:
            errs.append(f"[gke].zones: {z} is not one of {name}'s node locations "
                        f"({', '.join(locations)}); add it to the cluster or pick from those")
    netpath = nc.get("network") or ""
    if netpath.startswith("projects/") and netpath.split("/")[1] != cfg.cloud.get("project"):
        errs.append(f"{name}'s network {netpath} is in another project (Shared VPC), which the "
                    "installer does not support yet")
    pool = _net_or_none(cfg.network.get("pool"))
    node_cidr = _net_or_none(subnet.get("ipCidrRange"))
    if node_cidr and not node_cidr.subnet_of(PRIVATE_NET):
        errs.append(f"{name}'s node subnet {node_cidr} is outside 10.0.0.0/8, which boxd treats as "
                    "the cluster-private network (its guest firewall keeps machines off it): "
                    "use a cluster whose nodes are in 10/8")
    for what, value in (("node subnet", node_cidr),
                        ("pod range", _net_or_none(ip.get("clusterIpv4CidrBlock"))),
                        ("service range", _net_or_none(ip.get("servicesIpv4CidrBlock")))):
        if pool and value and pool.overlaps(value):
            errs.append(f"[network].pool {pool} overlaps {name}'s {what} {value}: pick another "
                        "/16 in 10.0.0.0/8 for the machines")
    mtu = network.get("mtu") or 1460
    if "guest_mtu" not in cfg.network and 0 < guest_mtu(mtu) < 1280:
        errs.append(f"{name}'s VPC has an MTU of {mtu}, which leaves guests less than 1280 after "
                    "Geneve's 50 bytes: raise the VPC MTU (8896 gives guests their own 1500)")
    elif "guest_mtu" not in cfg.network and guest_mtu(mtu):
        warns.append(f"{name}'s VPC MTU is {mtu}: guests get an MTU of {guest_mtu(mtu)} (Geneve "
                     "takes 50 bytes). A VPC MTU of 8896 gives them their own 1500")
    if _ar_registry(cfg.doc) and not subnet.get("privateIpGoogleAccess"):
        errs.append(f"{name}'s node subnet {subnet.get('name', '')} has Private Google Access off, "
                    f"and boxd's images come from {registry(cfg.doc).split('/')[0]}: turn it on "
                    f"(gcloud compute networks subnets update {subnet.get('name', '<subnet>')} "
                    f"--region {region} --enable-private-ip-google-access)")
    if s["observability"] and not ((c.get("monitoringConfig") or {})
                                   .get("managedPrometheusConfig") or {}).get("enabled"):
        errs.append(f"[gke].observability is on, but {name} has Managed Service for Prometheus "
                    f"off: turn it on (gcloud container clusters update {name} --location "
                    f"{c.get('location', '<location>')} --enable-managed-prometheus), or set "
                    "observability = false")
    if (c.get("releaseChannel") or {}).get("channel", "UNSPECIFIED") != "UNSPECIFIED":
        excl = ((c.get("maintenancePolicy") or {}).get("window") or {}).get("maintenanceExclusions") or {}
        scopes = {(e.get("maintenanceExclusionOptions") or {}).get("scope") for e in excl.values()}
        if not scopes & {"NO_MINOR_OR_NODE_UPGRADES", "NO_UPGRADES"}:
            warns.append(f"{name} has no maintenance exclusion on node upgrades: GKE's automatic "
                         "node upgrades recreate worker nodes, and every machine on a recreated "
                         "worker is lost (the workers' disruption budget only delays it). Add one: "
                         f"gcloud container clusters update {name} --location "
                         f"{c.get('location', '<location>')} --add-maintenance-exclusion-name "
                         "boxd-hold --add-maintenance-exclusion-scope no_minor_or_node_upgrades "
                         "--add-maintenance-exclusion-until-end-of-support")
    if (c.get("databaseEncryption") or {}).get("state") != "ENCRYPTED":
        warns.append(f"{name} stores Secrets without application-layer encryption: boxd's "
                     "configs (its keys among them) are in a Secret. Consider turning it on "
                     "(docs/REFERENCE.md, \"GKE\", Secrets at rest)")
    return errs, warns

def _gcloud_json(args: list[str], what: str) -> dict:
    res = run(["gcloud", *args, "--format=json"], capture=True, check=False, quiet=True)
    if res.returncode != 0:
        raise InstallError(f"can't read {what}: {(res.stderr or '').strip()[-600:]}")
    return json.loads(res.stdout or "{}")

def live_checks(cfg) -> tuple[list[str], list[str], dict]:
    """The checks that read the project: the org policy on Shielded VM (with
    host_kernel = "boxd"), and an existing cluster's settings. Read-only.
    Returns (errors, warnings, facts): facts has an existing cluster's VPC
    MTU (vpc_mtu), which terraform passes through to the render."""
    errs, warns, facts = [], [], {}
    s = settings(cfg.doc)
    project = cfg.cloud["project"]
    if s["host_kernel"] == "boxd":
        res = run(["gcloud", "resource-manager", "org-policies", "describe", SHIELDED_VM_POLICY,
                   f"--project={project}", "--effective", "--format=json"],
                  capture=True, check=False, quiet=True)
        if res.returncode == 0:
            if shielded_vm_enforced(json.loads(res.stdout or "{}")):
                errs.append(HOST_KERNEL_POLICY)
        else:
            warns.append(f"could not read the org policy {SHIELDED_VM_POLICY} on {project} "
                         f"({(res.stderr or '').strip()[-200:]}). If it is enforced, worker "
                         'nodes can\'t start with host_kernel = "boxd": set "stock" then')
    if not s["create"]:
        proj, loc, name = parse_cluster(s["cluster"])
        c = _gcloud_json(["container", "clusters", "describe", name, f"--location={loc}",
                          f"--project={proj}"], f"the cluster {s['cluster']}")
        nc = c.get("networkConfig") or {}
        netpath = nc.get("network") or c.get("network") or ""
        subpath = nc.get("subnetwork") or c.get("subnetwork") or ""
        network = _gcloud_json(["compute", "networks", "describe", netpath.rsplit("/", 1)[-1],
                                f"--project={proj}"], f"the network {netpath}")
        sub_region = subpath.split("/regions/")[1].split("/")[0] if "/regions/" in subpath \
            else cfg.cloud["region"]
        subnet = _gcloud_json(["compute", "networks", "subnets", "describe",
                               subpath.rsplit("/", 1)[-1], f"--region={sub_region}",
                               f"--project={proj}"], f"the subnet {subpath}")
        e, w = existing_cluster_problems(cfg, c, network, subnet)
        errs += e
        warns += w
        facts["vpc_mtu"] = int(network.get("mtu") or 1460)
    return errs, warns, facts

def state_errors(cfg, state) -> list[str]:
    """What can't change once the cluster exists (recorded by apply)."""
    g = state.data.get("gke") or {}
    s = settings(cfg.doc)
    errs = []
    if "cluster" in g and g["cluster"] != s.get("cluster"):
        errs.append(f"[gke].cluster was {g['cluster']!r} and is now {s.get('cluster')!r}: a boxd "
                    "cluster can't move between GKE clusters. Put it back")
    if "namespace" in g and g["namespace"] != s["namespace"]:
        errs.append(f"[gke].namespace was {g['namespace']!r} and is now {s['namespace']!r}: it "
                    "can't change after the first apply. Put it back")
    if "controls" in g and g["controls"] != s["control_replicas"]:
        errs.append(f"[gke].control_replicas was {g['controls']} and is now "
                    f"{s['control_replicas']}: the Raft voters are fixed after the first apply. "
                    "Put it back")
    if "edges" in g and s["pools"]["edge"]["nodes"] < g["edges"]:
        errs.append(f"[gke.pools.edge].nodes went from {g['edges']} to "
                    f"{s['pools']['edge']['nodes']}: edges can only be added. Each proxy holds its "
                    "machines' SSH ports and each nameserver is in your zone's delegation, so "
                    "removing one is a support procedure. Put it back")
    if g.get("analytics_disk_gb") and g["analytics_disk_gb"] != s["analytics_disk_gb"]:
        errs.append(f"[gke].analytics_disk_gb was {g['analytics_disk_gb']} and is now "
                    f"{s['analytics_disk_gb']}: ClickHouse's disk size is fixed once it exists (a "
                    "StatefulSet's volume template can't change). Put it back; to grow the disk, "
                    "resize its PVC (data-boxd-clickhouse-0) with kubectl")
    lssd = s["pools"]["worker"]["local_ssd"]
    if "local_ssd" in g and bool(g["local_ssd"]) != bool(lssd):
        errs.append("[gke.pools.worker].local_ssd switched between local SSDs and a persistent "
                    "disk: the workers' data dir would move. Put it back")
    return errs

def record(cfg, state) -> None:
    """After a deploy: what state_errors holds the next apply to."""
    s = settings(cfg.doc)
    g = state.data.setdefault("gke", {})
    g.update(cluster=s["cluster"], namespace=s["namespace"], controls=s["control_replicas"],
             edges=s["pools"]["edge"]["nodes"], local_ssd=s["pools"]["worker"]["local_ssd"],
             workers=s["pools"]["worker"]["nodes"])
    if s["analytics"] and not cfg.table("analytics"):
        # ClickHouse's disk exists from now on, and keeps its size (prune leaves it).
        g["analytics_disk_gb"] = s["analytics_disk_gb"]
    state.save()

# --------------------------------------------------------------- terraform

def _gke_state(state) -> dict:
    return state.data.setdefault("gke", {})

def kms_suffix(state) -> str:
    """The Secrets-encryption key ring's suffix: a key ring can't be deleted,
    so a cluster made again after destroy needs a new name."""
    g = _gke_state(state)
    if not g.get("kms_suffix"):
        g["kms_suffix"] = pysecrets.token_hex(3)
        state.save()
    return g["kms_suffix"]

def hold_start(state) -> str:
    """When the maintenance exclusion on node upgrades starts: the first
    apply, kept so the plan is stable."""
    g = _gke_state(state)
    if not g.get("hold_start"):
        g["hold_start"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        state.save()
    return g["hold_start"]

def edge_tcp_ports(doc: dict) -> list[str]:
    p = doc.get("proxy") or {}
    ports = {53, p.get("http_port", 80), p.get("https_port", 443), p.get("api_port", 9443)}
    return [str(x) for x in sorted(ports)] + [SSH_RANGE, EXPOSED_RANGE]

def tfvars(cfg, state, live_workers: int | None = None, facts: dict | None = None) -> dict:
    """terraform/gke's variables. `live_workers`: the worker StatefulSet's
    replicas now. The pool never shrinks under a running worker: apply drains
    and removes a worker, and its node, itself (kube.remove_workers)."""
    from .. import config as _config
    s = settings(cfg.doc)
    pools = s["pools"]
    parsed = parse_cluster(s.get("cluster")) or ("", "", "")
    zones = s["zones"]
    return {
        "project": cfg.cloud["project"],
        "region": cfg.cloud["region"],
        "cluster_name": cfg.name,
        "create_cluster": s["create"],
        "existing_cluster_name": parsed[2],
        "existing_cluster_location": parsed[1],
        "zones": zones,
        "vpc_cidr": s.get("vpc_cidr", ""),
        "pods_cidr": s["pods_cidr"],
        "services_cidr": s["services_cidr"],
        "admin_cidrs": s.get("admin_cidrs", []),
        "system_machine_type": pools["system"]["machine_type"],
        "system_nodes_per_zone": pools["system"]["nodes"] // max(len(zones), 1),
        # A cluster boxd made has only boxd's pools, and GKE's own system pods
        # need one without a taint; in your cluster they run on your pools.
        "system_tainted": not s["create"],
        "edge_machine_type": pools["edge"]["machine_type"],
        "edge_count": pools["edge"]["nodes"],
        "worker_machine_type": pools["worker"]["machine_type"],
        "worker_nodes": max(pools["worker"]["nodes"], live_workers or 0),
        "worker_local_ssd": pools["worker"]["local_ssd"],
        "worker_secure_boot": s["host_kernel"] == "stock",
        "ingress": s["ingress"],
        "internal_ips": s.get("internal_ips") or [],
        "edge_tcp_ports": edge_tcp_ports(cfg.doc),
        "edge_source_ranges": ["0.0.0.0/0"] if s["ingress"] == "public" else list(RFC1918),
        "observability": s["observability"],
        # The Cloud Monitoring dashboard terraform creates ("" = none).
        "dashboard_json": _dashboard(cfg, s, parsed[2] or cfg.name) if s["observability"] else "",
        "enable_backups": cfg.cloud.get("auto_backups", True),
        "enable_storage_bucket": _config.auto_storage_bucket(cfg.doc),
        "kms_suffix": kms_suffix(state) if s["create"] else "",
        "hold_start": hold_start(state) if s["create"] else "",
        "existing_vpc_mtu": (facts or {}).get("vpc_mtu", 1460),
        "machine_sizes": cfg.machine_sizes(),
    }

def _dashboard(cfg, s: dict, cluster: str) -> str:
    from .. import gke_dashboard
    return gke_dashboard.dashboard_json(s["namespace"], cfg.cloud["project"], cluster)

def write_tfvars(cfg, state, out_dir: str, live_workers: int | None = None,
                 facts: dict | None = None) -> str:
    path = os.path.join(out_dir, "terraform.tfvars.json")
    with open(path, "w") as fh:
        json.dump(tfvars(cfg, state, live_workers, facts), fh, indent=1, sort_keys=True)
    return path

# Changes to the worker pool that recreate no node.
POOL_IN_PLACE = {"node_count", "management", "upgrade_settings", "timeouts"}

def _changed(change: dict) -> set[str]:
    before, after = change.get("before") or {}, change.get("after") or {}
    unknown = change.get("after_unknown") or {}
    return {k for k in set(before) | set(after)
            if before.get(k) != after.get(k) and not unknown.get(k)}

def plan_problems(plan: dict) -> list[str]:
    """What a plan (`terraform show -json`) on a running cluster must not do:
    replace the GKE cluster, or change the worker pool in a way that
    recreates its nodes (a recreated worker node loses every machine on it).
    A worker pool's size changes in place."""
    out = []
    changes = plan.get("resource_changes", [])
    # A cluster that exists already (not one this plan creates).
    running = any(rc.get("type") == "google_container_cluster"
                  and rc.get("change", {}).get("actions") != ["create"] for rc in changes)
    for rc in changes:
        actions = rc.get("change", {}).get("actions", [])
        kind, name = rc.get("type"), rc.get("name")
        if kind == "google_container_cluster" and "delete" in actions:
            out.append("the plan replaces the GKE cluster, and every machine with it")
        elif kind == "google_kms_crypto_key" and "delete" in actions and running:
            out.append("the plan replaces the KMS key the GKE cluster's Secrets are encrypted "
                       "with: the installer state no longer holds the key ring's suffix (the "
                       "state was lost or is another cluster's). Restore this cluster's state "
                       "(the state bucket keeps versions) and apply again")
        elif kind == "google_container_node_pool" and name == "worker":
            if "delete" in actions:
                out.append("the plan replaces the worker pool: every worker node, and every "
                           "machine on them, would be lost. Changing the worker machine type, "
                           "local_ssd or host_kernel needs a new pool; this installer doesn't "
                           "move workers between pools yet")
            elif "update" in actions:
                changed = sorted(_changed(rc["change"]) - POOL_IN_PLACE)
                if changed:
                    out.append(f"the plan changes the worker pool's {', '.join(changed)}, which "
                               "GKE applies by recreating its nodes: every machine on them would "
                               "be lost. Put the setting back")
    return out

# --------------------------------------------------------------- operations

def preflight(cfg) -> None:
    run(["gcloud", "auth", "print-access-token"], capture=True, quiet=True)

def delete_worker_instance(cfg, node_name: str, groups: list[str]) -> None:
    """Delete one worker node's instance from its node pool's managed
    instance group, which also lowers the pool's size: the only way to choose
    WHICH node a smaller pool loses (a plain resize picks one itself). Done
    after the worker on it was drained and removed. A node already gone is fine."""
    chosen = None
    for url in groups:
        m = re.search(r"/zones/([^/]+)/instanceGroupManagers/([^/]+)$", url)
        if not m:
            continue
        zone, mig = m.groups()
        if node_name.rsplit("-", 1)[0] == mig.removesuffix("-grp") or len(groups) == 1:
            chosen = (zone, mig)
            break
    if chosen is None:
        raise InstallError(f"can't tell which instance group holds the node {node_name} "
                           f"({', '.join(groups) or 'no instance groups in the outputs'}): delete "
                           f"it from the worker pool by hand, then re-apply")
    zone, mig = chosen
    res = run(["gcloud", "compute", "instance-groups", "managed", "delete-instances", mig,
               f"--instances={node_name}", f"--zone={zone}", f"--project={cfg.cloud['project']}",
               "--quiet"], capture=True, check=False)
    err = (res.stderr or "") + (res.stdout or "")
    if res.returncode != 0 and "not found" not in err.lower() and "notfound" not in err.lower():
        raise InstallError(f"deleting the worker node {node_name} from {mig} failed: "
                           f"{err.strip()[-800:]}")
    log(f"worker node {node_name} deleted from the pool")
