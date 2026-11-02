"""The node's NIC: cluster.toml's interface when set, else read from each host
at the converge (render.IFACE_PLACEHOLDER, boxd-common). AWS names it ens5 on
some instance types and enp39s0 on others (m8i).
Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import os
import tomllib
import unittest

from boxd_installer import config, render

from test_machine_sizes import FakeAlloc, FakeState, aws_doc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

class Alloc(FakeAlloc):
    vnis = {"boxd-aws-demo-worker-0": 100}
    def bridge_gateway(self, name): return "10.100.0.1"

def rendered(doc):
    cfg = config.Config(doc)
    tf_out = {"nodes": {n.name: {"external_ip": "203.0.113.1"} for n in cfg.nodes}}
    r = render.Renderer(cfg, Alloc(), tf_out, FakeState())
    files = {p: tomllib.loads(b) for p, b in r.deploy_configs().items()}
    return files, r.group_vars()

def by_role(files, role):
    return [doc for path, doc in files.items() if path.endswith(f"/{role}.toml")]

class Interface(unittest.TestCase):
    def test_unset_is_read_from_the_host(self):
        files, gv = rendered(aws_doc())
        for doc in files.values():
            if "vrack" in doc:
                self.assertEqual(doc["vrack"]["interface"], render.IFACE_PLACEHOLDER)
        for doc in by_role(files, "proxy"):
            self.assertEqual(doc["proxy"]["public_interface"], render.IFACE_PLACEHOLDER)
        self.assertNotIn("boxd_node_interface:", gv)
        self.assertIn('vrack_interface: "{{ boxd_node_interface }}"', gv)
        self.assertIn('public_interface: "{{ boxd_node_interface }}"', gv)

    def test_set_wins(self):
        doc = aws_doc()
        doc["aws"]["interface"] = "ens5"
        files, gv = rendered(doc)
        for d in files.values():
            if "vrack" in d:
                self.assertEqual(d["vrack"]["interface"], "ens5")
        for d in by_role(files, "proxy"):
            self.assertEqual(d["proxy"]["public_interface"], "ens5")
        self.assertIn("boxd_node_interface: ens5", gv)
        self.assertNotIn(render.IFACE_PLACEHOLDER, "".join(str(d) for d in files.values()))

    def test_placeholder_has_no_braces(self):
        # boxd refuses a config with "{{" in it (an unrendered template).
        self.assertNotIn("{", render.IFACE_PLACEHOLDER)

    def test_ansible_fills_the_same_placeholder(self):
        """install-service replaces exactly render's placeholder, and
        boxd-common is where the name comes from."""
        roles = os.path.join(ROOT, "ansible", "roles", "boxd-common", "tasks")
        with open(os.path.join(roles, "install-service.yml")) as fh:
            install = fh.read()
        self.assertIn(f"replace('{render.IFACE_PLACEHOLDER}', boxd_node_interface)", install)
        with open(os.path.join(roles, "main.yml")) as fh:
            common = fh.read()
        self.assertIn("ip -o -4 addr show to {{ vrack_ip }}/32", common)
        self.assertIn("boxd_node_interface is not defined", common)

if __name__ == "__main__":
    unittest.main()
