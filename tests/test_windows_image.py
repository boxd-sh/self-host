"""`./boxd-install windows-image build`: the tools-image part (windows_image.py: the kit out of the
release bundle, the registry login for the push) and the wrapper's and host script's contract.
Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import base64
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

from boxd_installer import config, windows_image
from boxd_installer.util import InstallError

from test_windows import IMAGE, REGISTRY, aws_doc, win_doc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHA = "a" * 40


def make_bundle(test, files=windows_image.KIT_REQUIRED) -> str:
    """A release bundle under <tmp>/assets/<sha>/ with the kit's files on disk and in its manifest."""
    root = tempfile.mkdtemp(prefix="windows-image-")
    test.addCleanup(shutil.rmtree, root)
    b = os.path.join(root, "assets", SHA)
    os.makedirs(os.path.join(b, "windows-image", "licenses"))
    for f in files:
        with open(os.path.join(b, "windows-image", f), "w") as fh:
            fh.write(f"# {f}\n")
    with open(os.path.join(b, "windows-image", "licenses", "conpty-LICENSE.txt"), "w") as fh:
        fh.write("MIT\n")
    manifest = {"git_sha": SHA, "files": {f"windows-image/{f}": "h" for f in files}}
    with open(os.path.join(b, "MANIFEST.json"), "w") as fh:
        json.dump(manifest, fh)
    return b


def cfg_with(doc, providers=None):
    cfg = config.Config(doc)
    cfg.apply_overlay(providers or {})
    return cfg


class Kit(unittest.TestCase):
    def test_a_release_without_the_kit_is_refused(self):
        b = make_bundle(self, files=())
        why = windows_image.kit_error(cfg_with(win_doc()), b)
        self.assertIn("has no Windows image kit", why)
        self.assertIn("build-image.sh", why)
        self.assertIsNone(windows_image.kit_error(cfg_with(win_doc()), make_bundle(self)))

    def test_prepare_copies_the_kit_and_keeps_the_downloads(self):
        b = make_bundle(self)
        cache = os.path.dirname(os.path.dirname(b))
        work = windows_image.work_dir(cache, SHA)
        os.makedirs(os.path.join(work, "dl"))
        with open(os.path.join(work, "dl", "virtio-win.iso"), "w") as fh:
            fh.write("kept")
        out = windows_image.prepare(cfg_with(win_doc()), cache, b)
        self.assertEqual(out["kit"], work)
        self.assertEqual(out["image"], IMAGE)
        self.assertEqual(out["release"], SHA)
        self.assertEqual(out["docker_config"], "", "no [windows_registry]: the host's own login")
        for f in windows_image.KIT_REQUIRED:
            self.assertTrue(os.path.isfile(os.path.join(work, f)), f)
        self.assertTrue(os.path.isfile(os.path.join(work, "licenses", "conpty-LICENSE.txt")))
        self.assertTrue(os.access(os.path.join(work, "build-image.sh"), os.X_OK))
        with open(os.path.join(work, "dl", "virtio-win.iso")) as fh:
            self.assertEqual(fh.read(), "kept")

    def test_the_registry_login_is_written_private_for_the_push(self):
        b = make_bundle(self)
        cache = os.path.dirname(os.path.dirname(b))
        out = windows_image.prepare(cfg_with(win_doc(), {"windows_registry": dict(REGISTRY)}), cache, b)
        path = os.path.join(out["docker_config"], "config.json")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path) as fh:
            auths = json.load(fh)["auths"]
        want = base64.b64encode(f"{REGISTRY['username']}:{REGISTRY['password']}".encode()).decode()
        self.assertEqual(auths, {REGISTRY["host"]: {"auth": want}})

    def test_credentials_for_another_registry_stop_the_build(self):
        b = make_bundle(self)
        cache = os.path.dirname(os.path.dirname(b))
        with self.assertRaisesRegex(InstallError, "push would go without them"):
            windows_image.prepare(cfg_with(win_doc(), {"windows_registry": {**REGISTRY, "host": "ghcr.io"}}),
                                  cache, b)

    def test_no_windows_or_a_bad_image_is_refused(self):
        b = make_bundle(self)
        cache = os.path.dirname(os.path.dirname(b))
        with self.assertRaisesRegex(InstallError, r"\[windows\] isn't in cluster.toml"):
            windows_image.prepare(cfg_with(aws_doc()), cache, b)
        with self.assertRaisesRegex(InstallError, "needs a tag or a digest"):
            windows_image.prepare(cfg_with(win_doc(image="registry.example.com/w")), cache, b)


class Wrapper(unittest.TestCase):
    def test_usage_needs_no_tools_image(self):
        env = {**os.environ, "PATH": "/usr/bin:/bin"}  # no docker on PATH: help must not need it
        p = subprocess.run([os.path.join(ROOT, "boxd-install"), "windows-image", "--help"],
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertIn("usage: ./boxd-install windows-image build --iso FILE", p.stderr)
        p = subprocess.run([os.path.join(ROOT, "boxd-install"), "windows-image", "frob"],
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(p.returncode, 2)

    def test_the_host_script_checks_its_arguments_before_anything_runs(self):
        script = os.path.join(ROOT, "boxd_installer", "windows-image-build.sh")
        self.assertTrue(os.access(script, os.X_OK))
        p = subprocess.run([script, "--iso", "/nonexistent.iso"], capture_output=True, text=True,
                           timeout=30)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("no ISO at /nonexistent.iso", p.stderr)
        p = subprocess.run([script], capture_output=True, text=True, timeout=30)
        self.assertIn("usage:", p.stderr)

    def test_install_py_has_the_kit_step(self):
        from boxd_installer import main
        import inspect
        src = inspect.getsource(main.main)
        self.assertIn('"windows-image": cmd_windows_image', src)
        self.assertIn("windows_image.prepare", inspect.getsource(main.cmd_windows_image))
