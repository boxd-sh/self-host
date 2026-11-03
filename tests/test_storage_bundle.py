"""Storage bundles (boxd's bucket build) before the installer configures the
engine: bundle ids, the storage/* channels, and [storage_engine] failing
closed on a file-only bundle.
Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import json
import os
import tempfile
import unittest

from boxd_installer import assets, config, main
from boxd_installer.util import InstallError
from test_assets_channels import doc
from test_release_bundles import NEEDS_ED25519, SHA, Case

# A storage bundle's id is its own, not the commit's (boxd derives it from the sha).
BUCKET_ID = "fedcba9876543210fedcba9876543210fedcba98"

def unpacked(manifest: dict) -> str:
    root = tempfile.mkdtemp()
    with open(os.path.join(root, "MANIFEST.json"), "w") as fh:
        json.dump(manifest, fh)
    return root

class BundleIds(unittest.TestCase):
    def test_an_old_manifest_is_known_by_its_commit(self):
        self.assertEqual(assets.bundle_id({"git_sha": SHA}), SHA)

    def test_a_storage_manifest_is_known_by_its_bundle_id(self):
        self.assertEqual(assets.bundle_id({"git_sha": SHA, "bundle_id": BUCKET_ID}), BUCKET_ID)

class Features(unittest.TestCase):
    def test_a_manifest_without_features_has_none(self):
        self.assertEqual(assets.features(unpacked({"git_sha": SHA})), [])

    def test_features_are_read(self):
        self.assertEqual(assets.features(unpacked({"git_sha": SHA, "features": ["bucket"]})), ["bucket"])

    def test_a_malformed_list_counts_as_none(self):
        self.assertEqual(assets.features(unpacked({"git_sha": SHA, "features": "bucket"})), [])

@NEEDS_ED25519
class StorageBundles(Case):
    def test_a_storage_bundle_installs_from_a_storage_channel(self):
        self.bucket.bundle(BUCKET_ID, manifest_extra={"git_sha": SHA, "bundle_id": BUCKET_ID,
                                                      "features": ["bucket"]})
        self.bucket.channel("storage/latest", BUCKET_ID, 1)
        cfg, root = self.ensure("storage/latest")
        self.assertEqual((cfg.assets_version, cfg.assets_channel), (BUCKET_ID, "storage/latest"))
        self.assertEqual(assets.features(root), ["bucket"])

    def test_a_manifest_for_another_bundle_is_refused(self):
        self.bucket.bundle(BUCKET_ID, manifest_extra={"git_sha": SHA, "bundle_id": SHA})
        self.refused(BUCKET_ID, f"manifest is for bundle {SHA} .boxd commit {SHA}., not the pinned "
                     f"{BUCKET_ID}", sha=BUCKET_ID)

class Validation(unittest.TestCase):
    def with_engine(self, table, version="storage/latest") -> dict:
        d = doc(version)
        d["storage_engine"] = table
        d["license"] = {"key_file": "license.key"}
        return d

    def test_an_empty_table_with_a_license_is_accepted(self):
        config._validate(self.with_engine({}))

    def test_settings_are_accepted_now(self):
        # The installer configures the engine (tests/test_storage_engine.py has the rest).
        config._validate(self.with_engine({"cache_gib": 10}))

    def test_it_must_be_a_table(self):
        with self.assertRaisesRegex(InstallError, r"\[storage_engine\] must be a table"):
            config._validate(self.with_engine("on"))

    def test_validate_warns_about_a_channel_outside_storage(self):
        cfg = config.Config(self.with_engine({}, "latest"))
        self.assertRegex(" ".join(config.storage_bundle_warnings(cfg)), "storage bundles")

    def test_no_warning_for_a_storage_channel_a_pin_or_no_engine(self):
        for d in (self.with_engine({}), self.with_engine({}, SHA), doc("latest")):
            self.assertEqual(config.storage_bundle_warnings(config.Config(d)), [])

class FailClosed(unittest.TestCase):
    def cfg(self, engine: bool):
        d = doc(SHA)
        if engine:
            d["storage_engine"] = {}
        return config.Config(d)

    def test_without_the_table_any_bundle_goes(self):
        main.check_storage_bundle(self.cfg(False), unpacked({"git_sha": SHA}))

    def test_a_file_only_bundle_is_refused(self):
        for manifest in ({"git_sha": SHA}, {"git_sha": SHA, "features": []}):
            with self.assertRaisesRegex(InstallError, 'file-only one.*"storage/latest"'):
                main.check_storage_bundle(self.cfg(True), unpacked(manifest))

    def test_a_storage_bundle_passes(self):
        main.check_storage_bundle(self.cfg(True), unpacked({"git_sha": SHA, "features": ["bucket"]}))

if __name__ == "__main__":
    unittest.main()
