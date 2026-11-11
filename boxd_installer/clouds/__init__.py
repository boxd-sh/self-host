"""Cloud adapters. Everything that differs between clouds sits behind one
small surface — terraform dir + tfvars + backend, the state bucket's object
store, the operator ssh path, preflight, and a few defaults. Allocation,
rendering, the ansible tree, assets and seeding are cloud-neutral.

cluster.toml selects the adapter by carrying exactly one cloud table
([gcp], [aws] or [metal]). metal has no terraform (TF_DIR is None): the hosts are given.

A target changes how boxd is deployed on a cloud's account: [gke] next to
[gcp] runs boxd's roles as pods on Google Kubernetes Engine instead of on
Compute Engine instances (clouds/gke.py, kube.py). The cloud stays "gcp" (its
project, region, state bucket, the ACME default); get() returns the target's
adapter."""
from . import aws, gcp, gke, metal

CLOUDS = {"gcp": gcp, "aws": aws, "metal": metal}
# Target table -> (adapter, the cloud table it needs beside it).
TARGETS = {"gke": (gke, "gcp")}

def kubernetes(cfg) -> bool:
    """The roles run as pods on Kubernetes ([gke]), not on hosts the
    installer reaches over ssh."""
    return "gke" in (getattr(cfg, "doc", None) or {})

def get(cfg):
    doc = getattr(cfg, "doc", None) or {}
    for table, (mod, _) in TARGETS.items():
        if table in doc:
            return mod
    return CLOUDS[cfg.cloud_name]

def provisions(cfg) -> bool:
    """Whether terraform creates the hosts (gcp, aws, gke) or they exist (metal)."""
    return get(cfg).TF_DIR is not None
