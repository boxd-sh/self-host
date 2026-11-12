"""[windows] and the providers config's [windows_registry]: validation, the license feature, the
release gate, the rendered worker.toml and group_vars, terraform's RDP band, and the boxd-ctl
calls after the converge. Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import contextlib
import copy
import inspect
import io
import json
import os
import shutil
import tempfile
import tomllib
import unittest
from unittest import mock

from boxd_installer import config, license, main, render, tf, windows
from boxd_installer.util import InstallError

from test_gke_config import gke_doc
from test_guest_fw import TASKS
from test_local_ca import Alloc
from test_machine_sizes import FakeState, aws_doc, validate
from test_telemetry import key_text

IMAGE = "europe-docker.pkg.dev/acme/windows/windows-server-2025:2026-11"
REGISTRY = {"host": "europe-docker.pkg.dev", "username": "_json_key_base64", "password": "c2VjcmV0"}
WORKER = "boxd-aws-demo-worker-0"
WINDOWS_GUESTS = 5  # FEATURE_WINDOWS_GUESTS in boxd's license.proto


def win_doc(**windows_keys) -> dict:
    doc = aws_doc()
    doc["license"] = {"key_file": "license.key"}
    doc["windows"] = {"image": IMAGE, **windows_keys}
    return doc


def two_workers(doc: dict) -> dict:
    w = next(n for n in doc["nodes"] if "worker" in n["roles"])
    doc["nodes"].append({**copy.deepcopy(w), "name": "boxd-aws-demo-worker-1",
                         "internal_ip": "10.70.1.11"})
    return doc


class Validation(unittest.TestCase):
    def test_the_documented_shapes_are_valid(self):
        self.assertEqual(validate(win_doc()), "")
        self.assertEqual(validate(win_doc(kms="kms.example.com:1688")), "")
        self.assertEqual(validate(win_doc(licensing="per-vm")), "")
        self.assertEqual(validate(win_doc(licensing="per-host", hosts=[WORKER])), "")
        self.assertEqual(validate(aws_doc()), "", "no [windows]: nothing changes")

    def test_unknown_keys(self):
        self.assertIn("[windows].registry: unknown key", validate(win_doc(registry="x")))
        self.assertIn("[windows] must be a table", validate({**aws_doc(), "windows": "on"}))

    def test_image(self):
        for good in (IMAGE, "registry.example.com/boxd/windows-server-2025:2026-11",
                     "localhost:5000/win:1", "acme/windows:1",
                     "registry.example.com/w@sha256:" + "a" * 64,
                     "registry.example.com/w:1@sha256:" + "a" * 64):
            self.assertEqual(validate(win_doc(image=good)), "", good)
        self.assertIn("needs a tag or a digest",
                      validate(win_doc(image="registry.example.com/boxd/windows")))
        for bad in ("", "https://registry.example.com/w:1", "Registry.example.com/W:1",
                    "registry.example.com/w:1 ", "w:", 7):
            self.assertIn("[windows].image", validate(win_doc(image=bad)), bad)
        doc = win_doc()
        del doc["windows"]["image"]
        self.assertIn("[windows].image must be the image you pushed", validate(doc))

    def test_kms_as_boxd_normalizes_it(self):
        for good in ("kms.example.com", "kms.example.com:1688", "10.0.0.5:1688", "10.0.0.5",
                     "[2001:db8::5]:1688", "[2001:db8::5]", "kms-1"):
            self.assertIsNone(windows.kms_error(good), good)
        for bad in ("", "  ", "tcp://kms.example.com", "kms.example.com/x", "kms .example.com",
                    "kms.example.com:0", "kms.example.com:65536", "kms.example.com:x",
                    "2001:db8::5", "[2001:db8::5", "-kms.example.com", "kms..example.com", 1688):
            self.assertIsNotNone(windows.kms_error(bad), bad)
        self.assertIn("[windows].kms", validate(win_doc(kms="http://kms")))

    def test_licensing_and_hosts(self):
        self.assertIn("[windows].licensing must be one of per-vm, per-host",
                      validate(win_doc(licensing="per-core")))
        self.assertIn('licensing = "per-host" needs hosts', validate(win_doc(licensing="per-host")))
        self.assertIn('hosts is only for licensing = "per-host"',
                      validate(win_doc(hosts=[WORKER])))
        self.assertIn("'boxd-aws-demo-control-0' is not a worker",
                      validate(win_doc(licensing="per-host", hosts=["boxd-aws-demo-control-0"])))
        self.assertIn("'worker-2' is not a worker",
                      validate(win_doc(licensing="per-host", hosts=["worker-2"])),
                      "boxd's own worker ids are not node names")
        self.assertIn("names a worker twice",
                      validate(win_doc(licensing="per-host", hosts=[WORKER, WORKER])))
        self.assertIn("must be a list of worker names",
                      validate(win_doc(licensing="per-host", hosts=WORKER)))

    def test_it_needs_a_license_key(self):
        doc = win_doc()
        del doc["license"]
        self.assertIn("[windows] needs [license] key_file", validate(doc))

    def test_not_on_gke(self):
        doc = gke_doc()
        doc["windows"] = {"image": IMAGE}
        self.assertIn("[windows] is not supported on [gke]", validate(doc))

    def test_registry_credentials_never_in_cluster_toml(self):
        doc = aws_doc()
        doc["worker"]["registries"] = [REGISTRY]
        self.assertIn("[worker].registries holds a registry's password", validate(doc))


class Providers(unittest.TestCase):
    def errors(self, doc) -> str:
        try:
            config.validate_providers(doc)
        except InstallError as e:
            return str(e)
        return ""

    def test_windows_registry(self):
        self.assertEqual(self.errors({"windows_registry": dict(REGISTRY)}), "")
        self.assertIn("[windows_registry].password required",
                      self.errors({"windows_registry": {**REGISTRY, "password": ""}}))
        self.assertIn("[windows_registry].username required",
                      self.errors({"windows_registry": {"host": "r", "password": "p"}}))
        self.assertIn("[windows_registry].registry: unknown key",
                      self.errors({"windows_registry": {**REGISTRY, "registry": "x"}}))
        self.assertIn("not a URL or a path",
                      self.errors({"windows_registry": {**REGISTRY, "host": "https://r.example.com"}}))
        self.assertIn("[windows_registry] must be a table", self.errors({"windows_registry": "x"}))

    def test_the_template_shows_it(self):
        self.assertIn("# [windows_registry]", main.PROVIDERS_TEMPLATE)


class LicenseFeature(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="windows-license-")
        self.addCleanup(shutil.rmtree, self.dir)

    def cfg(self, features: list[int]) -> config.Config:
        with open(os.path.join(self.dir, "license.key"), "w") as fh:
            fh.write(key_text(features))
        return config.Config(win_doc(), self.dir)

    def test_the_feature_has_its_name(self):
        self.assertEqual(license.FEATURE_NAMES[WINDOWS_GUESTS], "windows-guests")
        self.assertIn("windows-guests", license.key_features(self.cfg([1, 2, WINDOWS_GUESTS])))

    def test_a_key_without_it_is_refused(self):
        self.assertEqual(windows.errors(self.cfg([1, WINDOWS_GUESTS])), [])
        errs = windows.errors(self.cfg([1, 2]))
        self.assertEqual(len(errs), 1)
        self.assertIn("includes Windows guests (windows-guests)", errs[0])
        self.assertIn("license.key has telemetry-opt-out, unlimited-worker-vcpus: ask", errs[0])
        self.assertEqual(windows.errors(config.Config(aws_doc(), self.dir)), [],
                         "without [windows] the key isn't asked")
        os.remove(os.path.join(self.dir, "license.key"))
        self.assertEqual(windows.errors(config.Config(win_doc(), self.dir)), [],
                         "a missing file is license.key_path's error, not a missing feature")

    def test_validate_and_apply_check_it_before_terraform(self):
        self.assertIn("windows.errors(cfg)", inspect.getsource(main.cmd_validate))
        src = inspect.getsource(main._apply)
        self.assertLess(src.index("windows.errors(cfg)"), src.index("tf.init(cfg)"))


class ReleaseGate(unittest.TestCase):
    def bundle(self, *files: str) -> str:
        d = tempfile.mkdtemp(prefix="windows-bundle-")
        self.addCleanup(shutil.rmtree, d)
        with open(os.path.join(d, "MANIFEST.json"), "w") as fh:
            json.dump({"git_sha": "0" * 40, "files": {f: "h" for f in ("bin/boxd-worker",) + files}}, fh)
        return d

    def test_a_release_without_the_firmware_is_refused(self):
        cfg = config.Config(win_doc())
        with self.assertRaisesRegex(InstallError, "lists no bin/uefi.fd"):
            windows.check_bundle(cfg, self.bundle())
        windows.check_bundle(config.Config(aws_doc()), self.bundle())

    def test_the_windows_clis_are_optional(self):
        cfg = config.Config(win_doc())
        with mock.patch.object(windows, "log") as log:
            windows.check_bundle(cfg, self.bundle("bin/uefi.fd"))
        self.assertIn("bin/boxd-sdk.exe or bin/boxd-run.exe", log.call_args[0][0])
        with mock.patch.object(windows, "log") as log:
            windows.check_bundle(cfg, self.bundle("bin/uefi.fd", "bin/boxd-sdk.exe",
                                                  "bin/boxd-run.exe"))
        log.assert_not_called()

    def test_it_runs_before_terraform(self):
        src = inspect.getsource(main._apply)
        self.assertLess(src.index("windows.check_bundle(cfg, assets_root)"), src.index("tf.init(cfg)"))

    def test_the_worker_files(self):
        full = self.bundle("bin/uefi.fd", "bin/boxd-sdk.exe", "bin/boxd-run.exe")
        self.assertEqual(windows.worker_files(config.Config(win_doc()), full), [
            {"src": "bin/uefi.fd", "dest": "uefi.fd"},
            {"src": "bin/boxd-sdk.exe", "dest": "sdk/boxd.exe"},
            {"src": "bin/boxd-run.exe", "dest": "sdk/run.exe"}])
        self.assertEqual(windows.worker_files(config.Config(win_doc()), self.bundle("bin/uefi.fd")),
                         [{"src": "bin/uefi.fd", "dest": "uefi.fd"}])
        self.assertEqual(windows.worker_files(config.Config(aws_doc()), full), [])
        self.assertEqual(windows.worker_files(config.Config(win_doc()), None), [])


class Render(unittest.TestCase):
    def renderer(self, doc, providers=None, assets_root=None) -> render.Renderer:
        cfg = config.Config(doc)
        cfg.apply_overlay(providers or {})
        tf_out = {"nodes": {n.name: {"external_ip": "203.0.113.1"} for n in cfg.nodes}}
        return render.Renderer(cfg, Alloc(), tf_out, FakeState(), assets_root=assets_root)

    def worker_toml(self, r) -> dict:
        return tomllib.loads(r.deploy_configs()[f"deploy-config/{WORKER}/worker.toml"])

    def test_the_registry_reaches_every_worker(self):
        r = self.renderer(win_doc(), {"windows_registry": dict(REGISTRY)})
        self.assertEqual(self.worker_toml(r)["worker"]["registries"], [REGISTRY])
        for path, body in r.deploy_configs().items():
            if not path.endswith("/worker.toml"):
                self.assertNotIn(REGISTRY["password"], body, path)

    def test_no_registry_without_windows_or_credentials(self):
        self.assertNotIn("registries", self.worker_toml(self.renderer(win_doc()))["worker"])
        r = self.renderer(aws_doc(), {"windows_registry": dict(REGISTRY)})
        self.assertNotIn("registries", self.worker_toml(r)["worker"])

    def test_an_incomplete_registry_stops_the_render(self):
        with self.assertRaisesRegex(InstallError, r"\[windows_registry\] has no password"):
            self.worker_toml(self.renderer(win_doc(),
                                           {"windows_registry": {**REGISTRY, "password": " "}}))

    def test_group_vars(self):
        d = tempfile.mkdtemp(prefix="windows-bundle-")
        self.addCleanup(shutil.rmtree, d)
        with open(os.path.join(d, "MANIFEST.json"), "w") as fh:
            json.dump({"git_sha": "0" * 40, "files": {"bin/uefi.fd": "h", "bin/boxd-sdk.exe": "h"}}, fh)
        gv = self.renderer(win_doc(), assets_root=d).group_vars().splitlines()
        self.assertIn('boxd_windows_files: [{ src: "bin/uefi.fd", dest: "uefi.fd" }, '
                      '{ src: "bin/boxd-sdk.exe", dest: "sdk/boxd.exe" }]', gv)
        gv = self.renderer(aws_doc(), assets_root=d).group_vars().splitlines()
        self.assertIn("boxd_windows_files: []", gv)

    def test_registry_warnings(self):
        def warnings(doc, providers):
            cfg = config.Config(doc)
            cfg.apply_overlay(providers)
            return windows.registry_warnings(cfg)
        self.assertEqual(warnings(win_doc(), {"windows_registry": dict(REGISTRY)}), [])
        self.assertEqual(warnings(aws_doc(), {}), [])
        self.assertIn("has no [windows_registry]", warnings(win_doc(), {})[0])
        other = {"windows_registry": {**REGISTRY, "host": "ghcr.io"}}
        self.assertIn("credentials for 'ghcr.io'", warnings(win_doc(), other)[0])
        self.assertIn("docker.io", warnings(win_doc(image="acme/windows:1"), other)[0])


class Role(unittest.TestCase):
    def test_the_worker_role_copies_the_files_and_restarts_the_worker(self):
        with open(TASKS) as fh:
            tasks = fh.read()
        task = tasks.split("- name: Copy what Windows machines need from the release\n", 1)[1]
        task = task.split("\n\n", 1)[0]
        self.assertIn('src: "{{ boxd_assets_dir }}/{{ item.src }}"', task)
        self.assertIn('dest: "{{ boxd_data_dir }}/{{ item.dest }}"', task)
        self.assertIn('loop: "{{ boxd_windows_files }}"', task)
        self.assertIn("notify: restart boxd service", task)
        defaults = os.path.join(os.path.dirname(os.path.dirname(TASKS)), "defaults", "main.yml")
        with open(defaults) as fh:
            self.assertIn("\nboxd_windows_files: []\n", fh.read())

    def test_the_files_come_from_the_nodes_copy_of_the_release(self):
        """boxd_copy takes a file from the node's own copy of the bundle only when it is under
        one of the directories the node fetches."""
        from boxd_installer import ansible
        for rel, _, _ in windows.FILES:
            self.assertTrue(rel.startswith(ansible.NODE_ASSET_DIRS), rel)


class Tfvars(unittest.TestCase):
    def tfvars(self, doc) -> dict:
        with mock.patch("boxd_installer.clouds.aws._resolve_ami", return_value="ami-1"):
            return tf.build_tfvars(config.Config(doc), FakeState())

    def test_the_rdp_band_only_with_windows(self):
        self.assertIs(self.tfvars(win_doc())["windows_rdp"], True)
        self.assertIs(self.tfvars(aws_doc())["windows_rdp"], False)

    def test_terraform_opens_the_band(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for cloud, band in (("aws", "[61000, 65000]"), ("gcp", '"61000-65000"')):
            with open(os.path.join(root, "terraform", cloud, "main.tf")) as fh:
                self.assertIn(f"var.windows_rdp ? [{band}] : []", fh.read(), cloud)
            with open(os.path.join(root, "terraform", cloud, "variables.tf")) as fh:
                self.assertIn('variable "windows_rdp"', fh.read(), cloud)


class Configure(unittest.TestCase):
    def commands(self, doc, fail_on: str | None = None) -> list[str]:
        cfg = config.Config(two_workers(doc))
        ids = {"boxd-aws-demo-worker-0": 2, "boxd-aws-demo-worker-1": 3}
        alloc = mock.Mock(node_id=lambda name, role: ids[name])
        seen = []

        def ctl(cfg, command, check=True, quiet=False, input=None):
            seen.append(command)
            failed = fail_on is not None and command.startswith(fail_on)
            return mock.Mock(returncode=1 if failed else 0, stdout="", stderr="boom" if failed else "")
        with mock.patch.object(windows.ansible, "ctl", side_effect=ctl), \
             mock.patch.object(windows, "log"):
            windows.configure(cfg, alloc)
        return seen

    def test_per_vm(self):
        self.assertEqual(self.commands(win_doc(kms="kms.example.com:1688")), [
            f"image set-windows {IMAGE}",
            "cluster-config set windows_kms kms.example.com:1688",
            "node windows-host worker-2 off",
            "node windows-host worker-3 off",
            "cluster-config set windows_licensing per-vm"])

    def test_per_host_marks_before_the_switch(self):
        cmds = self.commands(win_doc(licensing="per-host", hosts=["boxd-aws-demo-worker-1"]))
        self.assertEqual(cmds[1:], [
            "cluster-config unset windows_kms",
            "node windows-host worker-2 off",
            "node windows-host worker-3 on",
            "cluster-config set windows_licensing per-host"])

    def test_nothing_without_windows(self):
        self.assertEqual(self.commands(aws_doc()), [])

    def test_a_refused_call_stops_the_apply(self):
        with self.assertRaisesRegex(InstallError, "boxd-ctl node windows-host worker-2 off failed: boom"):
            self.commands(win_doc(), fail_on="node windows-host")

    def test_after_the_license_and_before_the_templates(self):
        src = inspect.getsource(main._finish)
        self.assertLess(src.index("install_license"), src.index("windows.configure"))
        self.assertLess(src.index("windows.configure"), src.index("seed_templates"))


if __name__ == "__main__":
    unittest.main()
