"""Converge via the vendored ansible tree, and post-install steps over ssh
(through the same tunnel ansible uses: IAP on GCP, SSM on AWS; direct on
metal).

Every boxd-ctl call goes through ctl_on: `sudo boxd-ctl` over ssh on a
control host, or on Kubernetes ([gke]) `kubectl exec` into that control pod
(kube.ctl). The few probes that read a host (logs, deployed files) have a
Kubernetes twin in kube.py, picked at the top of each function."""
import atexit
import json
import os
import shutil
import subprocess
import tempfile
import time

from . import assets, clouds
from .util import InstallError, log, run

ANSIBLE_DIR = os.path.join(os.path.dirname(__file__), "..", "ansible")

# The assets files boxd_copy may take from the node's own copy of the bundle.
# console/ isn't among them: it ships as one tarball (assets.console_tarball).
NODE_ASSET_DIRS = ("bin/", "kernel/", "cli/", "automations/")

_keys_dir: str | None = None
_tf_out: dict = {}
_mux_dir: str | None = None
_masters: set[str] = set()

def set_keys_dir(path: str) -> None:
    global _keys_dir
    _keys_dir = path

def set_tf_outputs(tf_out: dict) -> None:
    """Some clouds address ssh targets by instance id (AWS SSM), which only
    the terraform outputs know."""
    global _tf_out
    _tf_out = tf_out

def _target(cfg, node) -> str:
    return "ubuntu@" + clouds.get(cfg).ssh_target(node, _tf_out)

def preflight(cfg, timeout: int = 300) -> None:
    """Every node, not just the first: on AWS each instance's SSM agent
    registers on its own schedule (private-subnet ones only once the NAT
    gateway routes), so one reachable node says nothing about the rest."""
    cloud = clouds.get(cfg)
    cloud.preflight(cfg)
    deadline = time.time() + timeout
    for node in cfg.nodes:
        attempt = 0
        while True:
            attempt += 1
            res = _ssh(cfg, node, "true", check=False, capture=True)
            if res.returncode == 0:
                log(f"ssh preflight to {node.name} ok (attempt {attempt})")
                break
            if time.time() >= deadline:
                raise InstallError(
                    f"ssh preflight to {node.name} failed for {timeout}s — check "
                    f"{cloud.SSH_HINT} and that instances are up:\n"
                    f"{(res.stderr or '')[-800:]}")
            log(f"ssh preflight to {node.name} attempt {attempt} failed "
                f"(instance/{cloud.SSH_PATH} still propagating?); retrying in 15s…")
            time.sleep(15)

