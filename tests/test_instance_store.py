"""[[nodes]].data_disk = "instance-store" (AWS): validation, tfvars (no EBS data
volume), the ansible host var, the warnings, and the refusal to switch a worker
that exists. Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import os
import tomllib
import unittest
from unittest import mock

from boxd_installer import config, tf
from boxd_installer.clouds import aws
from boxd_installer.util import InstallError

from test_machine_sizes import FakeState, aws_doc, validate

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def gcp_doc() -> dict:
    with open(os.path.join(ROOT, "examples", "gcp", "cluster.toml"), "rb") as fh:
        return tomllib.load(fh)

def worker(doc: dict) -> dict:
    return next(n for n in doc["nodes"] if "worker" in n["roles"])

def instance_store_doc(machine_type="m8id.metal-48xl") -> dict:
    doc = aws_doc()
    w = worker(doc)
    del w["data_disk_gb"]
    w["machine_type"] = machine_type
    w["data_disk"] = "instance-store"
    return doc

class Validation(unittest.TestCase):
    def test_instance_store_worker_is_valid(self):
        self.assertEqual(validate(instance_store_doc()), "")

    def test_not_both(self):
        doc = instance_store_doc()
        worker(doc)["data_disk_gb"] = 500
        self.assertIn("not both", validate(doc))

    def test_unknown_value(self):
        doc = instance_store_doc()
        worker(doc)["data_disk"] = "/dev/nvme1n1"
        self.assertIn('data_disk must be "instance-store"', validate(doc))

    def test_worker_needs_a_data_disk(self):
        doc = instance_store_doc()
        del worker(doc)["data_disk"]
        self.assertIn("worker nodes must set data_disk_gb", validate(doc))

    def test_needs_a_machine_type(self):
        doc = instance_store_doc()
        del worker(doc)["machine_type"]
        self.assertIn("needs a machine_type with instance-store NVMe", validate(doc))

    def test_only_on_workers(self):
        doc = instance_store_doc()
        ctl = next(n for n in doc["nodes"] if "control" in n["roles"])
        ctl["data_disk"] = "instance-store"
        self.assertIn("only valid on worker nodes", validate(doc))

    def test_only_on_aws(self):
        doc = gcp_doc()
        w = worker(doc)
        del w["data_disk_gb"]
        w["data_disk"] = "instance-store"
        self.assertIn("only supported on AWS", validate(doc))

    def test_ebs_worker_unchanged(self):
        self.assertEqual(validate(aws_doc()), "")

class Warnings(unittest.TestCase):
    def test_every_instance_store_worker_is_warned_about(self):
        warnings = config.data_disk_warnings(config.Config(instance_store_doc()))
        self.assertEqual(len(warnings), 1)
        self.assertIn("wiped on stop", warnings[0])
        self.assertIn("never stop this instance", warnings[0])

    def test_type_without_instance_store(self):
        warnings = config.data_disk_warnings(config.Config(instance_store_doc("m8i.metal-48xl")))
        self.assertTrue(any("doesn't look like a type with instance-store" in w for w in warnings))

    def test_instance_store_families(self):
        for t in ("m8id.metal-48xl", "c7gd.16xlarge", "r6idn.32xlarge", "z1d.metal",
                  "i4i.metal", "i7ie.48xlarge", "im4gn.16xlarge", "is4gen.8xlarge"):
            warnings = config.data_disk_warnings(config.Config(instance_store_doc(t)))
            self.assertEqual(len(warnings), 1, t)

    def test_none_on_ebs(self):
        self.assertEqual(config.data_disk_warnings(config.Config(aws_doc())), [])

class Wiring(unittest.TestCase):
    def test_no_ebs_data_volume(self):
        cfg = config.Config(instance_store_doc())
        with mock.patch.object(aws, "tfvars", lambda cfg, state, nodes: {"nodes": nodes}):
            nodes = {n["name"]: n for n in tf.build_tfvars(cfg, FakeState())["nodes"]}
        w = next(n for n in cfg.nodes if n.has("worker"))
        self.assertIsNone(nodes[w.name]["data_disk_gb"])

    def test_host_vars(self):
        cfg = config.Config(instance_store_doc())
        w = next(n for n in cfg.nodes if n.has("worker"))
        tf_out = {"nodes": {w.name: {"instance_id": "i-0123", "data_volume_id": None}}}
        self.assertEqual(aws.host_vars(w, tf_out),
                         {"ansible_host": "i-0123", "boxd_volume_instance_store": True})

    def test_ebs_host_vars_unchanged(self):
        cfg = config.Config(aws_doc())
        w = next(n for n in cfg.nodes if n.has("worker"))
        tf_out = {"nodes": {w.name: {"instance_id": "i-0123", "data_volume_id": "vol-0abc"}}}
        hv = aws.host_vars(w, tf_out)
        self.assertEqual(hv["boxd_volume_device"],
                         "/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_vol0abc")
        self.assertNotIn("boxd_volume_instance_store", hv)

class Switch(unittest.TestCase):
    """A worker that exists keeps the kind of data disk it has."""
    def setUp(self):
        self.ebs_cfg = config.Config(aws_doc())
        self.nvme_cfg = config.Config(instance_store_doc())
        self.name = next(n.name for n in self.ebs_cfg.nodes if n.has("worker"))

    def test_new_worker_takes_either(self):
        aws.check_data_disk_switch(self.nvme_cfg, set(), set())
        aws.check_data_disk_switch(self.ebs_cfg, set(), set())

    def test_same_kind_passes(self):
        aws.check_data_disk_switch(self.ebs_cfg, {self.name}, {self.name})
        aws.check_data_disk_switch(self.nvme_cfg, {self.name}, set())

    def test_ebs_to_instance_store_refused(self):
        with self.assertRaisesRegex(InstallError, "runs on an EBS data disk"):
            aws.check_data_disk_switch(self.nvme_cfg, {self.name}, {self.name})

    def test_instance_store_to_ebs_refused(self):
        with self.assertRaisesRegex(InstallError, "runs on instance store"):
            aws.check_data_disk_switch(self.ebs_cfg, {self.name}, set())

    def test_state_data_volumes(self):
        raw = {"resources": [
            {"mode": "managed", "type": "aws_ebs_volume", "name": "data",
             "instances": [{"index_key": "w-0"}, {"index_key": "w-1"}]},
            {"mode": "managed", "type": "aws_instance", "name": "node",
             "instances": [{"index_key": "w-2"}]},
        ]}
        self.assertEqual(tf.state_data_volumes(raw), {"w-0", "w-1"})
        self.assertEqual(tf.state_data_volumes({}), set())

if __name__ == "__main__":
    unittest.main()
