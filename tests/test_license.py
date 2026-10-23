"""[license] key_file (BOX-630 step 4a): validation, the key file check, the
free tier warning, the worker type (an existing worker keeps its type; a new
one without a key gets the free type), and `boxd-ctl license install` on
apply. Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest import mock

from boxd_installer import ansible, config, license, main, telemetry, tf
from boxd_installer.util import InstallError

from test_machine_sizes import FakeState, aws_doc, validate

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AWS_WORKER = "boxd-aws-demo-worker-0"
# Shaped like a key; control would refuse it, which these tests never reach.
KEY = "boxd-lic1_not-a-real-key\n"


def gcp_doc() -> dict:
    with open(os.path.join(ROOT, "examples", "gcp", "cluster.toml"), "rb") as fh:
        return tomllib.load(fh)


def worker(doc: dict) -> dict:
    return next(n for n in doc["nodes"] if "worker" in n["roles"])


def without_type(doc: dict, key_file: bool = True) -> dict:
    worker(doc).pop("machine_type", None)
    if key_file:
        doc["license"] = {"key_file": "license.key"}
    return doc


class LicenseCluster(unittest.TestCase):
    """A cluster.toml directory, with or without license.key."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="license-")
        # The real customer defaults file exists in a customer tree.
        patcher = mock.patch.object(telemetry, "CUSTOMER_DEFAULTS",
                                    os.path.join(self.dir, "customer_defaults.toml"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def write_key(self, text: str = KEY, name: str = "license.key") -> str:
        path = os.path.join(self.dir, name)
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def cfg(self, doc: dict, key: bool = False) -> config.Config:
        if key:
            self.write_key()
        return config.Config(doc, self.dir)

    def tfvars_types(self, cfg, existing=None) -> dict:
        with mock.patch("boxd_installer.clouds.aws._resolve_ami", return_value="ami-1"), \
             mock.patch("boxd_installer.clouds.gcp._resolve_boot_image", return_value="img"):
            tfvars = tf.build_tfvars(cfg, FakeState(), existing)
        return {n["name"]: n["machine_type"] for n in tfvars["nodes"]}


class Validation(unittest.TestCase):
    def test_absent_and_key_file_are_valid(self):
        self.assertEqual(validate(aws_doc()), "")
        doc = aws_doc()
        doc["license"] = {"key_file": "license.key"}
        self.assertEqual(validate(doc), "")

    def test_unknown_key_and_bad_paths(self):
        doc = aws_doc()
        doc["license"] = {"key": "boxd-lic1_x"}
        self.assertIn("[license].key: unknown key", validate(doc))
        for bad in ("/etc/license.key", "../license.key", "", 3):
            doc = aws_doc()
            doc["license"] = {"key_file": bad}
            self.assertIn("[license].key_file", validate(doc), bad)

    def test_machine_type_must_be_a_string(self):
        for bad in (16, "", ["m8i.4xlarge"], True):
            doc = aws_doc()
            worker(doc)["machine_type"] = bad
            self.assertIn(f"{AWS_WORKER}: machine_type must be a quoted string",
                          validate(doc), bad)


class KeyFile(LicenseCluster):
    """[license].key_file set: the file must exist and look like a key."""

    def doc(self) -> dict:
        doc = aws_doc()
        doc["license"] = {"key_file": "license.key"}
        return doc

    def error(self) -> str:
        with self.assertRaises(InstallError) as e:
            license.key_path(config.Config(self.doc(), self.dir))
        return str(e.exception)

    def test_missing_file_is_an_error(self):
        self.assertIn("[license].key_file license.key does not exist", self.error())

    def test_download_key_is_refused(self):
        self.write_key("ab" * 32 + "\n")
        self.assertIn("holds a download key (64 hex characters), not a license key",
                      self.error())

    def test_not_a_key(self):
        for text in ("", "hello-secret\n", f"{KEY}{KEY}", "boxd-lic1_has spaces-xyz\n",
                     "boxd-lic1_abc$def-qrs\n"):
            self.write_key(text)
            err = self.error()
            self.assertIn("is not a license key: it must hold one line that starts with "
                          "boxd-lic", err)
            # The key body (after boxd-licN_) is what an echo of the file
            # would show; the error must not contain it.
            for line in text.split("\n"):
                body = re.sub(r"^.*?boxd-lic[0-9]*_?", "", line).strip()
                if body:
                    self.assertNotIn(body, err, "never echo the file")

    def test_unreadable_file(self):
        path = self.write_key()
        os.chmod(path, 0)
        try:
            if os.access(path, os.R_OK):
                self.skipTest("running as root: chmod 000 does not stop reads")
            err = self.error()
        finally:
            os.chmod(path, 0o600)
        self.assertIn("[license].key_file license.key cannot be read (Permission denied): "
                      "check its permissions", err)

    def test_a_key(self):
        path = self.write_key("\n" + KEY + "\n")
        self.assertEqual(license.key_path(config.Config(self.doc(), self.dir)), path)

    def test_other_name(self):
        doc = self.doc()
        doc["license"]["key_file"] = "keys/acme.key"
        os.makedirs(os.path.join(self.dir, "keys"))
        path = self.write_key(name="keys/acme.key")
        self.assertEqual(license.key_path(config.Config(doc, self.dir)), path)


class VcpuTable(unittest.TestCase):
    def test_published_counts(self):
        for cloud, t, n in (("aws", "m8i.4xlarge", 16), ("aws", "m8i.8xlarge", 32),
                            ("aws", "m7i.xlarge", 4), ("aws", "c7i.large", 2),
                            ("aws", "r8i.24xlarge", 96), ("aws", "c7i.metal-24xl", 96),
                            ("gcp", "n2-standard-16", 16), ("gcp", "n2-standard-32", 32),
                            ("gcp", "n2-highmem-8", 8)):
            self.assertEqual(license.vcpus(cloud, t), n, t)

    def test_unknown(self):
        for cloud, t in (("aws", "c7i.metal"), ("aws", "m8i"), ("gcp", "n2-custom-8-16384")):
            self.assertIsNone(license.vcpus(cloud, t), t)


class WorkerType(LicenseCluster):
    """D1.3 rule 1 before D4.8: the free default is only for new workers."""

    def test_new_worker_without_key_gets_the_free_type(self):
        for doc, name, free in ((aws_doc(), AWS_WORKER, "m8i.4xlarge"),
                                (gcp_doc(), worker(gcp_doc())["name"], "n2-standard-16")):
            cfg = self.cfg(without_type(doc, key_file=False))
            self.assertEqual(self.tfvars_types(cfg, {})[name], free, name)

    def test_new_worker_with_key_gets_terraforms_default(self):
        for doc in (aws_doc(), gcp_doc()):
            name = worker(doc)["name"]
            cfg = self.cfg(without_type(doc), key=True)
            self.assertIsNone(self.tfvars_types(cfg, {})[name], name)
            self.assertEqual(license.worker_type(cfg, cfg.nodes_with("worker")[0]),
                             license.PAID_WORKER_TYPES[cfg.cloud_name])

    def test_existing_worker_keeps_its_type(self):
        for doc, current in ((aws_doc(), "m8i.8xlarge"), (gcp_doc(), "n2-standard-32")):
            name = worker(doc)["name"]
            for key in (False, True):
                cfg = self.cfg(without_type(doc, key_file=key), key=key)
                self.assertEqual(self.tfvars_types(cfg, {name: current})[name], current,
                                 (name, key))

    def test_explicit_type_always_wins(self):
        doc = aws_doc()
        worker(doc)["machine_type"] = "m8i.2xlarge"
        cfg = self.cfg(doc)
        self.assertEqual(self.tfvars_types(cfg, {AWS_WORKER: "m8i.8xlarge"})[AWS_WORKER],
                         "m8i.2xlarge")

    def test_non_workers_keep_terraforms_default(self):
        cfg = self.cfg(without_type(aws_doc(), key_file=False))
        control = cfg.nodes_with("control")[0].name
        self.assertIsNone(self.tfvars_types(cfg, {control: "m7i.2xlarge"})[control])

    def test_types_from_the_raw_terraform_state(self):
        """`terraform state pull` output (format 4): managed `node` instances
        only, by index_key; a GCP machine type URL gives its name."""
        with open(os.path.join(ROOT, "tests", "fixtures", "license-tfstate.json")) as fh:
            raw = json.load(fh)
        self.assertEqual(tf.state_instance_types(raw), {
            "boxd-aws-demo-control-0": "m7i.2xlarge",
            "boxd-aws-demo-worker-0": "m8i.8xlarge",
            "gcp-worker-0": "n2-standard-32",
            "gcp-worker-1": "n2-standard-16",
        })
        self.assertEqual(tf.state_instance_types({}), {}, "no state yet")

    def test_instance_types_pulls_the_raw_state(self):
        with open(os.path.join(ROOT, "tests", "fixtures", "license-tfstate.json")) as fh:
            raw = fh.read()
        result = subprocess.CompletedProcess([], 0, raw, "")
        with mock.patch.object(tf, "_tf", return_value=result) as run:
            types = tf.instance_types(config.Config(aws_doc(), self.dir))
        self.assertEqual(run.call_args[0][1:3], ("state", "pull"))
        self.assertEqual(types[AWS_WORKER], "m8i.8xlarge")
        with mock.patch.object(tf, "_tf", return_value=subprocess.CompletedProcess([], 0, "", "")):
            self.assertEqual(tf.instance_types(config.Config(aws_doc(), self.dir)), {})


class Destroy(LicenseCluster):
    """Destroying a cluster never depends on its license file."""

    def test_destroy_tfvars_skip_the_license(self):
        for text in (None, "not a key\n"):
            if text is not None:
                self.write_key(text)
            cfg = self.cfg(without_type(aws_doc()))
            with self.assertRaises(InstallError):
                license.key_path(cfg)
            with mock.patch("boxd_installer.clouds.aws._resolve_ami", return_value="ami-1"):
                tfvars = tf.build_tfvars(cfg, FakeState(), {AWS_WORKER: "m8i.8xlarge"},
                                         for_destroy=True)
                fresh = tf.build_tfvars(cfg, FakeState(), {}, for_destroy=True)
            types = {n["name"]: n["machine_type"] for n in tfvars["nodes"]}
            self.assertEqual(types[AWS_WORKER], "m8i.8xlarge")
            self.assertIsNone({n["name"]: n["machine_type"] for n in fresh["nodes"]}[AWS_WORKER])

    def test_cmd_destroy_with_a_missing_key_file(self):
        doc = without_type(aws_doc())
        path = os.path.join(self.dir, "cluster.toml")
        cfg = config.Config(doc, self.dir)
        out = tempfile.mkdtemp(prefix="destroy-out-")
        self.addCleanup(shutil.rmtree, out)
        args = mock.Mock(cluster=path, state_dir=self.dir, out_dir=out)
        with mock.patch.object(main.config, "load", return_value=cfg), \
             mock.patch.object(main.remote_state, "synced"), \
             mock.patch.object(main, "State", return_value=FakeState()), \
             mock.patch.object(main.tf, "init"), \
             mock.patch.object(main.tf, "instance_types", return_value={}), \
             mock.patch.object(main.tf, "destroy") as destroy, \
             mock.patch.object(main, "control_carry"), \
             mock.patch("boxd_installer.clouds.aws._resolve_ami", return_value="ami-1"), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main.cmd_destroy(args), 0)
        destroy.assert_called_once()

    def test_cmd_destroy_passes_the_existing_types(self):
        """destroy writes its tfvars with the types in the state, so an
        existing worker is not resized on the way out."""
        cfg = config.Config(without_type(aws_doc()), self.dir)
        out = tempfile.mkdtemp(prefix="destroy-out-")
        self.addCleanup(shutil.rmtree, out)
        args = mock.Mock(cluster="unused", state_dir=self.dir, out_dir=out)
        existing = {AWS_WORKER: "m8i.8xlarge"}
        with mock.patch.object(main.config, "load", return_value=cfg), \
             mock.patch.object(main.remote_state, "synced"), \
             mock.patch.object(main, "State", return_value=FakeState()), \
             mock.patch.object(main.tf, "init"), \
             mock.patch.object(main.tf, "instance_types", return_value=existing), \
             mock.patch.object(main.tf, "write_tfvars", return_value="tfvars.json") as write, \
             mock.patch.object(main.tf, "destroy") as destroy, \
             mock.patch.object(main, "control_carry"), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main.cmd_destroy(args), 0)
        (_, _, _, types), kw = write.call_args
        self.assertEqual(types, existing)
        self.assertTrue(kw["for_destroy"])
        destroy.assert_called_once_with(cfg, "tfvars.json")


