"""[telemetry]: validation, the explicit-only rule, control's [telemetry]
cloud and region, `boxd-ctl telemetry on|off` on apply, the validate line,
and the customer defaults file. Every test points telemetry.CUSTOMER_DEFAULTS
at a temporary path, so the suite gives the same results in a tree that has
the real file.
Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import glob
import io
import os
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from unittest import mock

from boxd_installer import ansible, config, main, render, telemetry
from boxd_installer.util import InstallError

from test_machine_sizes import FakeAlloc, FakeState, aws_doc, validate

CONTROL = "boxd-aws-demo-control-0"
REFUSED = ("Error: Telemetry can be turned off only with a license key that includes "
           "telemetry opt-out.")
OLD_CTL = "error: unrecognized subcommand 'telemetry'"


def with_telemetry(enabled: bool, key_file: bool = True) -> dict:
    doc = aws_doc()
    doc["telemetry"] = {"enabled": enabled}
    if key_file:
        doc["license"] = {"key_file": "license.key"}
    return doc


class Isolated(unittest.TestCase):
    """telemetry.CUSTOMER_DEFAULTS is a temporary path that does not exist
    until write_defaults."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="telemetry-")
        self.addCleanup(shutil.rmtree, self.dir)
        self.defaults = os.path.join(self.dir, "customer_defaults.toml")
        patcher = mock.patch.object(telemetry, "CUSTOMER_DEFAULTS", self.defaults)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_defaults(self, enabled: bool) -> None:
        with open(self.defaults, "w") as fh:
            fh.write(f"[telemetry]\nenabled = {str(enabled).lower()}\n")


class Validation(Isolated):
    def test_absent_true_and_false_with_a_key_are_valid(self):
        self.assertEqual(validate(aws_doc()), "")
        self.assertEqual(validate(with_telemetry(True, key_file=False)), "")
        self.assertEqual(validate(with_telemetry(False)), "")

    def test_false_without_a_key_file_is_an_error(self):
        self.assertIn("[telemetry] enabled = false needs [license] key_file",
                      validate(with_telemetry(False, key_file=False)))

    def test_a_license_that_is_not_a_table_is_an_error_not_a_crash(self):
        doc = with_telemetry(False, key_file=False)
        doc["license"] = "license.key"
        self.assertIn("[license] must be a table", validate(doc))

    def test_bad_values(self):
        doc = aws_doc()
        doc["telemetry"] = {"enabled": "no"}
        self.assertIn("[telemetry].enabled must be true or false", validate(doc))
        doc["telemetry"] = {"endpoint": "https://example.com"}
        self.assertIn("[telemetry].endpoint: unknown key", validate(doc))


class Setting(Isolated):
    """The explicit-only rule: a value comes from cluster.toml, else from the
    customer defaults file, else there is none."""

    def test_unset_without_a_defaults_file(self):
        self.assertEqual(telemetry.setting(config.Config(aws_doc())), (None, None))

    def test_cluster_toml(self):
        for v in (True, False):
            self.assertEqual(telemetry.setting(config.Config(with_telemetry(v))),
                             (v, telemetry.CLUSTER_TOML))

    def test_customer_default(self):
        self.write_defaults(False)
        self.assertEqual(telemetry.setting(config.Config(aws_doc())),
                         (False, telemetry.CUSTOMER_DEFAULT))
        # An explicit cluster.toml value wins.
        self.assertEqual(telemetry.setting(config.Config(with_telemetry(True))),
                         (True, telemetry.CLUSTER_TOML))


class Render(Isolated):
    def rendered(self, doc: dict) -> dict:
        cfg = config.Config(doc)
        cfg.apply_overlay({})
        tf_out = {"nodes": {n.name: {"external_ip": "203.0.113.1"} for n in cfg.nodes}}
        out = render.Renderer(cfg, FakeAlloc(), tf_out, FakeState()).deploy_configs()
        return {path: tomllib.loads(body) for path, body in out.items()}

    def control(self, doc: dict) -> dict:
        return self.rendered(doc)[f"deploy-config/{CONTROL}/control.toml"]["telemetry"]

    def test_control_gets_the_cloud_and_region(self):
        doc = aws_doc()
        files = self.rendered(doc)
        self.assertEqual(files[f"deploy-config/{CONTROL}/control.toml"]["telemetry"],
                         {"cloud": "aws", "region": doc["aws"]["region"]})
        for path, body in files.items():
            if not path.endswith("/control.toml"):
                self.assertNotIn("telemetry", body, path)

    def test_default_enabled_false_when_the_setting_is_off(self):
        # Unset or true: no default_enabled (control's default, true).
        self.assertNotIn("default_enabled", self.control(aws_doc()))
        self.assertNotIn("default_enabled", self.control(with_telemetry(True)))
        # cluster.toml false, or the customer default false: false, so the
        # first upgrade never sends before apply's `telemetry off`.
        self.assertIs(self.control(with_telemetry(False))["default_enabled"], False)
        self.write_defaults(False)
        self.assertIs(self.control(aws_doc())["default_enabled"], False)
        self.assertNotIn("default_enabled", self.control(with_telemetry(True)))


