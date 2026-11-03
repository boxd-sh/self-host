"""boxd's storage engine on an installer cluster ([storage_engine]): the
one-way guard, the check that every worker opened its bucket, and the cluster
switches. The render (render.storage_store) and the worker role ship the
config; terraform makes the bucket.

Once the storage catalog is on, machines can live in the bucket, so the engine
never goes off by config and its bucket never moves: the guard. It is engaged
by its record in installer state, or by the live catalog reading on (a
cluster whose record was lost, or that was turned on by hand), and it refuses:
removing [storage_engine], a bundle without the engine, and any change to
which bucket and prefix the workers use."""
import datetime
import time

from . import ansible, assets, config, tf
from .util import InstallError, log

# boxd-ctl's refusals while the cluster catches up: a control node that
# doesn't know the membership yet, members still on an older build, the
# default set before the catalog's switch reached the local replica. Retried.
LAG_MESSAGES = ("does not know the Raft membership yet", "must run a build that applies",
                "turn it on first")
# boxd's control refuses `set-default-vm-storage bucket` without the license
# feature (boxd-control BUCKET_UNLICENSED).
UNLICENSED = "needs a license with bucket-storage"
SWITCH_TIMEOUT = 180
BUCKET_OPEN_TIMEOUT = 120
# What the worker logs once it tried its bucket (boxd's boxd-worker): opened,
# or not (bucket mode off on this worker), or a file-only build that refuses
# [storage_engine] at start.
OPEN_LINE = "storage engine: the bucket is open"
OFF_LINE = "storage engine: bucket mode is off on this worker"
FILE_ONLY_LINE = "built without the `bucket` feature"

class Context:
    """What apply learned before terraform: whether the guard is engaged, and
    what the live catalog said (None: not asked, or no answer)."""
    def __init__(self, guard: dict | None, catalog: str | None):
        self.guard = guard
        self.catalog = catalog

    @property
    def engaged(self) -> bool:
        return self.guard is not None or self.catalog == "on"

def parse_catalog(out: str) -> str | None:
    """`storage catalog: on` / `off` (boxd-ctl's first line)."""
    for line in (out or "").splitlines():
        if line.startswith("storage catalog:"):
            return line.split(":", 1)[1].strip().split()[0]
    return None

def catalog_state(cfg) -> str | None:
    """The live storage catalog switch: "on", "off" (also for a boxd-ctl
    without the command), or None when control gave no answer."""
    res = ansible.ctl(cfg, "cluster storage-catalog", check=False, quiet=True)
    err = (res.stderr or "") + (res.stdout or "")
    if res.returncode != 0:
        return "off" if "unrecognized subcommand" in err else None
    return parse_catalog(res.stdout)

def _live_catalog_before_terraform(cfg, state) -> str | None:
    """The catalog of a cluster that exists (applied before), through the
    terraform outputs cached in state (AWS addresses nodes by instance id).
    None when there is nothing to ask or no answer: the state record alone
    decides then."""
    if not state.data.get("applied_assets"):
        return None
    try:
        ansible.set_tf_outputs(tf.cached_outputs(state))
    except InstallError:
        return None
    try:
        return catalog_state(cfg)
    except InstallError:
        return None

def check_guard(cfg, state, assets_root: str) -> Context:
    """Before terraform: refuse what would take the engine away from machines
    that may live in the bucket. The bucket's identity is checked again after
    the render (check_identity), when terraform's bucket is known."""
    ctx = Context(state.storage_guard(), _live_catalog_before_terraform(cfg, state))
    if not ctx.engaged:
        return ctx
    why = ("its storage catalog is on" if ctx.catalog == "on" else
           "it turned the storage catalog on (installer state)")
    if not config.storage_engine_on(cfg.doc):
        raise InstallError(
            f"this cluster runs the storage engine ({why}), so machines may live in its "
            "bucket: [storage_engine] can't be removed. Put it back in cluster.toml. Taking a "
            "cluster off the engine is a support procedure, not a config edit")
    if "bucket" not in assets.features(assets_root):
        raise InstallError(f"this cluster runs the storage engine ({why}): it can't move to "
                           f"{cfg.assets_version[:12]}, a bundle without the engine")
    if ctx.guard is not None:
        auto = config.auto_storage_bucket(cfg.doc)
        if bool(ctx.guard.get("auto")) != auto:
            cloud = cfg.cloud_name
            raise InstallError(
                f"[{cloud}] auto_storage_bucket is now {str(auto).lower()}, but this cluster's "
                f"engine runs on {'the bucket the installer made' if ctx.guard.get('auto') else 'your own bucket'} "
                f"({ctx.guard.get('bucket')}): the bucket can't change once the catalog is on. "
                "Put auto_storage_bucket back")
        if not auto:
            own = state.load_providers().get("storage_engine") or {}
            for k in ("bucket", "endpoint"):
                if own.get(k, "") != ctx.guard.get(k, ""):
                    raise _moved(k, ctx.guard.get(k, ""), own.get(k, ""))
    if ctx.guard is not None and ctx.catalog == "off":
        raise InstallError(
            "the storage catalog is off on a cluster that runs the storage engine: someone "
            "turned it off (before rolling a node back?), and engine machines can't open their "
            "disks until it is on again. Turn it back on with "
            "`boxd-ctl cluster set-storage-catalog on` once every node runs a release with bucket "
            "storage, "
            "then re-apply. apply never turns it off, nor back on by itself")
    return ctx

