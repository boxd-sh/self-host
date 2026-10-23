"""[aws] bucket_access: the backups bucket's data only through the VPC's S3
gateway endpoint (terraform/aws/backups.tf). Run:
python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import unittest

from test_machine_sizes import aws_doc, validate
from test_private_ingress import tfvars

class BucketAccess(unittest.TestCase):
    def test_default_is_anywhere(self):
        self.assertEqual(validate(aws_doc()), "")
        self.assertEqual(tfvars(aws_doc())["bucket_access"], "anywhere")

    def test_vpc(self):
        doc = aws_doc()
        doc["aws"]["bucket_access"] = "vpc"
        self.assertEqual(validate(doc), "")
        self.assertEqual(tfvars(doc)["bucket_access"], "vpc")

    def test_unknown_value(self):
        doc = aws_doc()
        doc["aws"]["bucket_access"] = "private"
        self.assertIn("[aws].bucket_access must be one of anywhere, vpc, got 'private'",
                      validate(doc))

    def test_vpc_needs_the_installer_bucket(self):
        doc = aws_doc()
        doc["aws"].update(bucket_access="vpc", auto_backups=False)
        self.assertIn("there are none", validate(doc))
        doc["snapshot_archive"] = {}  # the archive bucket is enough
        self.assertEqual(validate(doc), "")

    def test_not_on_gcp(self):
        import os, tomllib
        from test_machine_sizes import ROOT
        with open(os.path.join(ROOT, "examples", "gcp", "cluster.toml"), "rb") as fh:
            doc = tomllib.load(fh)
        doc["gcp"]["bucket_access"] = "vpc"
        self.assertIn("[gcp].bucket_access is not supported", validate(doc))

if __name__ == "__main__":
    unittest.main()
