"""The machine_specs switch on apply: off (unset) before the converge of a
new bundle when it was on, on after the converge when it is unset, an
operator's "off" kept, and bundles from before machine specs warned about.
Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import contextlib
import io
import subprocess
import types
import unittest
from unittest import mock

from boxd_installer import ansible, config, main
from boxd_installer.util import InstallError

import test_license  # the module, not its classes: unittest would run those again here
from test_machine_sizes import aws_doc

UNSET = (1, "", "Error: machine_specs is unset")


def ctl_run(fn, *results) -> tuple[list[str], str]:
    """Call fn(cfg) with boxd-ctl answering `results` ((returncode, stdout,
    stderr) each) in turn: the commands it ran, and what it logged."""
    cfg = config.Config(aws_doc(), ".")
    answers = [subprocess.CompletedProcess([], rc, out, err) for rc, out, err in results]
    err = io.StringIO()
    with mock.patch.object(ansible, "ctl", side_effect=answers) as ctl, \
         contextlib.redirect_stderr(err):
        fn(cfg)
    return [c[0][1] for c in ctl.call_args_list], err.getvalue()


class Pause(unittest.TestCase):
    def test_on_is_unset(self):
        cmds, err = ctl_run(ansible.pause_machine_specs, (0, "on\n", ""),
                            (0, "machine_specs unset\n", ""))
        self.assertEqual(cmds, ["cluster-config get machine_specs",
                                "cluster-config unset machine_specs"])
        self.assertIn("machine specs off for the upgrade", err)

    def test_anything_but_on_is_left_alone(self):
        for answer in (UNSET, (0, "off\n", ""),
                       (127, "", "sudo: boxd-ctl: command not found"),  # a fresh cluster
                       (2, "", "error: unrecognized subcommand 'cluster-config'")):
            with self.subTest(answer=answer):
                cmds, _ = ctl_run(ansible.pause_machine_specs, answer)
                self.assertEqual(cmds, ["cluster-config get machine_specs"])

    def test_a_failed_unset_stops_the_apply(self):
        with self.assertRaises(InstallError) as e:
            ctl_run(ansible.pause_machine_specs, (0, "on\n", ""), (1, "", "Error: no leader"))
        self.assertIn("no leader", str(e.exception))


class TurnOn(unittest.TestCase):
    def test_unset_is_turned_on(self):
        cmds, err = ctl_run(ansible.turn_on_machine_specs, UNSET, (0, "machine_specs set\n", ""))
        self.assertEqual(cmds, ["cluster-config get machine_specs",
                                "cluster-config set machine_specs on"])
        self.assertIn("machine specs -> on", err)

    def test_on_stays_on(self):
        cmds, err = ctl_run(ansible.turn_on_machine_specs, (0, "on\n", ""))
        self.assertEqual(cmds, ["cluster-config get machine_specs"])
        self.assertEqual(err, "")

    def test_an_operators_off_is_kept(self):
        cmds, err = ctl_run(ansible.turn_on_machine_specs, (0, "off\n", ""))
        self.assertEqual(cmds, ["cluster-config get machine_specs"])
        self.assertIn("left off", err)
        self.assertIn("boxd-ctl cluster-config set machine_specs on", err)

    def test_boxd_ctl_without_cluster_config_warns(self):
        cmds, err = ctl_run(ansible.turn_on_machine_specs,
                            (2, "", "error: unrecognized subcommand 'cluster-config'"))
        self.assertEqual(cmds, ["cluster-config get machine_specs"])
        self.assertIn("warning: this assets bundle's boxd-ctl has no `cluster-config`", err)

    def test_release_without_machine_specs_warns(self):
        refused = ('Error: status: InvalidArgument, message: "cluster_config key '
                   '\\"machine_specs\\" can\'t be written with `cluster-config`; it has its '
                   'own verb or belongs to the control plane. Writable keys: extra_ca_certs"')
        _, err = ctl_run(ansible.turn_on_machine_specs, UNSET, (1, "", refused))
        self.assertIn("warning: this assets bundle has no machine specs", err)

    def test_a_failed_read_stops_the_apply(self):
        with self.assertRaises(InstallError) as e:
            ctl_run(ansible.turn_on_machine_specs, (1, "", "Error: no leader"))
        self.assertIn("no leader", str(e.exception))

    def test_a_failed_write_stops_the_apply(self):
        with self.assertRaises(InstallError) as e:
            ctl_run(ansible.turn_on_machine_specs, UNSET, (1, "", "Error: no leader"))
        self.assertIn("no leader", str(e.exception))


class BundleChanges(unittest.TestCase):
    def test_against_the_last_applied_bundle(self):
        cfg = types.SimpleNamespace(assets_version="a" * 40)
        for applied, want in ((None, True), ({"sha": "a" * 40}, False),
                              ({"sha": "b" * 40}, True)):
            with self.subTest(applied=applied):
                state = types.SimpleNamespace(data={} if applied is None
                                              else {"applied_assets": applied})
                self.assertIs(main.bundle_changes(state, cfg), want)


class ApplyOrder(test_license.LicenseCluster):
    apply = test_license.ApplyOrder.apply
    names = test_license.ApplyOrder.names

    def test_off_before_the_converge_on_after(self):
        names = self.names(self.apply(test_license.without_type(aws_doc(), key_file=False), {}))
        self.assertLess(names.index("pause_machine_specs"), names.index("converge"))
        # With every node on the new bundle, like the cluster ID, and before
        # the templates.
        self.assertLess(names.index("init_cluster_id"), names.index("turn_on_machine_specs"))
        self.assertLess(names.index("turn_on_machine_specs"), names.index("seed_templates"))

    def test_same_bundle_no_pause(self):
        with mock.patch.object(main, "bundle_changes", return_value=False):
            names = self.names(self.apply(test_license.without_type(aws_doc(), key_file=False),
                                          {}))
        self.assertNotIn("pause_machine_specs", names)
        self.assertIn("turn_on_machine_specs", names)


if __name__ == "__main__":
    unittest.main()
