"""Carry a control node's state across an instance replacement.

boxd-control keeps everything operators and users create (users, orgs,
VM records, domains, quotas — OPERATIONS.md) only in its raft log and
state.db under /var/lib/boxd/control, on the boot disk. A change terraform
can only make by replacing the instance (a new subnet or internal_ip, e.g.
switching [aws].ingress) would otherwise start an empty cluster there.

Before terraform applies, every control node the plan replaces is stopped
and its control dir archived into state's control-carry/ (encrypted, pushed
even if the apply fails). The boxd-control role unpacks it onto the new
instance before the service first starts, and only when that node has no
state.db of its own. Raft membership is by node id, which is keyed by node
name in state, so the new address in the rendered config is all the
restored node needs. Archives are dropped once an apply has verified the
cluster.
"""
import json
import os
import subprocess

from . import ansible, tf
from .util import InstallError, log

DIR = "control-carry"
REMOTE_DIR = "/var/lib/boxd/control"
# The node instance resource in terraform/<cloud>/main.tf, keyed by node name.
INSTANCE_TYPES = ("aws_instance", "google_compute_instance")

def carry_dir(state) -> str:
    return os.path.join(state.dir, DIR)

def archive_path(state, node_name: str) -> str:
    return os.path.join(carry_dir(state), f"{node_name}.tar.gz")

def replaced_nodes(plan: dict) -> list[str]:
    """Node names whose instance the plan (`terraform show -json`) destroys
    and recreates."""
    out = []
    for rc in plan.get("resource_changes", []):
        if rc.get("type") in INSTANCE_TYPES and rc.get("name") == "node" \
                and "delete" in rc["change"]["actions"] and "create" in rc["change"]["actions"]:
            out.append(rc["index"])
    return sorted(out)

def save(cfg, state, plan: dict) -> None:
    """Archive the control dir of every control node the plan replaces."""
    control = {n.name: n for n in cfg.nodes_with("control")}
    names = [n for n in replaced_nodes(plan) if n in control]
    if not names:
        return
    log(f"plan replaces control node(s) {', '.join(names)}: carrying their state over")
    # SSH targets come from the outputs of the previous apply: the instances
    # being replaced are the ones in it.
    ansible.set_tf_outputs(tf.cached_outputs(state))
    os.makedirs(carry_dir(state), mode=0o700, exist_ok=True)
    for name in names:
        _save_one(cfg, state, control[name])

def _save_one(cfg, state, node) -> None:
    dest = archive_path(state, node.name)
    target = ansible._target(cfg, node)
    probe = subprocess.run(["ssh", *ansible._ssh_opts(cfg), target,
                            f"sudo test -f {REMOTE_DIR}/state.db"], capture_output=True)
    if probe.returncode != 0:
        # A rerun after terraform already destroyed the old instance: the
        # archive from the first attempt is the state.
        if os.path.exists(dest):
            log(f"{node.name}: unreachable or no state.db; keeping the archive "
                "from the previous attempt")
            return
        raise InstallError(
            f"{node.name}: the plan replaces this control node, but its state could not "
            f"be read over ssh ({probe.stderr.decode(errors='replace').strip()[-400:]}). "
            "Replacing it would start an empty cluster; fix access and re-run `apply`.")
    # Stopped first, so raft and state.db are one consistent moment (both
    # record the applied index). The node is about to be replaced anyway.
    tmp = dest + ".part"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        res = subprocess.run(["ssh", *ansible._ssh_opts(cfg), target,
                              f"sudo systemctl stop boxd-control && "
                              f"sudo tar -C {REMOTE_DIR} -czf - ."],
                             stdout=fh, stderr=subprocess.PIPE)
    if res.returncode != 0:
        os.remove(tmp)
        raise InstallError(f"{node.name}: archiving {REMOTE_DIR} failed: "
                           f"{res.stderr.decode(errors='replace').strip()[-800:]}")
    members = subprocess.run(["tar", "-tzf", tmp], capture_output=True, text=True)
    names = {m.removeprefix("./").split("/")[0] for m in members.stdout.split()}
    if members.returncode != 0 or not {"state.db", "raft"} <= names:
        os.remove(tmp)
        raise InstallError(f"{node.name}: the archive of {REMOTE_DIR} is missing "
                           "state.db or raft/ — not replacing the node")
    os.replace(tmp, dest)
    log(f"{node.name}: control state archived ({os.path.getsize(dest) // 1024} KiB); "
        "boxd-control there is stopped until the new instance takes over")

def clear(state) -> None:
    """After a verified apply (every control node is running on its own
    state again), or a destroy (an archive must never seed a later,
    unrelated cluster)."""
    d = carry_dir(state)
    if not os.path.isdir(d):
        return
    for f in os.listdir(d):
        os.remove(os.path.join(d, f))
        log(f"dropped carried control state {f}")
    os.rmdir(d)

def plan_json(cfg, plan_path: str) -> dict:
    return json.loads(tf.show_json(cfg, plan_path))
