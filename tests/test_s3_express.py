"""[aws] storage_bucket_type = "express": the storage engine's bucket is an S3
Express One Zone directory bucket. Validation, tfvars, the workers'
bucket.toml, the warnings, and destroy keeping it.
Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import unittest

from boxd_installer import config, storage_engine
from boxd_installer.util import InstallError

from test_machine_sizes import aws_doc, validate
import test_storage_engine
from test_storage_engine import PREFIX, TF_S3, WORKER, engine_doc, tfvars

TF_EXPRESS = {**TF_S3, "bucket": "demo-storage-1--use1-az4--x-s3", "s3_express": True}

class Validation(unittest.TestCase):
    def test_express_and_standard_are_valid(self):
        self.assertEqual(validate(engine_doc(storage_bucket_type="express")), "")
        self.assertEqual(validate(engine_doc(storage_bucket_type="standard")), "")

    def test_unknown_type(self):
        self.assertIn("storage_bucket_type must be one of",
                      validate(engine_doc(storage_bucket_type="glacier")))

    def test_needs_the_installers_bucket(self):
        doc = aws_doc()
        doc["aws"]["storage_bucket_type"] = "express"
        self.assertIn("it needs [storage_engine]", validate(doc))
        self.assertIn("it needs [storage_engine]",
                      validate(engine_doc(storage_bucket_type="express",
                                          auto_storage_bucket=False)))

    def test_own_directory_bucket_in_providers(self):
        config.validate_providers({"storage_engine": {
            "region": "eu-central-1", "bucket": "own--euc1-az2--x-s3",
            "access_key": "AK", "secret_key": "s", "s3_express": True}})
        with self.assertRaisesRegex(InstallError, "s3_express must be true or false"):
            config.validate_providers({"storage_engine": {
                "region": "r", "bucket": "b", "access_key": "AK", "secret_key": "s",
                "s3_express": "yes"}})

class Tfvars(unittest.TestCase):
    def test_express(self):
        v = tfvars(engine_doc(storage_bucket_type="express"))
        self.assertTrue(v["enable_storage_bucket"])
        self.assertTrue(v["storage_bucket_express"])

    def test_standard_by_default(self):
        self.assertFalse(tfvars(engine_doc())["storage_bucket_express"])
        self.assertFalse(tfvars(aws_doc())["storage_bucket_express"])

class Warnings(unittest.TestCase):
    def test_single_az_warning(self):
        w = config.storage_bucket_warnings(config.Config(engine_doc(storage_bucket_type="express")))
        self.assertEqual(len(w), 1)
        self.assertIn("only durable copy", w[0])

    def test_vpc_access_doesnt_cover_it(self):
        w = config.storage_bucket_warnings(config.Config(
            engine_doc(storage_bucket_type="express", bucket_access="vpc")))
        self.assertTrue(any("doesn't cover the S3 Express" in x for x in w))

    def test_none_on_standard(self):
        self.assertEqual(config.storage_bucket_warnings(config.Config(engine_doc())), [])

class BucketToml(unittest.TestCase):
    """Renders with test_storage_engine.Render's helpers (not a subclass:
    its own tests would run again here)."""
    def setUp(self):
        self.r = test_storage_engine.Render()
        self.r.setUp()
        self.addCleanup(self.r.tearDown)

    def bucket_toml(self, doc, tf_store) -> dict:
        files = self.r.files(doc, tf_store)
        return self.r.toml(files, f"deploy-config/{WORKER}/bucket.toml")

    def test_express_store(self):
        self.assertEqual(self.bucket_toml(engine_doc(storage_bucket_type="express"), TF_EXPRESS), {
            "kind": "s3", "region": "us-east-1", "bucket": "demo-storage-1--use1-az4--x-s3",
            "prefix": PREFIX, "access_key_id": "AKIATF", "secret_access_key": "tf-secret",
            "s3_express": True})

    def test_standard_store_has_no_express_key(self):
        self.assertNotIn("s3_express", self.bucket_toml(engine_doc(), TF_S3))

class Destroy(unittest.TestCase):
    def test_a_directory_bucket_is_kept(self):
        addr = "aws_s3_directory_bucket.storage[0]"
        self.assertEqual(storage_engine.kept_buckets([addr]), [addr])

if __name__ == "__main__":
    unittest.main()