class SetTelemetry(Isolated):
    def run_set(self, enabled: bool, explicit: bool, *results):
        cfg = config.Config(aws_doc())
        results = [subprocess.CompletedProcess([], rc, "", err) for rc, err in results]
        logs = []
        with mock.patch.object(ansible, "_ssh", side_effect=results) as ssh, \
                mock.patch.object(ansible.time, "sleep"), \
                mock.patch.object(ansible, "log", side_effect=logs.append):
            ansible.set_telemetry(cfg, enabled, explicit)
        return [c.args[2] for c in ssh.call_args_list], logs

    def test_on_and_off(self):
        self.assertEqual(self.run_set(True, True, (0, ""))[0], ["sudo boxd-ctl telemetry on"])
        self.assertEqual(self.run_set(False, True, (0, ""))[0], ["sudo boxd-ctl telemetry off"])

    def test_an_explicit_refused_off_stops_the_apply(self):
        with self.assertRaises(InstallError) as e:
            self.run_set(False, True, *[(1, REFUSED)] * 5)
        self.assertIn("includes telemetry opt-out", str(e.exception))

    def test_off_is_retried_while_the_key_reaches_the_replica(self):
        calls, _ = self.run_set(False, True, (1, REFUSED), (0, ""))
        self.assertEqual(len(calls), 2)

    def test_old_boxd_ctl(self):
        self.run_set(True, True, (2, OLD_CTL))           # nothing to turn on
        with self.assertRaises(InstallError) as e:       # explicit off: stop
            self.run_set(False, True, (2, OLD_CTL))
        self.assertIn("no `telemetry` command", str(e.exception))
        # The customer default: a warning, and the apply goes on.
        _, logs = self.run_set(False, False, (2, OLD_CTL))
        self.assertTrue(any(l.startswith("warning: telemetry off (this installer's default)")
                            for l in logs), logs)

    def test_a_refused_customer_default_warns(self):
        _, logs = self.run_set(False, False, *[(1, REFUSED)] * 5)
        self.assertTrue(any("includes telemetry opt-out" in l for l in logs), logs)


