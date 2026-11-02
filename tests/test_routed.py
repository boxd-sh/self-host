"""[network] mode = "routed" (boxd's routed network, what live migration
needs): validation, the render, and the guard on switching an existing
cluster. Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import shutil
import tempfile
import tomllib
import unittest
from unittest import mock

from boxd_installer import config, main, render
from boxd_installer.state import State
from boxd_installer.util import InstallError

from test_machine_sizes import FakeState, aws_doc, validate

POOL = "10.120.0.0/16"
W0, W1, CTL = "boxd-aws-demo-worker-0", "boxd-aws-demo-worker-1", "boxd-aws-demo-control-0"

def routed_doc(**net) -> dict:
    doc = aws_doc()
    doc["network"] = {"vni_range_start": 100, "vni_range_end": 10000,
                      "mode": "routed", "pool": POOL, **net}
    doc["nodes"].append({"name": W1, "roles": ["worker"], "internal_ip": "10.70.1.21",
                         "data_disk_gb": 200})
    return doc

class Alloc:
    """Node ids as on the e2e cluster: workers 2 and 7, the proxy 3."""
    IDS = {(CTL, "control"): 1, (W0, "worker"): 2, (CTL, "proxy"): 3, (W1, "worker"): 7}
    vnis: dict = {}
    def node_id(self, name, role): return self.IDS.get((name, role), 9)
    def peers(self): return []
    def learners(self): return []
    def bridge_subnet(self, name): raise AssertionError("no per-worker subnet in routed mode")
    def bridge_gateway(self, name): raise AssertionError("no per-worker gateway in routed mode")

class State0(FakeState):
    def egress_ca(self): return None

def renderer(doc):
    cfg = config.Config(doc)
    tf_out = {"nodes": {n.name: {"external_ip": "203.0.113.1", "instance_id": "i-0"} for n in cfg.nodes}}
    return render.Renderer(cfg, Alloc(), tf_out, State0())

def configs(doc):
    return {p: tomllib.loads(b) for p, b in renderer(doc).deploy_configs().items()}

class Validation(unittest.TestCase):
    def test_routed_is_valid_without_bridge_subnet_base(self):
        self.assertEqual(validate(routed_doc()), "")

    def test_bridge_is_still_the_default(self):
        self.assertEqual(config.Config(aws_doc()).network_mode, "bridge")

    def test_routed_needs_a_pool(self):
        doc = routed_doc()
        del doc["network"]["pool"]
        self.assertIn("[network] missing: pool", validate(doc))

    def test_bad_pools(self):
        for pool, why in (("10.120.0.0/24", "must be a /16 inside 10.0.0.0/8"),
                          ("10.120.1.0/16", "[network].pool:"),
                          ("172.20.0.0/16", "must be a /16 inside 10.0.0.0/8"),
                          ("10.70.0.0/16", "overlaps vpc_cidr"),
                          ("10.112.0.0/12", "must be a /16"),
                          ("10.97.0.0/16", "overlaps 10.96.0.0/12"),
                          ("10.244.0.0/16", "overlaps 10.244.0.0/16"),
                          ("pool", "[network].pool:")):
            self.assertIn(why, validate(routed_doc(pool=pool)), pool)

    def test_pool_only_in_routed_mode(self):
        doc = aws_doc()
        doc["network"]["pool"] = POOL
        self.assertIn('[network].pool is only for mode = "routed"', validate(doc))

    def test_unknown_mode_and_key(self):
        self.assertIn("[network].mode must be one of bridge, routed",
                      validate(routed_doc(mode="overlay", bridge_subnet_base="10.100.0.0")))
        self.assertIn("[network].bridge_count is not a key", validate(routed_doc(bridge_count=2)))

    def test_guest_mtu(self):
        for ok in (0, 1280, 1450, 1500):
            self.assertEqual(validate(routed_doc(guest_mtu=ok)), "", ok)
        for bad in (1279, 1501, 9000, True, "1450"):
            self.assertIn("[network].guest_mtu must be", validate(routed_doc(guest_mtu=bad)), bad)

    def test_warning_without_the_storage_engine(self):
        cfg = config.Config(routed_doc())
        self.assertEqual(len(config.network_warnings(cfg)), 1)
        self.assertIn("storage engine", config.network_warnings(cfg)[0])
        self.assertEqual(config.network_warnings(config.Config(aws_doc())), [])

class Render(unittest.TestCase):
    def test_workers(self):
        files = configs(routed_doc())
        for name, host_ip, underlay in ((W0, "10.120.0.12", "10.70.1.10"),
                                        (W1, "10.120.0.17", "10.70.1.21")):
            net = files[f"deploy-config/{name}/worker.toml"]["network"]
            self.assertEqual(net, {"vni_range_start": 100, "vni_range_end": 10000,
                                   "bridge_subnet": POOL, "mode": "routed", "guest_mtu": 0,
                                   "routed": {"host_ip": host_ip, "underlay_ip": underlay}})

    def test_proxy(self):
        net = configs(routed_doc())[f"deploy-config/{CTL}/proxy.toml"]["network"]
        self.assertEqual(net, {"vni_range_start": 100, "vni_range_end": 10000,
                               "bridge_subnet": POOL, "mode": "routed",
                               "routed": {"host_ip": "10.120.0.254", "underlay_ip": "10.70.0.10"}})

    def test_other_roles_stay_plain(self):
        files = configs(routed_doc())
        for role in ("control", "console", "dns"):
            net = files[f"deploy-config/{CTL}/{role}.toml"]["network"]
            self.assertNotIn("mode", net, role)

    def test_inventory_and_group_vars_have_no_vnis(self):
        r = renderer(routed_doc())
        gv = r.group_vars()
        self.assertIn("boxd_network_mode: routed", gv)
        self.assertIn(f"boxd_routed_pool: {POOL}", gv)
        self.assertIn("boxd_workers: []", gv)
        self.assertNotIn("vni:", gv)
        inv = r.inventory()
        self.assertIn(f"bridge_subnet: {POOL}, bridge_gateway: 10.120.0.1", inv)

    def test_guest_mtu_override_and_metal_default(self):
        net = configs(routed_doc(guest_mtu=1450))[f"deploy-config/{W0}/worker.toml"]["network"]
        self.assertEqual(net["guest_mtu"], 1450)
        with mock.patch.object(render.clouds, "get", return_value=object()):
            # A cloud without ROUTED_GUEST_MTU (metal): boxd's own default.
            net = configs(routed_doc())[f"deploy-config/{W0}/worker.toml"]["network"]
        self.assertNotIn("guest_mtu", net)

    def test_bridge_render_has_no_routed_keys(self):
        class BridgeAlloc(Alloc):
            vnis = {W0: 100}
            def bridge_subnet(self, name): return "10.100.0.0/16"
            def bridge_gateway(self, name): return "10.100.0.1"
        cfg = config.Config(aws_doc())
        tf_out = {"nodes": {n.name: {"external_ip": "203.0.113.1"} for n in cfg.nodes}}
        r = render.Renderer(cfg, BridgeAlloc(), tf_out, State0())
        net = tomllib.loads(r.deploy_configs()[f"deploy-config/{W0}/worker.toml"])["network"]
        self.assertEqual(net, {"vni_range_start": 100, "vni_range_end": 10000,
                               "bridge_subnet": "10.100.0.0/16"})
        self.assertNotIn("boxd_network_mode", r.group_vars())

    def test_too_many_nodes_for_the_pool(self):
        class Big(Alloc):
            def node_id(self, name, role): return 244 if role == "worker" else 1
        cfg = config.Config(routed_doc())
        r = render.Renderer(cfg, Big(), {"nodes": {}}, State0())
        with self.assertRaisesRegex(InstallError, "too many nodes"):
            r.worker_doc(cfg.nodes_with("worker")[0])

class Allocate(unittest.TestCase):
    def test_routed_allocates_no_vni_or_subnet(self):
        d = tempfile.mkdtemp(prefix="routed-alloc-")
        self.addCleanup(shutil.rmtree, d)
        st = State(d)
        st.allocate(config.Config(routed_doc()))
        self.assertEqual(st.data["vnis"], {})
        self.assertEqual(st.data["bridge_indexes"], {})

class SwitchGuard(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp(prefix="routed-guard-")
        self.addCleanup(shutil.rmtree, d)
        self.state = State(d)
        self.state.data["applied_assets"] = {"sha": "x"}

    def check(self, doc, deployed, machines=0):
        logs = []
        with mock.patch.object(main.ansible, "deployed_network_modes", return_value=deployed), \
                mock.patch.object(main.ansible, "machine_count", return_value=machines) as count, \
                mock.patch.object(main, "log", side_effect=logs.append):
            main.check_network_mode(config.Config(doc), self.state)
        return logs, count

    def test_new_cluster_is_not_asked(self):
        self.state.data.pop("applied_assets")
        with mock.patch.object(main.ansible, "deployed_network_modes") as probe:
            main.check_network_mode(config.Config(routed_doc()), self.state)
        probe.assert_not_called()

    def test_same_mode_is_a_no_op(self):
        logs, count = self.check(routed_doc(), {W0: "routed", W1: "routed"})
        self.assertEqual(logs, [])
        count.assert_not_called()

    def test_a_new_worker_alone_is_no_switch(self):
        logs, count = self.check(routed_doc(), {W0: "routed"})
        count.assert_not_called()

    def test_switch_with_machines_is_refused(self):
        with self.assertRaisesRegex(InstallError, r"runs the other mode, and the cluster has 3"):
            self.check(routed_doc(), {W0: "bridge"}, machines=3)

    def test_switch_without_machines_goes_ahead(self):
        logs, _ = self.check(routed_doc(), {W0: "bridge"}, machines=0)
        self.assertIn(f'network: switching {W0} to mode = "routed"', logs[0])

    def test_back_to_bridge_is_guarded_too(self):
        with self.assertRaises(InstallError):
            self.check(aws_doc(), {W0: "routed"}, machines=1)

    def test_recorded_mode_when_no_worker_has_a_config(self):
        self.state.data["network_mode"] = "bridge"
        with self.assertRaisesRegex(InstallError, "installer state"):
            self.check(routed_doc(), {}, machines=2)

if __name__ == "__main__":
    unittest.main()
