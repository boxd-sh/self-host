"""Converge the nodes with the vendored ansible tree (../ansible): the one
place the installer runs ansible-playbook. Reaching a node over ssh and
operating the running cluster (boxd-ctl) are cluster.py."""
import json
import os

from . import assets
from .util import run

ANSIBLE_DIR = os.path.join(os.path.dirname(__file__), "..", "ansible")

# The assets files boxd_copy may take from the node's own copy of the bundle.
# console/ isn't among them: it ships as one tarball (assets.console_tarball).
NODE_ASSET_DIRS = ("bin/", "kernel/", "cli/", "automations/")

def converge(cfg, out_dir: str, assets_root: str, keys_dir: str,
             host_kernel_release: str = "", control_carry_dir: str = "") -> None:
    """ansible-playbook site.yml on the rendered inventory in out_dir: every
    node gets its packages, the assets bundle, its config and its units.
    `keys_dir` is the installer state's keys (the TLS material to copy)."""
    inv = os.path.abspath(os.path.join(out_dir, "inventory.yml"))
    src = os.path.abspath(os.path.join(out_dir, "group_vars", f"{cfg.env_group}.yml"))
    dst = os.path.join(ANSIBLE_DIR, "group_vars", f"{cfg.env_group}.yml")
    if os.path.islink(dst) or os.path.exists(dst):
        os.remove(dst)
    os.symlink(src, dst)
    # In a file, not on the command line: the URL carries the license. The
    # out dir is temporary and apply deletes it.
    node_vars = os.path.abspath(os.path.join(out_dir, "node-assets.json"))
    fd = os.open(node_vars, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(node_assets_vars(cfg, assets_root), fh)
    run(["ansible-playbook", "-i", inv, "site.yml",
         "-e", f"@{node_vars}",
         "-e", f"boxd_assets_dir={os.path.abspath(assets_root)}",
         "-e", f"boxd_tls_local_dir={os.path.abspath(keys_dir)}",
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
