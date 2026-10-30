"""[metal]: hosts the operator already runs. Config rules, the outputs and
inventory that stand in for terraform's, state in a local file, and the
free-tier count from the hosts. Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import os
import shutil
import tempfile
import tomllib
import unittest
from unittest import mock

from boxd_installer import ansible, config, license, main, remote_state, render, tf
from boxd_installer.clouds import metal
from boxd_installer.util import InstallError

from test_machine_sizes import FakeAlloc, FakeState, validate

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_KEY = "ab" * 32

def metal_doc() -> dict:
    with open(os.path.join(ROOT, "examples", "metal", "cluster.toml"), "rb") as fh:
        return tomllib.load(fh)

def worker(doc) -> dict:
    return next(n for n in doc["nodes"] if "worker" in n["roles"])

class Alloc(FakeAlloc):
    vnis = {"boxd-metal-worker-0": 100}
    def bridge_gateway(self, name): return "10.100.1.1"

class MetalValidation(unittest.TestCase):
    def test_example_is_valid(self):
        self.assertEqual(validate(metal_doc()), "")

    def test_required_keys(self):
        doc = metal_doc()
        del doc["metal"]["interface"]
        self.assertIn("[metal] missing: interface", validate(doc))

    def test_unknown_metal_key(self):
        doc = metal_doc()
        doc["metal"]["project"] = "p"
        self.assertIn("[metal].project is not a [metal] key", validate(doc))

    def test_proxy_needs_public_ip(self):
        doc = metal_doc()
        del doc["nodes"][0]["public_ip"]
        self.assertIn("proxy and dns nodes need public_ip", validate(doc))
        doc["nodes"][0]["public_ip"] = "not-an-ip"
        self.assertIn("public_ip: 'not-an-ip' is not an IPv4 address", validate(doc))

    def test_worker_needs_data_disk(self):
        doc = metal_doc()
        del worker(doc)["data_disk"]
        self.assertIn("worker nodes must set data_disk", validate(doc))
        worker(doc)["data_disk"] = "sdb"
        self.assertIn("worker nodes must set data_disk", validate(doc))
        doc = metal_doc()
        doc["nodes"][0]["data_disk"] = "/dev/sdb"
        self.assertIn("data_disk is only valid on worker nodes", validate(doc))

    def test_cloud_node_keys_rejected(self):
        for k, v in (("machine_type", "n2-standard-16"), ("data_disk_gb", 500),
                     ("boot_disk_gb", 100)):
            doc = metal_doc()
            worker(doc)[k] = v
            self.assertIn(f"{k} is not used on [metal]", validate(doc), k)

    def test_gts_rejected(self):
        doc = metal_doc()
        doc["acme"]["ca"] = "gts"
        self.assertIn('[acme].ca = "gts" needs a GCP project', validate(doc))

    def test_vpc_cidr_inside_10_8(self):
        doc = metal_doc()
        doc["metal"]["vpc_cidr"] = "192.168.0.0/24"
        self.assertIn("[metal].vpc_cidr must be inside 10.0.0.0/8", validate(doc))

class MetalRender(unittest.TestCase):
    def setUp(self):
        self.cfg = config.Config(metal_doc())

    def test_outputs_from_cluster_toml(self):
        out = metal.outputs(self.cfg)
        self.assertEqual(out["nodes"]["boxd-metal-control-0"],
                         {"internal_ip": "10.60.0.10", "external_ip": "203.0.113.10"})
        self.assertEqual(out["nodes"]["boxd-metal-worker-0"],
                         {"internal_ip": "10.60.0.20", "external_ip": None})

    def test_read_outputs_runs_no_terraform(self):
        state = mock.Mock(dir=tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, state.dir)
        with mock.patch.object(tf, "_tf", side_effect=AssertionError("terraform ran")):
            out = tf.read_outputs(self.cfg, state)
        self.assertEqual(tf.cached_outputs(state), out)

    def test_inventory_is_direct_ssh(self):
        r = render.Renderer(self.cfg, Alloc(), metal.outputs(self.cfg), FakeState())
        inv = r.inventory()
        self.assertNotIn("ProxyCommand", inv)
        self.assertNotIn("ansible_ssh_common_args", inv)
        self.assertIn("boxd-metal-control-0: { ansible_host: 203.0.113.10,", inv)
        self.assertIn("boxd-metal-worker-0: { ansible_host: 203.0.113.20, "
                      "boxd_volume_device: /dev/nvme1n1,", inv)
        gv = r.group_vars()
        self.assertIn("vrack_interface: eno2", gv)

    def test_ssh_target_falls_back_to_internal_ip(self):
        doc = metal_doc()
        del worker(doc)["ssh_host"]
        node = config.Config(doc).nodes_with("worker")[0]
        self.assertEqual(metal.ssh_target(node, {}), "10.60.0.20")

    def test_ssh_opts_have_no_tunnel(self):
        with mock.patch.object(ansible, "_keys_dir", "/k"):
            opts = ansible._ssh_opts(self.cfg)
        self.assertFalse(any(o.startswith("ProxyCommand") for o in opts))

    def test_proxy_advertises_public_ip(self):
        r = render.Renderer(self.cfg, Alloc(), metal.outputs(self.cfg), FakeState())
        self.assertEqual(r._advertised_ip(self.cfg.nodes[0]), "203.0.113.10")

class MetalState(unittest.TestCase):
    """The encrypted state goes to a file next to cluster.toml, through the
    same pull/push as a bucket."""
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base)
        self.cfg = config.Config(metal_doc(), self.base)
        env = mock.patch.dict(os.environ, {"BOXD_STATE_KEY": STATE_KEY})
        env.start()
        self.addCleanup(env.stop)

    def test_url_is_local(self):
        self.assertEqual(metal.state_url(self.cfg),
                         os.path.join(self.base, "metal-state", "boxd-metal", "installer-state.enc"))
        self.cfg.cloud["state_dir"] = "/srv/boxd"
        self.assertEqual(metal.state_url(self.cfg), "/srv/boxd/boxd-metal/installer-state.enc")

    def test_push_then_pull(self):
        state_dir = os.path.join(self.base, "work")
        os.makedirs(os.path.join(state_dir, "keys"))
        with open(os.path.join(state_dir, "keys", "boxd-deploy"), "w") as fh:
            fh.write("secret")
        remote_state.push(self.cfg, state_dir)
        self.assertFalse(os.path.exists(state_dir))
        url = metal.state_url(self.cfg)
        self.assertEqual(os.stat(url).st_mode & 0o777, 0o600)
        with open(url, "rb") as fh:
            self.assertNotIn(b"secret", fh.read())
        remote_state.push(self.cfg, self._make(state_dir))
        self.assertTrue(os.path.exists(url + ".prev"))
        remote_state.pull(self.cfg, state_dir)
        with open(os.path.join(state_dir, "keys", "boxd-deploy")) as fh:
            self.assertEqual(fh.read(), "again")

    def _make(self, state_dir):
        os.makedirs(os.path.join(state_dir, "keys"))
        with open(os.path.join(state_dir, "keys", "boxd-deploy"), "w") as fh:
            fh.write("again")
        return state_dir

class MetalLicense(unittest.TestCase):
    def test_validate_counts_nothing(self):
        self.assertEqual(license.free_tier_warnings(config.Config(metal_doc()), assume_new=True), [])

    def test_counted_on_the_hosts(self):
        cfg = config.Config(metal_doc())
        self.assertEqual(license.free_tier_warnings(cfg, counted={"boxd-metal-worker-0": 16}), [])
        warn = license.free_tier_warnings(cfg, counted={"boxd-metal-worker-0": 64})
        self.assertEqual(len(warn), 1)
        self.assertIn("the workers have 64 vCPUs", warn[0])

class MetalCommands(unittest.TestCase):
    def test_destroy_refused(self):
        cfg = config.Config(metal_doc())
        args = mock.Mock(cluster="unused")
        with mock.patch.object(config, "load", return_value=cfg), \
             self.assertRaisesRegex(InstallError, "nothing for it to destroy"):
            main.cmd_destroy(args)

    def test_check_hosts_prints_the_key(self):
        cfg = config.Config(metal_doc())
        denied = mock.Mock(returncode=255, stderr="Permission denied (publickey).")
        with mock.patch.object(ansible, "_ssh", return_value=denied), \
             mock.patch.object(ansible, "_keys_dir", "/k"), \
             self.assertRaises(InstallError) as e:
            ansible.check_hosts(cfg, "ssh-ed25519 AAAA boxd-deploy")
        self.assertIn("boxd-metal-worker-0 (ubuntu@203.0.113.20): ssh failed", str(e.exception))
        self.assertIn("  ssh-ed25519 AAAA boxd-deploy\n", str(e.exception))

    def test_check_hosts_names_kvm_on_workers(self):
        cfg = config.Config(metal_doc())
        def ssh(cfg, node, cmd, **kw):
            self.assertEqual("test -c /dev/kvm" in cmd, node.has("worker"))
            return mock.Mock(returncode=1 if node.has("worker") else 0, stderr="")
        with mock.patch.object(ansible, "_ssh", side_effect=ssh), \
             self.assertRaisesRegex(InstallError, "no\n?.*/dev/kvm"):
            ansible.check_hosts(cfg, "k")

if __name__ == "__main__":
    unittest.main()