class ApplyPassesTheSetting(Isolated):
    """`_apply` calls set_telemetry with the cluster.toml value, or with the
    customer default, and does not call it at all when nothing sets it."""

    def apply(self, doc: dict) -> mock.Mock:
        with open(os.path.join(self.dir, "license.key"), "w") as fh:
            fh.write("boxd-lic1_not-a-real-key\n")
        cfg = config.Config(doc, self.dir)
        calls = mock.Mock()
        args = mock.Mock(state_dir=self.dir, out_dir=self.dir, cache_dir=self.dir)
        with mock.patch.multiple(main, State=mock.DEFAULT, assets=mock.DEFAULT,
                                 control_carry=mock.DEFAULT, composio=mock.DEFAULT,
                                 render=mock.DEFAULT, license=mock.DEFAULT,
                                 storage_engine=mock.DEFAULT), \
             mock.patch.multiple(main.tf, init=mock.DEFAULT, plan=mock.DEFAULT,
                                 apply_plan=mock.DEFAULT, read_outputs=mock.DEFAULT,
                                 write_tfvars=mock.DEFAULT,
                                 instance_types=mock.Mock(return_value={}),
                                 network=mock.Mock(return_value=(None, {}))), \
             mock.patch.multiple(main.ansible, set_keys_dir=mock.DEFAULT,
                                 set_tf_outputs=mock.DEFAULT, preflight=mock.DEFAULT,
                                 converge=mock.DEFAULT, wait_for_leader=mock.DEFAULT, sync_config=mock.DEFAULT, deployed_network_modes=mock.Mock(return_value={}), machine_count=mock.Mock(return_value=0),
                                 learners_gave_up=mock.Mock(return_value=[]),
                                 _registered_nodes=mock.Mock(return_value=9),
                                 verify_registration=mock.DEFAULT,
                                 init_cluster_id=mock.DEFAULT, install_license=mock.DEFAULT,
                                 pause_machine_specs=mock.DEFAULT,
                                 turn_on_machine_specs=mock.DEFAULT,
                                 set_telemetry=calls.set_telemetry,
                                 seed_templates=mock.DEFAULT, configure_orgs=mock.DEFAULT,
                                 acme_eab_spent=mock.Mock(return_value=False)), \
             mock.patch.object(main, "log"), redirect_stdout(io.StringIO()):
            main.assets.computer_image.return_value = ("computer", "v1")
            main.State.return_value.load_providers.return_value = {}
            main.State.return_value.removed_members.return_value = {}
            main.license.free_tier_warnings.return_value = []
            main._apply(cfg, args)
        return calls.set_telemetry

    def test_the_configs_are_written_before_the_converge(self):
        """default_enabled must be in control's TOML when control starts:
        _apply writes the rendered configs before ansible.converge."""
        with open(os.path.join(self.dir, "license.key"), "w") as fh:
            fh.write("boxd-lic1_not-a-real-key\n")
        cfg = config.Config(with_telemetry(False), self.dir)
        calls = mock.Mock()
        args = mock.Mock(state_dir=self.dir, out_dir=self.dir, cache_dir=self.dir)
        with mock.patch.multiple(main, State=mock.DEFAULT, assets=mock.DEFAULT,
                                 control_carry=mock.DEFAULT, composio=mock.DEFAULT,
                                 render=mock.DEFAULT, license=mock.DEFAULT,
                                 storage_engine=mock.DEFAULT, _write_out=calls.write_out), \
             mock.patch.multiple(main.tf, init=mock.DEFAULT, plan=mock.DEFAULT,
                                 apply_plan=mock.DEFAULT, read_outputs=mock.DEFAULT,
                                 write_tfvars=mock.DEFAULT,
                                 instance_types=mock.Mock(return_value={}),
                                 network=mock.Mock(return_value=(None, {}))), \
             mock.patch.multiple(main.ansible, set_keys_dir=mock.DEFAULT,
                                 set_tf_outputs=mock.DEFAULT, preflight=mock.DEFAULT,
                                 converge=calls.converge, wait_for_leader=mock.DEFAULT, sync_config=mock.DEFAULT, deployed_network_modes=mock.Mock(return_value={}), machine_count=mock.Mock(return_value=0),
                                 learners_gave_up=mock.Mock(return_value=[]),
                                 _registered_nodes=mock.Mock(return_value=9),
                                 verify_registration=mock.DEFAULT,
                                 init_cluster_id=mock.DEFAULT, install_license=mock.DEFAULT,
                                 pause_machine_specs=mock.DEFAULT,
                                 turn_on_machine_specs=mock.DEFAULT,
                                 set_telemetry=calls.set_telemetry,
                                 seed_templates=mock.DEFAULT, configure_orgs=mock.DEFAULT,
                                 acme_eab_spent=mock.Mock(return_value=False)), \
             mock.patch.object(main, "log"), redirect_stdout(io.StringIO()):
            main.assets.computer_image.return_value = ("computer", "v1")
            main.State.return_value.load_providers.return_value = {}
            main.State.return_value.removed_members.return_value = {}
            main.license.free_tier_warnings.return_value = []
            main.render.Renderer.return_value.render_all.return_value = {"rendered": "x"}
            main._apply(cfg, args)
        names = [c[0] for c in calls.mock_calls]
        self.assertLess(names.index("write_out"), names.index("converge"))
        self.assertLess(names.index("converge"), names.index("set_telemetry"))
        self.assertEqual(calls.write_out.call_args.args[0], {"rendered": "x"})

    def test_unset_runs_neither_on_nor_off(self):
        self.apply(aws_doc()).assert_not_called()

    def test_cluster_toml_value(self):
        for v in (True, False):
            st = self.apply(with_telemetry(v))
            st.assert_called_once()
            self.assertEqual(st.call_args.args[1:], (v,))
            self.assertEqual(st.call_args.kwargs, {"explicit": True})

    def test_customer_default(self):
        self.write_defaults(False)
        st = self.apply(aws_doc())
        st.assert_called_once()
        self.assertEqual(st.call_args.args[1:], (False,))
        self.assertEqual(st.call_args.kwargs, {"explicit": False})


