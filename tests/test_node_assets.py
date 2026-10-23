"""The node's own copy of the assets bundle (ansible.node_assets_vars and
boxd-common's boxd-assets-unpack.py). Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

from boxd_installer import ansible

ROOT = os.path.join(os.path.dirname(__file__), "..")
UNPACK = os.path.join(ROOT, "ansible", "roles", "boxd-common", "files", "boxd-assets-unpack.py")
SHA = "a" * 40

class NodeAssetsVars(unittest.TestCase):
    def test_hashes_only_for_dirs_boxd_copy_ships(self):
        files = {"bin/boxd-worker": "h1", "kernel/k.deb": "h2", "cli/boxd-linux-amd64": "h3",
                 "automations/registry.tar.gz": "h4", "console/index.html": "h5"}
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "MANIFEST.json"), "w") as fh:
                json.dump({"files": files}, fh)
            with mock.patch.dict(os.environ, {"BOXD_PRODUCT_LICENSE": "0" * 64}):
                got = ansible.node_assets_vars(SHA, root)
        self.assertEqual(got["boxd_assets_sha"], SHA)
        self.assertTrue(got["boxd_assets_url"].endswith(f"/{'0' * 64}/boxd-assets-{SHA}.zip"))
        self.assertEqual(got["boxd_asset_hashes"],
                         {k: v for k, v in files.items() if not k.startswith("console/")})

class Unpack(unittest.TestCase):
    def bundle(self, root, tamper=False):
        """ROOT/SHA.zip + ROOT/SHA.hashes.json for a two-file bundle."""
        files = {"bin/boxd-worker": (b"worker", 0o755), "kernel/k.deb": (b"kernel", 0o644)}
        with zipfile.ZipFile(os.path.join(root, SHA + ".zip"), "w") as zf:
            for rel, (data, mode) in files.items():
                info = zipfile.ZipInfo("boxd-assets/" + rel)
                info.external_attr = mode << 16
                zf.writestr(info, data + (b"!" if tamper and rel == "kernel/k.deb" else b""))
        with open(os.path.join(root, SHA + ".hashes.json"), "w") as fh:
            json.dump({rel: hashlib.sha256(d).hexdigest() for rel, (d, _) in files.items()}, fh)

    def unpack(self, root):
        return subprocess.run([sys.executable, UNPACK, root, SHA], capture_output=True, text=True)

    def test_unpacks_keeps_modes_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "b" * 40))  # an older version
            self.bundle(root)
            self.assertEqual(self.unpack(root).returncode, 0)
            self.assertEqual(sorted(os.listdir(root)), [SHA])
            dest = os.path.join(root, SHA)
            self.assertTrue(os.path.exists(os.path.join(dest, ".verified")))
            self.assertEqual(os.stat(os.path.join(dest, "bin/boxd-worker")).st_mode & 0o777, 0o755)
            with open(os.path.join(dest, "kernel/k.deb"), "rb") as fh:
                self.assertEqual(fh.read(), b"kernel")

    def test_checksum_mismatch_fails_unverified(self):
        with tempfile.TemporaryDirectory() as root:
            self.bundle(root, tamper=True)
            res = self.unpack(root)
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("checksum mismatch on kernel/k.deb", res.stderr)
            self.assertFalse(os.path.exists(os.path.join(root, SHA, ".verified")))

if __name__ == "__main__":
    unittest.main()