class Warnings(LicenseCluster):
    def test_32_vcpu_worker_without_key(self):
        for doc, t in ((aws_doc(), "m8i.8xlarge"), (gcp_doc(), "n2-standard-32")):
            worker(doc)["machine_type"] = t
            warnings = license.free_tier_warnings(self.cfg(doc))
            self.assertEqual(len(warnings), 1, t)
            self.assertIn("the workers have 32 vCPUs and there is no license key", warnings[0])
            self.assertIn("refuses new machines while it is over the limit", warnings[0])

    def test_16_vcpu_worker_and_key_are_quiet(self):
        for doc in (aws_doc(), gcp_doc()):
            self.assertEqual(license.free_tier_warnings(self.cfg(without_type(doc, False))), [])
        doc = aws_doc()
        worker(doc)["machine_type"] = "m8i.8xlarge"
        doc["license"] = {"key_file": "license.key"}
        self.assertEqual(license.free_tier_warnings(self.cfg(doc, key=True)), [])

    def test_existing_32_vcpu_worker_counts(self):
        cfg = self.cfg(without_type(aws_doc(), key_file=False))
        warnings = license.free_tier_warnings(cfg, {AWS_WORKER: "m8i.8xlarge"})
        self.assertIn("32 vCPUs", warnings[0])

    def test_two_free_workers_are_over(self):
        doc = without_type(aws_doc(), key_file=False)
        doc["nodes"].append(dict(worker(doc), name="boxd-aws-demo-worker-1",
                                 internal_ip="10.70.1.11"))
        warnings = license.free_tier_warnings(self.cfg(doc))
        self.assertTrue(any("32 vCPUs" in w for w in warnings), warnings)

    def test_unknown_type_is_named(self):
        doc = aws_doc()
        worker(doc)["machine_type"] = "c7i.metal"
        warnings = license.free_tier_warnings(self.cfg(doc))
        self.assertIn(f"cannot count the vCPUs of {AWS_WORKER} (c7i.metal)", warnings[0])

    def test_missing_key_file_raises(self):
        with self.assertRaises(InstallError):
            license.free_tier_warnings(self.cfg(without_type(aws_doc())))