def _moved(key: str, was: str, now: str) -> InstallError:
    return InstallError(
        f"the storage engine's {key} would change from {was or '(none)'} to {now or '(none)'}, "
        "but machines may live in the bucket: once the catalog is on, the bucket and its prefix "
        "never change. Put it back (moving a cluster to another bucket is a support procedure)")

def adopt_prefix(cfg, state) -> None:
    """Before the render: a cluster whose workers already run the engine
    without a prefix in installer state (turned on by hand, or a lost state)
    keeps the prefix its workers use, so its machines' disks stay reachable."""
    if not config.storage_engine_on(cfg.doc) or state.data.get("storage_prefix"):
        return
    if not state.data.get("applied_assets"):
        return
    for name, ident in ansible.deployed_bucket_identities(cfg).items():
        if ident.get("prefix"):
            state.data["storage_prefix"] = ident["prefix"]
            state.save()
            log(f"storage engine: kept the prefix {ident['prefix']} that {name} already uses")
            return

def check_identity(cfg, ctx: Context, renderer) -> None:
    """After the render, before the converge: the bucket and prefix the
    workers would get must be the ones the engine runs on, by the guard's
    record and by each worker's deployed bucket file."""
    if not config.storage_engine_on(cfg.doc):
        return
    new = renderer.storage_identity()
    if ctx.guard is not None:
        for k in ("kind", "bucket", "endpoint", "prefix"):
            if ctx.guard.get(k, "") != new.get(k, ""):
                raise _moved(k, ctx.guard.get(k, ""), new.get(k, ""))
    if not ctx.engaged:
        return
    for name, old in ansible.deployed_bucket_identities(cfg).items():
        for k in ("kind", "bucket", "endpoint", "prefix"):
            if old.get(k, "") != new.get(k, ""):
                raise InstallError(f"{name} runs the storage engine on {old.get('bucket')} "
                                   f"(prefix {old.get('prefix')}), and this apply would move "
                                   f"it to {new.get('bucket')} (prefix {new.get('prefix')}): "
                                   "machines on the engine would lose their disks. Point "
                                   "[storage_engine] at that bucket (your own bucket: "
                                   "auto_storage_bucket = false and `providers edit`)")

def wait_bucket_open(cfg, timeout: int = BUCKET_OPEN_TIMEOUT) -> None:
    """After the converge, before any switch: every worker's current process
    must have opened its bucket. boxd only logs a failed open (the worker
    still reports the backend), so a wrong key or bucket would otherwise
    surface at the first machine. The bucket file asks for the probe (no
    conditional_put), so the open writes."""
    if not config.storage_engine_on(cfg.doc):
        return
    for node in cfg.nodes_with("worker"):
        deadline = time.time() + timeout
        while True:
            line = ansible.worker_bucket_line(cfg, node)
            if OPEN_LINE in line:
                log(f"{node.name}: the storage engine's bucket is open")
                break
            if OFF_LINE in line:
                raise InstallError(f"{node.name} could not open the storage engine's bucket:\n"
                                   f"  {line.strip()[-600:]}\nCheck the bucket and its "
                                   "credentials (the providers config's [storage_engine] for "
                                   "your own bucket)")
            if FILE_ONLY_LINE in line:
                raise InstallError(f"{node.name} runs a boxd-worker built without the storage "
                                   "engine; the release should include bucket storage")
            if time.time() >= deadline:
                raise InstallError(f"{node.name} did not report opening the storage engine's "
                                   f"bucket within {timeout}s (`journalctl -u boxd-worker` there)")
            time.sleep(5)

