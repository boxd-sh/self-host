"""[storage_engine]: cluster.toml turns boxd's storage engine on for every
worker. Terraform makes the bucket (AWS, GCP), the render gives workers
[storage_engine] and a 0600 bucket.toml, and apply sets the cluster switches
behind a one-way guard. Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from unittest import mock

from boxd_installer import ansible, config, local_ca, main, render, storage_engine, tf
from boxd_installer.state import State
from boxd_installer.util import InstallError

from test_local_ca import Alloc, KeysState
from test_machine_sizes import ROOT, FakeCtl, FakeState, aws_doc, validate

WORKER = "boxd-aws-demo-worker-0"

LICENSED = {"key_file": "license.key"}

def engine_doc(table=None, cloud="aws", **cloud_keys) -> dict:
    doc = aws_doc() if cloud == "aws" else gcp_doc()
    doc["storage_engine"] = {} if table is None else table
    doc["license"] = dict(LICENSED)
    doc[cloud].update(cloud_keys)
    return doc

def gcp_doc() -> dict:
    with open(os.path.join(ROOT, "examples", "gcp", "cluster.toml"), "rb") as fh:
        return tomllib.load(fh)

def tfvars(doc: dict) -> dict:
    if "aws" in doc:
        doc["aws"]["ami"] = "ami-0123456789abcdef0"  # skip the SSM lookup
        return tf.build_tfvars(config.Config(doc), FakeState())
    from boxd_installer.clouds import gcp
    from unittest import mock
    with mock.patch.object(gcp, "_resolve_boot_image", return_value="img"):
        return tf.build_tfvars(config.Config(doc), FakeState())

class Validation(unittest.TestCase):
    def test_absent_and_empty_are_valid(self):
        self.assertEqual(validate(aws_doc()), "")
        self.assertEqual(validate(engine_doc()), "")

    def test_every_setting_in_range(self):
        doc = engine_doc({"default_vm_storage": "bucket", "hibernation_blobs": "auto",
                          "ram_cache_gib": 8, "hibernated_ram_keep_hours": 0.05,
                          "copy_mib_per_sec": 512, "cache_gib": 40, "freeze_interval_secs": 300})
        self.assertEqual(validate(doc), "")

    def test_out_of_range_and_wrong_types(self):
        for key, bad, want in (("ram_cache_gib", 0, "a positive integer"),
                               ("ram_cache_gib", True, "a positive integer"),
                               ("cache_gib", 1.5, "a positive integer"),
                               ("freeze_interval_secs", -1, "a positive integer"),
                               ("copy_mib_per_sec", 1048577, "an integer from 1 to 1048576"),
                               ("hibernated_ram_keep_hours", 9000, "a number from 0 to 8784"),
                               ("hibernated_ram_keep_hours", False, "a number from 0 to 8784"),
                               ("default_vm_storage", "blockd", "must be one of bucket, file"),
                               ("hibernation_blobs", "on", "must be one of uploads, auto, off")):
            with self.subTest(key=key, bad=bad):
                self.assertIn(want, validate(engine_doc({key: bad})))

    def test_forced_keys_are_refused(self):
        for k in ("backends", "dir", "config", "prefix"):
            with self.subTest(k=k):
                self.assertIn(f"[storage_engine].{k} is forced by the installer",
                              validate(engine_doc({k: "x"})))

    def test_the_store_stays_out_of_cluster_toml(self):
        for k in ("bucket", "secret_key", "endpoint", "conditional_put"):
            with self.subTest(k=k):
                self.assertIn(f"[storage_engine].{k} doesn't belong in cluster.toml",
                              validate(engine_doc({k: "x"})))

    def test_unknown_key(self):
        self.assertIn("[storage_engine].ram_gib: unknown key", validate(engine_doc({"ram_gib": 1})))

    def test_not_a_table(self):
        doc = aws_doc()
        doc["storage_engine"] = "on"
        self.assertIn("[storage_engine] must be a table", validate(doc))

    def test_bucket_default_needs_a_license_file(self):
        doc = engine_doc()
        del doc["license"]
        self.assertIn("needs [license] key_file", validate(doc))
        doc["storage_engine"] = {"default_vm_storage": "file"}
        self.assertEqual(validate(doc), "")

    def test_auto_storage_bucket_is_a_bool_on_both_clouds(self):
        self.assertIn("[aws].auto_storage_bucket must be true or false",
                      validate(engine_doc(auto_storage_bucket="no")))
        self.assertEqual(validate(engine_doc(cloud="gcp", auto_storage_bucket=False)), "")
        self.assertIn("[gcp].auto_storage_bucket must be true or false",
                      validate(engine_doc(cloud="gcp", auto_storage_bucket=1)))

    def test_gcp_name_lengths(self):
        doc = engine_doc(cloud="gcp")
        doc["cluster"]["name"] = "a" * 23
        doc["nodes"] = [dict(n, name=n["name"].replace(n["name"].split("-")[0], "a" * 23, 1))
                        for n in doc["nodes"]]
        self.assertIn("must be <= 22 chars on GCP", validate(doc))
        doc["cluster"]["name"] = "a" * 22
        doc["gcp"]["project"] = "p" * 40
        self.assertIn("over GCS's 63", validate(doc))

    def test_aws_bucket_access_vpc_counts_the_storage_bucket(self):
        doc = engine_doc(auto_backups=False, bucket_access="vpc")
        self.assertEqual(validate(doc), "")
        del doc["storage_engine"]
        self.assertIn("there are none", validate(doc))

class ProvidersStore(unittest.TestCase):
    STORE = {"endpoint": "https://objects.example", "region": "r1", "bucket": "b",
             "access_key": "AK", "secret_key": "SK", "conditional_put": "probe",
             "virtual_hosted_style": False}

    def test_a_full_store_is_valid(self):
        config.validate_providers({"storage_engine": self.STORE})

    def test_bad_store_keys(self):
        for bad, want in (({"cache_gib": 3}, "not a store key"),
                          ({"conditional_put": "maybe"}, "conditional_put must be one of"),
                          ({"virtual_hosted_style": "yes"}, "virtual_hosted_style must be true"),
                          ({"bucket": ""}, "must be a non-empty quoted string"),
                          ({"endpoint": "objects.example"}, "must start with https://")):
            with self.subTest(bad=bad), self.assertRaisesRegex(InstallError, want):
                config.validate_providers({"storage_engine": {**self.STORE, **bad}})

class Tfvars(unittest.TestCase):
    def test_off_unless_declared(self):
        self.assertFalse(tfvars(aws_doc())["enable_storage_bucket"])
        self.assertFalse(tfvars(gcp_doc())["enable_storage_bucket"])

    def test_declared_makes_the_bucket(self):
        self.assertTrue(tfvars(engine_doc())["enable_storage_bucket"])
        self.assertTrue(tfvars(engine_doc(cloud="gcp"))["enable_storage_bucket"])

    def test_own_bucket(self):
        self.assertFalse(tfvars(engine_doc(auto_storage_bucket=False))["enable_storage_bucket"])
        self.assertFalse(tfvars(engine_doc(cloud="gcp", auto_storage_bucket=False))
                         ["enable_storage_bucket"])

TF_S3 = {"kind": "s3", "bucket": "tf-storage", "region": "us-east-1",
         "access_key": "AKIATF", "secret_key": "tf-secret"}
TF_GCS = {"kind": "gcs", "bucket": "proj-demo-storage"}
OWN = {"endpoint": "https://objects.example", "region": "lon2", "bucket": "own",
       "access_key": "AKOWN", "secret_key": "own-secret"}
PREFIX = "engine/0123456789abcdef"

class EngineState(KeysState):
    data: dict = {}
    def storage_prefix(self): return PREFIX

class Render(unittest.TestCase):
    def setUp(self):
        self.keys = tempfile.mkdtemp(prefix="storage-engine-")
        for f in (local_ca.CERT_FILE, local_ca.KEY_FILE):
            open(os.path.join(self.keys, f), "w").close()

    def tearDown(self):
        shutil.rmtree(self.keys)

    def renderer(self, doc, tf_store=None, overlay=None):
        doc["acme"] = {"ca": "local"}
        cfg = config.Config(doc)
        cfg.apply_overlay(overlay or {})
        tf_out = {"nodes": {n.name: {"external_ip": "203.0.113.7", "instance_id": "i-0"}
                            for n in cfg.nodes}}
        if tf_store:
            tf_out["storage_bucket"] = tf_store
        return render.Renderer(cfg, Alloc(), tf_out, EngineState(self.keys))

    def files(self, doc, tf_store=None, overlay=None) -> dict:
        r = self.renderer(doc, tf_store, overlay)
        files = r.deploy_configs()
        files["group_vars"] = r.group_vars()
        return files

    def toml(self, files, path) -> dict:
        return tomllib.loads(files[path])

    def test_only_workers_get_the_engine_and_the_bucket_file(self):
        files = self.files(engine_doc({"cache_gib": 40, "ram_cache_gib": 8}), TF_S3)
        worker = self.toml(files, f"deploy-config/{WORKER}/worker.toml")
        self.assertEqual(worker["storage_engine"], {
            "backends": ["bucket"], "ram_cache_gib": 8,
            "bucket": {"config": "/etc/boxd/bucket.toml", "cache_gib": 40}})
        self.assertEqual(worker["worker"]["templates"], "both")
        self.assertEqual(self.toml(files, f"deploy-config/{WORKER}/bucket.toml"), {
            "kind": "s3", "region": "us-east-1", "bucket": "tf-storage", "prefix": PREFIX,
            "access_key_id": "AKIATF", "secret_access_key": "tf-secret"})
        for path, body in files.items():
            if path.endswith(".toml") and not path.startswith(f"deploy-config/{WORKER}/"):
                self.assertNotIn("storage_engine", tomllib.loads(body), path)
        self.assertEqual([p for p in files if p.endswith("bucket.toml")],
                         [f"deploy-config/{WORKER}/bucket.toml"])
        self.assertIn("boxd_storage_engine: true", files["group_vars"])

    def test_gcs_has_no_key(self):
        files = self.files(engine_doc(cloud="gcp"), TF_GCS)
        worker = next(p for p in files if p.endswith("/bucket.toml"))
        self.assertEqual(self.toml(files, worker), {"kind": "gcs", "bucket": "proj-demo-storage",
                                                    "prefix": PREFIX})

    def test_no_conditional_put_so_the_open_probes(self):
        files = self.files(engine_doc(), TF_S3)
        self.assertNotIn("conditional_put", self.toml(files, f"deploy-config/{WORKER}/bucket.toml"))

    def test_without_the_table_nothing(self):
        files = self.files(aws_doc(), TF_S3)
        self.assertFalse([p for p in files if p.endswith("bucket.toml")])
        self.assertNotIn("storage_engine", self.toml(files, f"deploy-config/{WORKER}/worker.toml"))
        self.assertNotIn("templates", self.toml(files, f"deploy-config/{WORKER}/worker.toml")["worker"])
        self.assertIn("boxd_storage_engine: false", files["group_vars"])

    def test_an_operator_templates_setting_wins(self):
        doc = engine_doc()
        doc["worker"] = {"templates": "fetch"}
        files = self.files(doc, TF_S3)
        self.assertEqual(self.toml(files, f"deploy-config/{WORKER}/worker.toml")["worker"]["templates"],
                         "fetch")

    def test_own_bucket_replaces_terraforms(self):
        doc = engine_doc(auto_storage_bucket=False)
        files = self.files(doc, None, {"storage_engine": {**OWN, "virtual_hosted_style": True}})
        self.assertEqual(self.toml(files, f"deploy-config/{WORKER}/bucket.toml"), {
            "kind": "s3", "endpoint": "https://objects.example", "region": "lon2", "bucket": "own",
            "prefix": PREFIX, "access_key_id": "AKOWN", "secret_access_key": "own-secret",
            "virtual_hosted_style": True})

    def test_own_bucket_refused_while_terraform_makes_one(self):
        with self.assertRaisesRegex(InstallError, "auto_storage_bucket = false"):
            self.files(engine_doc(), TF_S3, {"storage_engine": OWN})

    def test_no_bucket_at_all(self):
        with self.assertRaisesRegex(InstallError, "auto_storage_bucket = false. Set your bucket"):
            self.files(engine_doc(auto_storage_bucket=False))
        with self.assertRaisesRegex(InstallError, "has no secret_key"):
            self.files(engine_doc(auto_storage_bucket=False), None,
                       {"storage_engine": {k: v for k, v in OWN.items() if k != "secret_key"}})

    def test_identity(self):
        r = self.renderer(engine_doc(), TF_S3)
        self.assertEqual(r.storage_identity(), {"kind": "s3", "bucket": "tf-storage", "endpoint": "",
                                                "prefix": PREFIX, "auto": True})
        self.assertEqual(self.renderer(aws_doc(), TF_S3).storage_identity(), {})

class Prefix(unittest.TestCase):
    def test_made_once_kept_and_cleared_by_destroy(self):
        d = tempfile.mkdtemp(prefix="storage-prefix-")
        self.addCleanup(shutil.rmtree, d)
        st = State(d)
        first = st.storage_prefix()
        self.assertRegex(first, r"^engine/[0-9a-f]{16}$")
        self.assertEqual(State(d).storage_prefix(), first)
        st.set_storage_guard({"bucket": "b"})
        st.clear_storage_engine()
        again = State(d)
        self.assertIsNone(again.storage_guard())
        self.assertNotEqual(again.storage_prefix(), first)

CATALOG_ON = "storage catalog: on\n  every Raft member must report storage-catalog-8 for on\n"
CATALOG_OFF = "storage catalog: off\n  every Raft member must report storage-catalog-8 for on\n"
BLOBS = "hibernation blobs: {}\n  a wake moves a machine away after …\n"
DEFAULT = "new machines: {} storage\nworkers with the storage engine: worker-2 (bucket)\n"
INACTIVE = ("new machines: bucket storage (inactive: no license key with bucket-storage; they are "
            "born on file)\nworkers with the storage engine: worker-2 (bucket)\n")
IDENTITY = {"kind": "s3", "bucket": "tf-storage", "endpoint": "", "prefix": PREFIX, "auto": True}

class Ctl:
    """boxd-ctl: each command answers from `replies` (a string, a
    (rc, stdout, stderr) tuple, or a list consumed in turn); setting a switch
    changes what its read-back says."""
    def __init__(self, **replies):
        self.replies = {"cluster storage-catalog": CATALOG_OFF,
                        "cluster hibernation-blobs": BLOBS.format("off"),
                        "cluster default-vm-storage": DEFAULT.format("file"), **replies}
        self.commands: list[str] = []

    def __call__(self, cfg, command, check=True, quiet=False):
        self.commands.append(command)
        if command == "cluster set-storage-catalog on":
            self.replies["cluster storage-catalog"] = CATALOG_ON
        r = self.replies.get(command, "")
        if isinstance(r, list):
            r = r.pop(0) if len(r) > 1 else r[0]
        if command == "cluster set-storage-catalog on" and not r:
            r = CATALOG_ON
        rc, out, err = r if isinstance(r, tuple) else (0, r, "")
        return subprocess.CompletedProcess(command, rc, out, err)

class FakeRenderer:
    def __init__(self, identity=None):
        self.identity = dict(IDENTITY) if identity is None else identity
    def storage_identity(self): return self.identity

def bundle(features) -> str:
    root = tempfile.mkdtemp(prefix="storage-bundle-")
    with open(os.path.join(root, "MANIFEST.json"), "w") as fh:
        json.dump({"git_sha": "a" * 40, "features": features}, fh)
    return root

class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="storage-state-")
        self.addCleanup(shutil.rmtree, self.dir)
        self.state = State(self.dir)
        self.state.data["applied_assets"] = {"sha": "a" * 40}
        self.state.save()
        quiet = contextlib.redirect_stderr(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        p = mock.patch.object(storage_engine.time, "sleep")
        p.start()
        self.addCleanup(p.stop)

    def cfg(self, doc=None):
        return config.Config(engine_doc() if doc is None else doc)

class Guard(Base):
    def check(self, cfg, ctl, features=("bucket",), cached=True):
        if cached:
            with open(os.path.join(self.dir, "tf-outputs.json"), "w") as fh:
                json.dump({"nodes": {}}, fh)
        with mock.patch.object(storage_engine.ansible, "ctl", ctl), \
                mock.patch.object(storage_engine.ansible, "set_tf_outputs"):
            return storage_engine.check_guard(cfg, self.state, bundle(list(features)))

    def test_not_engaged_on_a_fresh_cluster(self):
        self.state.data.pop("applied_assets")
        ctx = self.check(self.cfg(aws_doc()), Ctl(), features=(), cached=False)
        self.assertFalse(ctx.engaged)

    def test_the_live_catalog_engages_it_without_a_record(self):
        ctl = Ctl(**{"cluster storage-catalog": CATALOG_ON})
        with self.assertRaisesRegex(InstallError, r"its storage catalog is on.*can't be removed"):
            self.check(self.cfg(aws_doc()), ctl)

    def test_the_record_engages_it_when_control_does_not_answer(self):
        self.state.set_storage_guard(dict(IDENTITY))
        ctl = Ctl(**{"cluster storage-catalog": (1, "", "Error: transport error")})
        with self.assertRaisesRegex(InstallError, r"installer state.*can't be removed"):
            self.check(self.cfg(aws_doc()), ctl)

    def test_a_bundle_without_the_engine_is_refused(self):
        self.state.set_storage_guard(dict(IDENTITY))
        with self.assertRaisesRegex(InstallError, "a bundle without the engine"):
            self.check(self.cfg(), Ctl(**{"cluster storage-catalog": CATALOG_ON}), features=())

    def test_auto_storage_bucket_is_frozen(self):
        self.state.set_storage_guard(dict(IDENTITY))
        with self.assertRaisesRegex(InstallError, "auto_storage_bucket is now false"):
            self.check(self.cfg(engine_doc(auto_storage_bucket=False)),
                       Ctl(**{"cluster storage-catalog": CATALOG_ON}))

    def test_your_own_bucket_is_frozen(self):
        self.state.set_storage_guard({**IDENTITY, "auto": False, "bucket": "own",
                                      "endpoint": "https://objects.example"})
        with open(self.state.providers_path(), "w") as fh:
            fh.write('[storage_engine]\nbucket = "other"\nendpoint = "https://objects.example"\n')
        with self.assertRaisesRegex(InstallError, "bucket would change from own to other"):
            self.check(self.cfg(engine_doc(auto_storage_bucket=False)),
                       Ctl(**{"cluster storage-catalog": CATALOG_ON}))

    def test_a_catalog_found_off_is_refused_not_flipped(self):
        self.state.set_storage_guard(dict(IDENTITY))
        ctl = Ctl()
        with self.assertRaisesRegex(InstallError, "set-storage-catalog on` once every node"):
            self.check(self.cfg(), ctl)
        self.assertNotIn("cluster set-storage-catalog on", ctl.commands)

    def test_an_old_boxd_ctl_counts_as_off(self):
        ctl = Ctl(**{"cluster storage-catalog": (2, "", "error: unrecognized subcommand 'storage-catalog'")})
        self.assertEqual(self.check(self.cfg(), ctl).catalog, "off")

    def test_engaged_and_unchanged_passes(self):
        self.state.set_storage_guard(dict(IDENTITY))
        ctx = self.check(self.cfg(), Ctl(**{"cluster storage-catalog": CATALOG_ON}))
        self.assertTrue(ctx.engaged)

class Identity(Base):
    def check(self, ctx, identity, deployed):
        with mock.patch.object(storage_engine.ansible, "deployed_bucket_identities",
                               return_value=deployed):
            storage_engine.check_identity(self.cfg(), ctx, FakeRenderer(identity))

    def test_the_recorded_bucket_and_prefix_never_change(self):
        ctx = storage_engine.Context(dict(IDENTITY), "on")
        for k, v in (("bucket", "elsewhere"), ("prefix", "engine/other")):
            with self.subTest(k=k), self.assertRaisesRegex(InstallError, f"{k} would change"):
                self.check(ctx, {**IDENTITY, k: v}, {})

    def test_a_deployed_bucket_file_counts_without_a_record(self):
        # Turned on by hand: the catalog is on, the workers name another bucket.
        ctx = storage_engine.Context(None, "on")
        deployed = {WORKER: {"kind": "s3", "bucket": "hand-made", "endpoint": "",
                             "prefix": "engine/e2e"}}
        with self.assertRaisesRegex(InstallError, f"{WORKER} runs the storage engine on hand-made"):
            self.check(ctx, dict(IDENTITY), deployed)

    def test_not_engaged_the_workers_may_move(self):
        ctx = storage_engine.Context(None, "off")
        self.check(ctx, dict(IDENTITY), {WORKER: {"bucket": "old", "prefix": "x"}})

class AdoptPrefix(Base):
    def test_the_workers_prefix_is_kept(self):
        with mock.patch.object(storage_engine.ansible, "deployed_bucket_identities",
                               return_value={WORKER: {"prefix": "engine/hand-1", "bucket": "b"}}):
            storage_engine.adopt_prefix(self.cfg(), self.state)
        self.assertEqual(self.state.storage_prefix(), "engine/hand-1")

    def test_a_prefix_in_state_wins(self):
        first = self.state.storage_prefix()
        with mock.patch.object(storage_engine.ansible, "deployed_bucket_identities") as read:
            storage_engine.adopt_prefix(self.cfg(), self.state)
        read.assert_not_called()
        self.assertEqual(self.state.storage_prefix(), first)

class BucketOpen(Base):
    def wait(self, lines):
        with mock.patch.object(storage_engine.ansible, "worker_bucket_line", side_effect=lines):
            storage_engine.wait_bucket_open(self.cfg(), timeout=60)

    def test_open(self):
        self.wait(["", "INFO boxd_worker: storage engine: the bucket is open elapsed_ms=212"])

    def test_off_fails_the_apply(self):
        with self.assertRaisesRegex(InstallError, "could not open the storage engine's bucket"):
            self.wait(["WARN storage engine: bucket mode is off on this worker: 403 Forbidden"])

    def test_a_file_only_worker(self):
        with self.assertRaisesRegex(InstallError, "built without the storage engine"):
            self.wait(["this boxd-worker was built without the `bucket` feature; remove …"])

    def test_timeout(self):
        clock = iter(range(0, 1000, 30))
        with mock.patch.object(storage_engine.time, "time", lambda: next(clock)), \
                self.assertRaisesRegex(InstallError, "did not report opening"):
            self.wait(lambda *a: "")

    def test_without_the_engine_nothing_is_read(self):
        with mock.patch.object(storage_engine.ansible, "worker_bucket_line") as read:
            storage_engine.wait_bucket_open(self.cfg(aws_doc()))
        read.assert_not_called()

class Switches(Base):
    def run_switches(self, ctl, doc=None, ctx=None):
        push = mock.Mock()
        with mock.patch.object(storage_engine.ansible, "ctl", ctl):
            storage_engine.set_switches(self.cfg(doc), self.state,
                                        ctx or storage_engine.Context(None, None),
                                        FakeRenderer(), push)
        return push

    def test_a_first_enable_in_order(self):
        ctl = Ctl()
        push = self.run_switches(ctl)
        sets = [c for c in ctl.commands if " set-" in c]
        self.assertEqual(sets, ["cluster set-storage-catalog on",
                                "cluster set-hibernation-blobs uploads",
                                "cluster set-default-vm-storage bucket"])
        push.assert_called_once()
        guard = self.state.storage_guard()
        self.assertEqual({k: guard[k] for k in IDENTITY}, IDENTITY)
        self.assertEqual(guard["bundle"], self.cfg().assets_version)

    def test_the_guard_is_pushed_before_the_other_switches(self):
        ctl = Ctl()
        order = []
        push = mock.Mock(side_effect=lambda: order.append(len(ctl.commands)))
        with mock.patch.object(storage_engine.ansible, "ctl", ctl):
            storage_engine.set_switches(self.cfg(), self.state, storage_engine.Context(None, None),
                                        FakeRenderer(), push)
        self.assertEqual(ctl.commands[order[0] - 1], "cluster set-storage-catalog on")

    def test_a_second_apply_changes_nothing(self):
        ctl = Ctl(**{"cluster storage-catalog": CATALOG_ON,
                     "cluster hibernation-blobs": BLOBS.format("uploads"),
                     "cluster default-vm-storage": DEFAULT.format("bucket")})
        self.state.set_storage_guard({"catalog_on_at": "t", "bundle": self.cfg().assets_version,
                                      **IDENTITY})
        push = self.run_switches(ctl, ctx=storage_engine.Context(self.state.storage_guard(), "on"))
        self.assertFalse([c for c in ctl.commands if " set-" in c])
        push.assert_not_called()

    def test_a_lost_record_heals(self):
        ctl = Ctl(**{"cluster storage-catalog": CATALOG_ON,
                     "cluster hibernation-blobs": BLOBS.format("uploads"),
                     "cluster default-vm-storage": DEFAULT.format("bucket")})
        push = self.run_switches(ctl, ctx=storage_engine.Context(None, "on"))
        push.assert_called_once()
        self.assertIsNotNone(self.state.storage_guard())

    def test_lag_messages_are_retried(self):
        lag = (1, "", "Error: status: FailedPrecondition, message: \"the storage catalog stays off: "
               "every Raft member must run a build that applies it (storage-catalog-8) first\"")
        ctl = Ctl(**{"cluster set-storage-catalog on": [lag, (0, CATALOG_ON, "")]})
        self.run_switches(ctl)
        self.assertEqual(ctl.commands.count("cluster set-storage-catalog on"), 2)

    def test_first_enable_without_the_license_feature_fails(self):
        refused = (1, "", "Error: status: FailedPrecondition, message: \"bucket storage needs a "
                   "license with bucket-storage: install one first (boxd-ctl license install)\"")
        ctl = Ctl(**{"cluster set-default-vm-storage bucket": refused})
        with self.assertRaisesRegex(InstallError, "needs a license key that includes bucket-storage"):
            self.run_switches(ctl)

    def test_a_lapsed_license_only_warns(self):
        ctl = Ctl(**{"cluster storage-catalog": CATALOG_ON,
                     "cluster hibernation-blobs": BLOBS.format("uploads"),
                     "cluster default-vm-storage": INACTIVE})
        with mock.patch.object(storage_engine, "log") as log:
            self.run_switches(ctl)
        self.assertTrue(any("born on file" in c.args[0] for c in log.call_args_list))
        self.assertNotIn("cluster set-default-vm-storage bucket", ctl.commands)

    def test_file_default_and_hibernation_setting(self):
        ctl = Ctl(**{"cluster default-vm-storage": DEFAULT.format("bucket")})
        self.run_switches(ctl, engine_doc({"default_vm_storage": "file", "hibernation_blobs": "auto"}))
        self.assertIn("cluster set-hibernation-blobs auto", ctl.commands)
        self.assertIn("cluster set-default-vm-storage file", ctl.commands)

    def test_a_catalog_off_with_a_record_is_refused(self):
        self.state.set_storage_guard(dict(IDENTITY))
        ctl = Ctl()
        with self.assertRaisesRegex(InstallError, "turn it back on"):
            self.run_switches(ctl)
        self.assertNotIn("cluster set-storage-catalog on", ctl.commands)

    def test_without_the_engine_nothing_is_set(self):
        ctl = Ctl()
        self.run_switches(ctl, aws_doc())
        self.assertEqual(ctl.commands, [])

class BucketTemplates(unittest.TestCase):
    def seed(self, fake, storage):
        cfg = config.Config(engine_doc())
        with mock.patch.object(ansible, "ctl", fake), mock.patch.object(ansible.time, "sleep"), \
                contextlib.redirect_stderr(io.StringIO()):
            ansible.seed_templates(cfg, "computer", "v1", "abcdef0123456789", storage=storage)

    def test_bucket_templates_have_their_own_names_and_storage(self):
        fake = FakeCtl()
        self.seed(fake, "bucket")
        creates = [c for c in fake.commands if c.startswith("template create")]
        self.assertEqual(len(creates), 3)
        for c in creates:
            self.assertIn("G --storage bucket --name 'computer-bucket-", c)
        self.assertEqual(template_storage_of(engine_doc()), "bucket")
        self.assertEqual(template_storage_of(engine_doc({"default_vm_storage": "file"})), "file")
        self.assertEqual(template_storage_of(aws_doc()), "file")

    def test_whole_names_only(self):
        fake = FakeCtl()
        # A longer name holding the one to make: a substring match took it for its own.
        fake.templates = {"tpl-x": "computer-2x8-v1-abcdef01-routed"}
        self.seed(fake, "file")
        names = [c.split("--name '")[1].split("'")[0] for c in fake.commands
                 if c.startswith("template create")]
        self.assertIn("computer-2x8-v1-abcdef01", names)
        self.assertIn("template delete tpl-x", fake.commands)

def template_storage_of(doc) -> str:
    return storage_engine.template_storage(config.Config(doc))

class Destroy(unittest.TestCase):
    def test_kept_buckets(self):
        self.assertEqual(storage_engine.kept_buckets(
            ["data.aws_caller_identity.current", "aws_s3_bucket.storage[0]",
             "aws_s3_bucket.backups[0]"]), ["aws_s3_bucket.storage[0]", "aws_s3_bucket.backups[0]"])
        self.assertIsNone(storage_engine.kept_buckets(["aws_s3_bucket.storage[0]",
                                                       'aws_instance.node["w"]']))
        self.assertIsNone(storage_engine.kept_buckets([]))

    def destroy(self, error, left):
        d = tempfile.mkdtemp(prefix="storage-destroy-")
        self.addCleanup(shutil.rmtree, d)
        st = State(d)
        st.set_storage_guard(dict(IDENTITY))
        st.storage_prefix()
        with open(os.path.join(d, "tf-outputs.json"), "w") as fh:
            json.dump({"nodes": {}, "storage_bucket": {"bucket": "demo-storage-1-r"}}, fh)
        cfg = config.Config(engine_doc())
        args = mock.Mock(cluster="c.toml", state_dir=d, out_dir=d)
        open(os.path.join(d, "terraform.tfvars.json"), "w").close()
        with mock.patch.object(main.config, "load", return_value=cfg), \
                mock.patch.object(main.remote_state, "synced"), \
                mock.patch.object(main, "State", return_value=st), \
                mock.patch.object(main.tf, "init"), \
                mock.patch.object(main.tf, "destroy", side_effect=error), \
                mock.patch.object(main.tf, "state_list", return_value=left), \
                mock.patch.object(main, "control_carry"), \
                mock.patch.object(main, "log") as log, \
                contextlib.redirect_stdout(io.StringIO()):
            rc = main.cmd_destroy(args)
        return rc, st, " ".join(c.args[0] for c in log.call_args_list)

    def test_a_kept_bucket_is_reported_and_the_guard_cleared(self):
        rc, st, logged = self.destroy(InstallError("BucketNotEmpty"), ["aws_s3_bucket.storage[0]"])
        self.assertEqual(rc, 0)
        self.assertIn("kept demo-storage-1-r (aws_s3_bucket.storage[0]): not empty", logged)
        self.assertIsNone(st.storage_guard())
        self.assertNotIn("storage_prefix", st.data)

    def test_any_other_failure_stands(self):
        with self.assertRaisesRegex(InstallError, "boom"):
            self.destroy(InstallError("boom"), ['aws_instance.node["w"]'])

class ApplyOrder(unittest.TestCase):
    """The storage steps' places in _apply: the guard before terraform, the
    bucket-open check before the cluster id, the switches after the license
    and before the templates (with their storage)."""
    def test_order(self):
        d = tempfile.mkdtemp(prefix="storage-apply-")
        self.addCleanup(shutil.rmtree, d)
        with open(os.path.join(d, "license.key"), "w") as fh:
            fh.write("boxd-lic1_not-a-real-key\n")
        cfg = config.Config(engine_doc(), d)
        calls = mock.Mock()
        calls.template_storage.return_value = "bucket"
        args = mock.Mock(state_dir=d, out_dir=d, cache_dir=d)
        se = mock.Mock(check_guard=calls.check_guard, adopt_prefix=calls.adopt_prefix,
                       check_identity=calls.check_identity, wait_bucket_open=calls.wait_bucket_open,
                       set_switches=calls.set_switches, template_storage=calls.template_storage)
        with mock.patch.multiple(main, State=mock.DEFAULT, assets=mock.DEFAULT,
                                 control_carry=mock.DEFAULT, composio=mock.DEFAULT,
                                 render=mock.DEFAULT, license=mock.DEFAULT,
                                 check_storage_bundle=mock.DEFAULT, storage_engine=se), \
             mock.patch.multiple(main.tf, init=mock.DEFAULT, plan=calls.tf_plan,
                                 apply_plan=mock.DEFAULT, read_outputs=mock.DEFAULT,
                                 write_tfvars=mock.DEFAULT,
                                 instance_types=mock.Mock(return_value={}),
                                 network=mock.Mock(return_value=(None, {}))), \
             mock.patch.multiple(main.ansible, set_keys_dir=mock.DEFAULT,
                                 set_tf_outputs=mock.DEFAULT, preflight=mock.DEFAULT,
                                 converge=calls.converge, wait_for_leader=mock.DEFAULT,
                                 sync_config=mock.DEFAULT,
                                 deployed_network_modes=mock.Mock(return_value={}),
                                 machine_count=mock.Mock(return_value=0),
                                 learners_gave_up=mock.Mock(return_value=[]),
                                 _registered_nodes=mock.Mock(return_value=9),
                                 verify_registration=mock.DEFAULT,
                                 init_cluster_id=calls.init_cluster_id,
                                 pause_machine_specs=mock.DEFAULT,
                                 turn_on_machine_specs=mock.DEFAULT,
                                 install_license=calls.install_license,
                                 set_telemetry=mock.DEFAULT,
                                 seed_templates=calls.seed_templates, configure_orgs=mock.DEFAULT,
                                 acme_eab_spent=mock.Mock(return_value=False)), \
             mock.patch.object(main, "log"), contextlib.redirect_stdout(io.StringIO()):
            main.assets.computer_image.return_value = ("computer", "v1")
            main.State.return_value.load_providers.return_value = {}
            main.State.return_value.removed_members.return_value = {}
            main.license.free_tier_warnings.return_value = []
            main.license.key_path.return_value = os.path.join(d, "license.key")
            main._apply(cfg, args)
        names = [c[0] for c in calls.mock_calls]
        at = names.index
        self.assertLess(at("check_guard"), at("tf_plan"))
        self.assertLess(at("adopt_prefix"), at("converge"))
        self.assertLess(at("check_identity"), at("converge"))
        self.assertLess(at("converge"), at("wait_bucket_open"))
        self.assertLess(at("wait_bucket_open"), at("init_cluster_id"))
        self.assertLess(at("install_license"), at("set_switches"))
        self.assertLess(at("set_switches"), at("seed_templates"))
        self.assertEqual(calls.seed_templates.call_args.kwargs["storage"], "bucket")

if __name__ == "__main__":
    unittest.main()