class ValidateCommand(LicenseCluster):
    def run_validate(self, doc_text: str) -> tuple[int, str, str]:
        path = os.path.join(self.dir, "cluster.toml")
        with open(path, "w") as fh:
            fh.write(doc_text)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["install.py", "--cluster", path, "validate"]), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main.main()
        return rc, out.getvalue(), err.getvalue()

    def example(self, key_file: bool) -> str:
        with open(os.path.join(ROOT, "examples", "aws", "cluster.toml")) as fh:
            text = fh.read()
        text = text.replace('machine_type = "m8i.4xlarge"', 'machine_type = "m8i.8xlarge"', 1)
        if key_file:
            text = text.replace('# key_file = "license.key"', 'key_file = "license.key"', 1)
        return text

    def test_warns_and_exits_0_without_key(self):
        rc, out, err = self.run_validate(self.example(key_file=False))
        self.assertEqual(rc, 0)
        self.assertIn("cluster.toml OK", out)
        self.assertIn("warning: the workers have 32 vCPUs and there is no license key", err)
        self.assertNotIn("as new ones", err, "every worker has a machine_type")

    def test_one_untyped_free_worker_is_quiet(self):
        """One worker without machine_type, no key: the 16-vCPU default, at
        the limit. No warning and no note."""
        text = self.example(key_file=False).replace(
            'machine_type = "m8i.8xlarge"', "", 1)
        rc, out, err = self.run_validate(text)
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("cluster.toml OK", out)

    def test_missing_key_file_exits_1(self):
        rc, out, err = self.run_validate(self.example(key_file=True))
        self.assertEqual(rc, 1)
        self.assertIn("[license].key_file license.key does not exist", err)
        self.assertNotIn("cluster.toml OK", out)

    def test_untyped_worker_over_the_limit_says_so(self):
        text = self.example(key_file=False).replace('machine_type = "m8i.8xlarge"', "", 1)
        text = text.replace("[[nodes]]", "[[nodes]]", 1)
        doc = tomllib.loads(text)
        doc["nodes"].append(dict(worker(doc), name="boxd-aws-demo-worker-1",
                                 internal_ip="10.70.1.11"))
        warnings = license.free_tier_warnings(config.Config(doc, self.dir), assume_new=True)
        self.assertIn("32 vCPUs", warnings[0])
        self.assertIn("This count takes the workers without machine_type as new ones; "
                      "`apply` counts the types that exist.", warnings[0])

    def test_quiet_with_key(self):
        self.write_key()
        rc, out, err = self.run_validate(self.example(key_file=True))
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("cluster.toml OK", out)


