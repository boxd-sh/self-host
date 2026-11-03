"""GCP: IAP-tunnelled ssh, one GCS bucket for terraform + installer state,
Google Trust Services as the default ACME CA."""
import base64
import os

from ..util import InstallError, run

REQUIRED = ("project", "region", "zone", "vpc_cidr", "image_family", "state_bucket")
# None: read from each host, as on AWS (gVNIC machine types name it ens3 or
# enp0s3 rather than ens4).
DEFAULT_INTERFACE = None
# [network] guest_mtu in routed mode when cluster.toml sets none: 0, the
# guest's own 1500. The VPC terraform makes carries 8896 (terraform/gcp
# var.mtu), so Geneve's 50 bytes fit. (On a VPC left at GCP's 1460 it would
# be 1410: set guest_mtu.)
ROUTED_GUEST_MTU = 0
DEFAULT_CA = "gts"
TF_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "terraform", "gcp"))
SSH_PATH = "IAP"
SSH_HINT = "roles/iap.tunnelResourceAccessor"
# Service account ids are 6-30 chars: <name>-backups and <name>-storage.
SA_ID_MAX = 30
# GCS bucket names: <project>-<name>-storage, at most 63 chars.
BUCKET_NAME_MAX = 63

def _auto_storage(doc: dict) -> bool:
    """[storage_engine] declared makes terraform create the cluster's bucket,
    a service account with access to it alone, and attach that account to the
    worker instances (no key anywhere), unless [gcp] auto_storage_bucket = false."""
    return isinstance(doc.get("storage_engine"), dict) and \
        doc["gcp"].get("auto_storage_bucket", True) is not False

def validate(doc: dict, errs: list[str]) -> None:
    # Private ingress is AWS-only; ignoring it here would expose a cluster
    # the operator meant to keep private.
    for k in ("ingress", "ingress_cidrs"):
        if k in doc["gcp"]:
            errs.append(f"[gcp].{k} is not supported (private ingress is AWS-only)")
    for k in ("bucket_access", "auto_snapshot_archive"):
        if k in doc["gcp"]:
            errs.append(f"[gcp].{k} is not supported (AWS-only)")
    name = doc["cluster"]["name"]
    accounts = [s for s, on in (("-backups", doc["gcp"].get("auto_backups", True)),
                                ("-storage", _auto_storage(doc))) if on]
    if accounts and len(name) + len("-backups") > SA_ID_MAX:
        errs.append(f"[cluster].name must be <= {SA_ID_MAX - len('-backups')} chars on GCP: "
                    f"the service accounts {' and '.join(f'<name>{s}' for s in accounts)} "
                    f"are at most {SA_ID_MAX} chars")
    if _auto_storage(doc):
        bucket = f"{doc['gcp'].get('project', '')}-{name}-storage"
        if len(bucket) > BUCKET_NAME_MAX:
            errs.append(f"the storage bucket name {bucket} is {len(bucket)} chars, over GCS's "
                        f"{BUCKET_NAME_MAX}: shorten [cluster].name")

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
        "enable_storage_bucket": _auto_storage(cfg.doc),
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
