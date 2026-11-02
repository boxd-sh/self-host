"""One egress CA per cluster: made or adopted at apply, kept in
state, rendered into every worker.toml. Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import os
import shutil
import stat
import subprocess
import tempfile
import tomllib
import unittest
from unittest import mock

from boxd_installer import config, main, render
from boxd_installer.state import State, generate_egress_ca
from boxd_installer.util import InstallError

from test_machine_sizes import FakeAlloc, FakeState, aws_doc

HAVE_OPENSSL = shutil.which("openssl") is not None
# Built up, so a secret scanner does not read the test as holding a key.
KEY_LABEL = "PRIVATE " + "KEY"
PKCS8_BEGIN = f"-----BEGIN {KEY_LABEL}-----"

def openssl(*args, stdin: str) -> str:
    return subprocess.run(["openssl", *args], input=stdin, capture_output=True, text=True,
                          check=True).stdout

@unittest.skipUnless(HAVE_OPENSSL, "needs openssl")
class Generate(unittest.TestCase):
    def test_a_ca_with_its_pkcs8_key(self):
        cert, key = generate_egress_ca()
        text = openssl("x509", "-noout", "-text", stdin=cert)
        self.assertRegex(text, r"CA:TRUE")
        self.assertIn("Certificate Sign", text)
        self.assertTrue(key.startswith(PKCS8_BEGIN), key[:40])
        self.assertEqual(openssl("x509", "-noout", "-pubkey", stdin=cert),
                         openssl("pkey", "-pubout", stdin=key))

class StateKeepsIt(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="egress-ca-state-")
        self.addCleanup(shutil.rmtree, self.dir)

    def test_store_once_0600(self):
        st = State(self.dir)
        self.assertIsNone(st.egress_ca())
        st.store_egress_ca("CERT", "KEY")
        self.assertEqual(st.egress_ca(), ("CERT\n", "KEY\n"))
        for f in ("egress-ca.pem", "egress-ca.key"):
            mode = stat.S_IMODE(os.stat(os.path.join(st.keys_dir, f)).st_mode)
            self.assertEqual(mode, 0o600, f)
        with self.assertRaisesRegex(InstallError, "never replaced"):
            st.store_egress_ca("OTHER", "OTHER")

class CaState(FakeState):
    def __init__(self, pair):
        self.pair = pair
    def egress_ca(self):
        return self.pair

class Render(unittest.TestCase):
    def files(self, pair):
        cfg = config.Config(aws_doc())
        tf_out = {"nodes": {n.name: {"external_ip": "203.0.113.1"} for n in cfg.nodes}}
        out = render.Renderer(cfg, FakeAlloc(), tf_out, CaState(pair)).deploy_configs()
        return {p: tomllib.loads(b) for p, b in out.items()}

    def test_every_worker_and_nothing_else(self):
        pem = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
        key = f"{PKCS8_BEGIN}\nMIGH\n-----END {KEY_LABEL}-----\n"
        files = self.files((pem, key))
        workers = [p for p in files if p.endswith("/worker.toml")]
        self.assertTrue(workers)
        for path, doc in files.items():
            if path in workers:
                self.assertEqual((doc["egress_ca_cert"], doc["egress_ca_key"]), (pem, key))
            else:
                self.assertNotIn("egress_ca_key", doc, path)

    def test_none_yet_leaves_it_to_the_worker(self):
        for path, doc in self.files(None).items():
            self.assertNotIn("egress_ca_cert", doc, path)

class Ensure(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="egress-ca-ensure-")
        self.addCleanup(shutil.rmtree, self.dir)
        self.cfg = config.Config(aws_doc())
        self.state = State(self.dir)

    def ensure(self, worker_pair=None):
        with mock.patch.object(main.ansible, "worker_egress_ca",
                               return_value=worker_pair) as read, \
                mock.patch.object(main, "generate_egress_ca",
                                  return_value=("NEW-CERT", "NEW-KEY")) as gen, \
                mock.patch.object(main, "log"):
            main.ensure_egress_ca(self.cfg, self.state)
        return read, gen

    def test_new_cluster_makes_one_without_asking_the_workers(self):
        read, gen = self.ensure()
        read.assert_not_called()
        gen.assert_called_once()
        self.assertEqual(self.state.egress_ca(), ("NEW-CERT\n", "NEW-KEY\n"))

    def test_existing_cluster_adopts_a_workers_own(self):
        self.state.data["applied_assets"] = {"sha": "x"}
        read, gen = self.ensure(("w0", "OLD-CERT\n", "OLD-KEY\n"))
        read.assert_called_once()
        gen.assert_not_called()
        self.assertEqual(self.state.egress_ca(), ("OLD-CERT\n", "OLD-KEY\n"))

    def test_existing_cluster_without_one_on_disk_makes_one(self):
        self.state.data["applied_assets"] = {"sha": "x"}
        _, gen = self.ensure(None)
        gen.assert_called_once()

    def test_kept_once_made(self):
        self.state.store_egress_ca("KEPT", "KEPT")
        read, gen = self.ensure(("w0", "OLD", "OLD"))
        read.assert_not_called()
        gen.assert_not_called()
        self.assertEqual(self.state.egress_ca(), ("KEPT\n", "KEPT\n"))

if __name__ == "__main__":
    unittest.main()
