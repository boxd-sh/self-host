"""`state` subcommands under ./boxd-install: the pulled copy lives in the
checkout (the container's /tmp is thrown away), the leftover-state guard lets
`state push` through, and reset-eab forgets the single-use GTS key."""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

from boxd_installer import main
from boxd_installer.state import State


class StateCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        open(os.path.join(self.tmp, "cluster.toml"), "w").close()   # main() refuses without one
        self.pulled = os.path.join(self.tmp, "pulled-state")
        self.root = mock.patch.object(main, "ROOT", self.tmp)
        self.pulled_patch = mock.patch.object(main, "PULLED_STATE", self.pulled)
        self.root.start(), self.pulled_patch.start()
        self.addCleanup(self.root.stop)
        self.addCleanup(self.pulled_patch.stop)

    def run_main(self, *argv):
        err = io.StringIO()
        with mock.patch.object(sys, "argv", ["install.py", *argv]), \
             contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = main.main()
        return rc, err.getvalue()

    def test_unpushed_pulled_state_blocks_apply_not_state(self):
        os.makedirs(os.path.join(self.pulled, "keys"))
        open(os.path.join(self.pulled, "installer.json"), "w").close()
        rc, err = self.run_main("apply")
        self.assertEqual(rc, 1)
        self.assertIn("--state-dir pulled-state state push", err)
        with mock.patch.object(main, "cmd_state", return_value=0) as st:
            rc, _ = self.run_main("state", "push")
        self.assertEqual(rc, 0)
        self.assertEqual(st.call_args.args[0].state_dir, self.pulled)  # the default

    def test_legacy_state_dir_can_be_pushed(self):
        legacy = os.path.join(self.tmp, "state")
        os.makedirs(legacy)
        open(os.path.join(legacy, "installer.json"), "w").close()
        self.assertEqual(self.run_main("render")[0], 1)
        with mock.patch.object(main, "cmd_state", return_value=0):
            self.assertEqual(self.run_main("--state-dir", legacy, "state", "push")[0], 0)

    def test_drop_acme_eab(self):
        st = State(tempfile.mkdtemp())
        for n in ("acme_eab.json", "acme_eab_zerossl.json", "boxd-deploy"):
            open(os.path.join(st.keys_dir, n), "w").close()
        self.assertEqual(st.drop_acme_eab(), 2)
        self.assertEqual(os.listdir(st.keys_dir), ["boxd-deploy"])


if __name__ == "__main__":
    unittest.main()