class InstallLicense(LicenseCluster):
    def run_install(self, returncode: int, stdout: str = "", stderr: str = ""):
        cfg = self.cfg(aws_doc(), key=True)
        result = subprocess.CompletedProcess([], returncode, stdout, stderr)
        err = io.StringIO()
        with mock.patch.object(ansible, "_ssh", return_value=result) as ssh, \
             contextlib.redirect_stderr(err):
            ansible.install_license(cfg, os.path.join(self.dir, "license.key"))
        return ssh, err.getvalue()

    def test_key_goes_on_stdin_not_the_command_line(self):
        ssh, err = self.run_install(0, stdout="license key installed\nlicense:       valid\n")
        (cfg, host, command), kw = ssh.call_args
        self.assertEqual(command, "sudo boxd-ctl license install")
        self.assertEqual(kw["input"], KEY)
        self.assertEqual(host.name, "boxd-aws-demo-control-0")
        self.assertIn("license key installed: valid", err)
        self.assertNotIn("not-a-real-key", err)

    def test_rejection_stops_the_apply_with_the_reason(self):
        with self.assertRaises(InstallError) as e:
            self.run_install(1, stderr="Error: license key refused: The license key is for "
                                       "another cluster.")
        self.assertIn("The license key is for another cluster.", str(e.exception))
        self.assertNotIn("not-a-real-key", str(e.exception))

    def test_old_boxd_ctl_warns(self):
        _, err = self.run_install(2, stderr="error: unrecognized subcommand 'license'")
        self.assertIn("warning: this assets bundle's boxd-ctl has no `license install`; the "
                      "license key is not installed", err)


