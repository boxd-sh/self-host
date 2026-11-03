"""Ensure the versioned assets zip is downloaded, verified and extracted at
state/assets/<sha>/.

A cluster without a license key downloads from boxd's release bucket. A
cluster with one downloads the releases boxd publishes for that key, under a
path made from it (base_url). Both have the same layout, one bundle per
release:
    bundles/<id>/boxd-assets-<id>.zip, release.json, release.json.sig
    channels/<channel>, channels/<channel>.<sequence>.sig
Every signature is checked with `openssl pkeyutl -verify -rawin` against the
public keys in release-keys/ before anything is unpacked. A license's path is
made from its key, so it is as secret as the key: never log or print it.

A cluster installed before license keys sets BOXD_PRODUCT_LICENSE and keeps
its old download path (the _legacy_* functions) until it is moved. That key
is an env var only, never in cluster.toml."""
import base64
import functools
import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
import zipfile

from . import license
from .util import InstallError, log

# BOXD_RELEASES_URL: a copy of boxd's releases (file:// works), to test a
# bundle; it stands for both places below. The signatures are checked all
# the same.
_COPY = os.environ.get("BOXD_RELEASES_URL", "").rstrip("/")
# Without a license key: boxd's release bucket.
PUBLIC_URL = _COPY or "https://storage.googleapis.com/boxd-releases"
# With one: the releases of every license, each under its own path (base_url).
LICENSE_URL = _COPY or "https://storage.googleapis.com/self-host-assets"
# The context prefix of a download path: the path names the key's releases
# without giving the key away.
DOWNLOAD_PREFIX = b"boxd-download-v1\0"
KEYS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "release-keys")
# The context prefix of a signed message: a channel signature can never pass
# as a release signature, and the other way round.
RELEASE_PREFIX = b"boxd-release-v1\0"
CHANNEL_PREFIX = b"boxd-channel-v1\0"
SHA_RE = re.compile(r"[0-9a-f]{40}")

def assets_dir(cache_dir: str, sha: str) -> str:
    return os.path.join(cache_dir, "assets", sha)

def _legacy() -> bool:
    return bool(os.environ.get("BOXD_PRODUCT_LICENSE"))

def download_id(key: str) -> str:
    """The path boxd publishes a license key's releases under."""
    return hashlib.sha256(DOWNLOAD_PREFIX + key.strip().encode()).hexdigest()

def base_url(cfg) -> str:
    """Where this cluster's releases are: with a license key,
    LICENSE_URL/<download_id(key)>, which carries a secret (never log or
    print it); without one, PUBLIC_URL."""
    if license.key_path(cfg) is None:
        return PUBLIC_URL
    return f"{LICENSE_URL}/{download_id(license.key_text(cfg))}"

def _licensed(url: str) -> bool:
    """url is in a license's path: the download id follows LICENSE_URL."""
    return re.match(re.escape(LICENSE_URL) + r"/[0-9a-f]{64}(/|$)", url) is not None

def resolve(cfg, state) -> str:
    """Turn a channel `assets_version` ("latest") into the sha it points at.
    A sha passes through untouched (no network). Swaps the sha into
    cfg.assets_version so everything after this sees a pinned version; the
    name stays in cfg.assets_channel."""
    v = cfg.assets_version
    if SHA_RE.fullmatch(v):
        return v
    sha = _legacy_resolve(v) if _legacy() else _signed_resolve(base_url(cfg), v, state)
    cfg.assets_channel, cfg.assets_version = v, sha
    return sha

def zip_url(cfg, sha: str) -> str:
    """The bundle's URL, for nodes that download it themselves (they check
    it against the manifest hashes ensure() verified). It carries a secret
    (the license path, or the old download key): never log or print it."""
    if _legacy():
        return _legacy_zip_url(sha)
    return f"{base_url(cfg)}/bundles/{sha}/boxd-assets-{sha}.zip"