def converge(cfg, out_dir: str, assets_root: str, host_kernel_release: str = "",
             control_carry_dir: str = "") -> None:
    inv = os.path.abspath(os.path.join(out_dir, "inventory.yml"))
    src = os.path.abspath(os.path.join(out_dir, "group_vars", f"{cfg.env_group}.yml"))
    dst = os.path.join(ANSIBLE_DIR, "group_vars", f"{cfg.env_group}.yml")
    if os.path.islink(dst) or os.path.exists(dst):
        os.remove(dst)
    os.symlink(src, dst)
    if _keys_dir is None:
        raise InstallError("ansible.set_keys_dir() not called before converge")
    # In a file, not on the command line: the URL carries the license. The
    # out dir is temporary and apply deletes it.
    node_vars = os.path.abspath(os.path.join(out_dir, "node-assets.json"))
    fd = os.open(node_vars, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(node_assets_vars(cfg, assets_root), fh)
    run(["ansible-playbook", "-i", inv, "site.yml",
         "-e", f"@{node_vars}",
         "-e", f"boxd_assets_dir={os.path.abspath(assets_root)}",
         "-e", f"boxd_tls_local_dir={os.path.abspath(_keys_dir)}",
         "-e", f"boxd_host_kernel_release={host_kernel_release}",
         "-e", f"boxd_control_carry_dir={os.path.abspath(control_carry_dir) if control_carry_dir else ''}",
         "-e", f"boxd_config_local_dir={os.path.abspath(os.path.join(out_dir, 'deploy-config'))}",
         "-e", f"boxd_downloads_local_dir={os.path.abspath(os.path.join(out_dir, 'downloads'))}"],
        cwd=ANSIBLE_DIR)

def node_assets_vars(cfg, assets_root: str) -> dict:
    """Extra vars for boxd-common's fetch-assets: each node downloads the
    bundle from boxd's bucket itself and checks it against these hashes,
    from the manifest assets.ensure verified here. Pushed through the ssh
    tunnel instead, the files crawled at ~0.26 MB/s over SSM: 5 minutes for
    the 82 MB host kernel alone, against ~3s fetched by the node."""
    with open(os.path.join(assets_root, "MANIFEST.json")) as fh:
        files = json.load(fh)["files"]
    sha = cfg.assets_version
    return {"boxd_assets_sha": sha,
            "boxd_assets_url": assets.zip_url(cfg, sha),
            "boxd_asset_hashes": {rel: h for rel, h in files.items()
                                  if rel.startswith(NODE_ASSET_DIRS)}}

def check_hosts(cfg, deploy_pubkey: str) -> None:
    """Given hosts (metal): one quick try per node, before anything is
    downloaded. The first apply on a new cluster stops here, because the
    deploy key it just made is on no host yet; the error says what to add."""
    failed = []
    for node in cfg.nodes:
        check = "sudo -n true" + (" && test -c /dev/kvm" if node.has("worker") else "")
        res = _ssh(cfg, node, check, check=False, capture=True, quiet=True)
        if res.returncode == 0:
            log(f"{node.name}: ssh, sudo" + (" and /dev/kvm" if node.has("worker") else "") + " ok")
            continue
        why = (res.stderr or "").strip().splitlines()
        if res.returncode == 255:
            failed.append(f"{node.name} ({_target(cfg, node)}): ssh failed: "
                          f"{why[-1] if why else 'no reason given'}")
        elif node.has("worker"):
            failed.append(f"{node.name}: ubuntu has no passwordless sudo, or there is no "
                          "/dev/kvm (turn on VT-x/AMD-V in the firmware)")
        else:
            failed.append(f"{node.name}: ubuntu has no passwordless sudo")
    if failed:
        raise InstallError(
            "these hosts are not ready:\n  " + "\n  ".join(failed) + "\n"
            "On every node, the user ubuntu needs passwordless sudo and this line in "
            "~ubuntu/.ssh/authorized_keys (the cluster's deploy key):\n\n"
            f"  {deploy_pubkey}\n\nThen run apply again.")

def _ssh(cfg, node, command: str, **kw):
    """Run one command on a node over a per-node master connection, so the
    tunnel (an SSM session on AWS, seconds to open) is set up once rather
    than per call; seeding alone polls `template list` every 15s for up to
    20 minutes. The master is started on its own with no stdio: a
    ControlPersist master outlives the call, and the ProxyCommand it spawns
    would hold `run`'s captured pipes open (and `run` waiting) until it
    exits. If the master can't start, the command connects directly."""
    target = _target(cfg, node)
    opts = _ssh_opts(cfg) + ["-o", f"ControlPath={_mux_path()}/%C"]
    quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if subprocess.run(["ssh"] + opts + ["-O", "check", target], **quiet).returncode != 0:
        if subprocess.run(["ssh"] + opts + ["-o", "ControlMaster=yes", "-o", "ControlPersist=600",
                                            "-f", "-N", target], **quiet).returncode == 0:
            _masters.add(target)
    return run(["ssh"] + opts + ["-o", "ControlMaster=no", target, command], **kw)

def _mux_path() -> str:
    """Private, and short: a unix socket path is capped at 104 bytes on
    macOS, and %C alone is 40."""
    global _mux_dir
    if _mux_dir is None:
        _mux_dir = tempfile.mkdtemp(prefix="boxd-ssh-", dir="/tmp")
        atexit.register(_close_masters)
    return _mux_dir

def _close_masters() -> None:
    """Don't leave SSM sessions open for ControlPersist's 10 minutes."""
    for target in _masters:
        subprocess.run(["ssh", "-o", f"ControlPath={_mux_dir}/%C", "-O", "exit", target],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    shutil.rmtree(_mux_dir, ignore_errors=True)

def _ssh_opts(cfg) -> list[str]:
    proxy_cmd = clouds.get(cfg).ssh_proxy_command(cfg)
    if _keys_dir is None:
        raise InstallError("ansible.set_keys_dir() not called before ssh use")
    tunnel = ["-o", f"ProxyCommand={proxy_cmd}"] if proxy_cmd else []
    return tunnel + ["-o", "StrictHostKeyChecking=no",
                     "-o", "UserKnownHostsFile=/dev/null",
                     "-o", "LogLevel=ERROR",
                     "-o", "ConnectTimeout=30",
                     "-i", os.path.join(_keys_dir, "boxd-deploy")]

def _kube():
    from . import kube
    return kube

def deployed_network_modes(cfg) -> dict[str, str]:
    """Each worker's [network] mode as its /etc/boxd/worker.toml has it
    ("bridge" when the file names none); workers without the file (new ones)
    are left out. Read and parsed here, so nothing on the host needs to.
    Kubernetes runs routed only: nothing to switch from."""
    import tomllib
    if clouds.kubernetes(cfg):
        return {}
    out = {}
    for node in cfg.nodes_with("worker"):
        res = _ssh(cfg, node, "sudo cat /etc/boxd/worker.toml",
                   capture=True, check=False, quiet=True)
        if res.returncode != 0:
            continue
        try:
            doc = tomllib.loads(res.stdout)
        except tomllib.TOMLDecodeError:
            continue
        out[node.name] = doc.get("network", {}).get("mode", "bridge")
    return out

def deployed_bucket_identities(cfg) -> dict[str, dict]:
    """Which bucket and prefix each worker's deployed storage engine uses
    (/etc/boxd/bucket.toml): kind, bucket, endpoint, prefix. Never the keys:
    the file is parsed here and only those fields are kept. Workers without
    the file are left out."""
    import tomllib
    if clouds.kubernetes(cfg):
        return _kube().deployed_bucket_identities(cfg)
    out = {}
    for node in cfg.nodes_with("worker"):
        res = _ssh(cfg, node, "sudo cat /etc/boxd/bucket.toml",
                   capture=True, check=False, quiet=True)
        if res.returncode != 0:
            continue
        try:
            doc = tomllib.loads(res.stdout)
        except tomllib.TOMLDecodeError:
            continue
        out[node.name] = {"kind": doc.get("kind", "s3"), "bucket": doc.get("bucket", ""),
                          "endpoint": doc.get("endpoint", ""), "prefix": doc.get("prefix", "")}
    return out

def worker_bucket_line(cfg, node) -> str:
    """The last thing the worker's current process logged about its bucket:
    opened, bucket mode off, or a build without the engine refusing its
    config (""; nothing yet). Scoped to the systemd invocation, so an older
    process's line never counts."""
    if clouds.kubernetes(cfg):
        return _kube().worker_bucket_line(cfg, node)
    res = _ssh(cfg, node,
               "sudo journalctl -q --no-pager -o cat _SYSTEMD_INVOCATION_ID="
               "$(systemctl show -p InvocationID --value boxd-worker) "
               "| grep -E 'storage engine: (the bucket is open|bucket mode is off)"
               "|built without the .bucket. feature' | tail -n 1",
               check=False, capture=True, quiet=True)
    return res.stdout or ""

def machine_count(cfg) -> int:
    """The machines `boxd-ctl vm list` shows: "no vms", or a header line and
    one line each."""
    out = ctl(cfg, "vm list", quiet=True).stdout or ""
    lines = [ln for ln in out.splitlines() if ln.strip()]
    if not lines or lines[0].strip() == "no vms":
        return 0
    return len(lines) - 1

# Where a worker without a configured egress CA keeps the pair it made itself
# (boxd-worker: <[paths] data_dir>/egress-ca; render.worker_doc's data_dir).
WORKER_EGRESS_CA_DIR = "/mnt/boxd-data/egress-ca"

def worker_egress_ca(cfg) -> tuple[str, str, str] | None:
    """(worker name, cert PEM, key PEM) of the first worker that made its own
    egress CA, or None. A cluster from before the installer shipped one: its
    machines trust their worker's pair, so adopting one keeps that worker's
    machines working (the others' re-fetch the CA at their next boot,
    restore or fork). On Kubernetes every worker has had the installer's
    pair from its first start."""
    if clouds.kubernetes(cfg):
        return None
    for node in cfg.nodes_with("worker"):
        got = []
        for f in ("egress-ca.pem", "egress-ca.key"):
            res = _ssh(cfg, node, f"sudo cat {WORKER_EGRESS_CA_DIR}/{f}",
                       capture=True, check=False, quiet=True)
            if res.returncode != 0 or "-----BEGIN" not in (res.stdout or ""):
                break
            got.append(res.stdout)
        if len(got) == 2:
            return node.name, got[0], got[1]
    return None

def worker_vcpus(cfg) -> dict[str, int]:
    """Each worker's logical CPUs, what the free tier counts. For hosts
    without a machine type (metal)."""
    out = {}
    for n in cfg.nodes_with("worker"):
        res = _ssh(cfg, n, "nproc", capture=True, quiet=True)
        out[n.name] = int(res.stdout.strip())
    return out

def ctl_on(cfg, node, command: str, check: bool = True, quiet: bool = False,
           input: str | None = None):
    """`boxd-ctl <command>` on one control node: over ssh as root, or on
    Kubernetes in that node's control pod. `command` is shell-quoted (the
    pod's exec splits it with shlex); `input` goes to its stdin, never logged."""
    if clouds.kubernetes(cfg):
        return _kube().ctl(cfg, node, command, check=check, quiet=quiet, input=input)
    kw = {"input": input} if input is not None else {}
    return _ssh(cfg, node, f"sudo boxd-ctl {command}", capture=True, check=check, quiet=quiet, **kw)

def ctl(cfg, command: str, check: bool = True, quiet: bool = False, input: str | None = None):
    """Run boxd-ctl on ONE control node (parallel connections trip its rate limit)."""
    return ctl_on(cfg, cfg.nodes_with("control")[0], command, check=check, quiet=quiet,
                  input=input)

def acme_eab_spent(cfg) -> bool:
    """True when the running boxd-control can't register its ACME account
    because Google Trust Services already consumed the single-use EAB key
    (an earlier process used it and died before saving the account). Only
    the current process's log counts, so a recovered cluster reads clean."""
    if clouds.kubernetes(cfg):
        return _kube().acme_eab_spent(cfg)
    host = cfg.nodes_with("control")[0]
    probe = ("pid=$(systemctl show -p MainPID --value boxd-control); "
             "sudo journalctl -u boxd-control _PID=$pid --no-pager -o cat | "
             "grep -c 'Unknown external account binding' || true")
    res = _ssh(cfg, host, probe, capture=True, check=False, quiet=True)
    return res.returncode == 0 and res.stdout.strip() not in ("", "0")

def _registered_nodes(cfg) -> int | None:
    """Parse `Nodes:  N` (column-aligned, variable whitespace) from status."""
    import re
    res = ctl(cfg, "status", check=False, quiet=True)
    if res.returncode != 0:
        return None
    m = re.search(r"Nodes:\s*(\d+)", res.stdout)
    return int(m.group(1)) if m else None

def wait_for_leader(cfg, timeout: int = 300) -> None:
    log("waiting for a raft leader…")
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = ctl(cfg, "status", check=False, quiet=True)
        if res.returncode == 0 and "leader" in res.stdout.lower():
            log("leader elected")
            return
        time.sleep(10)
    raise InstallError(f"no raft leader within {timeout}s — check boxd-control logs")

def _is_leader(status_out: str) -> bool:
    """`boxd-ctl status` run ON the leader says so on its first line:
    `Leader: node 1 (10.70.1.10:7000) — this node`."""
    import re
    return re.search(r"^Leader:.*\bthis node\b", status_out, re.M) is not None

def leader_node(cfg, timeout: int = 120):
    """The control node that is the raft leader, or InstallError. Retried: a
    restarted boxd-control may still be electing right after wait_for_leader
    saw a leader through another node."""
    deadline = time.time() + timeout
    while True:
        for node in cfg.nodes_with("control"):
            res = ctl_on(cfg, node, "status", check=False, quiet=True)
            if res.returncode == 0 and _is_leader(res.stdout):
                return node
        if time.time() >= deadline:
            raise InstallError(f"no control node says it is the raft leader within {timeout}s "
                               "— run `boxd-ctl status` on each control node")
        time.sleep(10)

def sync_config(cfg) -> list[str]:
    """Add the learners new to the config (a worker, proxy or dns node added
    to cluster.toml) to the running raft cluster. A running cluster never
    reads them from the config by itself: the leader proposes them only on
    `boxd-ctl sync-config`, and until then the new node logs "still not
    registered: not the leader". On the leader only. Idempotent: with nothing
    new it prints "No new learners to add" (members are re-proposed, a raft
    no-op). Returns the "Added …" lines."""
    leader = leader_node(cfg)
    res = ctl_on(cfg, leader, "sync-config", check=False, quiet=True)
    if res.returncode != 0:
        raise InstallError(f"`boxd-ctl sync-config` on {leader.name} failed:\n"
                           f"{((res.stderr or '') + (res.stdout or '')).strip()[-800:]}")
    added = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.startswith("Added ")]
    for line in added:
        log(f"raft membership: {line}")
    return added

_LEARNER_UNITS = (("worker", "boxd-worker"), ("proxy", "boxd-proxy"), ("dns", "boxd-dns"))

def learners_gave_up(cfg) -> list[str]:
    """Learner units whose CURRENT run gave up registering ("registration
    failed after N attempts") — e.g. a proxy from an older release that started
    before the leader existed. A newer proxy retries until it registers and
    logs "registration failed, retrying" meanwhile, which isn't giving up.
    Workers show up in `boxd-ctl status`; proxies don't, so the node count
    alone can't see them. Scoped to the systemd invocation, so a healthy
    re-apply restarts nothing (on Kubernetes: the current container's log)."""
    if clouds.kubernetes(cfg):
        return _kube().learners_gave_up(cfg)
    failed = []
    for role, unit in _LEARNER_UNITS:
        for node in cfg.nodes_with(role):
            res = _ssh(cfg, node,
                       f"sudo journalctl -q --no-pager _SYSTEMD_INVOCATION_ID="
                       f"$(systemctl show -p InvocationID --value {unit}) "
                       f"| grep -c 'registration failed after'",
                       check=False, capture=True, quiet=True)
            if res.stdout.strip() not in ("", "0"):
                failed.append(f"{node.name}/{unit}")
    return failed

def restart_learners(cfg) -> None:
    """Fresh-cluster fixup: learners exhaust registration retries before the
    first leader exists; restart them once. A green converge does NOT imply a
    registered cluster (METAL_DEPLOYMENT.md)."""
    if clouds.kubernetes(cfg):
        _kube().restart_learners(cfg)
        return
    for role, unit in _LEARNER_UNITS:
        for node in cfg.nodes_with(role):
            _ssh(cfg, node, f"sudo systemctl restart {unit}", check=False)

def verify_registration(cfg, expected: int, timeout: int = 180) -> None:
    deadline = time.time() + timeout
    seen = None
    while time.time() < deadline:
        seen = _registered_nodes(cfg)
        if seen is not None and seen >= expected:
            log(f"cluster registered: {seen} node(s)")
            return
        time.sleep(10)
    raise InstallError(f"cluster did not reach {expected} registered node(s) "
                       f"(last saw: {seen}) — run `boxd-ctl status` on a "
                       "control node to inspect")

def seed_templates(cfg, image: str, tag: str, assets_sha: str, timeout: int = 1200,
                   storage: str = "file") -> None:
    """Mirror of scripts/deploy/templates.sh: create per-size templates, wait
    ready, set-default, THEN flip the default image (a not-ready default just
    routes creates down the ~12s cold-boot path). Names embed tag + assets
    sha so a new assets zip re-seeds even on an unchanged image tag.

    `storage`: "bucket" once the storage engine is the default for new
    machines, which match only bucket templates (built once, shared through
    the bucket); "file" otherwise. Templates are matched by their whole name:
    a substring match once took another template for one of these."""
    sizes = cfg.machine_sizes()
    image = cfg.table("templates").get("image", image)
    tag = cfg.table("templates").get("tag", tag)
    ref = f"{image}:{tag}"
    stamp = assets_sha[:8]

    # ONE AT A TIME. Each template build boots a VM from the image and
    # snapshots it; firing all the sizes at once makes them contend for the
    # same worker (and overlaps a snapshot with another build's prewarm).
    # Create, wait for ready, then the next — `timeout` is the budget for the
    # whole set, not per template.
    created = []
    deadline = time.time() + timeout
    listing = ctl(cfg, "template list", check=False).stdout or ""
    kind = "bucket-" if storage == "bucket" else ""
    flag = " --storage bucket" if storage == "bucket" else ""
    for s in sizes:
        name = f"computer-{kind}{s['vcpu']}x{s['memory_gb']}-{tag}-{stamp}"
        if name in _template_names(listing):
            log(f"template {name} already exists")
        else:
            ctl(cfg, f"template create --image '{ref}' --vcpu {s['vcpu']} "
                     f"--memory {s['memory_gb']}G{flag} --name '{name}'")
        created.append(name)
        log(f"waiting for template {name} to become ready…")
        while time.time() < deadline:
            listing = ctl(cfg, "template list", check=False).stdout or ""
            if _ready(listing, name):
                break
            if _failed(listing, name):
                raise InstallError(
                    f"template {name} failed to build — default image NOT changed; creates keep "
                    "using the previous image. The builder worker's log says why (search for the "
                    f"template's name); `boxd-ctl template delete` it, then apply again")
            time.sleep(15)
        else:
            raise InstallError(f"template {name} did not reach ready — default image "
                               "NOT changed; creates keep using the previous image")
        log(f"template {name} ready")

    for name in created:
        tid = _template_id(listing, name)
        ctl(cfg, f"template set-default {tid}")
    ctl(cfg, f"image set --name '{image}' --tag '{tag}'")
    log(f"default image -> {ref}")
    set_machine_sizes(cfg, sizes)

    listing = ctl(cfg, "template list", check=False).stdout or ""
    for line in listing.splitlines():
        cols = line.split()
        if len(cols) < 2 or not cols[1].startswith("computer-") or cols[1] in created:
            continue
        tid = cols[0]
        log(f"deleting superseded template {tid} ({line.strip()[:80]})")
        ctl(cfg, f"template delete {tid}", check=False)

def set_machine_sizes(cfg, sizes: list[dict]) -> None:
    """Make the API accept exactly the seeded sizes: a replicated cluster
    setting every role reads live, so no service restart. Runs only once
    every template is ready, so no size is accepted before it boots warm.
    A boxd-ctl from before the command existed (older assets) keeps its
    built-in list — warn instead of failing the apply over it."""
    spec = " ".join(f"{s['vcpu']}x{s['memory_gb']}" for s in sizes)
    res = ctl(cfg, f"machine-sizes set {spec}", check=False)
    if res.returncode == 0:
        log(f"machine sizes -> {spec}")
        return
    err = (res.stderr or "") + (res.stdout or "")
    if "unrecognized subcommand" in err:
        log("warning: this assets bundle's boxd-ctl has no `machine-sizes`; the API "
            "keeps its built-in sizes and refuses any other [templates].sizes entry")
        return
    raise InstallError(f"boxd-ctl machine-sizes set {spec} failed:\n{err.strip()[-2000:]}")

def init_cluster_id(cfg) -> None:
    """`boxd-ctl license init-cluster-id`: make the cluster ID when it has
    none, a no-op after. It runs after the converge, when every node
    (control, proxy, console) runs the new bundle: an older node skips the
    command for good, so it never gets the ID and rejects bound keys, and an
    older control node could later make a second ID. A bound license key
    needs the ID."""
    res = ctl(cfg, "license init-cluster-id", check=False)
    if res.returncode == 0:
        log((res.stdout or "").strip() or "cluster id set")
        return
    err = ((res.stderr or "") + (res.stdout or "")).strip()
    if "unrecognized subcommand" in err:
        log("warning: this assets bundle's boxd-ctl has no `license init-cluster-id`; the "
            "cluster has no ID yet")
        return
    raise InstallError(f"boxd-ctl license init-cluster-id failed: {err[-2000:]}")

# The cluster_config switch for machine specs: "on", "off", or unset (off).
# boxd refuses a create, restore or fork with a spec while it isn't "on",
# because a node on an older release skips their Raft entries for good.
MACHINE_SPECS = "machine_specs"

def pause_machine_specs(cfg) -> None:
    """Before the converge of a new bundle: when machine specs are on, unset
    the switch (off), so no new node proposes spec entries an old one would
    miss. Unset, not "off": "off" is the operator's own choice, which
    turn_on_machine_specs keeps. Anything but a clean "on" (a fresh cluster,
    a release without the switch) leaves it alone."""
    res = ctl(cfg, f"cluster-config get {MACHINE_SPECS}", check=False, quiet=True)
    if res.returncode != 0 or res.stdout.strip() != "on":
        return
    res = ctl(cfg, f"cluster-config unset {MACHINE_SPECS}", check=False)
    if res.returncode != 0:
        err = ((res.stderr or "") + (res.stdout or "")).strip()
        raise InstallError(f"boxd-ctl cluster-config unset {MACHINE_SPECS} failed: "
                           f"{err[-2000:]}")
    log("machine specs off for the upgrade (creates, restores and forks with a spec are "
        "refused until every node runs the new bundle)")

def turn_on_machine_specs(cfg) -> None:
    """After the converge, when every node runs the new bundle: turn machine
    specs on when the switch is unset (a cluster that never had them, or one
    pause_machine_specs paused). "off" stays off: an operator set it. A
    bundle from before machine specs warns instead of failing the apply."""
    res = ctl(cfg, f"cluster-config get {MACHINE_SPECS}", check=False, quiet=True)
    err = ((res.stderr or "") + (res.stdout or "")).strip()
    value = res.stdout.strip() if res.returncode == 0 else None
    if value == "on":
        return
    if value == "off":
        log("machine specs: off, as an operator set them; left off (`boxd-ctl "
            f"cluster-config set {MACHINE_SPECS} on` on a control node turns them on)")
        return
    if value is None and f"{MACHINE_SPECS} is unset" not in err:
        if "unrecognized subcommand" in err:
            log("warning: this assets bundle's boxd-ctl has no `cluster-config`; machine "
                "specs stay off")
            return
        raise InstallError(f"boxd-ctl cluster-config get {MACHINE_SPECS} failed: {err[-2000:]}")
    res = ctl(cfg, f"cluster-config set {MACHINE_SPECS} on", check=False)
    if res.returncode == 0:
        log("machine specs -> on")
        return
    err = ((res.stderr or "") + (res.stdout or "")).strip()
    if "can't be written" in err:
        log("warning: this assets bundle has no machine specs; creates, restores and forks "
            "with a spec stay refused")
        return
    raise InstallError(f"boxd-ctl cluster-config set {MACHINE_SPECS} on failed: {err[-2000:]}")

def install_license(cfg, key_path: str) -> None:
    """`boxd-ctl license install` with the key file on stdin (never on the
    command line), on one control node, on every apply. Control verifies the
    key and refuses one that does not verify; that stops the apply with the
    reason. Installing the same key again changes nothing."""
    with open(key_path) as fh:
        key = fh.read()
    res = ctl(cfg, "license install", check=False, input=key)
    if res.returncode == 0:
        state = next((line.split(":", 1)[1].strip() for line in res.stdout.splitlines()
                      if line.startswith("license:")), "installed")
        log(f"license key installed: {state}")
        return
    err = ((res.stderr or "") + (res.stdout or "")).strip()
    if "unrecognized subcommand" in err:
        log("warning: this assets bundle's boxd-ctl has no `license install`; the license "
            "key is not installed")
        return
    raise InstallError(f"boxd-ctl license install failed: {err[-2000:]}")

def set_telemetry(cfg, enabled: bool, explicit: bool) -> None:
    """`boxd-ctl telemetry on|off`, after the license key. `explicit`: the
    value is in cluster.toml; otherwise it is the customer default.

    Control refuses `off` without a key that includes telemetry opt-out, and
    an assets bundle from before telemetry has no `telemetry` command (it sends
    nothing either). A failure for an explicit cluster.toml value stops the
    apply; for the customer default it is a warning and the apply goes on,
    like `install_license` with an old bundle. (`enabled = false` without
    [license] key_file is already an error at validate, before terraform.)"""
    verb = "on" if enabled else "off"
    # A few tries for `off`: the key was installed a moment ago, and the
    # control node's replica can apply it a little after the commit.
    for attempt in range(5):
        res = ctl(cfg, f"telemetry {verb}", check=False)
        if res.returncode == 0:
            log(f"telemetry {verb}")
            return
        err = ((res.stderr or "") + (res.stdout or "")).strip()
        if enabled or "telemetry opt-out" not in err or attempt == 4:
            break
        time.sleep(2)
    old = "unrecognized subcommand" in err
    if old and enabled:
        return  # nothing to turn on: a bundle without the command sends nothing
    why = ("this assets bundle's boxd-ctl has no `telemetry` command" if old else
           f"boxd-ctl telemetry {verb} failed: {err[-2000:]}")
    if explicit:
        raise InstallError(f"[telemetry] enabled = {str(enabled).lower()}: {why}")
    log(f"warning: telemetry {verb} (this installer's default) not applied: {why}")

def configure_orgs(cfg, sizes: list[dict]) -> None:
    """Every org: quota = the largest listed vCPU count and the largest listed
    memory (so any size on the list fits), and comped. Orgs already comped
    are left alone, so an `enterprise` comp keeps its source. A fresh cluster
    has only boxd's own system org (`default-system`, owned by `system`, not
    a person); the first sign-in creates the cluster's org, which the next
    apply picks up."""
    cpu = max(s["vcpu"] for s in sizes)
    mem = max(s["memory_gb"] for s in sizes)
    listing = ctl(cfg, "org list").stdout
    orgs = _first_column(listing, skip=("ORG", "no"))
    system = {line.split()[0] for line in (listing or "").splitlines()
              if line.split() and "default-system" in line.split()}
    comped = set(_first_column(ctl(cfg, "org comp list").stdout, skip=("No",)))
    for org in orgs:  # one at a time: the control socket rate-limits
        ctl(cfg, f"org set-quota {org} --cpu {cpu} --mem {mem}G")
        if org not in comped:
            ctl(cfg, f"org comp add {org}")
            log(f"comped org {org}" + (" (boxd's system org)" if org in system else ""))
    people = len([o for o in orgs if o not in system])
    log(f"quota of {len(orgs)} org(s) -> {cpu} vCPU / {mem}G"
        + (f" ({people} from sign-ins, plus boxd's system org)" if system else ""))

def _first_column(listing: str, skip: tuple[str, ...]) -> list[str]:
    """First token of each line, minus headers / "none" lines."""
    return [line.split()[0] for line in (listing or "").splitlines()
            if line.split() and line.split()[0] not in skip]

def _template_rows(listing: str) -> list[list[str]]:
    """`template list` rows as columns: ID, NAME, then the rest."""
    return [cols for cols in (line.split() for line in listing.splitlines()) if len(cols) >= 2]

def _template_names(listing: str) -> set[str]:
    return {cols[1] for cols in _template_rows(listing)}

def _ready(listing: str, name: str) -> bool:
    return any(cols[1] == name and "ready" in cols[2:] for cols in _template_rows(listing))

def _failed(listing: str, name: str) -> bool:
    """The template's build failed: it never becomes ready, so waiting longer is pointless."""
    return any(cols[1] == name and "failed" in cols[2:] for cols in _template_rows(listing))

def _template_id(listing: str, name: str) -> str:
    for cols in _template_rows(listing):
        if cols[1] == name:
            return cols[0]
    raise InstallError(f"could not resolve template id for {name}")
