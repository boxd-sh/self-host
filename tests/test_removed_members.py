"""A Raft member dropped from cluster.toml: before terraform deletes its
instance, apply says what that breaks and what to run first (a missing proxy
keeps its machines' DNS names and SSH ports), and goes on only on a yes; then
its ids are retired for good. Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import contextlib
import copy
import io
import os
import tempfile
import tomllib
import types
import unittest
from unittest import mock

from boxd_installer import config, license, main, telemetry, tf
from boxd_installer.state import State
from boxd_installer.util import InstallError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EDGE = "boxd-gcp-demo-edge-1"
WORKER = "boxd-gcp-demo-worker-0"

def two_edge_doc() -> dict:
    """The GCP example with a second edge: a proxy and the second nameserver."""
    with open(os.path.join(ROOT, "examples", "gcp", "cluster.toml"), "rb") as fh:
        doc = tomllib.load(fh)
    doc["nodes"].append({"name": EDGE, "roles": ["proxy", "dns:ns2"],
                         "internal_ip": "10.60.0.11"})
    return doc

def without(doc: dict, name: str) -> dict:
    doc = copy.deepcopy(doc)
    doc["nodes"] = [n for n in doc["nodes"] if n["name"] != name]
    return doc

class RemovedMembers(unittest.TestCase):
    def setUp(self):
        self.doc = two_edge_doc()
        self.state = State(tempfile.mkdtemp())
        self.state.allocate(config.Config(self.doc))   # the cluster as first applied
        self.ids = self.state.data["node_ids"]

    def check(self, doc, answer=None):
        """check_removed_members with `answer` typed at the prompt; returns
        (its result, the warning printed, the prompts asked)."""
        prompts = []
        def ask(prompt):
            prompts.append(prompt)
            return answer
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = main.check_removed_members(self.state, config.Config(doc),
                                             ask if answer is not None else None)
        return out, err.getvalue(), prompts

    def test_nothing_removed_asks_nothing(self):
        self.assertEqual(self.check(self.doc, "n"), ({}, "", []))

    def test_dropped_edge_warns_and_goes_on_at_yes(self):
        out, warning, prompts = self.check(without(self.doc, EDGE), "yes")
        proxy, dns = self.ids[f"{EDGE}/proxy"], self.ids[f"{EDGE}/dns"]
        self.assertEqual(out, {EDGE: {"proxy": proxy, "dns": dns}})
        self.assertIn("terraform deletes their instances", warning)
        self.assertIn("leaves the cluster unusable", warning)
        self.assertIn(f"proxy proxy-{proxy}: every machine placed on it", warning)
        self.assertIn("put the node back and ask boxd", warning)
        self.assertIn(f"boxd-ctl remove-peer {dns}", warning)
        self.assertIn("glue (A) record", warning)
        self.assertEqual(len(prompts), 1)
        self.assertIn("[y/N]", prompts[0])

    def test_no_stops_the_apply(self):
        for answer in ("n", "", "nope"):
            with self.assertRaises(InstallError) as e:
                self.check(without(self.doc, EDGE), answer)
            self.assertIn("nothing was changed", str(e.exception))

    def test_without_a_terminal_it_stops_with_the_warning(self):
        with mock.patch("sys.stdin", io.StringIO("")), self.assertRaises(InstallError) as e:
            self.check(without(self.doc, EDGE))
        self.assertIn(f"proxy proxy-{self.ids[f'{EDGE}/proxy']}", str(e.exception))
        self.assertIn("needs a yes at a terminal, or --auto-approve", str(e.exception))

    def test_auto_approve_skips_the_question_not_the_warning(self):
        err = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO("")), contextlib.redirect_stderr(err), \
                mock.patch("builtins.input") as ask:
            out = main.check_removed_members(self.state, config.Config(without(self.doc, EDGE)),
                                             auto_approve=True)
        self.assertEqual(list(out), [EDGE])
        ask.assert_not_called()
        self.assertIn(f"proxy proxy-{self.ids[f'{EDGE}/proxy']}", err.getvalue())
        self.assertIn("--auto-approve: taking them out", err.getvalue())

    def test_lost_raft_role_counts(self):
        doc = copy.deepcopy(self.doc)
        next(n for n in doc["nodes"] if n["name"] == EDGE)["roles"] = ["dns:ns2"]
        out, warning, _ = self.check(doc, "y")
        self.assertEqual(list(out[EDGE]), ["proxy"])
        self.assertNotIn("glue", warning)

    def test_dropped_worker_gives_the_commands_with_its_ids(self):
        wid = self.ids[f"{WORKER}/worker"]
        _, warning, _ = self.check(without(self.doc, WORKER), "y")
        for cmd in (f"boxd-ctl node drain worker-{wid}", f"boxd-ctl node mark-dead worker-{wid}",
                    f"boxd-ctl remove-peer {wid}"):
            self.assertIn(cmd, warning)

    def test_moved_non_raft_role_asks_nothing(self):
        # console and metrics have ids but are not Raft members.
        doc = copy.deepcopy(self.doc)
        doc["nodes"][0]["roles"] = ["control", "proxy", "dns:ns1"]
        next(n for n in doc["nodes"] if n["name"] == EDGE)["roles"] += ["console", "metrics"]
        self.assertEqual(self.check(doc, "n")[0], {})

    def test_confirmed_removal_retires_the_ids(self):
        old = {self.ids[f"{EDGE}/proxy"], self.ids[f"{EDGE}/dns"]}
        doc = without(self.doc, EDGE)
        removals, _, _ = self.check(doc, "y")
        main.retire_removed(self.state, removals)
        self.assertEqual(self.check(doc, "n")[0], {})                # asked once only
        self.assertTrue(old <= set(self.state.data["retired_node_ids"]))
        # The same name coming back is a new node: it never gets the old ids.
        self.state.allocate(config.Config(self.doc))
        self.assertFalse(old & {self.ids[f"{EDGE}/proxy"], self.ids[f"{EDGE}/dns"]})

class ApplyAsksBeforeTerraform(unittest.TestCase):
    def test_no_stops_before_any_change(self):
        doc = two_edge_doc()
        state_dir = tempfile.mkdtemp()
        st = State(state_dir)
        st.allocate(config.Config(doc))
        st.save()
        args = types.SimpleNamespace(state_dir=state_dir, out_dir=tempfile.mkdtemp(),
                                     cache_dir=tempfile.mkdtemp(), auto_approve=False)
        with mock.patch.object(license, "key_path", return_value=None), \
                mock.patch.object(telemetry, "errors", return_value=[]), \
                mock.patch.object(main.sys.stdin, "isatty", return_value=True), \
                mock.patch("builtins.input", return_value="n") as ask, \
                mock.patch.object(State, "ensure_keypairs") as keys, \
                mock.patch.object(tf, "init") as init, \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(InstallError):
                main._apply(config.Config(without(doc, EDGE)), args)
        ask.assert_called_once()
        keys.assert_not_called()
        init.assert_not_called()

class AutoApproveFlag(unittest.TestCase):
    def test_parsed(self):
        seen = []
        tmp = tempfile.mkdtemp()
        open(os.path.join(tmp, "cluster.toml"), "w").close()   # main() refuses without one
        for argv, want in ((["apply"], False), (["apply", "-y"], True),
                           (["apply", "--auto-approve"], True)):
            with mock.patch.object(main, "ROOT", tmp), \
                    mock.patch.dict(main.__dict__, {"cmd_apply": lambda a: seen.append(a) or 0}), \
                    mock.patch("sys.argv", ["install.py", "--state-dir", tmp, *argv]):
                self.assertEqual(main.main(), 0)
            self.assertEqual(seen[-1].auto_approve, want)

if __name__ == "__main__":
    unittest.main()