def ensure(cfg, cache_dir: str, state) -> str:
    sha = resolve(cfg, state)
    dest = assets_dir(cache_dir, sha)
    if os.path.exists(os.path.join(dest, "MANIFEST.json")):
        if _legacy():
            verify_manifest(dest)
            log(f"assets {sha[:8]} present and verified")
            return dest
        if why := _signed_cache_problem(dest, sha):
            # Not removed here: the verified unpack below replaces it, so a
            # failed download (offline, 403) leaves the copy as it was.
            log(f"the local cache {dest} {why}: downloading assets {sha[:8]} again")
            try:
                return _signed_ensure(base_url(cfg), sha, cache_dir, dest)
            except DownloadFailed as e:
                # Only a failed download gets the cache advice. A refusal of
                # what came down (a bad signature, a zip that does not
                # match) and CannotCheck pass through unchanged.
                if why == NOT_SIGNED:
                    # An undamaged copy from the old path: deleting it would
                    # not help (a 403 carries PRIVATE_HINT).
                    raise
                raise DownloadFailed(
                    f"the local cache {dest} {why}, and downloading assets {sha[:8]} again "
                    f"failed ({e}). Delete {dest}, or run again when the download works.", e.private)
        else:
            log(f"assets {sha[:8]} present and verified (signed)")
            return dest

    tmp_zip = os.path.join(cache_dir, "assets", f".download-{sha}.zip")
    os.makedirs(os.path.dirname(tmp_zip), exist_ok=True)
    log(f"downloading assets {sha[:8]}…")
    if _legacy():
        try:
            _legacy_download(sha, tmp_zip)
        except BaseException:
            if os.path.exists(tmp_zip):
                os.remove(tmp_zip)
            raise
        return _extract(tmp_zip, sha, dest)
    return _signed_ensure(base_url(cfg), sha, cache_dir, dest)

def _signed_ensure(base: str, sha: str, cache_dir: str, dest: str) -> str:
    tmp_zip = os.path.join(cache_dir, "assets", f".download-{sha}.zip")
    os.makedirs(os.path.dirname(tmp_zip), exist_ok=True)
    try:
        body, sig, rel = _signed_download(base, sha, tmp_zip)
    except BaseException:
        if os.path.exists(tmp_zip):
            os.remove(tmp_zip)
        raise
    # The checked release.json and its signature go into the copy, so that a
    # later run can check it offline (_signed_cache_problem); MANIFEST.json
    # must be the one release.json names.
    return _extract(tmp_zip, sha, dest, manifest_sha256=rel["manifest_sha256"],
                    extra=dict(zip(SIGNED_MARKER, (body, sig))))

# --------------------------------------------------- the signed releases --

SIGNED_MARKER = (".release.json", ".release.json.sig")
# _signed_cache_problem's answer for a copy the old path unpacked: not
# damaged, just unsigned.
NOT_SIGNED = "was not unpacked from a signed bundle"

class CannotCheck(InstallError):
    """This installer cannot run the check (openssl, its release-keys/): that
    says nothing about the file checked, and downloading it again would not
    help."""

class UnknownKey(CannotCheck):
    """Signed with a key ID this installer does not have. From a fresh
    download: update the installer. From the local cache: the cache is
    damaged (_signed_cache_problem)."""

# GCS answers 401/403 when the bucket refuses the download.
PRIVATE_HINT = (" — the boxd release bucket refused the download; if it persists, tell boxd. "
                "A cluster installed before the release bucket sets BOXD_PRODUCT_LICENSE "
                "(the old download path) until your boxd contact moves it")
# A license's path answers 403 for an object it won't serve and for one
# that isn't there: either way, nothing is published for this key under
# that name.
LICENSE_HINT = (" — boxd has published no such release for this license key. Check that "
                "[license] key_file holds the key boxd sent you, and [cluster].assets_version; "
                "if both are right, ask your boxd contact")

class DownloadFailed(InstallError):
    """The network or the bucket said no (HTTP error, unreachable). Never a
    refusal of what was downloaded: those stay plain InstallError (or
    CannotCheck). private: a 401/403, which carries PRIVATE_HINT."""
    def __init__(self, msg: str, private: bool = False):
        super().__init__(msg)
        self.private = private

def _http_error(what: str, e: urllib.error.HTTPError, url: str) -> DownloadFailed:
    if _licensed(url):
        private = e.code in (401, 403, 404)
        hint = LICENSE_HINT
    else:
        private = e.code in (401, 403)
        hint = PRIVATE_HINT
    return DownloadFailed(f"{what}: download failed ({e.code})" + (hint if private else ""), private)

