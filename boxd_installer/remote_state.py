"""Encrypted installer state in the cloud's state bucket.

The state/ dir (allocations, generated keys, tf-outputs cache) lives as ONE
encrypted object next to the terraform state:

    gs://<state_bucket>/<cluster>/installer-state.enc    (GCP)
    s3://<state_bucket>/<cluster>/installer-state.enc    (AWS)

The bucket is only a dumb object store (size/get/put/copy, via the cloud
adapter); confidentiality comes entirely from the client-side encryption.

Encryption is stdlib + openssl (no python deps): PBKDF2-HMAC-SHA256 derives
an AES key, IV and MAC key from BOXD_STATE_KEY (a 64-hex secret the operator
generates ONCE with `openssl rand -hex 32` and must not lose — the state is
unrecoverable without it); `openssl enc -aes-256-ctr` encrypts; an
HMAC-SHA256 tag over salt+ciphertext authenticates (encrypt-then-MAC).

Lifecycle: every state-touching command pulls+decrypts to a local state/,
runs, then — success OR failure — encrypts, uploads (previous blob kept as
.prev) and deletes the local copy. Failure uploads too, deliberately: state
is append-only/idempotent (create-if-missing secrets, stable allocations),
so a partial run's state is strictly newer, and discarding it after e.g. the
deploy key already reached instance metadata would lock the operator out.
"""
import contextlib
import hashlib
import hmac as hmaclib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile

from . import clouds
from .util import InstallError, log

MAGIC = b"BOXDST01"
PBKDF2_ITERS = 600_000

def _key() -> str:
    key = os.environ.get("BOXD_STATE_KEY", "")
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        raise InstallError(
            "BOXD_STATE_KEY must be set to a 64-hex secret. Generate ONCE with\n"
            "  openssl rand -hex 32\n"
            "and store it safely — the remote state is unrecoverable without it.")
    return key

def _derive(key_hex: str, salt: bytes) -> tuple[bytes, bytes, bytes]:
    material = hashlib.pbkdf2_hmac("sha256", bytes.fromhex(key_hex), salt, PBKDF2_ITERS, dklen=80)
    return material[:32], material[32:48], material[48:80]

def _aes_ctr(data: bytes, enc_key: bytes, iv: bytes) -> bytes:
    proc = subprocess.run(
        ["openssl", "enc", "-aes-256-ctr", "-K", enc_key.hex(), "-iv", iv.hex()],
        input=data, capture_output=True)
    if proc.returncode != 0:
        raise InstallError(f"openssl enc failed: {proc.stderr.decode()[-300:]}")
    return proc.stdout

def encrypt(plaintext: bytes, key_hex: str) -> bytes:
    salt = os.urandom(16)
    enc_key, iv, mac_key = _derive(key_hex, salt)
    ct = _aes_ctr(plaintext, enc_key, iv)
    tag = hmaclib.new(mac_key, salt + ct, hashlib.sha256).digest()
    return MAGIC + salt + tag + ct

def decrypt(blob: bytes, key_hex: str) -> bytes:
    if blob[:8] != MAGIC:
        raise InstallError("state blob: bad magic — not an installer-state file")
    salt, tag, ct = blob[8:24], blob[24:56], blob[56:]
    enc_key, iv, mac_key = _derive(key_hex, salt)
    if not hmaclib.compare_digest(tag, hmaclib.new(mac_key, salt + ct, hashlib.sha256).digest()):
        raise InstallError("state blob: HMAC mismatch — wrong BOXD_STATE_KEY or corrupted object")
    return _aes_ctr(ct, enc_key, iv)

def _object_url(cfg) -> str:
    return clouds.get(cfg).state_url(cfg)

def _tar_dir(state_dir: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for root, _, names in os.walk(state_dir):
            for n in sorted(names):
                p = os.path.join(root, n)
                tar.add(p, arcname=os.path.relpath(p, state_dir))
    return buf.getvalue()

def pull(cfg, state_dir: str) -> None:
    """Materialize state/ from the state bucket. Local-already-present +
    remote-present is a conflict (a prior push must have failed) and needs
    the operator's call."""
    key = _key()
    cloud = clouds.get(cfg)
    url = _object_url(cfg)
    have_local = os.path.exists(os.path.join(state_dir, "installer.json")) or \
        os.path.isdir(os.path.join(state_dir, "keys"))
    if cloud.object_size(cfg, url) is None:
        if have_local:
            log("no remote state; using existing local state/ (will be pushed after)")
        else:
            log("no remote or local state — fresh cluster")
        return
    if have_local:
        raise InstallError(
            f"BOTH local {state_dir} and remote {url} exist — a previous push likely "
            "failed. Resolve explicitly:\n"
            f"  ./boxd-install --state-dir {state_dir} state push   # local is newer: overwrite remote\n"
            f"  rm -rf {state_dir}   # remote is authoritative: drop local")
    tmp = state_dir + ".download.enc"
    os.makedirs(os.path.dirname(state_dir) or ".", exist_ok=True)
    cloud.download(cfg, url, tmp)
    with open(tmp, "rb") as fh:
        blob = fh.read()
    os.remove(tmp)
    plaintext = decrypt(blob, key)
    os.makedirs(state_dir, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(plaintext), mode="r:gz") as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(state_dir, filter="data")
        else:
            tar.extractall(state_dir)
    keys_dir = os.path.join(state_dir, "keys")
    if os.path.isdir(keys_dir):
        for n in os.listdir(keys_dir):
            if not n.endswith(".pub"):
                os.chmod(os.path.join(keys_dir, n), 0o600)
    log(f"state pulled from {url}")

def push(cfg, state_dir: str, delete_local: bool = True) -> None:
    key = _key()
    if not os.path.isdir(state_dir):
        log("no local state/ to push")
        return
    cloud = clouds.get(cfg)
    url = _object_url(cfg)
    blob = encrypt(_tar_dir(state_dir), key)
    assert decrypt(blob, key)
    tmp = state_dir + ".upload.enc"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(blob)
    try:
        cloud.copy(cfg, url, url + ".prev")
        cloud.upload(cfg, tmp, url)
        if cloud.object_size(cfg, url) != len(blob):
            raise InstallError(f"uploaded state size mismatch at {url} — local state kept")
    finally:
        os.remove(tmp)
    if delete_local:
        shutil.rmtree(state_dir)

@contextlib.contextmanager
def synced(cfg, state_dir: str, rescue_dir: str | None = None):
    """pull -> yield -> push+delete, the push attempted even on failure.

    A failed push never masks the command's own error and never deletes the
    local copy — the operator re-pushes with `state push`. With rescue_dir,
    the copy is moved there first: under ./boxd-install, state_dir is in a
    container that is about to be thrown away."""
    pull(cfg, state_dir)
    ok = False
    try:
        yield
        ok = True
    finally:
        try:
            push(cfg, state_dir)
        except Exception as push_err:
            kept = state_dir
            if rescue_dir and os.path.isdir(state_dir) and not os.path.exists(rescue_dir):
                shutil.move(state_dir, rescue_dir)
                kept = rescue_dir
            log(f"STATE PUSH FAILED — local copy KEPT at {kept}; run "
                f"`./boxd-install --state-dir {kept} state push` once "
                f"fixed: {push_err}")
            if ok:
                raise
