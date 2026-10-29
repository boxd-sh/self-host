"""Bundles from the signed release bucket: the channel pointer, its
sequence number, release.json and the zip, checked before anything is
unpacked. A fake bucket (files in a temporary directory, as file:// URLs) and
a local key pair stand in for gs://boxd-releases and the release key.
Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import base64
import contextlib
import hashlib
import http.client
import io
import json
import os
import subprocess
import errno
import http.server
import shutil
import tempfile
import threading
import tomllib
import unittest
import urllib.error
import zipfile
from unittest import mock

from boxd_installer import ansible, assets, config
from boxd_installer.state import State
from boxd_installer.util import InstallError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_KEYS_DIR = assets.KEYS_DIR  # before any test patches it

def _openssl_has_ed25519() -> bool:
    """The openssl the installer accepts (assets.openssl_version, the same
    check), and Ed25519 keys for the test signer."""
    if not assets.openssl_version()[0]:
        return False
    k = subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519"], capture_output=True)
    return k.returncode == 0
HAVE_ED25519 = _openssl_has_ed25519()
NEEDS_ED25519 = unittest.skipUnless(HAVE_ED25519, "openssl lacks OpenSSL 3 / Ed25519")
SHA = "0123456789abcdef0123456789abcdef01234567"
OLD = "fedcba9876543210fedcba9876543210fedcba98"

def doc(version: str) -> dict:
    with open(os.path.join(ROOT, "examples", "aws", "cluster.toml"), "rb") as fh:
        d = tomllib.load(fh)
    d["cluster"]["assets_version"] = version
    return d

class Bucket:
    """bundles/ and channels/ in a directory, signed with a fresh key."""

    def __init__(self, root: str):
        self.root = os.path.join(root, "bucket")
        self.keys = os.path.join(root, "keys")
        os.makedirs(self.keys)
        self.key = os.path.join(root, "signing.pem")
        subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", self.key], check=True)
        subprocess.run(["openssl", "pkey", "-in", self.key, "-pubout",
                        "-out", os.path.join(self.keys, "1.pem")], check=True)
        self.tmp = root

    def put(self, name: str, data: bytes) -> None:
        path = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)

    def get(self, name: str) -> bytes:
        with open(os.path.join(self.root, name), "rb") as fh:
            return fh.read()

    def sign(self, data: bytes, prefix: bytes, key_id: int = 1) -> bytes:
        msg, sig = os.path.join(self.tmp, "msg"), os.path.join(self.tmp, "sig")
        with open(msg, "wb") as fh:
            fh.write(prefix + data)
        subprocess.run(["openssl", "pkeyutl", "-sign", "-rawin", "-inkey", self.key,
                        "-in", msg, "-out", sig], check=True)
        with open(sig, "rb") as fh:
            return json.dumps({"key_id": key_id, "sig": base64.b64encode(fh.read()).decode()}).encode()

    def bundle(self, sha: str = SHA, *, release_sha: str | None = None,
               size_delta: int = 0, manifest_sha256: str | None = None,
               extra_entries: dict | None = None, sha_field: str | None = None) -> bytes:
        """A two-file zip with its MANIFEST.json, release.json and signature."""
        files = {"bin/boxd-worker": b"worker", "kernel/k.deb": b"kernel"}
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for rel, data in files.items():
                zf.writestr("boxd-assets/" + rel, data)
            manifest = json.dumps({
                "git_sha": sha, "computer_image": {"name": "c", "tag": "t"},
                "host_kernel": {"release": "6.17.0-boxd+"},
                "files": {rel: hashlib.sha256(d).hexdigest() for rel, d in files.items()}}).encode()
            zf.writestr("boxd-assets/MANIFEST.json", manifest)
            for name, data in (extra_entries or {}).items():
                zf.writestr(name, data)
        z = buf.getvalue()
        self.put(f"bundles/{sha}/boxd-assets-{sha}.zip", z)
        rel = json.dumps({"format": 1, "sha": sha_field or release_sha or sha, "built_at": "2026-10-21T00:00:00Z",
                          "zip": {"name": f"boxd-assets-{release_sha or sha}.zip",
                                  "sha256": hashlib.sha256(z).hexdigest(), "size": len(z) + size_delta},
                          "host_kernel": "6.17.0-boxd+",
                          "manifest_sha256": manifest_sha256 or hashlib.sha256(manifest).hexdigest()},
                         separators=(",", ":")).encode()
        self.put(f"bundles/{sha}/release.json", rel)
        self.put(f"bundles/{sha}/release.json.sig", self.sign(rel, assets.RELEASE_PREFIX))
        return rel

    def channel(self, name: str, sha: str, seq: int, *, signed_as: str | None = None) -> bytes:
        p = json.dumps({"format": 1, "channel": signed_as or name, "sha": sha, "sequence": seq,
                        "published_at": "2026-10-21T00:00:00Z", "by": "t@example.com"},
                       separators=(",", ":")).encode()
        self.put(f"channels/{name}", p)
        self.put(f"channels/{name}.{seq}.sig", self.sign(p, assets.CHANNEL_PREFIX))
        return p

@NEEDS_ED25519
class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bucket = Bucket(self.tmp.name)
        self.cache = os.path.join(self.tmp.name, "cache")
        self.state = State(os.path.join(self.tmp.name, "state"))
        env = {k: v for k, v in os.environ.items() if k != "BOXD_PRODUCT_LICENSE"}
        for p in (mock.patch.dict(os.environ, env, clear=True),
                  mock.patch.object(assets, "BASE_URL", "file://" + self.bucket.root),
                  mock.patch.object(assets, "KEYS_DIR", self.bucket.keys),
                  contextlib.redirect_stderr(io.StringIO())):
            p.__enter__()
            self.addCleanup(p.__exit__, None, None, None)

    def ensure(self, version: str):
        cfg = config.Config(doc(version))
        return cfg, assets.ensure(cfg, self.cache, self.state)

    def unpacked(self, sha: str = SHA) -> bool:
        return os.path.exists(assets.assets_dir(self.cache, sha))

    def refused(self, version: str, pattern: str, sha: str = SHA):
        with self.assertRaisesRegex(InstallError, pattern):
            self.ensure(version)
        self.assertFalse(self.unpacked(sha))
        self.assertEqual(os.listdir(os.path.join(self.cache, "assets"))
                         if os.path.isdir(os.path.join(self.cache, "assets")) else [], [])

class GoodBundles(Case):
    def test_a_good_bundle_on_a_channel_installs(self):
        self.bucket.bundle()
        self.bucket.channel("beta", SHA, 3)
        cfg, root = self.ensure("beta")
        self.assertEqual((cfg.assets_version, cfg.assets_channel), (SHA, "beta"))
        self.assertEqual(root, assets.assets_dir(self.cache, SHA))
        with open(os.path.join(root, "bin", "boxd-worker"), "rb") as fh:
            self.assertEqual(fh.read(), b"worker")
        # The sequence number is in the install state, on disk.
        self.assertEqual(State(self.state.dir).data["assets_channel_sequences"], {"beta": 3})

    def test_a_pinned_sha_works_without_a_channel(self):
        self.bucket.bundle()
        self.assertFalse(os.path.exists(os.path.join(self.bucket.root, "channels")))
        cfg, root = self.ensure(SHA)
        self.assertEqual((cfg.assets_version, cfg.assets_channel), (SHA, None))
        self.assertTrue(os.path.exists(os.path.join(root, "MANIFEST.json")))
        self.assertNotIn("assets_channel_sequences", self.state.data)

    def test_the_same_or_a_higher_sequence_number_is_followed(self):
        self.bucket.bundle()
        self.bucket.bundle(OLD)
        self.bucket.channel("latest", OLD, 4)
        self.ensure("latest")
        self.ensure("latest")
        self.bucket.channel("latest", SHA, 5)
        cfg, _ = self.ensure("latest")
        self.assertEqual(cfg.assets_version, SHA)
        self.assertEqual(self.state.data["assets_channel_sequences"], {"latest": 5})

    def test_nodes_download_from_the_release_bucket(self):
        self.assertEqual(assets.zip_url(SHA),
                         f"file://{self.bucket.root}/bundles/{SHA}/boxd-assets-{SHA}.zip")
        with open(os.path.join(self.tmp.name, "MANIFEST.json"), "w") as fh:
            json.dump({"files": {}}, fh)
        self.assertEqual(ansible.node_assets_vars(SHA, self.tmp.name)["boxd_assets_url"],
                         assets.zip_url(SHA))

class Refused(Case):
    def test_a_bad_release_signature(self):
        rel = self.bucket.bundle()
        self.bucket.put(f"bundles/{SHA}/release.json", rel.replace(b'"format":1', b'"format":1 '))
        self.refused(SHA, "BAD signature")

    def test_a_signature_from_another_key(self):
        rel = self.bucket.bundle()
        self.bucket.key = os.path.join(self.tmp.name, "other.pem")
        subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", self.bucket.key], check=True)
        self.bucket.put(f"bundles/{SHA}/release.json.sig", self.bucket.sign(rel, assets.RELEASE_PREFIX))
        self.refused(SHA, "BAD signature")

    def test_an_unknown_key_id(self):
        rel = self.bucket.bundle()
        self.bucket.put(f"bundles/{SHA}/release.json.sig", self.bucket.sign(rel, assets.RELEASE_PREFIX, 2))
        self.refused(SHA, "signed with key 2")

    def test_a_changed_zip(self):
        self.bucket.bundle()
        z = bytearray(self.bucket.get(f"bundles/{SHA}/boxd-assets-{SHA}.zip"))
        z[-1] ^= 1
        self.bucket.put(f"bundles/{SHA}/boxd-assets-{SHA}.zip", bytes(z))
        self.refused(SHA, "does not match its signed release.json")

    def test_a_wrong_size(self):
        # The sha256 is right and the signed size is one byte short of the
        # zip: this fails only through the size check.
        self.bucket.bundle(size_delta=-1)
        self.refused(SHA, "does not match its signed release.json")

    def test_a_body_shorter_than_the_signed_size_is_a_cut_off_download(self):
        # The stream ends before the size in release.json: from here that is
        # a cut-off download (the release itself may be fine), not a refusal.
        self.bucket.bundle(size_delta=1)
        with self.assertRaises(assets.DownloadFailed) as cm:
            self.ensure(SHA)
        z = len(self.bucket.get(f"bundles/{SHA}/boxd-assets-{SHA}.zip"))
        self.assertIn(f"the download was cut off at {z} of {z + 1} bytes", str(cm.exception))
        self.assertFalse(self.unpacked())

    def test_a_manifest_release_json_does_not_name(self):
        self.bucket.bundle(manifest_sha256="0" * 64)
        self.refused(SHA, "MANIFEST.json is not the one its signed release.json names")

    def test_the_download_stops_at_the_signed_size(self):
        # A server that sends more than release.json says (here 64 MiB, as
        # good as endless): the download stops reading one chunk past the
        # signed size instead of filling the disk.
        self.bucket.bundle()
        size = len(self.bucket.get(f"bundles/{SHA}/boxd-assets-{SHA}.zip"))
        served = []

        class Endless(io.RawIOBase):
            def read(self, n=-1):
                left = (64 << 20) - sum(served)
                # read() / read(-1) gets all of it, as an endless body would.
                n = left if n is None or n < 0 else min(n, left)
                if n <= 0:
                    return b""
                served.append(n)
                return b"\0" * n
            def __enter__(self): return self
            def __exit__(self, *a): return False

        real = urllib.request.urlopen
        def urlopen(url, *a, **kw):
            if isinstance(url, str) and url.endswith(".zip"):
                return Endless()
            return real(url, *a, **kw)
        with mock.patch.object(assets.urllib.request, "urlopen", urlopen):
            self.refused(SHA, "does not match its signed release.json")
        self.assertLessEqual(sum(served), size + (1 << 20))

    def test_openssl_failing_is_not_called_a_forgery(self):
        self.bucket.bundle()
        with open(os.path.join(self.bucket.keys, "1.pem"), "w") as fh:
            fh.write("not a key\n")
        with self.assertRaises(InstallError) as cm:
            self.ensure(SHA)
        self.assertIn("openssl could not check the signature", str(cm.exception))
        self.assertNotIn("BAD signature", str(cm.exception))
        self.assertFalse(self.unpacked())

    def test_a_wrong_sha_in_release_json(self):
        # A good bundle of OLD, served as SHA's.
        self.bucket.bundle(SHA, release_sha=OLD)
        self.refused(SHA, f"release.json is for {OLD[:12]}, not {SHA}")

    def test_a_channel_signature_on_a_release(self):
        rel = self.bucket.bundle()
        self.bucket.put(f"bundles/{SHA}/release.json.sig", self.bucket.sign(rel, assets.CHANNEL_PREFIX))
        self.refused(SHA, "BAD signature")

    def test_a_release_signature_on_a_channel(self):
        self.bucket.bundle()
        p = self.bucket.channel("beta", SHA, 1)
        self.bucket.put("channels/beta.1.sig", self.bucket.sign(p, assets.RELEASE_PREFIX))
        self.refused("beta", "BAD signature")
        self.assertNotIn("assets_channel_sequences", self.state.data)

    def test_a_changed_pointer(self):
        self.bucket.bundle()
        self.bucket.bundle(OLD)
        p = self.bucket.channel("beta", SHA, 1)
        self.bucket.put("channels/beta", p.replace(SHA.encode(), OLD.encode()))
        self.refused("beta", "BAD signature", OLD)

    def test_the_pointer_of_another_channel(self):
        self.bucket.bundle()
        self.bucket.channel("latest", SHA, 9, signed_as="beta")
        self.refused("latest", "holds the pointer of channel 'beta'")

    def test_a_lower_sequence_number(self):
        self.bucket.bundle()
        self.bucket.bundle(OLD)
        self.bucket.channel("latest", SHA, 5)
        self.ensure("latest")
        # An attacker who can write the bucket puts back an older, validly
        # signed pointer.
        self.bucket.channel("latest", OLD, 4)
        with self.assertRaisesRegex(InstallError, "sequence 4, but this cluster has seen 5"):
            self.ensure("latest")
        self.assertFalse(self.unpacked(OLD))
        self.assertEqual(self.state.data["assets_channel_sequences"], {"latest": 5})

    def test_a_missing_channel_names_what_to_check(self):
        with self.assertRaises(InstallError) as cm:
            self.ensure("beta")
        self.assertIn("assets_version", str(cm.exception))
        self.assertNotIn("release-channel", str(cm.exception))

    def test_a_missing_signature(self):
        self.bucket.bundle()
        os.remove(os.path.join(self.bucket.root, "bundles", SHA, "release.json.sig"))
        self.refused(SHA, "signature: download failed")

class Cache(Case):
    """state/assets/<sha>/ from an earlier run."""

    def test_a_signed_copy_is_used_offline(self):
        self.bucket.bundle()
        self.ensure(SHA)
        shutil.rmtree(self.bucket.root)
        _, root = self.ensure(SHA)
        self.assertTrue(os.path.exists(os.path.join(root, "MANIFEST.json")))

    def test_a_copy_the_old_path_unpacked_is_downloaded_again(self):
        self.bucket.bundle()
        self.ensure(SHA)
        dest = assets.assets_dir(self.cache, SHA)
        for name in assets.SIGNED_MARKER:
            os.remove(os.path.join(dest, name))
        self.assert_downloaded_again("was not unpacked from a signed bundle")

    def assert_downloaded_again(self, why: str):
        dest = assets.assets_dir(self.cache, SHA)
        with open(os.path.join(dest, "planted"), "w") as fh:
            fh.write("x")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            _, root = self.ensure(SHA)
        self.assertIn(f"the local cache {dest} {why}", err.getvalue())
        self.assertFalse(os.path.exists(os.path.join(root, "planted")))
        assets.verify_manifest(root)
        self.assertIsNone(assets._signed_cache_problem(root, SHA))

    def test_a_changed_file_is_downloaded_again(self):
        self.bucket.bundle()
        self.ensure(SHA)
        with open(os.path.join(assets.assets_dir(self.cache, SHA), "bin", "boxd-worker"), "wb") as fh:
            fh.write(b"changed")
        self.assert_downloaded_again("does not check out")

    def test_a_changed_file_with_its_hash_in_manifest_is_downloaded_again(self):
        self.bucket.bundle()
        self.ensure(SHA)
        dest = assets.assets_dir(self.cache, SHA)
        with open(os.path.join(dest, "bin", "boxd-worker"), "wb") as fh:
            fh.write(b"changed")
        m = json.load(open(os.path.join(dest, "MANIFEST.json")))
        m["files"]["bin/boxd-worker"] = hashlib.sha256(b"changed").hexdigest()
        with open(os.path.join(dest, "MANIFEST.json"), "w") as fh:
            json.dump(m, fh)
        self.assert_downloaded_again("has a MANIFEST.json its signed release.json does not name")

    def test_an_old_path_copy_with_the_public_marker_is_downloaded_again(self):
        # An unsigned tree, plus the (public) release.json and .sig copied in.
        self.bucket.bundle()
        self.ensure(SHA)
        dest = assets.assets_dir(self.cache, SHA)
        with open(os.path.join(dest, "MANIFEST.json")) as fh:
            m = json.load(fh)
        m["extra"] = "legacy"
        with open(os.path.join(dest, "MANIFEST.json"), "w") as fh:
            json.dump(m, fh)
        self.assert_downloaded_again("has a MANIFEST.json its signed release.json does not name")

    def test_a_corrupt_marker_is_downloaded_again(self):
        for name, data in ((".release.json.sig", b"\0garbage"), (".release.json", b"{")):
            with self.subTest(name=name):
                shutil.rmtree(self.cache, ignore_errors=True)
                shutil.rmtree(self.bucket.root, ignore_errors=True)
                self.bucket.bundle()
                self.ensure(SHA)
                with open(os.path.join(assets.assets_dir(self.cache, SHA), name), "wb") as fh:
                    fh.write(data)
                self.assert_downloaded_again("does not check out")

    def test_an_installer_that_cannot_check_says_so(self):
        # Offline (no bucket), so a re-download could not hide the cause.
        self.bucket.bundle()
        self.ensure(SHA)
        dest = assets.assets_dir(self.cache, SHA)
        shutil.rmtree(self.bucket.root)
        pem = os.path.join(self.bucket.keys, "1.pem")
        good = open(pem).read()
        # (A key ID the installer lacks, read from the cache, is the cache's
        # damage: test_an_unknown_key_in_the_cache_*.)
        cases = [("a key that does not load", lambda: open(pem, "w").write("not a key\n"),
                  "openssl could not check the signature")]
        for name, break_it, message in cases:
            with self.subTest(name):
                break_it()
                with self.assertRaisesRegex(assets.CannotCheck, message) as cm:
                    self.ensure(SHA)
                self.assertIn(f"the local cache {dest}", str(cm.exception))
                with open(pem, "w") as fh:
                    fh.write(good)
        with self.subTest("an openssl that cannot check"):
            version = subprocess.CompletedProcess([], 0, "LibreSSL 3.8.2\n", "")
            assets._check_openssl.cache_clear()
            self.addCleanup(assets._check_openssl.cache_clear)
            with mock.patch.object(assets.subprocess, "run", return_value=version):
                with self.assertRaisesRegex(assets.CannotCheck, "needs OpenSSL 3"):
                    self.ensure(SHA)
        # The cached copy is untouched, and still good.
        assets._check_openssl.cache_clear()
        self.assertIsNone(assets._signed_cache_problem(dest, SHA))

    def flip_cached_key_id(self) -> str:
        dest = assets.assets_dir(self.cache, SHA)
        path = os.path.join(dest, ".release.json.sig")
        with open(path) as fh:
            sig = json.load(fh)
        sig["key_id"] = 9
        with open(path, "w") as fh:
            json.dump(sig, fh)
        return dest

    def test_an_unknown_key_in_the_cache_is_local_damage(self):
        self.bucket.bundle()
        self.ensure(SHA)
        self.flip_cached_key_id()
        self.assert_downloaded_again(
            "does not check out (its copy of release.json.sig names a key this installer does not have)")

    def test_an_unknown_key_in_the_cache_offline_names_the_cache(self):
        self.bucket.bundle()
        self.ensure(SHA)
        dest = self.flip_cached_key_id()
        shutil.rmtree(self.bucket.root)
        with self.assertRaises(InstallError) as cm:
            self.ensure(SHA)
        self.assertNotIsInstance(cm.exception, assets.CannotCheck)
        msg = str(cm.exception)
        self.assertIn(f"the local cache {dest} does not check out", msg)
        self.assertIn(f"Delete {dest}, or run again when the download works", msg)
        self.assertNotIn("update the installer", msg)

    def test_an_unknown_key_in_a_download_says_update_the_installer(self):
        rel = self.bucket.bundle()
        self.bucket.put(f"bundles/{SHA}/release.json.sig", self.bucket.sign(rel, assets.RELEASE_PREFIX, 9))
        with self.assertRaisesRegex(assets.CannotCheck, "signed with key 9.*update the installer"):
            self.ensure(SHA)

    def test_a_refused_re_download_is_not_told_to_delete_the_cache(self):
        # A damaged cache, and the bundle that comes down again is not a boxd
        # release: the refusal stands as it is, with no cache advice.
        rel = self.bucket.bundle()
        self.ensure(SHA)
        dest = self.flip_cached_key_id()
        self.bucket.put(f"bundles/{SHA}/release.json", rel.replace(b'"format":1', b'"format":1 '))
        with self.assertRaises(InstallError) as cm:
            self.ensure(SHA)
        self.assertNotIsInstance(cm.exception, assets.DownloadFailed)
        self.assertIn("BAD signature", str(cm.exception))
        self.assertIn("This is not a boxd release", str(cm.exception))
        self.assertNotIn("Delete", str(cm.exception))
        self.assertTrue(os.path.exists(os.path.join(dest, "MANIFEST.json")))

    def test_an_old_path_copy_and_a_403_gets_the_license_hint(self):
        self.bucket.bundle()
        self.ensure(SHA)
        dest = assets.assets_dir(self.cache, SHA)
        for name in assets.SIGNED_MARKER:
            os.remove(os.path.join(dest, name))
        err = urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
        with mock.patch.object(assets.urllib.request, "urlopen", mock.Mock(side_effect=err)):
            with self.assertRaises(assets.DownloadFailed) as cm:
                self.ensure(SHA)
        self.assertTrue(cm.exception.private)
        self.assertIn("sets BOXD_PRODUCT_LICENSE (the old download path)", str(cm.exception))
        self.assertNotIn("Delete", str(cm.exception))
        with open(os.path.join(dest, "bin", "boxd-worker"), "rb") as fh:
            self.assertEqual(fh.read(), b"worker")

    def test_a_failed_download_keeps_the_copy(self):
        self.bucket.bundle()
        self.ensure(SHA)
        dest = assets.assets_dir(self.cache, SHA)
        for name in assets.SIGNED_MARKER:
            os.remove(os.path.join(dest, name))
        shutil.rmtree(self.bucket.root)  # offline, or a 403
        with self.assertRaisesRegex(InstallError, "download failed"):
            self.ensure(SHA)
        with open(os.path.join(dest, "bin", "boxd-worker"), "rb") as fh:
            self.assertEqual(fh.read(), b"worker")
        assets.verify_manifest(dest)

    def test_the_old_path_still_uses_its_copy(self):
        self.bucket.bundle()
        self.ensure(SHA)
        dest = assets.assets_dir(self.cache, SHA)
        for name in assets.SIGNED_MARKER:
            os.remove(os.path.join(dest, name))
        with mock.patch.dict(os.environ, {"BOXD_PRODUCT_LICENSE": "ab" * 32}), \
                mock.patch.object(assets.urllib.request, "urlopen",
                                  mock.Mock(side_effect=AssertionError("no download"))):
            cfg = config.Config(doc(SHA))
            self.assertEqual(assets.ensure(cfg, self.cache, self.state), dest)

class ReDownload(Case):
    """After a damaged cache, the bundle is downloaded again. Only a failed
    download gets the cache advice ("Delete <cache>"); every refusal of what
    came down, and CannotCheck, passes through as it is, and the damaged copy
    stays where it is (only a verified unpack replaces it)."""

    def damage(self) -> str:
        """A good bundle unpacked, then its cached .sig damaged (key ID 9)."""
        self.bucket.bundle()
        self.ensure(SHA)
        dest = assets.assets_dir(self.cache, SHA)
        path = os.path.join(dest, ".release.json.sig")
        sig = json.load(open(path))
        sig["key_id"] = 9
        with open(path, "w") as fh:
            json.dump(sig, fh)
        return dest

    def passes_through(self, message: str, exc=InstallError, **kw):
        dest = self.damage()
        before = {n: open(os.path.join(dest, n), "rb").read() for n in ("bin/boxd-worker", "MANIFEST.json")}
        self.change_bucket(**kw)
        with self.assertRaises(exc) as cm:
            self.ensure(SHA)
        self.assertNotIsInstance(cm.exception, assets.DownloadFailed)
        self.assertRegex(str(cm.exception), message)
        self.assertNotIn("Delete", str(cm.exception))
        self.assertNotIn("local cache", str(cm.exception))
        for n, data in before.items():
            with open(os.path.join(dest, n), "rb") as fh:
                self.assertEqual(fh.read(), data, n)

    def change_bucket(self, *, kind: str):
        b = self.bucket
        zpath = f"bundles/{SHA}/boxd-assets-{SHA}.zip"
        if kind == "zip":
            z = bytearray(b.get(zpath))
            z[-1] ^= 1
            b.put(zpath, bytes(z))
        elif kind == "size":
            b.bundle(size_delta=-1)
        elif kind == "manifest":
            b.bundle(manifest_sha256="0" * 64)
        elif kind == "unsafe":
            b.bundle(extra_entries={"boxd-assets/../../evil": b"x"})
        elif kind == "sha":
            # Only the sha field: the zip name is still SHA's, so this fails
            # through the sha check alone.
            b.bundle(sha_field=OLD)
        elif kind == "bad-signature":
            rel = b.get(f"bundles/{SHA}/release.json")
            b.put(f"bundles/{SHA}/release.json", rel.replace(b'"format":1', b'"format":1 '))
        elif kind == "unknown-key":
            rel = b.get(f"bundles/{SHA}/release.json")
            b.put(f"bundles/{SHA}/release.json.sig", b.sign(rel, assets.RELEASE_PREFIX, 9))
        elif kind == "cannot-check":
            with open(os.path.join(b.keys, "1.pem"), "w") as fh:
                fh.write("not a key\n")

    def test_a_zip_that_does_not_match(self):
        self.passes_through("the zip does not match its signed release.json", kind="zip")

    def test_a_wrong_size(self):
        self.passes_through("the zip does not match its signed release.json", kind="size")

    def test_a_manifest_release_json_does_not_name(self):
        self.passes_through("MANIFEST.json is not the one its signed release.json names", kind="manifest")

    def test_an_unsafe_zip_path(self):
        self.passes_through("assets zip has an unsafe path", kind="unsafe")
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "evil")))

    def test_a_release_json_for_another_sha(self):
        self.passes_through(f"is for {OLD[:12]}, not {SHA}", kind="sha")

    def test_a_bad_signature(self):
        self.passes_through("BAD signature.*This is not a boxd release", kind="bad-signature")

    def test_an_unknown_key_in_the_download(self):
        self.passes_through("signed with key 9.*update the installer", assets.UnknownKey, kind="unknown-key")

    def test_an_installer_that_cannot_check(self):
        self.passes_through("openssl could not check the signature", assets.CannotCheck, kind="cannot-check")

    def zip_fails(self, error):
        real = urllib.request.urlopen
        def urlopen(url, *a, **kw):
            if isinstance(url, str) and url.endswith(".zip"):
                raise error
            return real(url, *a, **kw)
        return mock.patch.object(assets.urllib.request, "urlopen", urlopen)

    def test_a_failed_zip_download_gets_the_cache_advice(self):
        for name, error, private in (
                ("403", urllib.error.HTTPError("u", 403, "Forbidden", {}, None), True),
                ("unreachable", urllib.error.URLError("connection refused"), False)):
            with self.subTest(name):
                shutil.rmtree(self.cache, ignore_errors=True)
                shutil.rmtree(self.bucket.root, ignore_errors=True)
                dest = self.damage()
                with self.zip_fails(error), self.assertRaises(assets.DownloadFailed) as cm:
                    self.ensure(SHA)
                msg = str(cm.exception)
                self.assertIn(f"the local cache {dest} does not check out", msg)
                self.assertIn(f"Delete {dest}, or run again when the download works", msg)
                # The rewrap keeps .private (and the 403 hint).
                self.assertIs(cm.exception.private, private)
                self.assertEqual("sets BOXD_PRODUCT_LICENSE (the old download path)" in msg, private)
                self.assertTrue(os.path.exists(os.path.join(dest, "MANIFEST.json")))

    def test_an_unreadable_marker_is_damage_not_unsigned(self):
        for name, break_it in (
                ("a directory", lambda p: (os.remove(p), os.mkdir(p))),
                ("one missing", lambda p: os.remove(p))):
            with self.subTest(name):
                shutil.rmtree(self.cache, ignore_errors=True)
                shutil.rmtree(self.bucket.root, ignore_errors=True)
                self.bucket.bundle()
                self.ensure(SHA)
                dest = assets.assets_dir(self.cache, SHA)
                break_it(os.path.join(dest, ".release.json"))
                self.assertNotEqual(assets._signed_cache_problem(dest, SHA), assets.NOT_SIGNED)
                self.assertIn("cannot be read", assets._signed_cache_problem(dest, SHA))
                # Offline it gets the cache advice, as damage does.
                shutil.rmtree(self.bucket.root)
                with self.assertRaisesRegex(assets.DownloadFailed, f"Delete {dest}"):
                    self.ensure(SHA)

    def test_both_markers_absent_is_unsigned(self):
        self.bucket.bundle()
        self.ensure(SHA)
        dest = assets.assets_dir(self.cache, SHA)
        for n in assets.SIGNED_MARKER:
            os.remove(os.path.join(dest, n))
        self.assertEqual(assets._signed_cache_problem(dest, SHA), assets.NOT_SIGNED)


class DroppedConnection(Case):
    """A connection that drops while the zip comes down is a DownloadFailed,
    not a raw exception, and nothing is unpacked."""

    def test_a_dropped_connection(self):
        self.bucket.bundle()
        real = urllib.request.urlopen
        for error in (http.client.RemoteDisconnected("Remote end closed connection"),
                      http.client.IncompleteRead(b"abc", 100),
                      ConnectionResetError(104, "Connection reset by peer")):
            with self.subTest(type(error).__name__):
                class Dropping(io.RawIOBase):
                    def read(self, n=-1):
                        raise error
                    def __enter__(self): return self
                    def __exit__(self, *a): return False
                def urlopen(url, *a, **kw):
                    if isinstance(url, str) and url.endswith(".zip"):
                        return Dropping()
                    return real(url, *a, **kw)
                with mock.patch.object(assets.urllib.request, "urlopen", urlopen):
                    with self.assertRaises(assets.DownloadFailed) as cm:
                        self.ensure(SHA)
                self.assertIn(f"download failed ({type(error).__name__}", str(cm.exception))
                self.assertFalse(self.unpacked())
                self.assertFalse(os.path.exists(os.path.join(self.cache, "assets", f".download-{SHA}.zip")))

    def test_a_dropped_connection_on_a_small_file(self):
        self.bucket.bundle()
        def urlopen(req, *a, **kw):
            raise http.client.RemoteDisconnected("Remote end closed connection")
        with mock.patch.object(assets.urllib.request, "urlopen", urlopen):
            with self.assertRaisesRegex(assets.DownloadFailed, "RemoteDisconnected"):
                self.ensure(SHA)


class CutOff(Case):
    """A real HTTP server that sends the full Content-Length, then only part
    of the body, and closes: http.client then returns the short data and
    b"" with no error. That is a cut-off download, not a bad release."""

    def serve(self, cut: str):
        root, cut_path = self.bucket.root, cut

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                path = os.path.join(root, self.path.lstrip("/"))
                if not os.path.isfile(path):
                    self.send_error(404)
                    return
                data = open(path, "rb").read()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.path.lstrip("/") == cut_path:
                    data = data[:len(data) // 2]
                    self.close_connection = True
                self.wfile.write(data)
                self.wfile.flush()

            def log_message(self, *a):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        p = mock.patch.object(assets, "BASE_URL", f"http://127.0.0.1:{server.server_address[1]}")
        p.start()
        self.addCleanup(p.stop)

    def test_a_zip_cut_off_is_a_failed_download(self):
        self.bucket.bundle()
        z = len(self.bucket.get(f"bundles/{SHA}/boxd-assets-{SHA}.zip"))
        self.serve(f"bundles/{SHA}/boxd-assets-{SHA}.zip")
        with self.assertRaises(assets.DownloadFailed) as cm:
            self.ensure(SHA)
        self.assertIn(f"the download was cut off at {z // 2} of {z} bytes", str(cm.exception))
        self.assertNotIn("does not match", str(cm.exception))
        self.assertFalse(self.unpacked())

    def test_a_small_file_cut_off_is_a_failed_download(self):
        self.bucket.bundle()
        rel = len(self.bucket.get(f"bundles/{SHA}/release.json"))
        self.serve(f"bundles/{SHA}/release.json")
        with self.assertRaises(assets.DownloadFailed) as cm:
            self.ensure(SHA)
        self.assertIn(f"the download was cut off at {rel // 2} of {rel} bytes", str(cm.exception))
        self.assertNotIn("BAD signature", str(cm.exception))
        self.assertFalse(self.unpacked())

    def test_the_whole_bundle_over_http_installs(self):
        self.bucket.bundle()
        self.serve("nothing")
        _, root = self.ensure(SHA)
        self.assertTrue(os.path.exists(os.path.join(root, "MANIFEST.json")))


class NetworkVersusLocal(Case):
    """Which errors are the network's (DownloadFailed) and which are not."""

    def test_the_zip_urlopen_dropping_is_a_failed_download(self):
        self.bucket.bundle()
        real = urllib.request.urlopen
        def urlopen(url, *a, **kw):
            if isinstance(url, str) and url.endswith(".zip"):
                raise http.client.RemoteDisconnected("Remote end closed connection without response")
            return real(url, *a, **kw)
        with mock.patch.object(assets.urllib.request, "urlopen", urlopen):
            with self.assertRaisesRegex(assets.DownloadFailed, "RemoteDisconnected"):
                self.ensure(SHA)
        self.assertFalse(self.unpacked())

    def test_a_failing_read_of_a_small_file_is_a_failed_download(self):
        self.bucket.bundle()
        class Failing(io.RawIOBase):
            headers = {}
            def read(self, n=-1):
                raise ConnectionResetError(104, "Connection reset by peer")
            def __enter__(self): return self
            def __exit__(self, *a): return False
        with mock.patch.object(assets.urllib.request, "urlopen", lambda *a, **kw: Failing()):
            with self.assertRaisesRegex(assets.DownloadFailed, "ConnectionResetError"):
                self.ensure(SHA)

    def test_a_local_write_error_stays_what_it_is(self):
        # A full disk is not the network's fault: a raw OSError, and the
        # temp zip is removed.
        self.bucket.bundle()
        tmp_zip = os.path.join(self.cache, "assets", f".download-{SHA}.zip")
        real_open = open

        class Full:
            def __init__(self, fh): self.fh = fh
            def write(self, data): raise OSError(errno.ENOSPC, "No space left on device")
            def __enter__(self): return self
            def __exit__(self, *a): self.fh.close(); return False

        def fake_open(path, mode="r", *a, **kw):
            fh = real_open(path, mode, *a, **kw)
            return Full(fh) if path == tmp_zip and "w" in mode else fh
        with mock.patch("builtins.open", fake_open):
            with self.assertRaises(OSError) as cm:
                self.ensure(SHA)
        self.assertNotIsInstance(cm.exception, InstallError)
        self.assertEqual(cm.exception.errno, errno.ENOSPC)
        self.assertFalse(os.path.exists(tmp_zip))
        self.assertFalse(self.unpacked())

    def test_dangling_symlink_markers_count_as_present(self):
        self.bucket.bundle()
        self.ensure(SHA)
        dest = assets.assets_dir(self.cache, SHA)
        for n in assets.SIGNED_MARKER:
            os.remove(os.path.join(dest, n))
            os.symlink(os.path.join(dest, "nowhere"), os.path.join(dest, n))
        why = assets._signed_cache_problem(dest, SHA)
        self.assertNotEqual(why, assets.NOT_SIGNED)
        self.assertIn("cannot be read", why)