def _get(url: str, what: str, limit: int | None = None) -> bytes:
    # Channels are published no-store; the header asks any cache between here
    # and GCS not to serve a stale copy either.
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req) as resp:
            data = resp.read(limit) if limit else resp.read()
            length = resp.headers.get("Content-Length")
    except NETWORK_ERRORS as e:
        raise _download_error(what, e, url)
    # A connection closed early can end the body without an error: shorter
    # than its Content-Length is a cut-off download, not a bad file.
    if length and length.isdigit():
        want = min(int(length), limit) if limit else int(length)
        if len(data) < want:
            raise DownloadFailed(f"{what}: the download was cut off at {len(data)} of {want} bytes")
    return data

# What urlopen and reading its response raise for the network: an HTTP
# status (HTTPError), an unreachable host (URLError), a connection that
# dropped (http.client.RemoteDisconnected, IncompleteRead, a reset).
NETWORK_ERRORS = (http.client.HTTPException, OSError)

def _download_error(what: str, e: Exception, url: str) -> DownloadFailed:
    if isinstance(e, urllib.error.HTTPError):
        return _http_error(what, e, url)
    if isinstance(e, urllib.error.URLError):
        return DownloadFailed(f"{what}: download failed ({_no_url(e.reason)})")
    return DownloadFailed(f"{what}: download failed ({type(e).__name__}: {_no_url(e)})")

def _no_url(reason) -> str:
    """An error's text without the download path in it (a file:// copy
    names the missing file)."""
    return re.sub(r"[0-9a-f]{64}", "<license path>", str(reason))

def openssl_version() -> tuple[bool, str]:
    """(usable, what `openssl version` says). The check needs OpenSSL 3 or
    later (pkeyutl -rawin, Ed25519); macOS ships LibreSSL, which has neither.
    The tests use this too, to skip what needs it."""
    try:
        r = subprocess.run(["openssl", "version"], capture_output=True, text=True)
    except FileNotFoundError:
        return False, "no openssl"
    found = (r.stdout or r.stderr).strip() or "nothing"
    return r.returncode == 0 and bool(re.match(r"OpenSSL ([3-9]|[1-9][0-9])\.", found)), found

@functools.cache
def _check_openssl() -> None:
    """Say that openssl cannot run the check, rather than call a good
    release forged."""
    ok, found = openssl_version()
    if not ok:
        raise CannotCheck(f"the release check needs OpenSSL 3 (found {found}). On macOS: "
                           "brew install openssl@3, and put it first in PATH; or use ./boxd-install")

def verify_signature(message: bytes, sig_file: bytes, prefix: bytes, what: str) -> int:
    """Check sig_file ({"key_id": n, "sig": base64}) over prefix + message
    with release-keys/<n>.pem. Returns the key ID; raises on anything else."""
    try:
        sig = json.loads(sig_file)
        key_id, raw = sig["key_id"], base64.b64decode(sig["sig"], validate=True)
    except (ValueError, KeyError, TypeError):
        raise InstallError(f"{what}: the signature file is not valid")
    if type(key_id) is not int or key_id < 1 or len(raw) != 64:
        raise InstallError(f"{what}: the signature file is not valid")
    pem = os.path.join(KEYS_DIR, f"{key_id}.pem")
    if not os.path.exists(pem):
        raise UnknownKey(f"{what}: signed with key {key_id}, which this installer does not "
                           "have (release-keys/): update the installer")
    _check_openssl()
    with tempfile.TemporaryDirectory() as tmp:
        msg, sigpath = os.path.join(tmp, "message"), os.path.join(tmp, "sig")
        with open(msg, "wb") as fh:
            fh.write(prefix + message)
        with open(sigpath, "wb") as fh:
            fh.write(raw)
        r = subprocess.run(["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", pem, "-rawin",
                            "-in", msg, "-sigfile", sigpath], capture_output=True, text=True)
    if r.returncode == 0 and "Signature Verified Successfully" in r.stdout:
        return key_id
    if "Signature Verification Failure" not in r.stdout:
        # openssl could not run the check (the key did not load, ...): that
        # says nothing about the release.
        detail = (r.stderr or r.stdout).strip().splitlines()
        raise CannotCheck(f"{what}: openssl could not check the signature: "
                           f"{detail[0] if detail else f'exit {r.returncode}'}")
    raise InstallError(f"{what}: BAD signature. Nothing was installed. This is not a boxd "
                       "release: tell your boxd contact.")

