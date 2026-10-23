"""Private ingress on AWS ([aws] ingress / ingress_cidrs): config rules and
subnet placement. Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import os
import shutil
import tempfile
import tomllib
import unittest

from boxd_installer import config, local_ca, render, tf
from boxd_installer.clouds import aws
from boxd_installer.util import InstallError

from test_local_ca import Alloc, KeysState
from test_machine_sizes import FakeState, aws_doc, validate

# examples/aws/cluster.toml: the proxy/dns node is in the public subnet
# (10.70.0.0/24), the worker in the private one (10.70.1.0/24).
PRIVATE_PROXY_IP = "10.70.1.11"

def private_doc(**extra) -> dict:
    doc = aws_doc()
    doc["aws"].update(ingress="private", **extra)
    doc["acme"] = {"ca": "local"}
    doc["nodes"][0]["internal_ip"] = PRIVATE_PROXY_IP
    return doc

class IngressValidation(unittest.TestCase):
    def test_default_is_public(self):
        doc = aws_doc()
        self.assertEqual(validate(doc), "")
        self.assertEqual(config.Config(doc).ingress, "public")
        self.assertEqual(aws.ingress_cidrs(doc["aws"]), ["0.0.0.0/0"])

    def test_private_is_valid(self):
        doc = private_doc()
        self.assertEqual(validate(doc), "")
        self.assertEqual(config.Config(doc).ingress, "private")
        self.assertEqual(aws.ingress_cidrs(doc["aws"]), ["10.70.0.0/16"])

    def test_unknown_mode(self):
        doc = aws_doc()
        doc["aws"]["ingress"] = "internal"
        self.assertIn("[aws].ingress must be one of public, private, got 'internal'",
                      validate(doc))

    def test_private_requires_local_ca(self):
        doc = private_doc()
        doc["acme"] = {"ca": "zerossl", "contact_email": "ops@example.com"}
        self.assertIn('requires [acme] ca = "local"', validate(doc))
        del doc["acme"]["ca"]  # the AWS default is zerossl
        self.assertIn('requires [acme] ca = "local"', validate(doc))

    def test_cidrs(self):
        doc = private_doc(ingress_cidrs=["10.70.0.0/16", "192.168.0.0/24"])
        self.assertEqual(validate(doc), "")
        self.assertEqual(aws.ingress_cidrs(doc["aws"]), ["10.70.0.0/16", "192.168.0.0/24"])
        public = aws_doc()
        public["aws"]["ingress_cidrs"] = ["198.51.100.0/24"]
        self.assertEqual(validate(public), "")
        self.assertEqual(aws.ingress_cidrs(public["aws"]), ["198.51.100.0/24"])

    def test_bad_cidrs(self):
        for bad in ("10.70.0.0/33", "10.70.0.1/16", "fd00::/8", "vpc", 10):
            self.assertIn("[aws].ingress_cidrs:", validate(private_doc(ingress_cidrs=[bad])), bad)
        for bad in ([], "0.0.0.0/0"):
            self.assertIn("[aws].ingress_cidrs must be a non-empty list",
                          validate(private_doc(ingress_cidrs=bad)), bad)

    def test_proxy_subnet_follows_mode(self):
        doc = private_doc()
        doc["nodes"][0]["internal_ip"] = "10.70.0.10"
        err = validate(doc)
        self.assertIn("internal_ip 10.70.0.10 must be in the private subnet 10.70.1.0/24", err)
        self.assertIn('every node is private with ingress = "private"', err)
        public = aws_doc()
        public["nodes"][0]["internal_ip"] = PRIVATE_PROXY_IP
        self.assertIn(f"internal_ip {PRIVATE_PROXY_IP} must be in the public subnet", validate(public))

    def test_gcp_rejects_ingress(self):
        doc = aws_doc()
        doc["gcp"] = {"project": "p", "region": "r", "zone": "z", "vpc_cidr": "10.70.0.0/16",
                      "image_family": "ubuntu", "state_bucket": "b", "ingress": "private"}
        del doc["aws"]
        doc["acme"] = {"ca": "local"}
        self.assertIn("[gcp].ingress is not supported", validate(doc))

def tfvars(doc: dict) -> dict:
    doc["aws"]["ami"] = "ami-0123456789abcdef0"  # skip the SSM lookup
    return tf.build_tfvars(config.Config(doc), FakeState())

class Tfvars(unittest.TestCase):
    def public_ip(self, tv: dict) -> dict:
        return {n["name"]: n["public_ip"] for n in tv["nodes"]}

    def test_public(self):
        tv = tfvars(aws_doc())
        self.assertEqual(self.public_ip(tv), {"boxd-aws-demo-control-0": True,
                                              "boxd-aws-demo-worker-0": False})
        self.assertEqual(tv["ingress_cidrs"], ["0.0.0.0/0"])

    def test_private(self):
        tv = tfvars(private_doc())
        self.assertEqual(set(self.public_ip(tv).values()), {False})
        self.assertEqual(tv["ingress_cidrs"], ["10.70.0.0/16"])

    def test_explicit_cidrs(self):
        tv = tfvars(private_doc(ingress_cidrs=["100.64.0.0/10"]))
        self.assertEqual(tv["ingress_cidrs"], ["100.64.0.0/10"])

PROXY = "boxd-aws-demo-control-0"
EIP = "203.0.113.7"

class RenderIngress(unittest.TestCase):
    def setUp(self):
        self.keys = tempfile.mkdtemp(prefix="private-ingress-")
        for f in (local_ca.CERT_FILE, local_ca.KEY_FILE):
            open(os.path.join(self.keys, f), "w").close()

    def tearDown(self):
        shutil.rmtree(self.keys)

    def renderer(self, doc, external_ip):
        cfg = config.Config(doc)
        tf_out = {"nodes": {n.name: {"external_ip": external_ip if n.name == PROXY else None,
                                     "instance_id": "i-0"} for n in cfg.nodes}}
        return render.Renderer(cfg, Alloc(), tf_out, KeysState(self.keys))

    def tomls(self, r) -> tuple[dict, dict]:
        files = r.deploy_configs()
        return (tomllib.loads(files[f"deploy-config/{PROXY}/proxy.toml"]),
                tomllib.loads(files[f"deploy-config/{PROXY}/dns.toml"]))

    def static(self, dns: dict) -> dict:
        return {(rec["name"], rec["value"]) for rec in dns["dns"]["static_records"]}

    def test_private_advertises_internal_ip(self):
        r = self.renderer(private_doc(), None)
        proxy, dns = self.tomls(r)
        self.assertEqual(proxy["proxy"]["public_ips"], [PRIVATE_PROXY_IP])
        self.assertEqual(proxy["proxy"]["public_host"], "p0.boxd.example.com")
        recs = self.static(dns)
        for name in ("ns1", "p0", "app", "connect"):
            self.assertIn((name, PRIVATE_PROXY_IP), recs)
        self.assertEqual({v for _, v in recs}, {PRIVATE_PROXY_IP})

    def test_private_dns_setup_replaces_glue(self):
        r = self.renderer(private_doc(), None)
        out = render.glue_records(r.cfg, r)
        self.assertNotIn("Registrar glue", out)
        self.assertIn(f"ns1  {PRIVATE_PROXY_IP}", out)
        self.assertIn(f"Conditional forwarder: boxd.example.com -> {PRIVATE_PROXY_IP}", out)
        self.assertIn("Route 53 Resolver outbound rule", out)
        self.assertIn("Twingate: resources boxd.example.com and *.boxd.example.com", out)

    def test_public_unchanged(self):
        doc = aws_doc()
        doc["acme"] = {"ca": "local"}
        r = self.renderer(doc, EIP)
        proxy, dns = self.tomls(r)
        self.assertEqual(proxy["proxy"]["public_ips"], [EIP])
        self.assertEqual({v for _, v in self.static(dns)}, {EIP})
        out = render.glue_records(r.cfg, r)
        self.assertIn(f"ns1.boxd.example.com  A  {EIP}", out)
        self.assertNotIn("Twingate", out)

    def test_public_without_eip_raises(self):
        doc = aws_doc()
        doc["acme"] = {"ca": "local"}
        with self.assertRaises(InstallError) as cm:
            self.renderer(doc, None).deploy_configs()
        self.assertIn("no external IP in terraform outputs", str(cm.exception))

if __name__ == "__main__":
    unittest.main()
