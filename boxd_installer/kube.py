"""Apply boxd on Kubernetes ([gke]) and drive it: kubectl with a private
kubeconfig, server-side apply and pruning, the rollout of control, console
and the edges, workers updated in place, Raft membership, the drain that
removes a worker, and the boxd-ctl transport (kubectl exec into a control pod).

One deploy (deploy()), in this order:

  1. base objects (namespace, RBAC, Services, budgets, ConfigMaps, node-prep),
     then the boxd-config Secret;
  2. control: applied, rolled out (it rolls only when its image or its config
     less the learners changed), a leader; then, once every control pod's
     mounted config has the new content, `boxd-ctl sync-config` adds any new
     learner before its pod starts;
  3. the console, then each edge (proxy-<i> and dns-<i>), one edge at a time;
  4. workers: removals still pending from an earlier apply, then the workers
     cluster.toml no longer has (drain, remove, delete their node); the
     StatefulSet at its new size; each running worker updated in place (an
     image is patched into the running pod, so its machines keep running; a
     config change restarts the worker's process; any other pod change drains
     the worker and recreates its pod); new workers waited for (node-prep
     reboots a new node once into boxd's kernel);
  5. the PodMonitorings (once their CRD is there), then what is labelled as
     the installer's and no longer rendered, deleted;
  6. every worker registered.

A worker leaves the cluster only here: `boxd-ctl node exclude`, `node drain`
(boxd moves every machine off it), `node mark-dead`, its pod removed by
scaling the StatefulSet down, `remove-peer`, its Raft-state disk deleted, its
node deleted from the pool, its node id retired (never handed out again).
Each removal is recorded in installer state before it starts, so an apply
that stops half-way finishes it next time."""
import atexit
import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import tempfile
import time
import tomllib

from . import ansible, kube_render, tf
from .clouds import gke
from .kube_render import CONFIG_HASH, SPEC_HASH, SRC
from .util import InstallError, log, run

FIELD_MANAGER = "boxd-install"
# What prune may delete (labelled as ours and no longer rendered): never
# Pods, PVCs, Secrets or the Namespace.
PRUNE = (("services", "Service", True),
         ("statefulsets.apps", "StatefulSet", True),
         ("daemonsets.apps", "DaemonSet", True),
         ("configmaps", "ConfigMap", True),
         ("poddisruptionbudgets.policy", "PodDisruptionBudget", True),
         ("serviceaccounts", "ServiceAccount", True),
         ("podmonitorings.monitoring.googleapis.com", "PodMonitoring", True),
         ("clusterroles.rbac.authorization.k8s.io", "ClusterRole", False),
         ("clusterrolebindings.rbac.authorization.k8s.io", "ClusterRoleBinding", False),
         ("priorityclasses.scheduling.k8s.io", "PriorityClass", False),
         ("storageclasses.storage.k8s.io", "StorageClass", False))
PROTECTED = {("StatefulSet", "boxd-worker"), ("StatefulSet", "boxd-control")}
ROLLOUT_TIMEOUT = 900
WORKER_READY_TIMEOUT = 1800
FILE_TIMEOUT = 300
REGISTER_TIMEOUT = 300
GAVE_UP = "registration failed after"
MONITORING_CRD = "podmonitorings.monitoring.googleapis.com"

_kubeconfig: str | None = None
_kube_dir: str | None = None

# ------------------------------------------------------------------ kubectl

def namespace(cfg) -> str:
    return gke.settings(cfg.doc)["namespace"]

def selector(cfg) -> str:
    return f"app.kubernetes.io/managed-by={kube_render.MANAGED_BY},boxd.sh/install={namespace(cfg)}"

def connect(cfg, tf_out: dict) -> None:
    """Credentials for the cluster terraform made or read, in a kubeconfig of
    our own (a temp dir, removed at exit): the operator's ~/.kube is never
    touched. Through gcloud, as the account apply runs as."""
    global _kubeconfig, _kube_dir
    g = tf_out.get("gke")
    if not g:
        raise InstallError("terraform outputs have no 'gke' (the cluster): run apply")
    if _kube_dir is None:
        _kube_dir = tempfile.mkdtemp(prefix="boxd-kube-")
        atexit.register(shutil.rmtree, _kube_dir, True)
    path = os.path.join(_kube_dir, "config")
    run(["gcloud", "container", "clusters", "get-credentials", g["name"],
         f"--location={g['location']}", f"--project={cfg.cloud['project']}"],
        capture=True, quiet=True, env={**os.environ, "KUBECONFIG": path})
    _kubeconfig = path

def _ensure(cfg) -> None:
    """Connect from the outputs ansible.set_tf_outputs was given (a ctl call
    before terraform reads the previous apply's)."""
    if _kubeconfig is None:
        connect(cfg, ansible._tf_out)

