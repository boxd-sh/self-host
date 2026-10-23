"""cluster.toml is git-ignored, so a fresh clone has none: install.py says to
copy cluster.example.toml, and exits 2.
Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

class NoClusterToml(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="test-no-cluster-")
        self.addCleanup(shutil.rmtree, self.dir)
        shutil.copy(os.path.join(ROOT, "install.py"), self.dir)
        shutil.copytree(os.path.join(ROOT, "boxd_installer"), os.path.join(self.dir, "boxd_installer"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.tmp = os.path.join(self.dir, "tmp")   # the TMPDIR install.py sees
        os.mkdir(self.tmp)

    def install(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "install.py", *args], cwd=self.dir,
                              capture_output=True, text=True, env=os.environ | {"TMPDIR": self.tmp})

    def test_default_path(self):
        for cmd in ("validate", "apply", "state"):
            p = self.install(*([cmd, "pull"] if cmd == "state" else [cmd]))
            self.assertEqual(p.returncode, 2, p.stderr)
            self.assertIn("No cluster.toml. Copy cluster.example.toml to cluster.toml and edit it",
                          p.stderr)
            self.assertIn("examples/aws/cluster.toml", p.stderr)
            # Stops before it makes its temporary state and out dirs.
            self.assertEqual(os.listdir(self.tmp), [], cmd)

    def test_named_path(self):
        p = self.install("--cluster", "nope.toml", "validate")
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("No nope.toml.", p.stderr)

    def test_example_is_the_gcp_example(self):
        """cluster.example.toml is a 4-line header and examples/gcp/cluster.toml,
        so the two never drift; it has the [license] and [telemetry] tables."""
        with open(os.path.join(ROOT, "cluster.example.toml")) as fh:
            example = fh.read()
        with open(os.path.join(ROOT, "examples", "gcp", "cluster.toml")) as fh:
            gcp = fh.read()
        header, body = example.split("\n", 4)[:4], example.split("\n", 4)[4]
        self.assertTrue(all(h.startswith("#") for h in header), header)
        self.assertIn("examples/aws/cluster.toml", "\n".join(header))
        self.assertEqual(body, gcp)
        doc = tomllib.loads(example)
        self.assertIn("license", doc)
        self.assertIn("telemetry", doc)
        self.assertIn('# key_file = "license.key"', example)
        self.assertIn("# enabled = false", example)

    def test_example_validates(self):
        shutil.copy(os.path.join(ROOT, "cluster.example.toml"), os.path.join(self.dir, "cluster.toml"))
        p = self.install("validate")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("cluster.toml OK", p.stdout)

if __name__ == "__main__":
    unittest.main()
