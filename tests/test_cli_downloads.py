"""The CLI downloads the installer renders for the zone (render.downloads()): the
latest-<platform>.json manifests, install.sh, and install.ps1 for Windows when the bundle
carries its template. Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import hashlib
import json
import os
import shutil
import tempfile
import unittest

from boxd_installer import config, render
from boxd_installer.util import InstallError

from test_local_ca import Alloc
from test_machine_sizes import FakeState, aws_doc

# The template lines the installer rewrites, as boxd's scripts/install-cli.{sh,ps1} have them.
SH = '#!/bin/sh\nBASE_URL="https://boxd.sh/downloads/cli"\nBINARY_NAME="boxd"\n'
PS1 = ('$BaseUrl = "https://boxd.sh/downloads/cli"\n$BinaryName = "boxd"\n$Onboarding = 0\n'
       '$ApiUrl = ""\n\nfunction Install-BoxdCli {\n    $BaseUrl = "inside"\n}\n')


class Downloads(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="cli-downloads-")
        self.addCleanup(shutil.rmtree, self.root)
        os.mkdir(os.path.join(self.root, "cli"))
        with open(os.path.join(self.root, "MANIFEST.json"), "w") as fh:
            json.dump({"git_sha": "0" * 40, "cli": {"version": "0.3.6"}}, fh)
        for name in ("boxd-linux-amd64", "boxd-windows-amd64"):
            self.write(f"cli/{name}", name)
        self.write("cli/install-template.sh", SH)
        self.cfg = config.Config(aws_doc())

    def write(self, rel: str, body: str) -> None:
        with open(os.path.join(self.root, rel), "w") as fh:
            fh.write(body)

    def downloads(self) -> dict[str, str]:
        tf_out = {"nodes": {n.name: {"external_ip": "203.0.113.1"} for n in self.cfg.nodes}}
        return render.Renderer(self.cfg, Alloc(), tf_out, FakeState(),
                               assets_root=self.root).downloads()

    def test_every_platform_gets_a_manifest(self):
        files = self.downloads()
        zone = self.cfg.zone
        m = json.loads(files["downloads/cli/latest-windows-amd64.json"])
        self.assertEqual(m, {"version": "0.3.6",
                             "url": f"https://app.{zone}/downloads/cli/boxd-windows-amd64",
                             "sha256": hashlib.sha256(b"boxd-windows-amd64").hexdigest()})
        self.assertIn("downloads/cli/latest-linux-amd64.json", files)

    def test_install_ps1_points_at_the_cluster(self):
        self.write("cli/install-template.ps1", PS1)
        ps1 = self.downloads()["downloads/install.ps1"]
        zone = self.cfg.zone
        self.assertIn(f'$BaseUrl = "https://app.{zone}/downloads/cli"\n', ps1)
        self.assertIn(f'$ApiUrl = "https://{zone}"\n', ps1)
        self.assertIn('$BinaryName = "boxd"\n', ps1)
        self.assertIn('    $BaseUrl = "inside"\n', ps1)  # only the top-level line

    def test_no_install_ps1_from_an_older_bundle(self):
        files = self.downloads()
        self.assertIn("downloads/install.sh", files)
        self.assertNotIn("downloads/install.ps1", files)

    def test_a_template_without_the_api_url_line_stops_the_render(self):
        self.write("cli/install-template.ps1", PS1.replace('$ApiUrl = ""\n', ""))
        with self.assertRaisesRegex(InstallError, r"\$ApiUrl line not found"):
            self.downloads()


if __name__ == "__main__":
    unittest.main()
