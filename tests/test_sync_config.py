"""A node added to an existing cluster joins raft through the leader's
`boxd-ctl sync-config` (ansible.sync_config). Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import subprocess
import unittest
from unittest import mock

from boxd_installer import ansible
from boxd_installer.util import InstallError

LEADER = "Leader: node 1 (10.70.1.10:7000) — this node\nNodes:  2\nVMs:    0\n"
FOLLOWER = "Leader: node 1 (10.70.1.10:7000)\nNodes:  2\nVMs:    0\n"
ELECTING = "Leader: (unknown)\nNodes:  0\nVMs:    0\n"

class Node:
    def __init__(self, name):
        self.name = name

class Cfg:
    def __init__(self, *names):
        self.controls = [Node(n) for n in names]

    def nodes_with(self, role):
        return self.controls if role == "control" else []

def fake_ssh(outputs: dict, calls: list):
    """outputs[(node, command)] -> stdout; records every (node, command)."""
    def ssh(cfg, node, command, **kw):
        calls.append((node.name, command))
        out = outputs.get((node.name, command))
        if out is None:
            return subprocess.CompletedProcess([], 1, "", "no such host")
        return subprocess.CompletedProcess([], 0, out, "")
    return ssh

class LeaderLine(unittest.TestCase):
    def test_parse(self):
        self.assertTrue(ansible._is_leader(LEADER))
        self.assertTrue(ansible._is_leader("Leader: node 3 — this node\n"))
        self.assertFalse(ansible._is_leader(FOLLOWER))
        self.assertFalse(ansible._is_leader(ELECTING))
        # Only the Leader line counts, not a peer list that mentions it.
        self.assertFalse(ansible._is_leader("Leader: node 1\nPeers:\n  2 — this node\n"))

class SyncConfig(unittest.TestCase):
    def run_sync(self, outputs):
        calls = []
        with mock.patch.object(ansible, "_ssh", fake_ssh(outputs, calls)), \
                mock.patch.object(ansible.time, "sleep"):
            added = ansible.sync_config(Cfg("c0", "c1", "c2"))
        return added, calls

    def test_runs_on_the_leader_and_reports_added(self):
        added, calls = self.run_sync({
            ("c0", "sudo boxd-ctl status"): FOLLOWER,
            ("c1", "sudo boxd-ctl status"): LEADER,
            ("c1", "sudo boxd-ctl sync-config"): "Added learner: learner 7 at 10.70.1.21:7000\n",
        })
        self.assertEqual(added, ["Added learner: learner 7 at 10.70.1.21:7000"])
        self.assertEqual([c for c in calls if c[1].endswith("sync-config")],
                         [("c1", "sudo boxd-ctl sync-config")])

    def test_nothing_new_is_a_no_op(self):
        added, _ = self.run_sync({
            ("c0", "sudo boxd-ctl status"): LEADER,
            ("c0", "sudo boxd-ctl sync-config"): "No new learners to add\n",
        })
        self.assertEqual(added, [])

    def test_failure_is_an_error(self):
        with self.assertRaisesRegex(InstallError, "sync-config` on c0 failed"):
            self.run_sync({("c0", "sudo boxd-ctl status"): LEADER})

    def test_no_leader_times_out(self):
        clock = iter(range(0, 10_000, 30))
        with mock.patch.object(ansible.time, "time", lambda: next(clock)), \
                self.assertRaisesRegex(InstallError, "no control node says it is the raft leader"):
            self.run_sync({(n, "sudo boxd-ctl status"): ELECTING for n in ("c0", "c1", "c2")})

if __name__ == "__main__":
    unittest.main()