class InitClusterId(LicenseCluster):
    def run_init(self, returncode: int, stdout: str = "", stderr: str = "") -> tuple:
        cfg = self.cfg(aws_doc())
        result = subprocess.CompletedProcess([], returncode, stdout, stderr)
        err = io.StringIO()
        with mock.patch.object(ansible, "ctl", return_value=result) as ctl, \
             contextlib.redirect_stderr(err):
            ansible.init_cluster_id(cfg)
        return ctl, err.getvalue()

    def test_runs_the_verb(self):
        ctl, err = self.run_init(0, stdout="cluster id created: " + "a" * 32 + "\n")
        self.assertEqual(ctl.call_args[0][1], "license init-cluster-id")
        self.assertIn("cluster id created", err)

    def test_old_boxd_ctl_warns(self):
        _, err = self.run_init(2, stderr="error: unrecognized subcommand 'init-cluster-id'")
        self.assertIn("warning: this assets bundle's boxd-ctl has no `license "
                      "init-cluster-id`", err)

    def test_failure_stops_the_apply(self):
        with self.assertRaises(InstallError):
            self.run_init(1, stderr="Error: no leader")


class ApplyOrder(LicenseCluster):
    """`apply`: the key file is checked before terraform; the warnings come
    before the plan; the key is installed before the templates are seeded."""

    def apply(self, doc: dict, existing: dict, calls: mock.Mock | None = None) -> mock.Mock:
        cfg = config.Config(doc, self.dir)
        calls = calls or mock.Mock()
        args = mock.Mock(state_dir=self.dir, out_dir=self.dir, cache_dir=self.dir)
        with mock.patch.multiple(main, State=mock.DEFAULT, assets=mock.DEFAULT,
                                 control_carry=mock.DEFAULT, composio=mock.DEFAULT,
                                 render=mock.DEFAULT), \
             mock.patch.multiple(main.tf, init=calls.tf_init, plan=calls.tf_plan,
                                 apply_plan=mock.DEFAULT, read_outputs=mock.DEFAULT,
                                 write_tfvars=calls.write_tfvars,
                                 instance_types=mock.Mock(return_value=existing)), \
             mock.patch.multiple(main.ansible, set_keys_dir=mock.DEFAULT,
                                 set_tf_outputs=mock.DEFAULT, preflight=mock.DEFAULT,
                                 converge=calls.converge, wait_for_leader=mock.DEFAULT,
                                 learners_gave_up=mock.Mock(return_value=[]),
                                 _registered_nodes=mock.Mock(return_value=9),
                                 verify_registration=mock.DEFAULT,
                                 init_cluster_id=calls.init_cluster_id,
                                 install_license=calls.install_license,
                                 set_telemetry=calls.set_telemetry,
                                 seed_templates=calls.seed_templates,
                                 configure_orgs=mock.DEFAULT,
                                 acme_eab_spent=mock.Mock(return_value=False)), \
             mock.patch.object(main, "log", side_effect=lambda m: calls.log(m)), \
             contextlib.redirect_stdout(io.StringIO()):
            main.assets.computer_image.return_value = ("computer", "v1")
            main.State.return_value.load_providers.return_value = {}
            main._apply(cfg, args)
        return calls

    def names(self, calls) -> list[str]:
        return [c[0] for c in calls.mock_calls]

    def test_install_runs_before_seed_templates(self):
        self.write_key()
        calls = self.apply(without_type(aws_doc()), {})
        names = self.names(calls)
        self.assertLess(names.index("install_license"), names.index("seed_templates"))
        # The cluster ID after the converge (every node on the new bundle),
        # and before the key: a bound key needs it.
        self.assertLess(names.index("converge"), names.index("init_cluster_id"))
        self.assertLess(names.index("init_cluster_id"), names.index("install_license"))
        self.assertEqual(calls.install_license.call_args[0][1],
                         os.path.join(self.dir, "license.key"))
        # [telemetry] enabled is not set: neither on nor off.
        self.assertNotIn("set_telemetry", names)

    def test_telemetry_runs_after_the_key(self):
        self.write_key()
        doc = without_type(aws_doc())
        doc["telemetry"] = {"enabled": False}
        names = self.names(self.apply(doc, {}))
        # `off` needs a key with telemetry opt-out.
        self.assertLess(names.index("install_license"), names.index("set_telemetry"))

    def test_no_key_file_no_install(self):
        calls = self.apply(without_type(aws_doc(), key_file=False), {})
        self.assertNotIn("install_license", self.names(calls))
        self.assertIn("init_cluster_id", self.names(calls), "the ID is made without a key too")

    def test_warnings_come_before_the_plan(self):
        calls = self.apply(without_type(aws_doc(), key_file=False),
                           {AWS_WORKER: "m8i.8xlarge"})
        names = self.names(calls)
        warn = next(i for i, c in enumerate(calls.mock_calls)
                    if c[0] == "log" and "32 vCPUs" in c[1][0])
        self.assertLess(warn, names.index("tf_plan"))
        # The existing type goes to the tfvars.
        self.assertEqual(calls.write_tfvars.call_args[0][3], {AWS_WORKER: "m8i.8xlarge"})

    def test_missing_key_file_stops_before_terraform(self):
        calls = mock.Mock()
        with self.assertRaises(InstallError) as e:
            self.apply(without_type(aws_doc()), {}, calls)
        self.assertIn("does not exist", str(e.exception))
        self.assertEqual(calls.mock_calls, [], "nothing ran: no terraform, no tfvars")


if __name__ == "__main__":
    unittest.main()
