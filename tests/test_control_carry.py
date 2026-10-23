"""Control state carried across an instance replacement (control_carry).
Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

from boxd_installer import ansible, config, control_carry
from boxd_installer.state import State
from boxd_installer.util import InstallError

from test_machine_sizes import aws_doc

def change(type_, index, actions, name="node"):
    return {"type": type_, "name": name, "index": index, "change": {"actions": actions}}

def control_tarball() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in (("./state.db", b"db"), ("./raft/CURRENT", b"x")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()

class ReplacedNodes(unittest.TestCase):
    def test_only_replaced_instances(self):
        plan = {"resource_changes": [
            change("aws_instance", "control-0", ["delete", "create"]),
            change("aws_instance", "worker-0", ["update"]),
            change("google_compute_instance", "proxy-0", ["create", "delete"]),
            change("aws_instance", "new-0", ["create"]),
            change("aws_eip", "control-0", ["delete"], name="public"),
        ]}
        self.assertEqual(control_carry.replaced_nodes(plan), ["control-0", "proxy-0"])

    def test_no_changes(self):
        self.assertEqual(control_carry.replaced_nodes({}), [])

class Save(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state = State(self.tmp)
        self.cfg = config.Config(aws_doc())
        self.control = self.cfg.nodes_with("control")[0].name
        worker = self.cfg.nodes_with("worker")[0].name
        with open(os.path.join(self.tmp, "tf-outputs.json"), "w") as fh:
            json.dump({"nodes": {self.control: {"instance_id": "i-control"},
                                 worker: {"instance_id": "i-worker"}}}, fh)
        ansible.set_keys_dir(self.state.keys_dir)
        self.dest = control_carry.archive_path(self.state, self.control)

    def plan(self, *names):
        return {"resource_changes": [change("aws_instance", n, ["delete", "create"])
                                     for n in names]}

    def fake_ssh(self, reachable=True, tarball=None):
        calls = []
        real_run = subprocess.run
        def run(cmd, stdout=None, **kw):
            if cmd[0] == "tar":  # the local listing of the archive: real
                return real_run(cmd, **kw)
            calls.append(cmd[-1])
            if "test -f" in cmd[-1]:
                return subprocess.CompletedProcess(cmd, 0 if reachable else 255,
                                                   b"", b"unreachable")
            stdout.write(tarball or control_tarball())
            return subprocess.CompletedProcess(cmd, 0, None, b"")
        return calls, mock.patch.object(control_carry.subprocess, "run", side_effect=run)

    def test_archives_replaced_control_node(self):
        calls, patch = self.fake_ssh()
        with patch:
            control_carry.save(self.cfg, self.state, self.plan(self.control))
        self.assertTrue(os.path.exists(self.dest))
        self.assertEqual(os.stat(self.dest).st_mode & 0o777, 0o600)
        self.assertIn("systemctl stop boxd-control", calls[-1])

    def test_ignores_non_control_and_unchanged(self):
        worker = self.cfg.nodes_with("worker")[0].name
        calls, patch = self.fake_ssh()
        with patch:
            control_carry.save(self.cfg, self.state, self.plan(worker))
            control_carry.save(self.cfg, self.state, {})
        self.assertEqual(calls, [])
        self.assertFalse(os.path.exists(control_carry.carry_dir(self.state)))

    def test_unreachable_without_archive_refuses(self):
        _, patch = self.fake_ssh(reachable=False)
        with patch, self.assertRaisesRegex(InstallError, "empty cluster"):
            control_carry.save(self.cfg, self.state, self.plan(self.control))

    def test_unreachable_keeps_previous_archive(self):
        os.makedirs(control_carry.carry_dir(self.state))
        with open(self.dest, "wb") as fh:
            fh.write(b"previous")
        _, patch = self.fake_ssh(reachable=False)
        with patch:
            control_carry.save(self.cfg, self.state, self.plan(self.control))
        with open(self.dest, "rb") as fh:
            self.assertEqual(fh.read(), b"previous")

    def test_incomplete_archive_refused(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo("./raft/CURRENT")
            tar.addfile(info, io.BytesIO(b""))
        _, patch = self.fake_ssh(tarball=buf.getvalue())
        with patch, self.assertRaisesRegex(InstallError, "missing state.db"):
            control_carry.save(self.cfg, self.state, self.plan(self.control))
        self.assertEqual(os.listdir(control_carry.carry_dir(self.state)), [])

    def test_clear(self):
        _, patch = self.fake_ssh()
        with patch:
            control_carry.save(self.cfg, self.state, self.plan(self.control))
        control_carry.clear(self.state)
        self.assertFalse(os.path.exists(control_carry.carry_dir(self.state)))
        control_carry.clear(self.state)  # nothing to clear is fine

if __name__ == "__main__":
    unittest.main()
