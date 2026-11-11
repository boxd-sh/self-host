"""The guest firewall vendored from boxd (ansible/roles/boxd-worker/files/
boxd-guest-fw.sh) renders a ruleset for both network modes with the
arguments the worker role passes. Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import os
import shutil
import subprocess
import tempfile
import time
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

REAPPLY = "- name: Reapply guest firewall when its script or unit is newer than the loaded ruleset"

def task_shell(name: str) -> str:
    """The `shell: |` block of the worker role's task `name`, as written."""
    with open(TASKS) as fh:
        lines = fh.read().splitlines()
    i = lines.index(name)
    j = lines.index("  shell: |", i)
    out = []
    for line in lines[j + 1:]:
        if line and not line.startswith("    "):
            break
        out.append(line[4:])
    return "\n".join(out) + "\n"

def gnu_coreutils() -> bool:
    return subprocess.run(["date", "-d", "@0", "+%s"], capture_output=True).returncode == 0 and \
        subprocess.run(["stat", "-c", "%Y", "/"], capture_output=True).returncode == 0

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

class Reapply(unittest.TestCase):
    """The task that reapplies the firewall when its script or unit is newer than its last
    start, run with a stub systemctl: a re-run after a failed play still reapplies it."""
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="guest-fw-reapply-")
        self.addCleanup(shutil.rmtree, self.dir)
        self.script, self.unit = (os.path.join(self.dir, f) for f in ("fw", "fw.service"))
        for f in (self.script, self.unit):
            open(f, "w").close()
        self.log = os.path.join(self.dir, "calls")
        stub = os.path.join(self.dir, "bin", "systemctl")
        os.makedirs(os.path.dirname(stub))
        with open(stub, "w") as fh:
            fh.write('#!/bin/sh\nif [ "$1" = show ]; then echo "$STARTED"; '
                     'else echo "$*" >> "$CALLS"; fi\n')
        os.chmod(stub, 0o755)
        self.shell = (task_shell(REAPPLY).replace("/usr/local/bin/boxd-guest-fw", self.script)
                      .replace("/etc/systemd/system/boxd-guest-fw.service", self.unit))

    def run_task(self, started: float | None, script_mtime: float, unit_mtime: float):
        os.utime(self.script, (script_mtime, script_mtime))
        os.utime(self.unit, (unit_mtime, unit_mtime))
        when = "" if started is None else time.strftime("%a %Y-%m-%d %H:%M:%S UTC",
                                                         time.gmtime(started))
        env = {**os.environ, "PATH": f"{self.dir}/bin:{os.environ['PATH']}",
               "STARTED": when, "CALLS": self.log}
        res = subprocess.run([BASH, "-c", self.shell], env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        calls = []
        if os.path.exists(self.log):
            with open(self.log) as fh:
                calls = fh.read().split("\n")
        return res.stdout.strip(), [c for c in calls if c]

    @unittest.skipUnless(BASH and gnu_coreutils(), "needs bash >= 4 and GNU date/stat")
    def test_newer_files_reapply_it_and_older_ones_dont(self):
        t = time.time() - 3600
        self.assertEqual(self.run_task(t, t - 60, t - 60), ("", []), "loaded after both changed")
        self.assertEqual(self.run_task(t, t - 60, t + 30),
                         ("reapplied", ["daemon-reload", "restart boxd-guest-fw"]),
                         "a unit changed by a run that failed before reapplying it")

    @unittest.skipUnless(BASH and gnu_coreutils(), "needs bash >= 4 and GNU date/stat")
    def test_a_newer_script_alone_reapplies_it(self):
        t = time.time() - 3600
        self.assertEqual(self.run_task(t, t + 30, t - 60)[0], "reapplied")

    @unittest.skipUnless(BASH and gnu_coreutils(), "needs bash >= 4 and GNU date/stat")
    def test_never_started_is_left_to_the_start_task(self):
        self.assertEqual(self.run_task(None, time.time(), time.time()), ("", []))

class Role(unittest.TestCase):
    def test_the_firewall_is_reapplied_by_a_task_not_a_handler(self):
        with open(TASKS) as fh:
            tasks = fh.read()
        self.assertNotIn("notify: reapply guest firewall", tasks)
        start = tasks.index("- name: Enable + start guest firewall service")
        self.assertLess(start, tasks.index(REAPPLY))
        self.assertLess(tasks.index(REAPPLY),
                        tasks.index("- name: Load the guest firewall's relay chain"))

    def test_worker_role_runs_it_fail_closed(self):
        with open(TASKS) as fh:
            tasks = fh.read()
        self.assertIn("src: boxd-guest-fw.sh", tasks)
        self.assertIn("ExecStartPre=/usr/local/bin/boxd-guest-fw --verify", tasks)
        self.assertIn("--routed", tasks)

if __name__ == "__main__":
    unittest.main()
