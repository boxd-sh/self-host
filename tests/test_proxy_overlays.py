"""[proxy.tailscale] and [proxy.netbird]: the per-node name each proxy registers
under in an org's overlay network. Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import os
import shutil
import tempfile
import tomllib
import unittest

from boxd_installer import config, local_ca, render

from test_local_ca import Alloc, KeysState
from test_machine_sizes import aws_doc

class RenderProxyOverlays(unittest.TestCase):
    def setUp(self):
        self.keys = tempfile.mkdtemp(prefix="proxy-overlays-")
        for f in (local_ca.CERT_FILE, local_ca.KEY_FILE):
            open(os.path.join(self.keys, f), "w").close()

    def tearDown(self):
        shutil.rmtree(self.keys)

    def proxy_toml(self, doc) -> tuple[dict, str]:
        doc["acme"] = {"ca": "local"}
        cfg = config.Config(doc)
        node = cfg.nodes_with("proxy")[0]
        tf_out = {"nodes": {n.name: {"external_ip": "203.0.113.7", "instance_id": "i-0"}
                            for n in cfg.nodes}}
        files = render.Renderer(cfg, Alloc(), tf_out, KeysState(self.keys)).deploy_configs()
        text = files[f"deploy-config/{node.name}/proxy.toml"]
        return tomllib.loads(text)["proxy"], f"boxd-{cfg.name}-{node.name}"

    def test_absent_leaves_the_binary_default(self):
        proxy, _ = self.proxy_toml(aws_doc())
        self.assertNotIn("tailscale", proxy)
        self.assertNotIn("netbird", proxy)

    def test_named_per_node(self):
        doc = aws_doc()
        doc["proxy"]["tailscale"] = {}
        doc["proxy"]["netbird"] = {}
        proxy, name = self.proxy_toml(doc)
        self.assertEqual(proxy["tailscale"], {"hostname": name})
        self.assertEqual(proxy["netbird"], {"device_name": name})

    def test_cluster_toml_wins(self):
        doc = aws_doc()
        doc["proxy"]["tailscale"] = {"hostname": "ts-edge"}
        doc["proxy"]["netbird"] = {"device_name": "nb-edge", "state_dir": "/srv/netbird"}
        proxy, _ = self.proxy_toml(doc)
        self.assertEqual(proxy["tailscale"], {"hostname": "ts-edge"})
        self.assertEqual(proxy["netbird"], {"device_name": "nb-edge", "state_dir": "/srv/netbird"})

    def test_internal_services_survive(self):
        doc = aws_doc()
        doc["proxy"]["netbird"] = {}
        proxy, _ = self.proxy_toml(doc)
        plain, _ = self.proxy_toml(aws_doc())
        self.assertEqual(proxy["internal_services"], plain["internal_services"])

if __name__ == "__main__":
    unittest.main()
