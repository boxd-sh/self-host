"""Terraform driver: render tfvars.json from cluster.toml, run terraform,
read outputs back. Outputs are cached in state/tf-outputs.json so `render`
works offline; --fake-tf substitutes a hand-written outputs file (tests)."""
import json
import os

from . import clouds, license
from .util import InstallError, log, run

def _tf_env(cfg) -> dict:
    """Every terraform invocation carries a customer-supplied encryption key
    for the state object (GCS CSEK / S3 SSE-C), derived deterministically
    from BOXD_STATE_KEY (domain-separated from the installer-blob key). The
    bucket uses it server-side but never stores it — bucket access alone
    reads nothing. Same recovery contract as everything else: no
    BOXD_STATE_KEY, no state.

    TF_DATA_DIR is per-cluster: .terraform/ caches the initialized backend,
    so a shared one makes `init` for a second cluster fail with "Backend
    configuration changed"."""
    import hashlib
    import hmac
    from . import remote_state
    raw = hmac.new(bytes.fromhex(remote_state._key()),
                   b"boxd-tf-csek", hashlib.sha256).digest()
    cloud = clouds.get(cfg)
    return {**os.environ, **cloud.tf_key_env(raw),
            "TF_DATA_DIR": os.path.join(cloud.TF_DIR, f".terraform-{cfg.name}")}

def build_tfvars(cfg, state, existing: dict[str, str] | None = None,
                 for_destroy: bool = False) -> dict:
    """`existing`: the instance type of each node terraform already manages
    (instance_types). A worker without machine_type keeps it, so the license
    never resizes a worker that exists. `for_destroy`: no license check at
    all (destroying a cluster never depends on its license file): a node
    gets its machine_type, else its current type, else terraform's default."""
    nodes = []
    for n in cfg.nodes:
        nodes.append({
            "name": n.name,
            "primary_role": n.primary_role,
            "roles": sorted(set(n.roles)),
            "internal_ip": n.internal_ip,
            # Without a license key, a worker with no type gets a 16-vCPU
            # one (the free tier); otherwise terraform's default applies.
            "machine_type": (n.machine_type or (existing or {}).get(n.name)) if for_destroy
                            else license.tfvars_machine_type(cfg, n, existing),
            # Private ingress: no public IP anywhere; proxy/dns advertise
            # their internal one instead (render._advertised_ip).
            "public_ip": (n.has("proxy") or n.has("dns")) and cfg.ingress == "public",
            "nested_virt": n.has("worker"),
            "data_disk_gb": n.data_disk_gb,
            "boot_disk_gb": n.boot_disk_gb or (200 if n.has("worker") else 50),
        })
    tfvars = clouds.get(cfg).tfvars(cfg, state, nodes)
    # Checked against the real worker types in terraform/<cloud>/machine_sizes.tf.
    tfvars["machine_sizes"] = cfg.machine_sizes()
    return tfvars

def write_tfvars(cfg, state, out_dir: str, existing: dict[str, str] | None = None,
                 for_destroy: bool = False) -> str:
    path = os.path.join(out_dir, "terraform.tfvars.json")
    with open(path, "w") as fh:
        json.dump(build_tfvars(cfg, state, existing, for_destroy), fh, indent=1,
                  sort_keys=True)
    return path

def _tf(cfg, *args: str, capture: bool = False, quiet: bool = False):
    return run(["terraform", f"-chdir={clouds.get(cfg).TF_DIR}", *args],
               capture=capture, env=_tf_env(cfg), quiet=quiet)

# The node instance resource in terraform/<cloud>/main.tf, keyed by node
# name, and the attribute that holds its type.
NODE_INSTANCE_TYPE_ATTR = {"aws_instance": "instance_type",
                           "google_compute_instance": "machine_type"}

def instance_types(cfg) -> dict[str, str]:
    """The instance type of every node in the terraform state, by node name;
    empty before the first apply. Needs `init`. Reads the raw state (`state
    pull`), not `show -json`: show refuses a state whose resource schema
    version differs from the installed provider until a plan upgrades it."""
    res = _tf(cfg, "state", "pull", capture=True, quiet=True)
    return state_instance_types(json.loads(res.stdout or "{}"))

def state_instance_types(raw_state: dict) -> dict[str, str]:
    """instance_types from a raw terraform state (format version 4)."""
    out = {}
    for r in raw_state.get("resources", []):
        attr = NODE_INSTANCE_TYPE_ATTR.get(r.get("type"))
        if not attr or r.get("mode") != "managed" or r.get("name") != "node":
            continue
        for inst in r.get("instances", []):
            name, t = inst.get("index_key"), inst.get("attributes", {}).get(attr)
            if isinstance(name, str) and isinstance(t, str) and t:
                # GCP may store a machine type's URL; its last part is the name.
                out[name] = t.rsplit("/", 1)[-1]
    return out

def init(cfg) -> None:
    _tf(cfg, "init", "-input=false", *clouds.get(cfg).backend_args(cfg))

def plan(cfg, tfvars_path: str, plan_path: str) -> None:
    """Saved, so what control_carry inspects is exactly what apply_plan does."""
    _tf(cfg, "plan", "-input=false", f"-out={os.path.abspath(plan_path)}",
        f"-var-file={os.path.abspath(tfvars_path)}")

def show_json(cfg, plan_path: str) -> str:
    return _tf(cfg, "show", "-json", os.path.abspath(plan_path),
               capture=True, quiet=True).stdout

def apply_plan(cfg, plan_path: str) -> None:
    _tf(cfg, "apply", "-input=false", os.path.abspath(plan_path))

def destroy(cfg, tfvars_path: str) -> None:
    _tf(cfg, "destroy", "-input=false", "-auto-approve",
        f"-var-file={os.path.abspath(tfvars_path)}")

def read_outputs(cfg, state) -> dict:
    """terraform's outputs; on metal (no terraform) the adapter's, from
    cluster.toml. Cached either way, for `render`."""
    if clouds.provisions(cfg):
        res = _tf(cfg, "output", "-json", capture=True)
        out = {k: v["value"] for k, v in json.loads(res.stdout).items()}
    else:
        out = clouds.get(cfg).outputs(cfg)
    _check(out)
    cache = os.path.join(state.dir, "tf-outputs.json")
    fd = os.open(cache, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(out, fh, indent=1, sort_keys=True)
    return out

def cached_outputs(state, fake_path: str | None = None) -> dict:
    path = fake_path or os.path.join(state.dir, "tf-outputs.json")
    if not os.path.exists(path):
        raise InstallError(
            f"no terraform outputs at {path} — run `apply` first (or pass --fake-tf)")
    with open(path) as fh:
        out = json.load(fh)
    _check(out)
    return out

def _check(out: dict) -> None:
    if "nodes" not in out:
        raise InstallError("terraform outputs missing 'nodes'")
    log(f"terraform outputs: {len(out['nodes'])} nodes")

