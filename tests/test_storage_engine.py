"""[storage_engine]: cluster.toml turns boxd's storage engine on for every
worker. Terraform makes the bucket (AWS, GCP), the render gives workers
[storage_engine] and a 0600 bucket.toml, and apply sets the cluster switches
behind a one-way guard. Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import os
import tomllib
import unittest

from boxd_installer import config, tf
from boxd_installer.util import InstallError

from test_machine_sizes import ROOT, FakeState, aws_doc, validate

LICENSED = {"key_file": "license.key"}

def engine_doc(table=None, cloud="aws", **cloud_keys) -> dict:
    doc = aws_doc() if cloud == "aws" else gcp_doc()
    doc["storage_engine"] = {} if table is None else table
    doc["license"] = dict(LICENSED)
    doc[cloud].update(cloud_keys)
    return doc

def gcp_doc() -> dict:
    with open(os.path.join(ROOT, "examples", "gcp", "cluster.toml"), "rb") as fh:
        return tomllib.load(fh)

def tfvars(doc: dict) -> dict:
    if "aws" in doc:
        doc["aws"]["ami"] = "ami-0123456789abcdef0"  # skip the SSM lookup
        return tf.build_tfvars(config.Config(doc), FakeState())
    from boxd_installer.clouds import gcp
    from unittest import mock
    with mock.patch.object(gcp, "_resolve_boot_image", return_value="img"):
        return tf.build_tfvars(config.Config(doc), FakeState())

class Validation(unittest.TestCase):
    def test_absent_and_empty_are_valid(self):
        self.assertEqual(validate(aws_doc()), "")
        self.assertEqual(validate(engine_doc()), "")

    def test_every_setting_in_range(self):
        doc = engine_doc({"default_vm_storage": "bucket", "hibernation_blobs": "auto",
                          "ram_cache_gib": 8, "hibernated_ram_keep_hours": 0.05,
                          "copy_mib_per_sec": 512, "cache_gib": 40, "freeze_interval_secs": 300})
        self.assertEqual(validate(doc), "")

    def test_out_of_range_and_wrong_types(self):
        for key, bad, want in (("ram_cache_gib", 0, "a positive integer"),
                               ("ram_cache_gib", True, "a positive integer"),
                               ("cache_gib", 1.5, "a positive integer"),
                               ("freeze_interval_secs", -1, "a positive integer"),
                               ("copy_mib_per_sec", 1048577, "an integer from 1 to 1048576"),
                               ("hibernated_ram_keep_hours", 9000, "a number from 0 to 8784"),
                               ("hibernated_ram_keep_hours", False, "a number from 0 to 8784"),
                               ("default_vm_storage", "blockd", "must be one of bucket, file"),
                               ("hibernation_blobs", "on", "must be one of uploads, auto, off")):
            with self.subTest(key=key, bad=bad):
                self.assertIn(want, validate(engine_doc({key: bad})))

    def test_forced_keys_are_refused(self):
        for k in ("backends", "dir", "config", "prefix"):
            with self.subTest(k=k):
                self.assertIn(f"[storage_engine].{k} is forced by the installer",
                              validate(engine_doc({k: "x"})))

    def test_the_store_stays_out_of_cluster_toml(self):
        for k in ("bucket", "secret_key", "endpoint", "conditional_put"):
            with self.subTest(k=k):
                self.assertIn(f"[storage_engine].{k} doesn't belong in cluster.toml",
                              validate(engine_doc({k: "x"})))

    def test_unknown_key(self):
        self.assertIn("[storage_engine].ram_gib: unknown key", validate(engine_doc({"ram_gib": 1})))

    def test_not_a_table(self):
        doc = aws_doc()
        doc["storage_engine"] = "on"
        self.assertIn("[storage_engine] must be a table", validate(doc))

    def test_bucket_default_needs_a_license_file(self):
        doc = engine_doc()
        del doc["license"]
        self.assertIn("needs [license] key_file", validate(doc))
        doc["storage_engine"] = {"default_vm_storage": "file"}
        self.assertEqual(validate(doc), "")

    def test_auto_storage_bucket_is_a_bool_on_both_clouds(self):
        self.assertIn("[aws].auto_storage_bucket must be true or false",
                      validate(engine_doc(auto_storage_bucket="no")))
        self.assertEqual(validate(engine_doc(cloud="gcp", auto_storage_bucket=False)), "")
        self.assertIn("[gcp].auto_storage_bucket must be true or false",
                      validate(engine_doc(cloud="gcp", auto_storage_bucket=1)))

    def test_gcp_name_lengths(self):
        doc = engine_doc(cloud="gcp")
        doc["cluster"]["name"] = "a" * 23
        doc["nodes"] = [dict(n, name=n["name"].replace(n["name"].split("-")[0], "a" * 23, 1))
                        for n in doc["nodes"]]
        self.assertIn("must be <= 22 chars on GCP", validate(doc))
        doc["cluster"]["name"] = "a" * 22
        doc["gcp"]["project"] = "p" * 40
        self.assertIn("over GCS's 63", validate(doc))

    def test_aws_bucket_access_vpc_counts_the_storage_bucket(self):
        doc = engine_doc(auto_backups=False, bucket_access="vpc")
        self.assertEqual(validate(doc), "")
        del doc["storage_engine"]
        self.assertIn("there are none", validate(doc))

class ProvidersStore(unittest.TestCase):
    STORE = {"endpoint": "https://objects.example", "region": "r1", "bucket": "b",
             "access_key": "AK", "secret_key": "SK", "conditional_put": "probe",
             "virtual_hosted_style": False}

    def test_a_full_store_is_valid(self):
        config.validate_providers({"storage_engine": self.STORE})

    def test_bad_store_keys(self):
        for bad, want in (({"cache_gib": 3}, "not a store key"),
                          ({"conditional_put": "maybe"}, "conditional_put must be one of"),
                          ({"virtual_hosted_style": "yes"}, "virtual_hosted_style must be true"),
                          ({"bucket": ""}, "must be a non-empty quoted string"),
                          ({"endpoint": "objects.example"}, "must start with https://")):
            with self.subTest(bad=bad), self.assertRaisesRegex(InstallError, want):
                config.validate_providers({"storage_engine": {**self.STORE, **bad}})

class Tfvars(unittest.TestCase):
    def test_off_unless_declared(self):
        self.assertFalse(tfvars(aws_doc())["enable_storage_bucket"])
        self.assertFalse(tfvars(gcp_doc())["enable_storage_bucket"])

    def test_declared_makes_the_bucket(self):
        self.assertTrue(tfvars(engine_doc())["enable_storage_bucket"])
        self.assertTrue(tfvars(engine_doc(cloud="gcp"))["enable_storage_bucket"])

    def test_own_bucket(self):
        self.assertFalse(tfvars(engine_doc(auto_storage_bucket=False))["enable_storage_bucket"])
        self.assertFalse(tfvars(engine_doc(cloud="gcp", auto_storage_bucket=False))
                         ["enable_storage_bucket"])

if __name__ == "__main__":
    unittest.main()
