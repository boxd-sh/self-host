"""[worker].tc_filters and the per-node override: validation and the rendered
worker.toml. Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import os
import shutil
import tempfile
import tomllib
import unittest

from boxd_installer import config, local_ca, render

from test_local_ca import Alloc, KeysState
from test_machine_sizes import aws_doc, validate

WORKER = "boxd-aws-demo-worker-0"
CONTROL = "boxd-aws-demo-control-0"

def node(doc: dict, name: str) -> dict:
    return next(n for n in doc["nodes"] if n["name"] == name)

class TcFiltersValidation(unittest.TestCase):
    def test_absent_is_valid(self):
        self.assertEqual(validate(aws_doc()), "")

    def test_both_modes_are_valid(self):
        for mode in config.TC_FILTERS:
            doc = aws_doc()
            doc["worker"]["tc_filters"] = mode
            self.assertEqual(validate(doc), "", mode)
            doc = aws_doc()
            node(doc, WORKER)["tc_filters"] = mode
            self.assertEqual(validate(doc), "", mode)

    def test_unknown_mode(self):
        doc = aws_doc()
        doc["worker"]["tc_filters"] = "off"
        self.assertIn("[worker].tc_filters must be one of full, anti-spoof, got 'off'",
                      validate(doc))
        doc = aws_doc()
        node(doc, WORKER)["tc_filters"] = "antispoof"
        self.assertIn(f"{WORKER}: tc_filters must be one of full, anti-spoof",
                      validate(doc))

    def test_node_override_only_on_workers(self):
        doc = aws_doc()
        node(doc, CONTROL)["tc_filters"] = "anti-spoof"
        self.assertIn(f"{CONTROL}: tc_filters is only valid on worker nodes", validate(doc))

class RenderTcFilters(unittest.TestCase):
    def setUp(self):
        self.keys = tempfile.mkdtemp(prefix="tc-filters-")
        for f in (local_ca.CERT_FILE, local_ca.KEY_FILE):
            open(os.path.join(self.keys, f), "w").close()

    def tearDown(self):
        shutil.rmtree(self.keys)

    def worker_toml(self, doc) -> dict:
        doc["acme"] = {"ca": "local"}
        cfg = config.Config(doc)
        tf_out = {"nodes": {n.name: {"external_ip": "203.0.113.7", "instance_id": "i-0"}
                            for n in cfg.nodes}}
        files = render.Renderer(cfg, Alloc(), tf_out, KeysState(self.keys)).deploy_configs()
        return tomllib.loads(files[f"deploy-config/{WORKER}/worker.toml"])["worker"]

    def test_absent_leaves_the_binary_default(self):
        self.assertNotIn("tc_filters", self.worker_toml(aws_doc()))

    def test_cluster_wide(self):
        doc = aws_doc()
        doc["worker"]["tc_filters"] = "anti-spoof"
        self.assertEqual(self.worker_toml(doc)["tc_filters"], "anti-spoof")

    def test_node_overrides_cluster(self):
        doc = aws_doc()
        doc["worker"]["tc_filters"] = "anti-spoof"
        node(doc, WORKER)["tc_filters"] = "full"
        self.assertEqual(self.worker_toml(doc)["tc_filters"], "full")

if __name__ == "__main__":
    unittest.main()