def _signed_resolve(base: str, channel: str, state) -> str:
    """channels/<name> holds {"format": 1, "channel", "sha", "sequence", ...};
    channels/<name>.<sequence>.sig is its signature. The highest sequence
    number seen per channel of each download path is kept in the install
    state, and a lower one is refused: a replaced pointer cannot move this
    cluster back. Another path (another key) numbers its channels anew."""
    what = f"assets channel {channel!r}"
    try:
        body = _get(f"{base}/channels/{channel}", what, 4096)
    except DownloadFailed as e:
        if e.private and _licensed(base):
            raise  # the hint names assets_version already
        raise DownloadFailed(
            f"{e} — check [cluster].assets_version (\"latest\", or a release id from your "
            "release notes), or ask your boxd contact", e.private)
    try:
        p = json.loads(body)
        seq = p["sequence"]
        ok = (p["format"] == 1 and type(seq) is int and seq >= 1
              and isinstance(p["sha"], str) and SHA_RE.fullmatch(p["sha"]))
    except (ValueError, KeyError, TypeError):
        ok = False
    if not ok:
        raise InstallError(f"{what} is not a channel pointer: {body[:80]!r}")
    verify_signature(body, _get(f"{base}/channels/{channel}.{seq}.sig", f"{what} signature", 4096),
                     CHANNEL_PREFIX, what)
    if p.get("channel") != channel:
        raise InstallError(f"{what} holds the pointer of channel {p.get('channel')!r}")
    seen = state.data.setdefault("assets_channel_sequences", {})
    name = sequence_name(base, channel)
    if seq < seen.get(name, 0):
        raise InstallError(
            f"{what} is at sequence {seq}, but this cluster has seen {seen[name]}: the "
            "pointer is older than one it already followed, so it is refused. To go back "
            "to an older release on purpose, pin its sha in [cluster].assets_version.")
    seen[name] = seq
    state.save()
    log(f"assets channel {channel} -> {p['sha']} (sequence {seq}; pin this id to stay on it)")
    return p["sha"]

def sequence_name(base: str, channel: str) -> str:
    """The install state's name for a channel of one download path: a hash
    of the path, not the path itself."""
    return f"{hashlib.sha256(base.encode()).hexdigest()[:16]}/{channel}"

def _check_release(body: bytes, sig: bytes, sha: str, what: str | None = None) -> dict:
    """release.json's signature, format and sha; returns it."""
    what = what or f"assets {sha[:8]} release.json"
    verify_signature(body, sig, RELEASE_PREFIX, what)
    try:
        rel = json.loads(body)
        z = rel["zip"]
        ok = (rel["format"] == 1 and isinstance(z["sha256"], str) and type(z["size"]) is int
              and isinstance(rel["manifest_sha256"], str))
    except (ValueError, KeyError, TypeError):
        ok = False
    if not ok:
        raise InstallError(f"{what} is not valid")
    if rel.get("sha") != sha or z.get("name") != f"boxd-assets-{sha}.zip":
        raise InstallError(f"{what} is for {str(rel.get('sha'))[:12]}, not {sha}")
    return rel

def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def _signed_cache_problem(dest: str, sha: str) -> str | None:
    """None when an unpacked copy is the signed bundle: the release.json and
    signature checked when it was unpacked are next to it and verify again
    (offline), MANIFEST.json is the one release.json names, and the files
    match MANIFEST.json. Else why not; the caller downloads it again."""
    paths = [os.path.join(dest, n) for n in SIGNED_MARKER]
    if not any(os.path.lexists(p) for p in paths):
        return NOT_SIGNED  # the old path unpacked it: not damaged, just unsigned
    try:
        body, sig = (open(p, "rb").read() for p in paths)
    except OSError as e:
        # One of them is missing, or there and unreadable: damage.
        return f"cannot be read (its copy of release.json or its signature: {e})"
    try:
        rel = _check_release(body, sig, sha, what=f"the local cache {dest}: its copy of release.json")
    except UnknownKey:
        # Read from the local copy, not downloaded: its .sig is damaged.
        return "does not check out (its copy of release.json.sig names a key this installer does not have)"
    except CannotCheck:
        raise  # this installer's problem, not the cache's: say what it is
    except InstallError:
        # Bad JSON, a bad signature, another sha: the local copy is damaged.
        return "does not check out (its copy of release.json or its signature is damaged)"
    try:
        if _file_sha256(os.path.join(dest, "MANIFEST.json")) != rel["manifest_sha256"]:
            return "has a MANIFEST.json its signed release.json does not name"
        verify_manifest(dest)
    except InstallError as e:
        return f"does not check out ({e})"
    except OSError as e:
        return f"cannot be read ({e})"
    return None

