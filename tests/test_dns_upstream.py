"""[worker].dns_upstream: the resolvers boxes' DNS goes to (boxd-worker's BOXD_DNS_UPSTREAM).
Validation, the rendered worker unit variable and GKE pod env, and worker.toml left without it.
Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import os
import shutil
import tempfile
import tomllib
import unittest

from boxd_installer import config, local_ca, render

from test_gke_config import gke_doc
from test_gke_render import renderer as gke_renderer
from test_local_ca import Alloc, KeysState
from test_machine_sizes import aws_doc, validate

WORKER = "boxd-aws-demo-worker-0"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UNIT = os.path.join(ROOT, "ansible", "roles", "boxd-common", "templates", "boxd.service.j2")

def with_upstream(value, doc=None) -> dict:
    doc = doc or aws_doc()
    doc.setdefault("worker", {})["dns_upstream"] = value
    return doc

class Format(unittest.TestCase):
    """The value boxd-worker parses at start (and refuses to start without)."""

    def test_forms(self):
        self.assertEqual(config.dns_upstream(["10.70.0.2"]), "10.70.0.2:53")
        self.assertEqual(config.dns_upstream(["10.70.0.2:5353", "Corp.Example.com.=10.70.0.53"]),
                         "10.70.0.2:5353,corp.example.com=10.70.0.53:53")
        self.assertEqual(config.dns_upstream("8.8.8.8, env=100.100.100.100:53"),
                         "8.8.8.8:53,env=100.100.100.100:53")
        self.assertEqual(config.dns_upstream(["[fd00::53]:53"]), "[fd00::53]:53")

    def test_refusals(self):
        for value, why in (([], "list of resolvers"),
                           (5, "list of resolvers"),
                           (["corp.example.com=10.0.0.1"], "one resolver without a zone"),
                           (["1.1.1.1", "8.8.8.8"], "more than one default"),
                           (["dns.google"], "not an IP address"),
                           (["10.0.0.2", "a.com=1.1.1.1", "A.com.=2.2.2.2"], "given twice"),
                           (["10.0.0.2", "x y=1.1.1.1"], "not a DNS name"),
                           (["10.0.0.2", ""], "empty entry"),
                           (["10.0.0.2:0"], "bad port")):
            with self.assertRaisesRegex(ValueError, why, msg=repr(value)):
                config.dns_upstream(value)

class Validation(unittest.TestCase):
    def test_valid_and_invalid(self):
        self.assertEqual(validate(with_upstream(["10.70.0.2", "corp.example.com=10.80.0.53"])), "")
        self.assertIn("[worker].dns_upstream: more than one default resolver",
                      validate(with_upstream(["10.70.0.2", "10.70.0.3"])))
        self.assertEqual(validate(with_upstream(["10.60.0.2"], doc=gke_doc())), "",
                         "GKE takes it too")

class Render(unittest.TestCase):
    def setUp(self):
        self.keys = tempfile.mkdtemp(prefix="dns-upstream-")
        for f in (local_ca.CERT_FILE, local_ca.KEY_FILE):
            open(os.path.join(self.keys, f), "w").close()

    def tearDown(self):
        shutil.rmtree(self.keys)

    def renderer(self, doc):
        doc["acme"] = {"ca": "local"}
        cfg = config.Config(doc)
        tf_out = {"nodes": {n.name: {"external_ip": "203.0.113.7", "instance_id": "i-0"}
                            for n in cfg.nodes}}
        return render.Renderer(cfg, Alloc(), tf_out, KeysState(self.keys))

    def test_a_unit_variable_not_a_config_key(self):
        r = self.renderer(with_upstream(["10.70.0.2", "corp.example.com=10.80.0.53:5353"]))
        worker = tomllib.loads(r.deploy_configs()[f"deploy-config/{WORKER}/worker.toml"])["worker"]
        self.assertNotIn("dns_upstream", worker)
        self.assertIn('boxd_dns_upstream: "10.70.0.2:53,corp.example.com=10.80.0.53:5353"',
                      r.group_vars())

    def test_unset_leaves_boxds_default(self):
        self.assertNotIn("boxd_dns_upstream", self.renderer(aws_doc()).group_vars())

    def test_the_worker_unit_sets_it_only_when_given(self):
        with open(UNIT) as fh:
            unit = fh.read()
        self.assertIn("{% if boxd_dns_upstream | default('') %}\n"
                      "Environment=BOXD_DNS_UPSTREAM={{ boxd_dns_upstream }}\n{% endif %}", unit)
        worker_block = unit[unit.index("{% elif boxd_service == 'boxd-worker' %}"):]
        self.assertLess(worker_block.index("BOXD_DNS_UPSTREAM"),
                        worker_block.index("{% endif %}\nExecStart"),
                        "only in the worker's unit")

class Gke(unittest.TestCase):
    def env(self, doc) -> dict:
        r = gke_renderer(doc)
        container = next(c for c in r.worker_pod()["containers"] if c["name"] == "worker")
        return {e["name"]: e.get("value") for e in container["env"]}

    def test_the_worker_pod_gets_it(self):
        self.assertEqual(self.env(with_upstream(["10.60.0.2"], doc=gke_doc()))["BOXD_DNS_UPSTREAM"],
                         "10.60.0.2:53")
        self.assertNotIn("BOXD_DNS_UPSTREAM", self.env(gke_doc()))

if __name__ == "__main__":
    unittest.main()