def kubectl(cfg, *args: str, check: bool = True, quiet: bool = True,
            input: str | None = None):
    _ensure(cfg)
    return run(["kubectl", f"--kubeconfig={_kubeconfig}", f"--namespace={namespace(cfg)}", *args],
               capture=True, check=check, quiet=quiet, input=input)

def get(cfg, resource: str, name: str | None = None, selector_: str | None = None) -> dict | None:
    """One object (None when it doesn't exist) or a list."""
    args = ["get", resource] + ([name] if name else []) + \
        (["-l", selector_] if selector_ else []) + ["-o", "json"]
    res = kubectl(cfg, *args, check=False)
    if res.returncode != 0:
        if "NotFound" in (res.stderr or "") or "not found" in (res.stderr or ""):
            return None
        raise InstallError(f"kubectl get {resource} {name or ''} failed: "
                           f"{(res.stderr or '').strip()[-600:]}")
    return json.loads(res.stdout or "{}")

def apply(cfg, objs: list[dict], out_dir: str, name: str) -> None:
    """Server-side apply, owning every field it sets (--force-conflicts: a
    `kubectl scale` or an edit by hand is taken back)."""
    if not objs:
        return
    path = os.path.join(out_dir, f"kube-{name}.json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"apiVersion": "v1", "kind": "List", "items": objs}, fh)
    res = kubectl(cfg, "apply", "--server-side", "--force-conflicts",
                  f"--field-manager={FIELD_MANAGER}", "-f", path, check=False)
    if res.returncode != 0:
        raise InstallError(f"kubectl apply ({name}) failed: {(res.stderr or '').strip()[-1500:]}")
    os.remove(path)
    changed = [ln for ln in (res.stdout or "").splitlines() if ln.strip()]
    log(f"applied {name}: {len(objs)} object(s)" + (f" ({len(changed)} reported)" if changed else ""))

def prune(cfg, rendered: list[dict]) -> list[str]:
    """Delete what carries the installer's labels and is no longer rendered
    (an edge's objects, PodMonitorings with observability off, ...). The
    StatefulSets of control and the workers are never pruned."""
    keep = {(o["kind"], o["metadata"]["name"]) for o in rendered}
    deleted = []
    for resource, kind, _ in PRUNE:
        res = kubectl(cfg, "get", resource, "-l", selector(cfg), "-o", "json", check=False)
        if res.returncode != 0:
            err = res.stderr or ""
            if "doesn't have a resource type" in err or "could not find the requested resource" in err:
                continue  # e.g. no PodMonitoring CRD (managed Prometheus off)
            raise InstallError(f"kubectl get {resource} failed: {err.strip()[-600:]}")
        for item in json.loads(res.stdout or "{}").get("items", []):
            name = item["metadata"]["name"]
            if (kind, name) in keep:
                continue
            if (kind, name) in PROTECTED:
                raise InstallError(f"refusing to prune {kind} {name}: it is never removed by apply")
            log(f"pruning {kind} {name} (no longer rendered)")
            kubectl(cfg, "delete", resource, name, "--ignore-not-found")
            deleted.append(f"{kind}/{name}")
    return deleted

def _wait(probe, timeout: int, every: int, what: str, hint: str = ""):
    """probe() until it returns something truthy; InstallError after timeout."""
    deadline = time.time() + timeout
    while True:
        got = probe()
        if got:
            return got
        if time.time() >= deadline:
            raise InstallError(f"{what} within {timeout}s" + (f": {hint}" if hint else ""))
        time.sleep(every)

def rollout(cfg, sts: str, timeout: int = ROLLOUT_TIMEOUT) -> None:
    log(f"waiting for {sts} to roll out…")
    res = kubectl(cfg, "rollout", "status", f"statefulset/{sts}", f"--timeout={timeout}s",
                  check=False)
    if res.returncode != 0:
        ns = namespace(cfg)
        raise InstallError(
            f"{sts} did not roll out within {timeout}s ({(res.stderr or res.stdout or '').strip()[-300:]}). "
            f"Look at its pods: kubectl -n {ns} get pods -l app={sts} -o wide; "
            f"kubectl -n {ns} describe pod {sts}-0; kubectl -n {ns} logs {sts}-0")

# ---------------------------------------------------------------- boxd-ctl

def ctl(cfg, node, command: str, check: bool = True, quiet: bool = False,
        input: str | None = None):
    """boxd-ctl in control pod <ordinal> (node: control-<ordinal>). `command`
    is shell-quoted as for ssh; the exec has no shell, so it is split here."""
    pod = f"boxd-control-{gke.ordinal(node.name)}"
    if not quiet:
        log(f"boxd-ctl {command}  ({pod})")
    exec_args = ["exec"] + (["-i"] if input is not None else []) + \
        [pod, "-c", "control", "--", "boxd-ctl", *shlex.split(command)]
    return kubectl(cfg, *exec_args, check=check, quiet=True, input=input)

def registered_ids(status_out: str) -> set[str]:
    """The node ids `boxd-ctl status` lists under Peers (workers register
    there as worker-<node id>; proxies don't)."""
    ids, peers = set(), False
    for line in (status_out or "").splitlines():
        if line.startswith("Peers:"):
            peers = True
        elif peers and line.strip():
            ids.add(line.split()[0])
    return ids

def _registered(cfg) -> set[str]:
    res = ansible.ctl(cfg, "status", check=False, quiet=True)
    return registered_ids(res.stdout) if res.returncode == 0 else set()

def _ctl_err(res) -> str:
    return ((res.stderr or "") + (res.stdout or "")).strip()

def drain(cfg, worker_id: str) -> None:
    """`boxd-ctl node drain <worker>`: boxd excludes the worker and moves every
    machine off it (live for running bucket machines, relocation for
    hibernated ones, a stopped machine's move for the rest), prints a table
    and a last line, and exits 1 while machines are left on it, 0 once it is
    empty. Anything but 0 stops here: the worker stays."""
    log(f"draining {worker_id} (every machine moves to another worker)…")
    res = ansible.ctl(cfg, f"node drain {worker_id}", check=False, quiet=True)
    err = _ctl_err(res)
    if res.returncode != 0:
        if "unrecognized subcommand" in err:
            raise InstallError(
                f"this release's boxd-ctl has no `node drain`, which taking {worker_id} out needs "
                "(it moves the worker's machines off first). Upgrade to a release that has it "
                "with the worker count unchanged, then lower it")
        raise InstallError(f"boxd-ctl node drain {worker_id} failed, so the worker stays: "
                           f"{err[-1500:]}")
    for line in (res.stdout or "").splitlines()[-20:]:
        if line.strip():
            log(f"  {line.strip()}")

# ------------------------------------------------------------------ probes

def deployed_bucket_identities(cfg) -> dict[str, dict]:
    """The bucket the deployed workers use: bucket.toml in the live Secret."""
    obj = get(cfg, "secret", kube_render.SECRET)
    data = (obj or {}).get("data") or {}
    if "bucket.toml" not in data:
        return {}
    try:
        doc = tomllib.loads(base64.b64decode(data["bucket.toml"]).decode())
    except (tomllib.TOMLDecodeError, ValueError):
        return {}
    return {kube_render.SECRET: {"kind": doc.get("kind", "s3"), "bucket": doc.get("bucket", ""),
                                 "endpoint": doc.get("endpoint", ""),
                                 "prefix": doc.get("prefix", "")}}

def logs(cfg, pod: str, container: str) -> str:
    """The current container's log ("" when there is none yet)."""
    res = kubectl(cfg, "logs", pod, "-c", container, check=False)
    return res.stdout or "" if res.returncode == 0 else ""

def worker_bucket_line(cfg, node) -> str:
    from .storage_engine import FILE_ONLY_LINE, OFF_LINE, OPEN_LINE
    pod = f"boxd-worker-{gke.ordinal(node.name)}"
    lines = [ln for ln in logs(cfg, pod, "worker").splitlines()
             if OPEN_LINE in ln or OFF_LINE in ln or FILE_ONLY_LINE in ln]
    return lines[-1] if lines else ""

def acme_eab_spent(cfg) -> bool:
    return any("Unknown external account binding" in logs(cfg, f"boxd-control-{i}", "control")
               for i in range(len(cfg.nodes_with("control"))))

def _learner_pods(cfg) -> list[tuple[str, str]]:
    out = []
    for n in cfg.nodes_with("proxy"):
        out.append((f"boxd-proxy-{gke.ordinal(n.name)}-0", "proxy"))
    for n in cfg.nodes_with("dns"):
        out.append((f"boxd-dns-{gke.ordinal(n.name)}-0", "dns"))
    for n in cfg.nodes_with("worker"):
        out.append((f"boxd-worker-{gke.ordinal(n.name)}", "worker"))
    return out

def learners_gave_up(cfg) -> list[str]:
    return [f"{pod}/{c}" for pod, c in _learner_pods(cfg) if GAVE_UP in logs(cfg, pod, c)]

def restart_learners(cfg) -> None:
    """Restart the learners whose current process gave up registering: a
    proxy's or dns's pod is deleted (its StatefulSet makes it again); a
    worker's process is ended, so its container restarts and its machines
    keep running (deleting its pod would stop them)."""
    for pod, container in _learner_pods(cfg):
        if GAVE_UP not in logs(cfg, pod, container):
            continue
        if container == "worker":
            restart_worker_process(cfg, pod)
        else:
            kubectl(cfg, "delete", "pod", pod, "--wait=false", check=False, quiet=False)

def _container_status(pod: dict, container: str) -> dict:
    for st in (pod.get("status") or {}).get("containerStatuses") or []:
        if st.get("name") == container:
            return st
    return {}

def restart_worker_process(cfg, pod: str) -> None:
    """End boxd-worker so the kubelet restarts its container: the pod stays,
    and so do the VMMs (they live in the pod's own cgroup, beside the
    container). Not `kill 1`: the worker shares the host's PID namespace."""
    before = get(cfg, "pod", pod)
    restarts = _container_status(before or {}, "worker").get("restartCount", 0)
    kubectl(cfg, "exec", pod, "-c", "worker", "--", "pkill", "-TERM", "-x", "boxd-worker",
            check=False, quiet=False)
    _wait(lambda: _restarted(cfg, pod, restarts), ROLLOUT_TIMEOUT, 5,
          f"{pod}'s worker did not come back after its restart",
          f"kubectl -n {namespace(cfg)} logs {pod} -c worker")

def _restarted(cfg, pod: str, restarts: int) -> bool:
    p = get(cfg, "pod", pod) or {}
    st = _container_status(p, "worker")
    return st.get("restartCount", 0) > restarts and st.get("ready") and \
        "running" in (st.get("state") or {})

def _file_sha(cfg, pod: str, container: str, path: str) -> str | None:
    res = kubectl(cfg, "exec", pod, "-c", container, "--", "sha256sum", path, check=False)
    return res.stdout.split()[0] if res.returncode == 0 and res.stdout.split() else None

def wait_for_file(cfg, pod: str, container: str, path: str, content: str,
                  timeout: int = FILE_TIMEOUT) -> None:
    """Until the pod's mounted Secret holds the new content (the kubelet
    syncs Secret volumes on its own schedule, up to a minute or two; touching
    the pod's annotations makes it sync sooner)."""
    want = hashlib.sha256(content.encode()).hexdigest()
    if _file_sha(cfg, pod, container, path) == want:
        return
    if not _container_running(cfg, pod, container):
        # Nothing to exec into (a crash loop: an image whose entrypoint fails, which the
        # update after this wait is often what fixes), and nothing that read the old file:
        # the kubelet syncs the pod's volumes before it starts the container again.
        log(f"{pod}: its {container} container isn't running; it reads the new "
            f"{os.path.basename(path)} when it starts")
        return
    kubectl(cfg, "annotate", "pod", pod, f"boxd.sh/config-sync={want[:16]}", "--overwrite",
            check=False)
    log(f"waiting for {pod} to see the new {os.path.basename(path)}…")
    _wait(lambda: _file_sha(cfg, pod, container, path) == want, timeout, 5,
          f"{pod} did not see the new {path}",
          f"kubectl -n {namespace(cfg)} exec {pod} -c {container} -- cat {path}")

def _container_running(cfg, pod: str, container: str) -> bool:
    """False only when the pod's status says the container isn't running; when it says
    nothing of it, the caller waits as before."""
    p = get(cfg, "pod", pod) or {}
    for st in (p.get("status") or {}).get("containerStatuses") or []:
        if st.get("name") == container:
            return "running" in (st.get("state") or {})
    return True

# ----------------------------------------------------------------- workers

def removals(state) -> dict:
    """Worker removals under way: ordinal (str) -> {"id": node id, "node":
    the Kubernetes node it ran on}."""
    return state.data.setdefault("gke", {}).setdefault("removing", {})

def live_worker_replicas(cfg, state) -> int:
    """How many workers run now, for terraform's pool size (which must never
    drop under a running worker): the StatefulSet's replicas, else what the
    last apply recorded, and never below a removal still under way."""
    g = state.data.get("gke") or {}
    count = g.get("workers", 0)
    pending = [int(i) + 1 for i in removals(state)]
    try:
        out = tf.cached_outputs(state)
    except InstallError:
        out = {}
    if out.get("gke"):
        try:
            connect(cfg, out)
            sts = get(cfg, "statefulset", "boxd-worker")
            if sts:
                count = max(count, sts["spec"].get("replicas", 0))
        except InstallError as e:
            log(f"warning: can't read the worker StatefulSet ({e}); sizing the worker pool from "
                "installer state")
    return max([count] + pending)

def _worker_pod(cfg, i: int) -> dict | None:
    return get(cfg, "pod", f"boxd-worker-{i}")

def _sts_replicas(cfg) -> int:
    sts = get(cfg, "statefulset", "boxd-worker")
    return (sts or {}).get("spec", {}).get("replicas", 0)

def scale_workers(cfg, replicas: int) -> None:
    kubectl(cfg, "scale", "statefulset/boxd-worker", f"--replicas={replicas}", quiet=False)

def remove_worker(cfg, state, i: int, groups: list[str]) -> None:
    """Take worker <i> (the StatefulSet's highest ordinal) out of the cluster
    for good. Idempotent: each step checks what is left to do."""
    rec = removals(state).setdefault(str(i), {})
    key = f"worker-{i}/worker"
    if "id" not in rec:
        rec["id"] = (state.data.get("node_ids") or {}).get(key)
    pod = _worker_pod(cfg, i)
    if pod and (pod.get("spec") or {}).get("nodeName"):
        rec["node"] = pod["spec"]["nodeName"]
    state.save()
    wid = f"worker-{rec['id']}" if rec.get("id") is not None else None
    log(f"removing boxd-worker-{i}" + (f" ({wid})" if wid else ""))
    if wid and wid in _registered(cfg):
        ansible.ctl(cfg, f"node exclude {wid}")
        drain(cfg, wid)
        res = ansible.ctl(cfg, f"node mark-dead {wid}", check=False)
        if res.returncode != 0 and "dead" not in _ctl_err(res).lower():
            raise InstallError(f"boxd-ctl node mark-dead {wid} refused, so the worker stays "
                               f"(is a machine still on it? `boxd-ctl vm list`): "
                               f"{_ctl_err(res)[-800:]}")
    if _sts_replicas(cfg) > i:
        scale_workers(cfg, i)
    _wait(lambda: _worker_pod(cfg, i) is None, ROLLOUT_TIMEOUT, 5,
          f"boxd-worker-{i} was not gone")
    if rec.get("id") is not None:
        leader = ansible.leader_node(cfg)
        ansible.ctl_on(cfg, leader, f"remove-peer {rec['id']}")
    for claim in (f"state-boxd-worker-{i}", f"data-boxd-worker-{i}"):
        kubectl(cfg, "delete", "pvc", claim, "--ignore-not-found", "--wait=false")
    if rec.get("node"):
        gke.delete_worker_instance(cfg, rec["node"], groups)
    state.retire_node_id(key)
    removals(state).pop(str(i), None)
    state.save()
    log(f"boxd-worker-{i} removed" + (f"; node id {rec['id']} retired" if rec.get("id") else ""))

def remove_workers(cfg, state, desired: int, groups: list[str]) -> None:
    """Finish removals under way, then remove the workers above `desired`,
    highest ordinal first (a StatefulSet shrinks from the top)."""
    todo = {int(i) for i in removals(state)} | set(range(desired, _sts_replicas(cfg)))
    for i in sorted(todo, reverse=True):
        remove_worker(cfg, state, i, groups)

def _images(pod: dict) -> dict[str, str]:
    return {c["name"]: c["image"] for c in (pod.get("spec") or {}).get("containers", [])}

def _image_ready(cfg, pod_name: str, images: dict[str, str]) -> bool:
    p = get(cfg, "pod", pod_name) or {}
    for name, image in images.items():
        st = _container_status(p, name)
        seen = st.get("image", "")
        if not (seen == image or seen.endswith("/" + image.rsplit("/", 1)[-1])):
            return False
        if not (st.get("ready") and "running" in (st.get("state") or {})):
            return False
    return True

def patch_worker_images(cfg, i: int, images: dict[str, str]) -> None:
    """Patch the running pod's container images: the kubelet restarts those
    containers in place, the pod (and its VMMs) stays. The entrypoint then
    starts the new worker, which adopts the running VMMs."""
    pod = f"boxd-worker-{i}"
    patch = {"spec": {"containers": [{"name": n, "image": img} for n, img in images.items()]}}
    kubectl(cfg, "patch", "pod", pod, "--type=strategic", "-p", json.dumps(patch), quiet=False)
    _wait(lambda: _image_ready(cfg, pod, images), ROLLOUT_TIMEOUT, 5,
          f"{pod} did not come back on its new image",
          f"kubectl -n {namespace(cfg)} describe pod {pod}")
    time.sleep(10)  # and stays up
    if not _image_ready(cfg, pod, images):
        raise InstallError(f"{pod} restarted again on its new image: kubectl -n "
                           f"{namespace(cfg)} logs {pod} -c worker --previous")

def _annotate(cfg, pod: str, key: str, value: str) -> None:
    kubectl(cfg, "annotate", "pod", pod, f"{key}={value}", "--overwrite")

def recreate_worker(cfg, i: int, worker_id: str | None) -> None:
    """A pod spec change other than an image: drain the worker, recreate its
    pod (same ordinal, same Raft disk, same node id), put it back in placement."""
    pod = f"boxd-worker-{i}"
    old = (_worker_pod(cfg, i) or {}).get("metadata", {}).get("uid")
    registered = worker_id and worker_id in _registered(cfg)
    if registered:
        ansible.ctl(cfg, f"node exclude {worker_id}")
        drain(cfg, worker_id)
    kubectl(cfg, "delete", "pod", pod, "--wait=true", quiet=False)
    _wait(lambda: _pod_ready(cfg, pod, other_than=old), WORKER_READY_TIMEOUT, 10,
          f"{pod} did not come back after it was recreated",
          f"kubectl -n {namespace(cfg)} describe pod {pod}")
    if registered:
        # Back in placement once the new pod is registered again.
        _wait(lambda: worker_id in _registered(cfg), REGISTER_TIMEOUT, 10,
              f"{worker_id} did not register again after its pod was recreated",
              f"kubectl -n {namespace(cfg)} logs {pod} -c worker")
        ansible.ctl(cfg, f"node include {worker_id}")

def _pod_ready(cfg, pod: str, other_than: str | None = None) -> bool:
    p = get(cfg, "pod", pod)
    if not p or (other_than and p["metadata"].get("uid") == other_than):
        return False
    conds = {c["type"]: c["status"] for c in (p.get("status") or {}).get("conditions") or []}
    return conds.get("Ready") == "True"

def update_workers(cfg, r, files: dict[str, str], hashes: dict[str, str], existing: dict,
                   count: int) -> None:
    """Bring each running worker pod (ordinals below `count`) to the rendered
    spec, one at a time. `existing`: the pods that ran before this apply's
    StatefulSet change (name -> uid); a pod made since started on the
    current Secret and needs nothing."""
    images = r.worker_images()
    spec_hash = r.worker_spec_hash()
    ids = r.alloc.ids
    for i in range(count):
        name = f"boxd-worker-{i}"
        pod = _worker_pod(cfg, i)
        if pod is None:
            continue
        ann = pod["metadata"].get("annotations") or {}
        want = hashes[f"worker-{i}"]
        node_id = ids.get(f"worker-{i}/worker")
        worker_id = f"worker-{node_id}" if node_id is not None else None
        if existing.get(name) != pod["metadata"].get("uid"):
            # Made by this apply, from the current template and Secret.
            if ann.get(CONFIG_HASH) != want:
                _annotate(cfg, name, CONFIG_HASH, want)
            continue
        if ann.get(SPEC_HASH) != spec_hash:
            log(f"{name}: its pod spec changed, which needs a new pod: draining it first")
            recreate_worker(cfg, i, worker_id)
        elif _images(pod) != images:
            wait_for_file(cfg, name, "worker", f"{SRC}/worker-{i}.toml", files[f"worker-{i}.toml"])
            log(f"{name}: updating in place to {images['worker'].rsplit(':', 1)[-1][:12]} "
                "(its machines keep running)")
            patch_worker_images(cfg, i, images)
        elif ann.get(CONFIG_HASH) != want:
            wait_for_file(cfg, name, "worker", f"{SRC}/worker-{i}.toml", files[f"worker-{i}.toml"])
            log(f"{name}: config changed: restarting its worker (its machines keep running)")
            restart_worker_process(cfg, name)
        else:
            continue
        _annotate(cfg, name, CONFIG_HASH, want)

def wait_workers_ready(cfg, count: int, timeout: int = WORKER_READY_TIMEOUT) -> None:
    """Every worker pod ready. A new node is ready for one only once
    node-prep has run (boxd's kernel and a reboot, the local SSD RAID)."""
    if count == 0:
        return
    ns = namespace(cfg)
    log(f"waiting for {count} worker(s) (a new node first gets boxd's host kernel and reboots)…")
    deadline = time.time() + timeout
    last = 0.0
    while True:
        sts = get(cfg, "statefulset", "boxd-worker") or {}
        ready = (sts.get("status") or {}).get("readyReplicas", 0)
        if ready >= count:
            log(f"workers ready: {ready}")
            return
        if time.time() >= deadline:
            raise InstallError(
                f"only {ready} of {count} workers became ready within {timeout}s. Look at "
                f"kubectl -n {ns} get pods -l app=boxd-worker -o wide, at node prep "
                f"(kubectl -n {ns} logs ds/boxd-node-prep, and kubectl get nodes "
                f"-l boxd.sh/pool=worker -L boxd.sh/node-ready), and at "
                f"kubectl -n {ns} describe pod boxd-worker-<n>")
        if time.time() - last > 60:
            log(f"  {ready}/{count} workers ready")
            last = time.time()
        time.sleep(15)

def verify_registration(cfg, alloc, timeout: int = REGISTER_TIMEOUT) -> None:
    """Every worker cluster.toml has is registered (by node id: a removed
    worker's entry can't stand in for a new one). Learners that gave up
    (they started before a leader existed) are restarted once."""
    want = {f"worker-{alloc.node_id(n.name, 'worker')}" for n in cfg.nodes_with("worker")}
    restarted = False
    deadline = time.time() + timeout
    while True:
        missing = want - _registered(cfg)
        if not missing:
            log(f"cluster registered: {len(want)} worker(s)")
            return
        if not restarted and (gave_up := learners_gave_up(cfg)):
            log(f"restarting learners that gave up registering: {', '.join(gave_up)}")
            restart_learners(cfg)
            restarted = True
            deadline = time.time() + timeout
        if time.time() >= deadline:
            raise InstallError(f"{', '.join(sorted(missing))} did not register within "
                               f"{timeout}s: `./boxd-install ctl -- status`, and the worker's log "
                               f"(kubectl -n {namespace(cfg)} logs boxd-worker-<n> -c worker)")
        time.sleep(10)

# ------------------------------------------------------------------ deploy

def pull_config(cfg) -> str | None:
    """[images] pull_secret: the Secret's docker config, for the workers' own
    pulls of machine images (docker-config.json in boxd-config)."""
    name = (cfg.doc.get("images") or {}).get("pull_secret")
    if not name:
        return None
    ns = namespace(cfg)
    obj = get(cfg, "secret", name)
    if obj is None:
        raise InstallError(f"[images].pull_secret {name}: there is no such Secret in the "
                           f"namespace {ns}. Create the namespace and the Secret first "
                           f"(kubectl create namespace {ns}; kubectl -n {ns} create secret "
                           f"docker-registry {name} --docker-server=... ), then apply")
    if obj.get("type") != "kubernetes.io/dockerconfigjson" or \
            ".dockerconfigjson" not in (obj.get("data") or {}):
        raise InstallError(f"[images].pull_secret {name} is not a kubernetes.io/dockerconfigjson "
                           "Secret (kubectl create secret docker-registry makes one)")
    return base64.b64decode(obj["data"][".dockerconfigjson"]).decode()

def _wait_crd(cfg, crd: str, timeout: int = 180) -> bool:
    deadline = time.time() + timeout
    while True:
        if kubectl(cfg, "get", "crd", crd, check=False).returncode == 0:
            return True
        if time.time() >= deadline:
            return False
        time.sleep(10)

def _pods(cfg, app: str) -> dict[str, str]:
    lst = get(cfg, "pods", selector_=f"app={app}") or {}
    return {p["metadata"]["name"]: p["metadata"].get("uid") for p in lst.get("items", [])}

def deploy(cfg, r, state, out_dir: str, tf_out: dict) -> None:
    """The whole Kubernetes side of an apply (module docstring)."""
    s = r.s
    desired = s["pools"]["worker"]["nodes"]
    groups = tf_out.get("worker_instance_groups") or []
    live = _sts_replicas(cfg)
    pending = {int(i) for i in removals(state)}
    # Every worker that runs or will (those being removed keep their config
    # until they are gone), as long as it holds a node id.
    ordinals = [i for i in range(max([desired, live] + [i + 1 for i in pending]))
                if f"worker-{i}/worker" in r.alloc.ids]
    files, hashes = r.config_files(ordinals)
    downloads = r.downloads()
    base = r.base_objects(downloads)
    secret = r.secret(files)
    apply(cfg, base, out_dir, "base")
    apply(cfg, [secret], out_dir, "secret")

    # The usage graphs' ClickHouse ([gke] analytics), before the roles that write to it.
    clickhouse = r.clickhouse_objects(hashes.get("clickhouse", ""))
    if clickhouse:
        apply(cfg, clickhouse, out_dir, "clickhouse")
        rollout(cfg, "boxd-clickhouse")
        clickhouse_schema(cfg)

    control = r.control_objects(hashes["control"])
    apply(cfg, control, out_dir, "control")
    rollout(cfg, "boxd-control")
    ansible.wait_for_leader(cfg)
    # sync-config reads the leader's config file: the Secret's new content
    # must be there first (control reads its config from the mount).
    for n in cfg.nodes_with("control"):
        i = gke.ordinal(n.name)
        wait_for_file(cfg, f"boxd-control-{i}", "control", f"{SRC}/control-{i}.toml",
                      files[f"control-{i}.toml"])
    ansible.sync_config(cfg)

    console = r.console_objects(hashes["console"], downloads=downloads)
    apply(cfg, console, out_dir, "console")
    rollout(cfg, "boxd-console")
    edges = []
    for n in cfg.nodes_with("proxy"):
        i = gke.ordinal(n.name)
        objs = r.edge_objects(i, hashes, downloads=downloads)
        edges += objs
        apply(cfg, objs, out_dir, f"edge-{i}")
        rollout(cfg, f"boxd-proxy-{i}")
        rollout(cfg, f"boxd-dns-{i}")

    # Workers: removals first (never update one that is about to go), then
    # the StatefulSet at its new size, then every running pod brought up to date.
    remove_workers(cfg, state, desired, groups)
    existing = _pods(cfg, "boxd-worker")
    running = _sts_replicas(cfg)
    workers = r.worker_objects(desired)
    apply(cfg, workers, out_dir, "worker")
    update_workers(cfg, r, files, hashes, existing, min(running, desired))
    wait_workers_ready(cfg, desired)
    update_workers(cfg, r, files, hashes, existing, desired)  # annotates the new pods

    monitorings = r.pod_monitorings()
    if monitorings and not _wait_crd(cfg, MONITORING_CRD):
        log(f"warning: the cluster has no {MONITORING_CRD} (Managed Service for Prometheus is "
            "still starting, or off): the PodMonitorings are not applied; the next apply does it")
        monitorings = []
    apply(cfg, monitorings, out_dir, "monitoring")

    prune(cfg, base + [secret] + clickhouse + control + console + edges + workers + monitorings)
    verify_registration(cfg, r.alloc)

def clickhouse_schema(cfg) -> None:
    """roles/boxd-clickhouse's schema migrations, each once: the ones applied are listed in
    default.boxd_migrations (run again in order they fail: a later one changes what an
    earlier one made). Then the role's system log sweep: the orphaned system.*_log_<N> tables
    a TTL change leaves, and the profiler logs the config turns off. The password stays in
    the pod's environment."""
    from .kube_render import CLICKHOUSE_MIGRATIONS

    def ch(script: str, sql: str | None = None, what: str = "ClickHouse") -> str:
        res = kubectl(cfg, "exec", *(["-i"] if sql is not None else []), "boxd-clickhouse-0",
                      "-c", "clickhouse", "--", "sh", "-c", script, input=sql, check=False)
        if res.returncode != 0:
            raise InstallError(f"{what} failed: "
                               f"{((res.stderr or '') + (res.stdout or '')).strip()[-800:]}")
        return res.stdout or ""

    client = 'clickhouse-client --password "$BOXD_CLICKHOUSE_PASSWORD"'
    ch(f'{client} -q "CREATE TABLE IF NOT EXISTS default.boxd_migrations (name String, '
       f'applied_at DateTime DEFAULT now()) ENGINE = MergeTree ORDER BY name"')
    done = set(ch(f'{client} -q "SELECT name FROM default.boxd_migrations"').split())
    files = sorted(f for f in os.listdir(CLICKHOUSE_MIGRATIONS) if f.endswith(".sql"))
    new = [f for f in files if f not in done]
    for name in new:
        if not re.fullmatch(r"[0-9A-Za-z_.-]+", name):
            raise InstallError(f"ClickHouse migration file name {name!r} is not a plain name")
        with open(os.path.join(CLICKHOUSE_MIGRATIONS, name)) as fh:
            ch(f"{client} --multiquery", fh.read(), f"ClickHouse migration {name}")
        ch(f"{client} -q \"INSERT INTO default.boxd_migrations (name) VALUES ('{name}')\"")
    ch(f'set -e; {client} -q "SYSTEM FLUSH LOGS"; '
       f'for t in $({client} -q "SELECT name FROM system.tables WHERE database = \'system\' '
       f"AND (match(name, '_log_[0-9]+$') OR name IN ('trace_log', 'processors_profile_log'))\"); "
       f'do {client} -q "DROP TABLE IF EXISTS system.$t SYNC"; done', what="ClickHouse log sweep")
    log(f"ClickHouse schema: {len(new)} migration(s) applied, {len(files) - len(new)} already there")

def teardown(cfg) -> None:
    """Before terraform destroy on a cluster that stays (cluster = an existing
    one): boxd's namespace and its cluster-scoped objects. Best effort."""
    ns = namespace(cfg)
    for resource, _, namespaced in PRUNE:
        if not namespaced:
            kubectl(cfg, "delete", resource, "-l", selector(cfg), "--ignore-not-found",
                    check=False, quiet=False)
    kubectl(cfg, "delete", "namespace", ns, "--ignore-not-found", "--wait=false", check=False,
            quiet=False)
