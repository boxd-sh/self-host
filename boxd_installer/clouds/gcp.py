"""GCP: IAP-tunnelled ssh, one GCS bucket for terraform + installer state,
Google Trust Services as the default ACME CA."""
import base64
import os

from ..util import InstallError, run

REQUIRED = ("project", "region", "zone", "vpc_cidr", "image_family", "state_bucket")
DEFAULT_INTERFACE = "ens4"
DEFAULT_CA = "gts"
TF_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "terraform", "gcp"))
SSH_PATH = "IAP"
SSH_HINT = "roles/iap.tunnelResourceAccessor"

def validate(doc: dict, errs: list[str]) -> None:
    # Private ingress is AWS-only; ignoring it here would expose a cluster
    # the operator meant to keep private.
    for k in ("ingress", "ingress_cidrs"):
        if k in doc["gcp"]:
            errs.append(f"[gcp].{k} is not supported (private ingress is AWS-only)")
    for k in ("bucket_access", "auto_snapshot_archive"):
        if k in doc["gcp"]:
            errs.append(f"[gcp].{k} is not supported (AWS-only)")

def tfvars(cfg, state, nodes: list[dict]) -> dict:
    return {
        "project": cfg.cloud["project"],
        "region": cfg.cloud["region"],
        "zone": cfg.cloud["zone"],
        "cluster_name": cfg.name,
        "vpc_cidr": cfg.vpc_cidr,
        "boot_image": _resolve_boot_image(cfg),
        "deploy_ssh_pubkey": state.deploy_pubkey(),
        "enable_backups": cfg.cloud.get("auto_backups", True),
        "nodes": nodes,
    }

def _resolve_boot_image(cfg) -> str:
    """Concrete image self_link for [gcp].image_family — resolved here so the
    tf plan never contains an unknown create-only image (see main.tf)."""
    res = run(["gcloud", "compute", "images", "describe-from-family",
               cfg.cloud["image_family"], "--project=ubuntu-os-cloud",
               "--format=value(selfLink)"], capture=True)
    link = res.stdout.strip()
    if not link:
        raise InstallError(f"could not resolve image family {cfg.cloud['image_family']}")
    return link

def tf_key_env(key: bytes) -> dict:
    """CSEK for the GCS state object: GCS uses it server-side but never
    stores it — bucket access alone reads nothing."""
    return {"GOOGLE_ENCRYPTION_KEY": base64.b64encode(key).decode()}

def backend_args(cfg) -> list[str]:
    return [f"-backend-config=bucket={cfg.cloud['state_bucket']}",
            f"-backend-config=prefix={cfg.name}"]

def state_url(cfg) -> str:
    return f"gs://{cfg.cloud['state_bucket']}/{cfg.name}/installer-state.enc"

def object_size(cfg, url: str) -> int | None:
    res = run(["gcloud", "storage", "objects", "describe", url,
               "--format=value(size)"], capture=True, check=False)
    return int(res.stdout.strip()) if res.returncode == 0 else None

def download(cfg, url: str, path: str) -> None:
    run(["gcloud", "storage", "cp", url, path], capture=True)

def upload(cfg, path: str, url: str) -> None:
    run(["gcloud", "storage", "cp", path, url], capture=True)

def copy(cfg, src: str, dst: str) -> None:
    run(["gcloud", "storage", "cp", src, dst], capture=True, check=False)

def preflight(cfg) -> None:
    run(["gcloud", "auth", "print-access-token"], capture=True)

def ssh_proxy_command(cfg) -> str:
    return (f"gcloud compute start-iap-tunnel %h %p --listen-on-stdin "
            f"--project={cfg.cloud['project']} --zone={cfg.cloud['zone']} --verbosity=warning")

def ssh_target(node, tf_out: dict) -> str:
    """IAP resolves instance names, which equal node names."""
    return node.name

def host_vars(node, tf_out: dict) -> dict:
    return {}
