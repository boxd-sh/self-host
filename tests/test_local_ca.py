"""Local CA TLS mode: `providers set-tls-ca` validation (openssl fixtures),
the [acme] config rules, and what render emits for ca = "local".
Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import collections
import os
import shutil
import subprocess
import tempfile
import tomllib
import unittest

from boxd_installer import config, local_ca, render
from boxd_installer.util import InstallError

from test_machine_sizes import FakeAlloc, FakeState, aws_doc, validate

HAVE_OPENSSL = shutil.which("openssl") is not None

def _ssl(*args, cwd):
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)

def _ca_cert(key: str, out: str, cwd: str, ca: bool = True) -> None:
    bc = "critical,CA:TRUE,pathlen:0" if ca else "critical,CA:FALSE"
    _ssl("req", "-x509", "-new", "-key", key, "-out", out, "-days", "30",
         "-subj", "/DC=com/DC=corp/CN=Test Intermediate",
         "-addext", f"basicConstraints={bc}",
         "-addext", "nameConstraints=critical,permitted;DNS:boxd.corp.test,"
                    "permitted;DNS:.boxd.corp.test", cwd=cwd)

def _pem_blocks(path: str) -> list[str]:
    with open(path) as fh:
        return [l for l in fh.read().splitlines() if l.startswith("-----BEGIN")]

@unittest.skipUnless(HAVE_OPENSSL, "openssl not on PATH")
class SetTlsCa(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="local-ca-test-")
        self.keys = os.path.join(self.dir, "keys")
        os.mkdir(self.keys)
        d = self.dir
        # EC key as `ecparam -genkey` writes it: SEC1 + an EC PARAMETERS block.
        _ssl("ecparam", "-name", "prime256v1", "-genkey", "-out", "ec.key", cwd=d)
        _ca_cert("ec.key", "ec.pem", d)
        _ssl("genrsa", "-traditional", "-out", "rsa.key", "2048", cwd=d)
        _ca_cert("rsa.key", "rsa.pem", d)
        _ssl("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", "other.key", cwd=d)
        _ca_cert("other.key", "leaf.pem", d, ca=False)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def p(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def stored_key(self) -> str:
        with open(os.path.join(self.keys, local_ca.KEY_FILE)) as fh:
            return fh.read()

    def test_accepts_ec_pair(self):
        local_ca.store(self.p("ec.pem"), self.p("ec.key"), self.keys)
        self.assertTrue(local_ca.stored(self.keys))
        self.assertTrue(self.stored_key().startswith("-----BEGIN PRIVATE KEY-----"))  # gitleaks:allow (a PEM header, not a key)
        for f in (local_ca.CERT_FILE, local_ca.KEY_FILE):
            self.assertEqual(os.stat(os.path.join(self.keys, f)).st_mode & 0o777, 0o600)

    def test_accepts_rsa_pkcs1_and_stores_pkcs8(self):
        with open(self.p("rsa.key")) as fh:
            self.assertIn("BEGIN RSA PRIVATE KEY", fh.read())
        local_ca.store(self.p("rsa.pem"), self.p("rsa.key"), self.keys)
        key = self.stored_key()
        self.assertTrue(key.startswith("-----BEGIN PRIVATE KEY-----"))
        self.assertNotIn("RSA PRIVATE KEY", key)

    def test_chain_with_root_is_kept_and_normalised(self):
        with open(self.p("chain.pem"), "w") as out:
            out.write("subject=CN = Test Intermediate\n")
            for f in ("ec.pem", "rsa.pem"):
                with open(self.p(f)) as fh:
                    out.write(fh.read())
        local_ca.store(self.p("chain.pem"), self.p("ec.key"), self.keys)
        stored = os.path.join(self.keys, local_ca.CERT_FILE)
        self.assertEqual(_pem_blocks(stored), ["-----BEGIN CERTIFICATE-----"] * 2)
        with open(stored) as fh:
            self.assertTrue(fh.read().startswith("-----BEGIN CERTIFICATE-----"))

    def assertRefused(self, cert: str, key: str, msg: str):
        with self.assertRaises(InstallError) as cm:
            local_ca.store(self.p(cert), self.p(key), self.keys)
        self.assertIn(msg, str(cm.exception))
        self.assertFalse(local_ca.stored(self.keys))

    def test_rejects_non_ca_cert(self):
        self.assertRefused("leaf.pem", "other.key", "not a CA")

    def test_rejects_mismatched_key(self):
        self.assertRefused("ec.pem", "rsa.key", "is not the private key of the CA")

    def test_rejects_encrypted_key(self):
        _ssl("pkey", "-in", "ec.key", "-aes256", "-passout", "pass:secret",
             "-out", "enc.key", cwd=self.dir)
        self.assertRefused("ec.pem", "enc.key", "encrypted")

    def test_rejects_key_block_in_cert_file(self):
        with open(self.p("bundle.pem"), "w") as out:
            for f in ("ec.pem", "ec.key"):
                with open(self.p(f)) as fh:
                    out.write(fh.read())
        self.assertRefused("bundle.pem", "ec.key", "may only contain certificates")

    def test_rejects_unsupported_curve(self):
        _ssl("ecparam", "-name", "secp521r1", "-genkey", "-noout", "-out", "p521.key", cwd=self.dir)
        _ca_cert("p521.key", "p521.pem", self.dir)
        self.assertRefused("p521.pem", "p521.key", "secp521r1 is not supported")

    def test_accepts_p384(self):
        _ssl("ecparam", "-name", "secp384r1", "-genkey", "-noout", "-out", "p384.key", cwd=self.dir)
        _ca_cert("p384.key", "p384.pem", self.dir)
        local_ca.store(self.p("p384.pem"), self.p("p384.key"), self.keys)

    def test_rejects_file_without_pem(self):
        _ssl("x509", "-in", "ec.pem", "-outform", "der", "-out", "ec.der", cwd=self.dir)
        self.assertRefused("ec.der", "ec.key", "no PEM certificate")

def local_doc(**acme) -> dict:
    doc = aws_doc()
    doc["acme"] = {"ca": "local", **acme}
    return doc

class AcmeValidation(unittest.TestCase):
    def test_example_is_valid(self):
        self.assertEqual(validate(aws_doc()), "")

    def test_local_without_contact_email_is_valid(self):
        self.assertEqual(validate(local_doc()), "")

    def test_zerossl_without_contact_email(self):
        doc = aws_doc()
        doc["acme"] = {"ca": "zerossl"}
        self.assertIn("[acme].contact_email required", validate(doc))

    def test_acme_table_required(self):
        doc = aws_doc()
        del doc["acme"]
        self.assertIn("[acme] required", validate(doc))

    def test_installer_forced_keys(self):
        err = validate(local_doc(local_ca={"cert_path": "/x", "key_path": "/y"}))
        self.assertIn("[acme].local_ca is forced by the installer", err)
        self.assertEqual(validate(local_doc(renewal_days=5)), "")

    def test_unknown_ca(self):
        doc = aws_doc()
        doc["acme"]["ca"] = "letsencrypt"
        self.assertIn("[acme].ca must be one of gts, zerossl, local", validate(doc))

    def test_resolver(self):
        cases = {"10.0.0.2": "10.0.0.2:53", "10.0.0.2:5353": "10.0.0.2:5353",
                 "fd00::2": "[fd00::2]:53", "[fd00::2]": "[fd00::2]:53",
                 "[fd00::2]:5353": "[fd00::2]:5353"}
        for given, want in cases.items():
            self.assertEqual(config.resolver_addr(given), want, given)
            self.assertEqual(validate(local_doc(dns_verify_resolver=given)), "", given)
        for bad in ("dns.corp.example", "10.0.0.2:0", "10.0.0.2:65536", "10.0.0.2:",
                    "[fd00::2]:", "", 53, "10.0.0.256"):
            self.assertIn("[acme].dns_verify_resolver",
                          validate(local_doc(dns_verify_resolver=bad)), bad)

class Alloc(FakeAlloc):
    vnis = collections.defaultdict(lambda: 100)

class KeysState(FakeState):
    def __init__(self, keys_dir):
        self.keys_dir = keys_dir

class RenderLocal(unittest.TestCase):
    def setUp(self):
        self.keys = tempfile.mkdtemp(prefix="local-ca-render-")

    def tearDown(self):
        shutil.rmtree(self.keys)

    def renderer(self, doc, stored=True):
        if stored:
            for f in (local_ca.CERT_FILE, local_ca.KEY_FILE):
                open(os.path.join(self.keys, f), "w").close()
        cfg = config.Config(doc)
        tf_out = {"nodes": {n.name: {"external_ip": "203.0.113.1", "instance_id": "i-0"} for n in cfg.nodes}}
        return render.Renderer(cfg, Alloc(), tf_out, KeysState(self.keys))

    def control_toml(self, r) -> dict:
        node = r.cfg.nodes_with("control")[0]
        return tomllib.loads(r.deploy_configs()[f"deploy-config/{node.name}/control.toml"])

    def test_control_toml(self):
        acme = self.control_toml(self.renderer(local_doc(dns_verify_resolver="10.0.0.2")))["acme"]
        self.assertEqual(acme, {
            "renewal_days": 10,
            "dns_verify_resolver": "10.0.0.2:53",
            "local_ca": {"cert_path": "/etc/boxd/ca/local-ca.pem",
                         "key_path": "/etc/boxd/ca/local-ca.key"},
        })

    def test_contact_email_is_not_rendered_in_local_mode(self):
        acme = self.control_toml(self.renderer(local_doc(contact_email="ops@corp.test")))["acme"]
        for k in ("contact_email", "directory_url", "eab_kid", "eab_hmac_key", "eab_single_use"):
            self.assertNotIn(k, acme)

    def test_no_eab_minted_in_local_mode(self):
        r = self.renderer(local_doc())
        r.state.acme_eab = lambda *a, **kw: self.fail("acme_eab called in local mode")
        self.control_toml(r)

    def test_requires_stored_ca(self):
        with self.assertRaises(InstallError) as cm:
            self.renderer(local_doc(), stored=False).deploy_configs()
        self.assertIn("providers set-tls-ca", str(cm.exception))

    def test_group_vars(self):
        self.assertIn("boxd_local_ca: true", self.renderer(local_doc()).group_vars())
        self.assertNotIn("boxd_local_ca", self.renderer(aws_doc(), stored=False).group_vars())

    def test_public_ca_also_gets_the_resolver(self):
        doc = aws_doc()
        doc["acme"]["dns_verify_resolver"] = "[fd00::53]"
        acme = self.control_toml(self.renderer(doc, stored=False))["acme"]
        self.assertEqual(acme["dns_verify_resolver"], "[fd00::53]:53")
        self.assertIn("contact_email", acme)
        self.assertNotIn("local_ca", acme)
        self.assertNotIn("renewal_days", acme)

    def test_only_control_gets_acme(self):
        for path, body in self.renderer(local_doc()).deploy_configs().items():
            if not path.endswith("/control.toml"):
                self.assertNotIn("acme", tomllib.loads(body), path)