class ValidateCommand(Isolated):
    def validate_out(self, doc: dict) -> tuple[str, list[str]]:
        logs = []
        with open(os.path.join(self.dir, "license.key"), "w") as fh:
            fh.write("boxd-lic1_not-a-real-key\n")
        with mock.patch.object(config, "load", return_value=config.Config(doc, self.dir)), \
                mock.patch.object(main.license, "free_tier_warnings", return_value=[]), \
                mock.patch.object(main, "log", side_effect=logs.append):
            out = io.StringIO()
            with redirect_stdout(out):
                main.cmd_validate(mock.Mock(cluster="cluster.toml"))
        return out.getvalue(), logs

    def test_the_line(self):
        out, _ = self.validate_out(with_telemetry(True))
        self.assertIn("This cluster sends an hourly usage report to boxd (no personal data). "
                      "See it with `boxd-ctl telemetry show`.", out)
        out, _ = self.validate_out(aws_doc())
        self.assertIn("apply leaves the cluster's setting as it is", out)

    def test_customer_default_off_without_a_key_warns(self):
        self.write_defaults(False)
        out, logs = self.validate_out(aws_doc())
        self.assertIn("apply turns it off (this installer's default", out)
        self.assertTrue(any("there is no [license] key_file" in l for l in logs), logs)


class Isolation(unittest.TestCase):
    """No test reads this tree's real customer defaults file (_isolation)."""

    def test_reading_the_real_path_fails(self):
        self.assertNotEqual(os.path.abspath(telemetry.CUSTOMER_DEFAULTS), _isolation.REAL)
        with self.assertRaises(AssertionError):
            open(_isolation.REAL, "rb")
        for look in (os.path.isfile, os.path.exists, os.stat, os.lstat):
            with self.assertRaises(AssertionError, msg=look.__name__):
                look(_isolation.REAL)

    def test_a_test_that_bypasses_the_repoint_fails_here_too(self):
        """The mutant: code that reads the real path directly, as a test that
        bypassed the repoint would. It must fail in this repo (where the real
        file does not exist), not only in a customer tree."""
        with self.assertRaises(AssertionError):
            telemetry.customer_default(_isolation.REAL)
        with mock.patch.object(telemetry, "CUSTOMER_DEFAULTS", _isolation.REAL), \
                self.assertRaises(AssertionError):
            telemetry.setting(config.Config(aws_doc()))

    def test_the_guard_keeps_os_stat_usable(self):
        """The wrappers are in os.supports_* where os.stat/os.lstat were, so
        shutil.copy2(follow_symlinks=False) and stat(dir_fd=, fd) work."""
        for name in ("stat", "lstat"):
            for supports in ("supports_follow_symlinks", "supports_dir_fd", "supports_fd"):
                orig = getattr(_isolation, f"_{name}")
                if orig in getattr(os, supports):
                    self.assertIn(getattr(os, name), getattr(os, supports), (name, supports))
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "a")
            with open(src, "w") as fh:
                fh.write("x")
            link = os.path.join(d, "link")
            os.symlink(src, link)
            shutil.copy2(src, os.path.join(d, "b"), follow_symlinks=False)
            shutil.copy2(link, os.path.join(d, "c"), follow_symlinks=False)
            self.assertTrue(os.path.islink(os.path.join(d, "c")))
            shutil.copytree(d, os.path.join(d, "tree"), symlinks=True)
            fd = os.open(d, os.O_RDONLY)
            try:
                os.stat("a", dir_fd=fd)
                os.stat(fd)
            finally:
                os.close(fd)

    def test_every_test_module_imports_the_isolation_first(self):
        for path in glob.glob(os.path.join(os.path.dirname(__file__), "test_*.py")):
            with open(path) as fh:
                lines = [l for l in fh.read().split('"""', 2)[-1].splitlines()
                         if l.startswith(("import ", "from "))]
            self.assertTrue(lines and lines[0].startswith("import _isolation"),
                            f"{os.path.basename(path)}: first import must be _isolation")



