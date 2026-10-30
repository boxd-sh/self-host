"""Cloud adapters. Everything that differs between clouds sits behind one
small surface — terraform dir + tfvars + backend, the state bucket's object
store, the operator ssh path, preflight, and a few defaults. Allocation,
rendering, the ansible tree, assets and seeding are cloud-neutral.

cluster.toml selects the adapter by carrying exactly one cloud table
([gcp], [aws] or [metal]); see DESIGN.md "Clouds" for the contract each must
meet. metal has no terraform (TF_DIR is None): the hosts are given."""
from . import aws, gcp, metal

CLOUDS = {"gcp": gcp, "aws": aws, "metal": metal}

def get(cfg):
    return CLOUDS[cfg.cloud_name]

def provisions(cfg) -> bool:
    """Whether terraform creates the hosts (gcp, aws) or they exist (metal)."""
    return get(cfg).TF_DIR is not None