def _ctl(cfg, command: str, timeout: int = SWITCH_TIMEOUT) -> str:
    """boxd-ctl, retried while the refusal says the cluster is catching up."""
    deadline = time.time() + timeout
    while True:
        res = ansible.ctl(cfg, command, check=False)
        if res.returncode == 0:
            return res.stdout or ""
        err = ((res.stderr or "") + (res.stdout or "")).strip()
        if not any(m in err for m in LAG_MESSAGES) or time.time() >= deadline:
            raise _CtlError(command, err)
        time.sleep(10)

class _CtlError(InstallError):
    def __init__(self, command: str, err: str):
        super().__init__(f"boxd-ctl {command} failed: {err[-2000:]}")
        self.err = err

def parse_first(out: str, label: str) -> str | None:
    """`<label>: <value> ...` on boxd-ctl's first matching line."""
    for line in (out or "").splitlines():
        if line.startswith(label + ":"):
            rest = line.split(":", 1)[1].strip()
            return rest.split()[0] if rest else None
    return None

def parse_default(out: str) -> tuple[str | None, bool]:
    """`new machines: bucket storage`, or with `(inactive: …)` when the
    license lacks bucket-storage: (stored default, inactive)."""
    for line in (out or "").splitlines():
        if line.startswith("new machines:"):
            words = line.split(":", 1)[1].split()
            return (words[0] if words else None), "(inactive:" in line
    return None, False

def set_switches(cfg, state, ctx: Context, renderer, push) -> None:
    """After the license is installed: the storage catalog on (never off),
    the guard written and pushed at once, then hibernation blobs and the
    default for new machines, each set only when it differs."""
    if not config.storage_engine_on(cfg.doc):
        return
    catalog = parse_catalog(_ctl(cfg, "cluster storage-catalog"))
    if catalog != "on":
        if ctx.guard is not None or state.storage_guard() is not None:
            raise InstallError("the storage catalog is off on a cluster that runs the storage "
                               "engine; turn it back on with `boxd-ctl cluster "
                               "set-storage-catalog on`, then re-apply")
        catalog = parse_catalog(_ctl(cfg, "cluster set-storage-catalog on"))
        if catalog != "on":
            raise InstallError("boxd-ctl cluster set-storage-catalog on did not turn the storage "
                               "catalog on")
        log("storage catalog on")
    old = state.storage_guard() or {}
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    guard = {"catalog_on_at": old.get("catalog_on_at", now), "bundle": cfg.assets_version,
             **renderer.storage_identity()}
    if guard != old:
        state.set_storage_guard(guard)
        push()
        log("storage engine: the guard is set (installer state pushed)")

    want = cfg.doc["storage_engine"].get("hibernation_blobs", "uploads")
    have = parse_first(_ctl(cfg, "cluster hibernation-blobs"), "hibernation blobs")
    if have != want:
        _ctl(cfg, f"cluster set-hibernation-blobs {want}")
        log(f"hibernation blobs: {have} -> {want}")

    want = config.storage_default(cfg.doc)
    stored, inactive = parse_default(_ctl(cfg, "cluster default-vm-storage"))
    if stored == want:
        if inactive:
            log("warning: new machines are set to go in the bucket, but the license key lacks "
                "bucket-storage (expired, or a key without it), so they are born on file. "
                "Machines already in the bucket keep working. Install a key with "
                "bucket-storage ([license] key_file)")
        return
    try:
        _ctl(cfg, f"cluster set-default-vm-storage {want}")
    except _CtlError as e:
        if UNLICENSED in e.err:
            raise InstallError(
                '[storage_engine] default_vm_storage = "bucket" needs a license key that '
                "includes bucket-storage, and the key in [license] key_file doesn't. Install "
                'one, or set default_vm_storage = "file"') from None
        raise
    log(f"new machines: {stored} -> {want} storage")

def template_storage(cfg) -> str:
    """The storage seed_templates builds templates for: the default for new
    machines once the engine is on (a bucket machine matches only bucket
    templates), file otherwise."""
    return config.storage_default(cfg.doc) if config.storage_engine_on(cfg.doc) else "file"

# Resources a failed destroy may leave behind on purpose: buckets with
# objects in them (force_destroy = false).
KEPT_BUCKET_TYPES = ("aws_s3_bucket", "google_storage_bucket")

def kept_buckets(state_list: list[str]) -> list[str] | None:
    """The bucket addresses left in terraform state after a failed destroy,
    or None when anything else is left (then the failure is real)."""
    left = [a for a in state_list if a and not a.startswith("data.")]
    if not left or any(a.split(".")[0] not in KEPT_BUCKET_TYPES for a in left):
        return None
    return left