def _signed_download(base: str, sha: str, tmp_zip: str) -> tuple[bytes, bytes, dict]:
    """release.json and its signature, then the zip they describe. Any
    failure raises before anything is unpacked. Returns release.json, its
    signature, and release.json decoded."""
    base = f"{base}/bundles/{sha}"
    what = f"assets {sha[:8]}"
    body = _get(f"{base}/release.json", f"{what} release.json", 1 << 16)
    sig = _get(f"{base}/release.json.sig", f"{what} signature", 4096)
    rel = _check_release(body, sig, sha)
    z = rel["zip"]
    h, size = hashlib.sha256(), 0
    # Only the network side becomes DownloadFailed; a local write error
    # (a full disk) stays what it is.
    with open(tmp_zip, "wb") as fh:
        try:
            resp = urllib.request.urlopen(f"{base}/{z['name']}")
        except NETWORK_ERRORS as e:
            raise _download_error(what, e, base)
        with resp:
            while True:
                try:
                    chunk = resp.read(1 << 20)
                except NETWORK_ERRORS as e:
                    raise _download_error(what, e, base)
                if not chunk:
                    break
                h.update(chunk)
                size += len(chunk)
                if size > z["size"]:
                    break
                fh.write(chunk)
    if size < z["size"]:
        # The stream ended early (a connection closed cleanly mid-body): a
        # failed download, not a refusal of the release.
        raise DownloadFailed(f"{what}: the download was cut off at {size} of {z['size']} bytes")
    if size != z["size"] or h.hexdigest() != z["sha256"]:
        raise InstallError(f"{what}: the zip does not match its signed release.json. "
                           "Nothing was installed.")
    return body, sig, rel

# --------------------------- the old download path (BOXD_PRODUCT_LICENSE) -----

LEGACY_BASE_URL = "https://storage.googleapis.com/self-host-assets"

def _license() -> str:
    license_key = os.environ.get("BOXD_PRODUCT_LICENSE", "")
    if not re.fullmatch(r"[0-9a-f]{64}", license_key):
        raise InstallError("BOXD_PRODUCT_LICENSE must be set (32-byte hex license)")
    return license_key

def _legacy_resolve(v: str) -> str:
    """`channels/<name>` next to the zips, a text object holding one sha,
    written AFTER the zip, so it never names a missing bundle."""
    url = f"{LEGACY_BASE_URL}/{_license()}/channels/{v}"
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req) as resp:
            body = resp.read(200).decode("ascii", "replace").strip()
    except urllib.error.HTTPError as e:
        raise InstallError(
            f"assets channel {v!r}: no boxd release on it for this license ({e.code}) — "
            "check [cluster].assets_version (\"latest\", or a 40-hex sha from your "
            "release notes) and BOXD_PRODUCT_LICENSE, or ask your boxd contact")
    if not SHA_RE.fullmatch(body):
        raise InstallError(f"assets channel {v!r} does not hold a git sha: {body[:80]!r}")
    log(f"assets channel {v} -> {body} (pin this sha to stay on it)")
    return body

def _legacy_zip_url(sha: str) -> str:
    """Carries the license: never log or print it."""
    return f"{LEGACY_BASE_URL}/{_license()}/boxd-assets-{sha}.zip"

def _legacy_download(sha: str, tmp_zip: str) -> None:
    try:
        with urllib.request.urlopen(_legacy_zip_url(sha)) as resp, open(tmp_zip, "wb") as fh:
            shutil.copyfileobj(resp, fh)
    except urllib.error.HTTPError as e:
        raise InstallError(
            f"assets download failed ({e.code}) — check BOXD_PRODUCT_LICENSE and "
            f"assets_version {sha}")

