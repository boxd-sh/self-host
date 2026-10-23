"""[snapshot_archive]: cluster.toml turns it on, terraform creates the bucket
on AWS (terraform/aws/snapshot_archive.tf), and only control and worker
configs carry it. Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import os
import shutil
import tempfile
import tomllib
import unittest

from boxd_installer import config, local_ca, render
from boxd_installer.util import InstallError

from test_local_ca import Alloc, KeysState
from test_machine_sizes import ROOT, aws_doc, validate
from test_private_ingress import tfvars

CONTROL = "boxd-aws-demo-control-0"
WORKER = "boxd-aws-demo-worker-0"
TF_STORE = {"endpoint": "https://s3.us-east-1.amazonaws.com", "bucket": "tf-archive",
            "region": "us-east-1", "access_key": "AKIATF", "secret_key": "tf-secret"}

def archive_doc(**table) -> dict:
    doc = aws_doc()
    doc["snapshot_archive"] = table
    return doc

class Validation(unittest.TestCase):
    def test_absent_and_empty_are_valid(self):
        self.assertEqual(validate(aws_doc()), "")
        self.assertEqual(validate(archive_doc()), "")
        self.assertEqual(validate(archive_doc(archive_dir="snaps/archive")), "")

    def test_archive_dir(self):
        for bad in ("", "/abs", "trailing/", "a//b", "a/../b", "./a", "org", "org/x"):
            with self.subTest(bad=bad):
                self.assertIn("must be a relative folder", validate(archive_doc(archive_dir=bad)))
        self.assertEqual(validate(archive_doc(archive_dir="orgs")), "")

    def test_store_and_sse_stay_out_of_cluster_toml(self):
        self.assertIn("[snapshot_archive].secret_key doesn't belong in cluster.toml",
                      validate(archive_doc(secret_key="x")))
        self.assertIn("[snapshot_archive].bucket doesn't belong", validate(archive_doc(bucket="b")))
        self.assertIn("[snapshot_archive].sse is not supported", validate(archive_doc(sse="AES256")))
        self.assertIn("[snapshot_archive].prefix: unknown key", validate(archive_doc(prefix="p")))

    def test_aws_only_switch(self):
        doc = archive_doc()
        doc["aws"]["auto_snapshot_archive"] = "no"
        self.assertIn("auto_snapshot_archive must be true or false", validate(doc))
        with open(os.path.join(ROOT, "examples", "gcp", "cluster.toml"), "rb") as fh:
            gcp = tomllib.load(fh)
        gcp["gcp"]["auto_snapshot_archive"] = False
        self.assertIn("[gcp].auto_snapshot_archive is not supported", validate(gcp))

class Tfvars(unittest.TestCase):
    def test_off_unless_declared(self):
        self.assertFalse(tfvars(aws_doc())["enable_snapshot_archive"])

    def test_declared_creates_the_bucket(self):
        self.assertTrue(tfvars(archive_doc())["enable_snapshot_archive"])

    def test_own_bucket(self):
        doc = archive_doc()
        doc["aws"]["auto_snapshot_archive"] = False
        self.assertFalse(tfvars(doc)["enable_snapshot_archive"])

class Render(unittest.TestCase):
    def setUp(self):
        self.keys = tempfile.mkdtemp(prefix="snapshot-archive-")
        for f in (local_ca.CERT_FILE, local_ca.KEY_FILE):
            open(os.path.join(self.keys, f), "w").close()

    def tearDown(self):
        shutil.rmtree(self.keys)

    def configs(self, doc, tf_store=None, overlay=None) -> dict:
        doc["acme"] = {"ca": "local"}
        cfg = config.Config(doc)
        cfg.apply_overlay(overlay or {})
        tf_out = {"nodes": {n.name: {"external_ip": "203.0.113.7", "instance_id": "i-0"}
                            for n in cfg.nodes}}
        if tf_store:
            tf_out["snapshot_archive"] = tf_store
        files = render.Renderer(cfg, Alloc(), tf_out, KeysState(self.keys)).deploy_configs()
        return {k: tomllib.loads(v) for k, v in files.items() if k.endswith(".toml")}

    def test_only_control_and_worker(self):
        out = self.configs(archive_doc(archive_dir="snaps"), TF_STORE)
        want = {**TF_STORE, "archive_dir": "snaps"}
        self.assertEqual(out[f"deploy-config/{CONTROL}/control.toml"]["snapshot_archive"], want)
        self.assertEqual(out[f"deploy-config/{WORKER}/worker.toml"]["snapshot_archive"], want)
        for name, doc in out.items():
            if not name.endswith(("/control.toml", "/worker.toml")):
                self.assertNotIn("snapshot_archive", doc, name)

    def test_not_declared_renders_nothing(self):
        # e.g. the table was removed: the bucket's outputs alone don't turn it on
        out = self.configs(aws_doc(), TF_STORE)
        self.assertFalse(any("snapshot_archive" in d for d in out.values()))

    def test_providers_win(self):
        own = {**TF_STORE, "bucket": "own-bucket", "secret_key": "own"}
        out = self.configs(archive_doc(), TF_STORE, {"snapshot_archive": own})
        self.assertEqual(out[f"deploy-config/{WORKER}/worker.toml"]["snapshot_archive"], own)

    def test_own_bucket_from_providers_only(self):
        doc = aws_doc()
        doc["aws"]["auto_snapshot_archive"] = False
        out = self.configs(doc, None, {"snapshot_archive": TF_STORE})
        self.assertEqual(out[f"deploy-config/{CONTROL}/control.toml"]["snapshot_archive"], TF_STORE)

    def test_declared_without_a_store_fails(self):
        with self.assertRaisesRegex(InstallError, "has no endpoint, bucket, region, access_key, secret_key"):
            self.configs(archive_doc())

if __name__ == "__main__":
    unittest.main()
