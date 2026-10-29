"""[cluster].assets_version as a channel ("dev") resolved to a pinned sha, on
the old download path (BOXD_PRODUCT_LICENSE set).
The signed release bucket: test_release_bundles.py.
Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import contextlib
import io
import os
import tomllib
import unittest
import urllib.error
from unittest import mock

from boxd_installer import assets, config
from boxd_installer.util import InstallError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHA = "0123456789abcdef0123456789abcdef01234567"
LICENSE = "ab" * 32

def doc(version: str) -> dict:
    with open(os.path.join(ROOT, "examples", "aws", "cluster.toml"), "rb") as fh:
        d = tomllib.load(fh)
    d["cluster"]["assets_version"] = version
    return d

class Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): self.close()

class Validation(unittest.TestCase):
    def test_sha_and_channel_names_are_valid(self):
        for v in (SHA, "dev", "staging", "rc-2"):
            config._validate(doc(v))

    def test_anything_else_is_refused(self):
        for v in ("Dev", "dev/latest", "", "957e5c5a", "-dev", 3):
            with self.assertRaisesRegex(InstallError, "40-hex git sha .pinned. or a channel"):
                config._validate(doc(v))

class Resolve(unittest.TestCase):
    def resolve(self, version, urlopen):
        cfg = config.Config(doc(version))
        with mock.patch.dict(os.environ, {"BOXD_PRODUCT_LICENSE": LICENSE}), \
                mock.patch.object(assets.urllib.request, "urlopen", urlopen), \
                contextlib.redirect_stderr(io.StringIO()):
            return cfg, assets.resolve(cfg, None)

    def test_a_sha_is_used_as_is_without_the_network(self):
        urlopen = mock.Mock(side_effect=AssertionError("no fetch for a pinned sha"))
        cfg, sha = self.resolve(SHA, urlopen)
        self.assertEqual((sha, cfg.assets_version, cfg.assets_channel), (SHA, SHA, None))

    def test_a_channel_resolves_to_the_sha_it_points_at(self):
        urlopen = mock.Mock(return_value=Response(f"{SHA}\n".encode()))
        cfg, sha = self.resolve("dev", urlopen)
        self.assertEqual((sha, cfg.assets_version, cfg.assets_channel), (SHA, SHA, "dev"))
        req = urlopen.call_args.args[0]
        self.assertEqual(req.full_url,
                         f"https://storage.googleapis.com/self-host-assets/{LICENSE}/channels/dev")
        self.assertEqual(req.get_header("Cache-control"), "no-cache")

    def test_an_unpublished_channel_names_what_to_check(self):
        # The customer sees this: no boxd-internal tooling in it.
        err = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        with self.assertRaises(InstallError) as cm:
            self.resolve("dev", mock.Mock(side_effect=err))
        self.assertIn("assets_version", str(cm.exception))
        self.assertNotRegex(str(cm.exception), r"\bjust [a-z]")

    def test_a_pointer_without_a_sha_is_refused(self):
        with self.assertRaisesRegex(InstallError, "does not hold a git sha"):
            self.resolve("dev", mock.Mock(return_value=Response(b"<html>oops</html>")))

if __name__ == "__main__":
    unittest.main()
