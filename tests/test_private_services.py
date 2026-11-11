"""[[worker.private_services]]: validation, the service address allocation, the rendered
worker.toml and group_vars, the release gate, and the relay's firewall flags.
Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import copy
import ipaddress
import json
import os
import shutil
import subprocess
import tempfile
import tomllib
import unittest

from boxd_installer import config, local_ca, main, private_services, render
from boxd_installer.state import State
from boxd_installer.util import InstallError

from test_gke_config import gke_doc
from test_guest_fw import BASH, SCRIPT, TASKS
from test_local_ca import Alloc, KeysState
from test_machine_sizes import aws_doc, validate

WORKER = "boxd-aws-demo-worker-0"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ALB = {"name": "platform.internal.example.com", "port": 443,
       "target_subnets": ["10.70.8.0/24", "10.70.9.0/24"]}
DB = {"name": "db.internal.example.com", "port": 5432, "target": "10.70.8.50"}

def with_services(*services, doc=None) -> dict:
    doc = doc or aws_doc()
    doc.setdefault("worker", {})["private_services"] = [copy.deepcopy(s) for s in services]
    return doc

class Validation(unittest.TestCase):
    def test_the_example_shapes_are_valid(self):
        self.assertEqual(validate(with_services(ALB, DB)), "")
        self.assertEqual(validate(with_services(ALB, {**DB, "port": 443})), "",
                         "two names may share a port")
        self.assertEqual(validate(with_services(ALB, {**ALB, "port": 8443})), "",
                         "one name on several ports")
        doc = with_services(ALB)
        doc["network"]["service_range"] = "100.64.10.0/24"
        self.assertEqual(validate(doc), "")

    def test_the_address_is_the_installers(self):
        self.assertIn("address is allocated by the installer",
                      validate(with_services({**DB, "address": "198.18.0.9"})))

    def test_names(self):
        for bad in ("platform", "*.example.com", "a..b.com", "-a.example.com", "x.boxd",
                    "10.1.2.3", ""):
            self.assertIn("[[worker.private_services]]",
                          validate(with_services({**DB, "name": bad})), bad)
        self.assertIn("declared twice",
                      validate(with_services(DB, {**DB, "name": "DB.Internal.Example.COM."})))

    def test_keys_ports_and_target_forms(self):
        self.assertIn("unknown key 'targets'", validate(with_services({**DB, "targets": []})))
        for port in (0, 70000, "443", True):
            self.assertIn("port must be 1 to 65535", validate(with_services({**DB, "port": port})))
        self.assertIn("exactly one of target",
                      validate(with_services({**DB, "target_subnets": ["10.70.8.0/24"]})))
        neither = {k: v for k, v in DB.items() if k != "target"}
        self.assertIn("exactly one of target", validate(with_services(neither)))
        self.assertIn("IPv4", validate(with_services({**DB, "target": "fd00::1"})))
        self.assertIn("non-empty list", validate(with_services({**ALB, "target_subnets": []})))

    def test_targets_that_would_reach_the_cluster(self):
        sub = lambda c: {**ALB, "target_subnets": [c]}  # noqa: E731
        self.assertIn("wider than /20", validate(with_services(sub("10.0.0.0/8"))))
        self.assertIn("wider than /20", validate(with_services(sub("10.70.0.0/19"))))
        self.assertEqual(validate(with_services(sub("10.70.16.0/20"))), "")
        self.assertIn("holds the cluster node", validate(with_services(sub("10.70.1.0/24"))))
        self.assertIn("holds the cluster node",
                      validate(with_services({**DB, "target": "10.70.1.10"})))
        self.assertIn("never a target",
                      validate(with_services({**DB, "target": "169.254.169.254"})))
        self.assertIn("never a target", validate(with_services(sub("127.0.0.0/24"))))
        self.assertIn("overlaps [network].service_range",
                      validate(with_services({**DB, "target": "198.18.0.7"})))

    def test_the_routed_pool_is_the_box_network(self):
        doc = with_services(ALB)
        net = doc["network"]
        net.pop("bridge_subnet_base")
        net.update(mode="routed", pool="10.120.0.0/16")
        self.assertEqual(validate(doc), "")
        doc["worker"]["private_services"][0]["target_subnets"] = ["10.120.5.0/24"]
        self.assertIn("overlaps the box network [network].pool", validate(doc))

    def test_service_range(self):
        for rng, why in (("10.200.0.0/24", "must be inside one of"),
                         ("8.8.8.0/24", "must be inside one of"),
                         ("198.18.0.0/26", "a /16 to /24"),
                         ("198.18.0.0/15", "a /16 to /24"),
                         ("198.18.0.1/24", "service_range")):
            doc = with_services(ALB)
            doc["network"]["service_range"] = rng
            self.assertIn(why, validate(doc), rng)
        doc = aws_doc()
        doc["network"]["service_range"] = "198.18.0.0/24"
        self.assertIn("only used with [[worker.private_services]]", validate(doc))

    def test_gke_is_refused(self):
        doc = with_services(ALB, doc=gke_doc())
        self.assertIn("not supported on [gke] yet", validate(doc))

class Allocation(unittest.TestCase):
    RNG = ipaddress.ip_network("198.18.0.0/24")

    def test_lowest_free_and_remembered(self):
        mem: dict = {}
        self.assertEqual(private_services.allocate(mem, ["a.x.com", "b.x.com"], self.RNG),
                         {"a.x.com": "198.18.0.1", "b.x.com": "198.18.0.2"})
        # a removed, c added: c never takes a's address.
        self.assertEqual(private_services.allocate(mem, ["b.x.com", "c.x.com"], self.RNG),
                         {"b.x.com": "198.18.0.2", "c.x.com": "198.18.0.3"})
        # a comes back with its old address; the order of the list moves nothing.
        self.assertEqual(private_services.allocate(mem, ["c.x.com", "a.x.com"], self.RNG),
                         {"c.x.com": "198.18.0.3", "a.x.com": "198.18.0.1"})

    def test_a_range_that_lost_an_address_or_is_full(self):
        mem = {"a.x.com": "198.18.0.1"}
        with self.assertRaisesRegex(private_services.AllocationError, "reset-service-addresses"):
            private_services.allocate(mem, ["a.x.com"], ipaddress.ip_network("198.19.0.0/24"))
        small = ipaddress.ip_network("198.18.0.0/30")  # two hosts
        mem = {}
        private_services.allocate(mem, ["a.x.com", "b.x.com"], small)
        with self.assertRaisesRegex(private_services.AllocationError, "no address left"):
            private_services.allocate(mem, ["c.x.com"], small)

    def test_the_state_keeps_and_resets_them(self):
        d = tempfile.mkdtemp(prefix="private-services-state-")
        try:
            st = State(d)
            cfg = config.Config(with_services(ALB, DB, {**ALB, "port": 8443}))
            got = st.allocate_service_addresses(cfg)
            self.assertEqual(got, {"platform.internal.example.com": "198.18.0.1",
                                   "db.internal.example.com": "198.18.0.2"})
            st.save()
            self.assertEqual(State(d).data["service_addresses"], got)
            self.assertEqual(State(d).drop_service_addresses(), 2)
            self.assertNotIn("service_addresses", State(d).data)
        finally:
            shutil.rmtree(d)

class ServiceAlloc(Alloc):
    bridge_indexes = {WORKER: 0, "a-removed-worker": 1}
    service_addresses = {"platform.internal.example.com": "198.18.0.1",
                         "db.internal.example.com": "198.18.0.2"}

    def bridge_subnet(self, name):
        return f"10.{100 + self.bridge_indexes[name]}.0.0/16"

class Render(unittest.TestCase):
    def setUp(self):
        self.keys = tempfile.mkdtemp(prefix="private-services-render-")
        for f in (local_ca.CERT_FILE, local_ca.KEY_FILE):
            open(os.path.join(self.keys, f), "w").close()

    def tearDown(self):
        shutil.rmtree(self.keys)

    def renderer(self, doc):
        doc["acme"] = {"ca": "local"}
        cfg = config.Config(doc)
        tf_out = {"nodes": {n.name: {"external_ip": "203.0.113.7", "instance_id": "i-0"}
                            for n in cfg.nodes}}
        return render.Renderer(cfg, ServiceAlloc(), tf_out, KeysState(self.keys))

    def worker_toml(self, doc) -> str:
        return self.renderer(doc).deploy_configs()[f"deploy-config/{WORKER}/worker.toml"]

    def test_worker_toml_carries_each_table_with_its_address(self):
        doc = with_services({**ALB, "name": "Platform.Internal.Example.com."}, DB)
        doc["worker"]["tc_filters"] = "full"
        text = self.worker_toml(doc)
        worker = tomllib.loads(text)["worker"]
        self.assertEqual(worker["tc_filters"], "full", "keys after the tables still parse")
        self.assertEqual(worker["private_services"], [
            {"name": "platform.internal.example.com", "address": "198.18.0.1", "port": 443,
             "target_subnets": ["10.70.8.0/24", "10.70.9.0/24"]},
            {"name": "db.internal.example.com", "address": "198.18.0.2", "port": 5432,
             "target": "10.70.8.50"},
        ])
        self.assertIn("[[worker.private_services]]", text)
        self.assertNotIn("service_range", text)

    def test_none_declared_renders_nothing(self):
        text = self.worker_toml(aws_doc())
        self.assertNotIn("private_services", text)
        self.assertIn("boxd_private_service_bounds: []", self.renderer(aws_doc()).group_vars())

    def test_group_vars_carry_the_firewall_bounds(self):
        gv = self.renderer(with_services(ALB, DB, {**DB, "name": "db2.internal.example.com"})
                           ).group_vars()
        self.assertIn('boxd_private_service_bounds: ["10.70.8.0/24:443", "10.70.9.0/24:443", '
                      '"10.70.8.50/32:5432"]', gv)

    def test_a_target_in_any_allocated_bridge_subnet_is_refused(self):
        for target in ("10.100.3.4", "10.101.0.9"):
            with self.assertRaisesRegex(InstallError, "overlaps the bridge subnet"):
                self.worker_toml(with_services({**DB, "target": target}))

class ReleaseGate(unittest.TestCase):
    def bundle(self, files: dict) -> str:
        d = tempfile.mkdtemp(prefix="private-services-bundle-")
        self.addCleanup(shutil.rmtree, d)
        with open(os.path.join(d, "MANIFEST.json"), "w") as fh:
            json.dump({"git_sha": "0" * 40, "files": files}, fh)
        return d

    def test_a_release_without_the_relay_is_refused(self):
        cfg = config.Config(with_services(ALB))
        with self.assertRaisesRegex(InstallError, "lists no bin/boxd-relay"):
            main.check_relay_bundle(cfg, self.bundle({"bin/boxd-worker": "h"}))
        main.check_relay_bundle(cfg, self.bundle({"bin/boxd-worker": "h", "bin/boxd-relay": "h"}))
        main.check_relay_bundle(config.Config(aws_doc()), self.bundle({}))

@unittest.skipUnless(BASH, "needs bash >= 4")
class Firewall(unittest.TestCase):
    def ruleset(self, *args) -> str:
        return subprocess.run([BASH, SCRIPT, *args, "--print"], check=True, capture_output=True,
                              text=True).stdout

    def test_the_relay_port_and_the_relay_users_chain(self):
        for mode in ((), ("--routed",)):
            out = self.ruleset(*mode, "--subnet", "10.100.0.0/16", "--gateway", "10.100.0.1",
                               "--relay-user", "boxd-relay",
                               "--private-service", "10.70.8.0/24:443",
                               "--private-service", "10.70.8.50/32:5432")
            self.assertIn("ip daddr 10.100.0.1 tcp dport 3129 ct status dnat accept", out, mode)
            self.assertIn('meta skuid "boxd-relay" jump relay_only', out)
            self.assertIn("ip daddr 10.70.8.0/24 tcp dport 443 accept", out)
            self.assertIn("ip daddr 10.70.8.50/32 tcp dport 5432 accept", out)
            self.assertIn('counter drop comment "relay: outside the declared services"', out)

    def test_without_a_relay_user_there_is_no_chain(self):
        out = self.ruleset("--subnet", "10.100.0.0/16", "--gateway", "10.100.0.1")
        self.assertIn("tcp dport 3129 ct status dnat accept", out, "the port rule is always there")
        self.assertNotIn("relay_only", out)
        bad = subprocess.run([BASH, SCRIPT, "--subnet", "10.100.0.0/16", "--gateway",
                              "10.100.0.1", "--private-service", "10.70.8.0/24:443", "--print"],
                             capture_output=True, text=True)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("--private-service needs --relay-user", bad.stderr)

class Role(unittest.TestCase):
    def test_the_worker_role_installs_the_relay_only_with_services(self):
        with open(TASKS) as fh:
            tasks = fh.read()
        self.assertIn("--relay-user boxd-relay", tasks)
        self.assertIn("--private-service {{ bound }}", tasks)
        self.assertIn("ExecStartPre=+/usr/local/bin/boxd-guest-fw --verify --relay-user boxd-relay",
                      tasks)
        self.assertIn("User=boxd-relay", tasks)
        self.assertGreaterEqual(tasks.count("when: boxd_private_service_bounds | length > 0"), 5)
        self.assertIn("name: Stop and disable boxd-relay (no private services)", tasks)

if __name__ == "__main__":
    unittest.main()