# ----------------------------------------------------------------- unpacking --

def _extract(tmp_zip: str, sha: str, dest: str, *, manifest_sha256: str | None = None,
             extra: dict | None = None) -> str:
    """Unpack, check, then replace dest in one rename: until then an older
    copy at dest stays as it was. manifest_sha256 (the signed path): the
    MANIFEST.json release.json names. extra: files to add before the rename."""
    tmp_dir = dest + ".extracting"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    try:
        with zipfile.ZipFile(tmp_zip) as zf:
            for info in zf.infolist():
                name = info.filename
                if name.startswith("/") or ".." in name.split("/"):
                    raise InstallError(f"assets zip has an unsafe path: {name}")
                zf.extract(info, tmp_dir)
                mode = (info.external_attr >> 16) & 0o7777
                if mode and not name.endswith("/"):
                    os.chmod(os.path.join(tmp_dir, name), mode)
        inner = os.path.join(tmp_dir, "boxd-assets")
        if not os.path.isdir(inner):
            raise InstallError("assets zip missing top-level boxd-assets/ directory")

        if manifest_sha256 and _file_sha256(os.path.join(inner, "MANIFEST.json")) != manifest_sha256:
            raise InstallError(f"assets {sha[:8]}: MANIFEST.json is not the one its signed "
                               "release.json names. Nothing was installed.")
        manifest = _load_manifest(inner)
        if bundle_id(manifest) != sha:
            raise InstallError(f"assets manifest is for bundle {bundle_id(manifest)} "
                               f"(boxd commit {manifest['git_sha']}), not the pinned {sha}")
        verify_manifest(inner)
        for name, data in (extra or {}).items():
            with open(os.path.join(inner, name), "wb") as fh:
                fh.write(data)

        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.rmtree(dest, ignore_errors=True)
        os.rename(inner, dest)
    finally:
        os.remove(tmp_zip)
        shutil.rmtree(tmp_dir, ignore_errors=True)
    log(f"assets {sha[:8]} extracted to {dest}")
    return dest

def _load_manifest(root: str) -> dict:
    with open(os.path.join(root, "MANIFEST.json")) as fh:
        return json.load(fh)

def bundle_id(manifest: dict) -> str:
    """What a release is pinned and published by: its manifest's bundle_id.
    A manifest without one is known by its git_sha."""
    return manifest.get("bundle_id", manifest["git_sha"])

def features(root: str) -> list[str]:
    """The paid features a bundle's binaries are built with ("bucket": the
    storage engine). A manifest from before the field has none."""
    got = _load_manifest(root).get("features", [])
    return [f for f in got if isinstance(f, str)] if isinstance(got, list) else []

def verify_manifest(root: str) -> None:
    manifest = _load_manifest(root)
    for rel, want in manifest["files"].items():
        path = os.path.join(root, rel)
        if not os.path.exists(path):
            raise InstallError(f"assets corrupted: missing {rel}")
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != want:
            raise InstallError(f"assets corrupted: checksum mismatch on {rel}")

def console_tarball(root: str) -> str:
    """One-file SPA artifact for ansible's unarchive — per-file copy over the
    IAP/SSM tunnel costs several round-trips per file and took minutes."""
    tarball = os.path.join(root, "console.tar.gz")
    if not os.path.exists(tarball):
        import tarfile
        src = os.path.join(root, "console")
        with tarfile.open(tarball + ".tmp", "w:gz") as tar:
            for name in sorted(os.listdir(src)):
                tar.add(os.path.join(src, name), arcname=name)
        os.rename(tarball + ".tmp", tarball)
    return tarball

def computer_image(root: str) -> tuple[str, str]:
    m = _load_manifest(root)["computer_image"]
    return m["name"], m["tag"]

def host_kernel_release(root: str) -> str:
    """Exact kernel release workers must run ("" = assets ship no kernel).
    Manifest field when present; derived from the deb filename for zips
    published before the field existed."""
    m = _load_manifest(root)
    if "host_kernel" in m:
        return m["host_kernel"]["release"]
    import glob
    debs = sorted(glob.glob(os.path.join(root, "kernel", "linux-image-*.deb")))
    if not debs:
        return ""
    return re.sub(r"^linux-image-", "", os.path.basename(debs[0])).split("_")[0]
