"""Cloud adapters. Everything that differs between clouds sits behind one
small surface — terraform dir + tfvars + backend, the state bucket's object
store, the operator ssh path, preflight, and a few defaults. Allocation,
rendering, the ansible tree, assets and seeding are cloud-neutral.

cluster.toml selects the adapter by carrying exactly one cloud table
([gcp] or [aws]); see DESIGN.md "Clouds" for the contract each must meet."""
from . import aws, gcp

CLOUDS = {"gcp": gcp, "aws": aws}

def get(cfg):
    return CLOUDS[cfg.cloud_name]
