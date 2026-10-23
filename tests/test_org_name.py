"""[cluster] org_name: the name the cluster org takes when the first sign-in
creates it (boxd-console's cluster_org_name), and the slug boxd-control gives
it after.
Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import tomllib
import unittest

from boxd_installer import config, render

from test_machine_sizes import FakeAlloc, FakeState, aws_doc, validate

CONSOLE = "boxd-aws-demo-control-0"

def with_org_name(name) -> dict:
    doc = aws_doc()
    doc["cluster"]["org_name"] = name
    return doc

def rendered(doc: dict) -> dict:
    cfg = config.Config(doc)
    cfg.apply_overlay({})
    tf_out = {"nodes": {n.name: {"external_ip": "203.0.113.1"} for n in cfg.nodes}}
    out = render.Renderer(cfg, FakeAlloc(), tf_out, FakeState()).deploy_configs()
    return {path: tomllib.loads(body) for path, body in out.items()}

class OrgName(unittest.TestCase):
    def test_checked_as_the_console_does(self):
        for ok in ("Contoso", "  ", "x" * 64, "Société Générale"):
            self.assertEqual(validate(with_org_name(ok)), "", ok)
        for bad, why in (("x" * 65, "longer than 64 bytes"),
                         ("é" * 33, "longer than 64 bytes"),
                         ("Personal", "can't be 'personal'"),
                         (7, "must be a quoted string")):
            self.assertIn(why, validate(with_org_name(bad)), bad)

    def test_the_console_and_control_get_it(self):
        files = rendered(with_org_name(" Contoso "))
        for role in ("console", "control"):
            self.assertEqual(files[f"deploy-config/{CONSOLE}/{role}.toml"]["cluster_org_name"],
                             "Contoso", role)
        others = [p for p, t in files.items() if "cluster_org_name" in t
                  and not p.endswith(("/console.toml", "/control.toml"))]
        self.assertEqual(others, [])

    def test_absent_or_empty_renders_nothing(self):
        for doc in (aws_doc(), with_org_name("")):
            for role in ("console", "control"):
                toml = rendered(doc)[f"deploy-config/{CONSOLE}/{role}.toml"]
                self.assertNotIn("cluster_org_name", toml)

if __name__ == "__main__":
    unittest.main()
