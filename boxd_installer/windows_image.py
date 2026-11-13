"""`./boxd-install windows-image build`, the part that runs in the tools image (win-img/README.md,
"Building the image"): the cluster's release bundle fetched and checked as `apply` does, its
Windows image kit (windows-image/: boxd's half of the image, its build scripts and binaries) copied
into a work directory under the cache, and the registry's pull credentials from the providers
config written beside it as a Docker client config for the push. The build itself needs KVM and
Docker on the host, so `boxd-install` runs it there, from what this prints.

The work directory keeps its downloads (dl/: the drivers, boxd's tools, the ISO) across builds of
the same release; the kit's own files are copied again each time.
"""
import base64
import json
import os
import shutil

from . import assets, windows
from .util import InstallError, log

# What the bundle's kit must have for a build (boxd scripts/package-assets.sh WIN_KIT_FILES and
# the three binaries).
KIT_DIR = "windows-image"
KIT_REQUIRED = ("build-image.sh", "run-install.sh", "package-image.sh", "Dockerfile", "fetch-tools.sh",
                "autounattend.xml.tmpl", "sysprep-unattend.xml.tmpl", "setup.ps1", "boxd-boot.ps1",
                "boxd-agent.exe", "boxd-name.exe", "boxd-ready.exe")


def work_dir(cache_dir: str, sha: str) -> str:
    return os.path.join(cache_dir, KIT_DIR, sha)


def kit_error(cfg, assets_root: str) -> str | None:
    """Why the release can't build a Windows image: a bundle from before the kit."""
    missing = [f for f in KIT_REQUIRED if not assets.has_file(assets_root, f"{KIT_DIR}/{f}")]
    if not missing:
        return None
    return (f"release {cfg.assets_version[:12]} has no Windows image kit ({KIT_DIR}/ lacks "
            f"{', '.join(missing)}): set [cluster].assets_version to a newer release")


def docker_config(cfg) -> dict | None:
    """The Docker client config that logs `docker push` in to the image's registry, from the
    providers config's [windows_registry]; None without credentials (the build host's own
    `docker login` is used then)."""
    regs = windows.registries(cfg)
    if not regs:
        return None
    r = regs[0]
    host = windows.image_host(cfg.doc["windows"]["image"])
    if r["host"] != host:
        raise InstallError(f"[windows] image is on {host}, but [{windows.PROVIDER}] has "
                           f"credentials for {r['host']!r}: the push would go without them")
    auth = base64.b64encode(f"{r['username']}:{r['password']}".encode()).decode()
    return {"auths": {host: {"auth": auth}}}


def prepare(cfg, cache_dir: str, assets_root: str) -> dict:
    """The work directory, ready for the host's build: the kit copied in, the Docker config
    written (0600). Returns what `boxd-install` needs, paths relative to the installer's root."""
    if not windows.on(cfg.doc):
        raise InstallError('[windows] isn\'t in cluster.toml: name the image this build pushes '
                           'there first (image = "registry.example.com/boxd/windows-server-2025:'
                           '2026-11", docs/REFERENCE.md "Windows machines")')
    if why := windows.image_error(cfg.doc["windows"].get("image")):
        raise InstallError(f"[windows].image {why}")
    if why := kit_error(cfg, assets_root):
        raise InstallError(why)
    sha = os.path.basename(os.path.normpath(assets_root))
    work = work_dir(cache_dir, sha)
    os.makedirs(os.path.join(work, "dl"), exist_ok=True)
    src = os.path.join(assets_root, KIT_DIR)
    for name in os.listdir(src):
        s, d = os.path.join(src, name), os.path.join(work, name)
        if os.path.isdir(s):
            shutil.rmtree(d, ignore_errors=True)
            shutil.copytree(s, d)
        else:
            shutil.copy2(s, d)
    for f in ("build-image.sh", "run-install.sh", "package-image.sh", "fetch-tools.sh", "mon.sh"):
        p = os.path.join(work, f)
        if os.path.exists(p):
            os.chmod(p, 0o755)
    dconf = os.path.join(work, "docker")
    shutil.rmtree(dconf, ignore_errors=True)
    out = {"kit": work, "image": cfg.doc["windows"]["image"], "release": sha, "docker_config": ""}
    if conf := docker_config(cfg):
        os.makedirs(dconf, mode=0o700)
        path = os.path.join(dconf, "config.json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(conf, fh)
        out["docker_config"] = dconf
    log(f"Windows image kit of release {sha[:12]} in {work}")
    return out
