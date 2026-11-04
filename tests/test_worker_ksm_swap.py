"""[worker].ksm and [worker].swap_gb: validation, the ansible vars, and keeping
both out of worker.toml. Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import collections
import tomllib
import unittest

from boxd_installer import config, render

from test_machine_sizes import FakeAlloc, FakeState, aws_doc, validate

class Alloc(FakeAlloc):
    vnis = collections.defaultdict(lambda: 100)

def worker_doc(**worker) -> dict:
    doc = aws_doc()
    doc["worker"] = {**doc["worker"], **worker}
    return doc

def renderer(doc) -> render.Renderer:
    cfg = config.Config(doc)
    tf_out = {"nodes": {n.name: {"external_ip": "203.0.113.1", "instance_id": "i-0"}
                        for n in cfg.nodes}}
    return render.Renderer(cfg, Alloc(), tf_out, FakeState())

def worker_tomls(r: render.Renderer) -> list[dict]:
    files = r.deploy_configs()
    return [tomllib.loads(files[f"deploy-config/{n.name}/worker.toml"])["worker"]
            for n in r.cfg.nodes_with("worker")]

class Ksm(unittest.TestCase):
    def test_validation(self):
        self.assertEqual(validate(worker_doc(ksm=True)), "")
        self.assertEqual(validate(worker_doc(ksm=False)), "")
        self.assertIn("[worker].ksm must be true or false", validate(worker_doc(ksm="yes")))

    def test_group_vars(self):
        self.assertIn("boxd_worker_ksm: true", renderer(worker_doc(ksm=True)).group_vars())
        self.assertNotIn("boxd_worker_ksm", renderer(worker_doc(ksm=False)).group_vars())
        self.assertNotIn("boxd_worker_ksm", renderer(aws_doc()).group_vars())

    def test_not_passed_to_boxd_worker(self):
        for worker in worker_tomls(renderer(worker_doc(ksm=True))):
            self.assertNotIn("ksm", worker)
            self.assertEqual(worker["warm_slots"], aws_doc()["worker"]["warm_slots"])

class Swap(unittest.TestCase):
    def test_validation(self):
        self.assertEqual(validate(worker_doc(swap_gb=[64, 256])), "")
        for bad in ([], [0], ["64G"], 64, [True]):
            self.assertIn("[worker].swap_gb", validate(worker_doc(swap_gb=bad)), bad)

    def test_default_is_one_64g_file(self):
        gv = renderer(aws_doc()).group_vars()
        self.assertIn("  - { path: /mnt/boxd-data/swap0, size: 64G }", gv)
        self.assertNotIn("swap1", gv)

    def test_one_file_per_entry(self):
        gv = renderer(worker_doc(swap_gb=[64, 256])).group_vars()
        self.assertIn("  - { path: /mnt/boxd-data/swap0, size: 64G }", gv)
        self.assertIn("  - { path: /mnt/boxd-data/swap1, size: 256G }", gv)

    def test_not_passed_to_boxd_worker(self):
        for worker in worker_tomls(renderer(worker_doc(swap_gb=[64, 256]))):
            self.assertNotIn("swap_gb", worker)

if __name__ == "__main__":
    unittest.main()