class NoNetworkOrKeys(unittest.TestCase):
    """What does not need a key pair."""

    def test_the_committed_release_keys_are_found(self):
        self.assertEqual(DEFAULT_KEYS_DIR, os.path.join(ROOT, "release-keys"))
        for key_id in (1, 2):
            pem = os.path.join(DEFAULT_KEYS_DIR, f"{key_id}.pem")
            self.assertTrue(os.path.exists(pem), pem)
            if HAVE_ED25519:
                r = subprocess.run(["openssl", "pkey", "-pubin", "-in", pem, "-noout", "-text"],
                                   capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("ED25519 Public-Key", r.stdout)

    def test_libressl_says_it_needs_openssl_3(self):
        sig = json.dumps({"key_id": 1, "sig": base64.b64encode(b"\0" * 64).decode()}).encode()
        version = subprocess.CompletedProcess([], 0, "LibreSSL 3.3.6\n", "")
        assets._check_openssl.cache_clear()
        self.addCleanup(assets._check_openssl.cache_clear)
        with mock.patch.object(assets.subprocess, "run", return_value=version):
            with self.assertRaises(InstallError) as cm:
                assets.verify_signature(b"x", sig, assets.RELEASE_PREFIX, "assets")
        self.assertIn("needs OpenSSL 3 (found LibreSSL 3.3.6)", str(cm.exception))
        self.assertNotIn("BAD signature", str(cm.exception))

    def test_resolve_keeps_private_through_its_rewrap(self):
        env = {k: v for k, v in os.environ.items() if k != "BOXD_PRODUCT_LICENSE"}
        for code, private in ((403, True), (401, True), (404, False)):
            with self.subTest(code):
                err = urllib.error.HTTPError("u", code, "x", {}, None)
                with mock.patch.dict(os.environ, env, clear=True), \
                        mock.patch.object(assets.urllib.request, "urlopen", mock.Mock(side_effect=err)), \
                        contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(assets.DownloadFailed) as cm:
                        assets.resolve(config.Config(doc("latest")), None)
                self.assertIs(cm.exception.private, private)
                self.assertIn("assets_version", str(cm.exception))

    def test_a_private_bucket_tells_current_customers_what_to_set(self):
        cfg = config.Config(doc("latest"))
        err = urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
        env = {k: v for k, v in os.environ.items() if k != "BOXD_PRODUCT_LICENSE"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(assets.urllib.request, "urlopen", mock.Mock(side_effect=err)), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(InstallError) as cm:
                assets.resolve(cfg, None)
        self.assertIn("sets BOXD_PRODUCT_LICENSE (the old download path)", str(cm.exception))
        self.assertIn("assets_version", str(cm.exception))

    def test_the_license_selects_the_old_path(self):
        with mock.patch.dict(os.environ, {"BOXD_PRODUCT_LICENSE": "ab" * 32}):
            self.assertEqual(assets.zip_url(SHA),
                             f"{assets.LEGACY_BASE_URL}/{'ab' * 32}/boxd-assets-{SHA}.zip")

if __name__ == "__main__":
    unittest.main()
