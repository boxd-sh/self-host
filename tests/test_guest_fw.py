"""The guest firewall vendored from boxd (ansible/roles/boxd-worker/files/
boxd-guest-fw.sh) renders a ruleset for both network modes with the
arguments the worker role passes. Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "ansible", "roles", "boxd-worker", "files", "boxd-guest-fw.sh")
TASKS = os.path.join(ROOT, "ansible", "roles", "boxd-worker", "tasks", "main.yml")

def bash4() -> str | None:
    """A bash new enough for the script (macOS ships 3.2)."""
    for cand in ("bash", "/opt/homebrew/bin/bash", "/usr/local/bin/bash", "/bin/bash"):
        path = shutil.which(cand)
        if not path:
            continue
        out = subprocess.run([path, "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True,
                             text=True).stdout.strip()
        if out.isdigit() and int(out) >= 4:
            return path
    return None

BASH = bash4()

class Script(unittest.TestCase):
    def test_provenance(self):
        with open(SCRIPT) as fh:
            head = fh.read(400)
        self.assertIn("Vendored from boxd-sh/boxd scripts/boxd-guest-fw.sh", head)
        self.assertTrue(os.access(SCRIPT, os.X_OK))

    @unittest.skipUnless(BASH, "needs bash >= 4")
    def test_syntax(self):
        subprocess.run([BASH, "-n", SCRIPT], check=True)

    @unittest.skipUnless(shutil.which("shellcheck"), "needs shellcheck")
    def test_shellcheck(self):
        subprocess.run(["shellcheck", "-S", "warning", SCRIPT], check=True)

    def print_ruleset(self, *args) -> str:
        return subprocess.run([BASH, SCRIPT, *args, "--print"], check=True, capture_output=True,
                              text=True).stdout

    @unittest.skipUnless(BASH, "needs bash >= 4")
    def test_bridge(self):
        out = self.print_ruleset("--subnet", "10.100.0.0/16", "--gateway", "10.100.0.1",
                                 "--peer-subnet", "10.101.0.0/16", "--proxy", "10.100.0.254")
        self.assertIn("table bridge boxd_guest", out)
        self.assertIn("table inet boxd_guest", out)
        self.assertIn("10.101.0.0/16", out)

    @unittest.skipUnless(BASH, "needs bash >= 4")
    def test_routed(self):
        out = self.print_ruleset("--routed", "--subnet", "10.120.0.0/16",
                                 "--gateway", "10.120.0.1", "--proxy", "10.120.0.254")
        self.assertNotIn("table bridge", out)
        self.assertIn('iifname != "tap-*" accept', out)
        self.assertIn("ip daddr { 10.120.0.254 } tcp dport 9460 accept", out)

class Role(unittest.TestCase):
    def test_worker_role_runs_it_fail_closed(self):
        with open(TASKS) as fh:
            tasks = fh.read()
        self.assertIn("src: boxd-guest-fw.sh", tasks)
        self.assertIn("ExecStartPre=/usr/local/bin/boxd-guest-fw --verify", tasks)
        self.assertIn("--routed", tasks)

if __name__ == "__main__":
    unittest.main()