def key_text(features: list[int], token: bool = False, packed: bool = True) -> str:
    """A key's text with these feature numbers, shaped as boxd signs them (a
    SignedLicense around a LicenseData), with a zero signature: the installer
    decodes it, control would refuse it."""
    import base64

    def varint(n: int) -> bytes:
        out = b""
        while True:
            b = n & 0x7F
            n >>= 7
            if n:
                out += bytes([b | 0x80])
            else:
                return out + bytes([b])

    def field(number: int, wire: int, value) -> bytes:
        tag = varint(number << 3 | wire)
        if wire == 0:
            return tag + varint(value)
        return tag + varint(len(value)) + value

    data = field(1, 0, 1) + field(2, 2, b"\x5a" * 16) + field(3, 2, b"example-co")
    data += field(5, 0, 4_102_444_800)
    if packed:
        data += field(10, 2, b"".join(varint(f) for f in features)) if features else b""
    else:
        data += b"".join(field(10, 0, f) for f in features)
    if token:
        data += field(11, 2, b"\x07" * 32)
    signed = field(1, 0, 7) + field(2, 2, data) + field(3, 2, b"\0" * 64)
    return "boxd-lic1_" + base64.urlsafe_b64encode(signed).decode().rstrip("=") + "\n"


class UsageBilling(Isolated):
    """A license key with usage billing (feature 4): telemetry can't be turned
    off with it."""

    def cfg(self, doc: dict, features: list[int]) -> config.Config:
        with open(os.path.join(self.dir, "license.key"), "w") as fh:
            fh.write(key_text(features, token=4 in features))
        return config.Config(doc, self.dir)

    def test_the_key_payload_decodes(self):
        from boxd_installer import license
        p = license.key_payload(key_text([1, 4], token=True))
        self.assertEqual(p, {"format": 1, "features": {"unlimited-worker-vcpus", "usage-billing"},
                             "report_token": True})
        p = license.key_payload(key_text([2, 9], packed=False))
        self.assertEqual(p["features"], {"telemetry-opt-out", "feature-9"})
        self.assertFalse(p["report_token"])
        self.assertIsNone(license.key_payload("boxd-lic1_AAAA"))
        self.assertIsNone(license.key_payload("not a key"))

    def test_enabled_false_in_cluster_toml_is_refused(self):
        cfg = self.cfg(with_telemetry(False), [1, 4])
        self.assertEqual(telemetry.errors(cfg), [telemetry.BILLING_OFF])
        with mock.patch.object(main.config, "load", return_value=cfg), \
             self.assertRaises(InstallError) as e:
            main.cmd_validate(mock.Mock(cluster="cluster.toml"))
        self.assertIn("usage billing", str(e.exception))
        # true, or unset, is fine; and without usage billing false is fine.
        self.assertEqual(telemetry.errors(self.cfg(with_telemetry(True), [1, 4])), [])
        self.assertEqual(telemetry.errors(self.cfg(with_telemetry(False), [1, 2])), [])

    def test_a_customer_default_of_off_is_not_applied_and_warns(self):
        self.write_defaults(False)
        doc = aws_doc()
        doc["license"] = {"key_file": "license.key"}
        cfg = self.cfg(doc, [1, 4])
        self.assertEqual(telemetry.setting(cfg), (None, None))
        self.assertEqual(telemetry.errors(cfg), [])
        (w,) = telemetry.warnings(cfg)
        self.assertIn("usage billing", w)
        # control's [telemetry] gets no default_enabled = false.
        self.assertNotIn("default_enabled", telemetry.control_table(cfg))
        # Without usage billing the default still applies.
        cfg = self.cfg(doc, [1, 2])
        self.assertEqual(telemetry.setting(cfg), (False, telemetry.CUSTOMER_DEFAULT))

    def test_the_validate_line(self):
        doc = aws_doc()
        doc["license"] = {"key_file": "license.key"}
        self.assertEqual(telemetry.validate_line(self.cfg(doc, [4])), telemetry.BILLING_LINE)
        self.assertEqual(telemetry.validate_line(self.cfg(doc, [1])),
                         telemetry.VALIDATE_LINE + " apply leaves the cluster's setting as it "
                         "is ([telemetry] enabled is not set).")


if __name__ == "__main__":
    unittest.main()
