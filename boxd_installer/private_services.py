"""[[worker.private_services]]: services on the operator's private network (an internal load
balancer, a database) that boxes reach by hostname and port through their gateway. The worker's
resolver answers the name with a service address, the worker's nft rule sends that address and
port to the relay on the gateway (boxd-relay, its own unit), and the relay connects onward from the
worker host. No box gains reach into the private network.

The installer owns two things boxd only reads: each name's service address, allocated from
[network] service_range and remembered in the installer state (a box and its snapshots hold the
address, so it never moves to another name), and the guest firewall's --private-service bounds.
The rules below mirror boxd's (boxd_raft::private_services::validate) and add the ones only the
installer can make: every node's address, every worker's bridge subnet, the routed pool.
"""
import ipaddress
import re

KEYS = {"name", "port", "target", "target_subnets"}
SERVICE_RANGE_DEFAULT = "198.18.0.0/24"
# Where a service address may come from: blocks the guest firewall already drops for boxes, all
# outside 10.0.0.0/8 (the one range the tap filters judge replies in). boxd pins the same list.
SERVICE_ADDRESS_BLOCKS = tuple(ipaddress.ip_network(b) for b in (
    "198.18.0.0/15", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"))
# The widest target_subnets entry, so nobody writes 10.0.0.0/8.
WIDEST_TARGET_SUBNET = 20
NEVER_TARGET = tuple(ipaddress.ip_network(b) for b in (
    "0.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "224.0.0.0/3"))
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")

def canonical(name: str) -> str:
    """The form every lookup compares: lowercase, no trailing dot (boxd's canonical_name)."""
    return name.strip().rstrip(".").lower()

def declared(doc: dict) -> list[dict]:
    return list(doc.get("worker", {}).get("private_services", []) or [])

def bounds(entry: dict) -> list[ipaddress.IPv4Network]:
    """Where the relay may connect for one table: the fixed target as a /32, else the subnets."""
    if "target" in entry:
        return [ipaddress.ip_network(f"{entry['target']}/32")]
    return [ipaddress.ip_network(s) for s in entry.get("target_subnets", [])]

def firewall_bounds(services: list[dict]) -> list[str]:
    """The guest firewall's --private-service CIDR:PORT values, one per target or subnet."""
    out: list[str] = []
    for s in services:
        for b in bounds(s):
            v = f"{b}:{s['port']}"
            if v not in out:
                out.append(v)
    return out

def service_range(doc: dict) -> ipaddress.IPv4Network:
    return ipaddress.ip_network(doc.get("network", {}).get("service_range", SERVICE_RANGE_DEFAULT))

def _name_error(name) -> str | None:
    if not isinstance(name, str) or not name.strip():
        return "name must be a hostname"
    n = canonical(name)
    labels = n.split(".")
    if len(n) > 253 or len(labels) < 2 or not all(_LABEL.fullmatch(lbl) for lbl in labels):
        return (f"name {name!r} must be a fully qualified hostname (letters, digits and '-' in "
                "each label, no wildcard), e.g. platform.internal.example.com")
    if labels[-1] == "boxd":
        return f"name {name!r}: names under .boxd are the cluster's own"
    try:
        ipaddress.ip_address(n)
        return f"name {name!r} must be a hostname, not an address"
    except ValueError:
        return None

def validate(doc: dict, errs: list[str], target: str | None) -> None:
    """Append what is wrong with [[worker.private_services]] and [network] service_range."""
    services = declared(doc)
    net = doc.get("network", {})
    if "service_range" in net and not services:
        errs.append("[network].service_range is only used with [[worker.private_services]]")
    if not services:
        return
    if target == "gke":
        errs.append("[[worker.private_services]] is not supported on [gke] yet: the relay "
                    "runs as a systemd unit on a worker host, which a GKE worker pod has not")
        return
    if not isinstance(doc["worker"]["private_services"], list):
        errs.append("[worker].private_services must be tables: [[worker.private_services]]")
        return

    try:
        rng = service_range(doc)
        if rng.version != 4:
            raise ValueError("must be IPv4")
    except ValueError as e:
        errs.append(f"[network].service_range: {e}")
        rng = None
    if rng is not None:
        if not any(rng.subnet_of(b) for b in SERVICE_ADDRESS_BLOCKS):
            errs.append(f"[network].service_range {rng} must be inside one of "
                        + ", ".join(map(str, SERVICE_ADDRESS_BLOCKS))
                        + " (ranges the guest firewall drops for boxes)")
        if not 16 <= rng.prefixlen <= 24:
            errs.append(f"[network].service_range {rng} must be a /16 to /24")

    nodes = []
    for n in doc.get("nodes", []):
        try:
            nodes.append((n.get("name", "?"), ipaddress.ip_address(n["internal_ip"])))
        except (KeyError, ValueError):
            pass
    # The routed pool (a bridge worker's /16 is checked at render, once allocated).
    box_nets = []
    if net.get("mode") == "routed" and "pool" in net:
        try:
            box_nets.append(("[network].pool", ipaddress.ip_network(net["pool"])))
        except ValueError:
            pass

    seen: set[tuple[str, int]] = set()
    for i, s in enumerate(services):
        at = f"[[worker.private_services]] #{i + 1}"
        if not isinstance(s, dict):
            errs.append(f"{at}: must be a table")
            continue
        if "name" in s and isinstance(s["name"], str):
            at = f"[[worker.private_services]] {s['name']!r}"
        if "address" in s:
            errs.append(f"{at}: address is allocated by the installer from [network].service_range "
                        "— remove it")
        for k in s:
            if k not in KEYS | {"address"}:
                errs.append(f"{at}: unknown key {k!r} ({', '.join(sorted(KEYS))})")
        if why := _name_error(s.get("name")):
            errs.append(f"{at}: {why}")
            continue
        port = s.get("port")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
            errs.append(f"{at}: port must be 1 to 65535, got {port!r}")
            continue
        key = (canonical(s["name"]), port)
        if key in seen:
            errs.append(f"{at}: name and port {key[0]}:{port} are declared twice")
        seen.add(key)
        has_t, has_s = "target" in s, "target_subnets" in s
        if has_t == has_s:
            errs.append(f"{at}: give exactly one of target (an IPv4 address) and target_subnets "
                        "(IPv4 CIDRs the name may resolve to)")
            continue
        try:
            if has_t:
                if not isinstance(s["target"], str) or ipaddress.ip_address(s["target"]).version != 4:
                    raise ValueError(f"target must be an IPv4 address, got {s['target']!r}")
            else:
                subs = s["target_subnets"]
                if not isinstance(subs, list) or not subs or not all(isinstance(x, str) for x in subs):
                    raise ValueError("target_subnets must be a non-empty list of IPv4 CIDRs")
                for x in subs:
                    if ipaddress.ip_network(x).version != 4:
                        raise ValueError(f"target_subnets: {x!r} is not IPv4")
            bs = bounds(s)
        except ValueError as e:
            errs.append(f"{at}: {e}")
            continue
        for b in bs:
            if b.prefixlen < WIDEST_TARGET_SUBNET:
                errs.append(f"{at}: {b} is wider than /{WIDEST_TARGET_SUBNET}")
            if bad := next((r for r in NEVER_TARGET if r.overlaps(b)), None):
                errs.append(f"{at}: {b} overlaps {bad}, which is never a target")
            if hit := next((nm for nm, ip in nodes if ip in b), None):
                errs.append(f"{at}: {b} holds the cluster node {hit}")
            for label, bn in box_nets:
                if bn.overlaps(b):
                    errs.append(f"{at}: {b} overlaps the box network {label} {bn}")
            if rng is not None and rng.overlaps(b):
                errs.append(f"{at}: {b} overlaps [network].service_range {rng}")

def bridge_overlap_error(services: list[dict], bridge_subnets: dict[str, str]) -> str | None:
    """A target inside a bridge worker's /16 (known only once the installer has allocated them,
    every worker it ever allocated one to included)."""
    for s in services:
        for b in bounds(s):
            for worker, sub in sorted(bridge_subnets.items()):
                if ipaddress.ip_network(sub).overlaps(b):
                    return (f"[[worker.private_services]] {s['name']!r}: {b} overlaps the bridge "
                            f"subnet {sub} of {worker} (the box network)")
    return None

class AllocationError(ValueError):
    pass

def allocate(remembered: dict[str, str], names: list[str], rng: ipaddress.IPv4Network) -> dict[str, str]:
    """Each canonical name's service address. A name keeps the address it had, also after it was
    removed and comes back; a new name takes the lowest host address of the range that no name
    ever held. Updates `remembered` in place (the state keeps every name it ever saw)."""
    for name, addr in sorted(remembered.items()):
        if ipaddress.ip_address(addr) not in rng:
            raise AllocationError(
                f"service address {addr} of {name!r} (remembered) is outside [network].service_range "
                f"{rng}; put the range back, or reset the allocation: `./boxd-install state "
                "reset-service-addresses` (boxes then get new addresses on their next lookup)")
    held = {ipaddress.ip_address(a) for a in remembered.values()}
    free = (h for h in rng.hosts() if h not in held)
    for name in names:
        if name in remembered:
            continue
        addr = next(free, None)
        if addr is None:
            raise AllocationError(
                f"[network].service_range {rng} has no address left for {name!r} (every one was "
                "given to a name, removed names included); widen it, or reset the allocation: "
                "`./boxd-install state reset-service-addresses`")
        remembered[name] = str(addr)
    return {n: remembered[n] for n in names}
