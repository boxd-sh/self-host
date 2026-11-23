"""A Raft member dropped from cluster.toml: before terraform deletes its
instance, apply says what that breaks and what to run first (a missing proxy
keeps its machines' DNS names and SSH ports), and goes on only on a yes; then
its ids are retired for good. Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import contextlib
import copy
import inspect
import io
import os
import tempfile
import tomllib
import types
import unittest
from unittest import mock

from boxd_installer import apply, config, license, main, telemetry, tf
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

    def check(self, doc, answer=None, existing=None):
        """check_removed_members with `answer` typed at the prompt; returns
        (its result, the warning printed, the prompts asked). `existing`: the
        nodes terraform has an instance for (None: not looked up, as on metal)."""
        prompts = []
        def ask(prompt):
            prompts.append(prompt)
            return answer
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = apply.check_removed_members(
                self.state, config.Config(doc), ask if answer is not None else None,
                existing=None if existing is None else (lambda: set(existing)))
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
            out = apply.check_removed_members(self.state, config.Config(without(self.doc, EDGE)),
                                             auto_approve=True)
        self.assertEqual(list(out), [EDGE])
        ask.assert_not_called()
        self.assertIn(f"proxy proxy-{self.ids[f'{EDGE}/proxy']}", err.getvalue())
        self.assertIn("--auto-approve: taking them out", err.getvalue())

    def test_a_node_without_an_instance_is_not_asked_about(self):
        """After a destroy the state keeps every id. The cluster coming back
        with fewer nodes has nothing to take out: no question, even with no
        terminal, and the ids are still retired."""
        doc = without(self.doc, EDGE)
        with mock.patch("sys.stdin", io.StringIO("")):
            out, said, prompts = self.check(doc, existing=[])
        proxy, dns = self.ids[f"{EDGE}/proxy"], self.ids[f"{EDGE}/dns"]
        self.assertEqual(out, {EDGE: {"proxy": proxy, "dns": dns}})
        self.assertEqual(prompts, [])
        self.assertIn(f"terraform has no instance for them, so there is nothing to take out: "
                      f"{EDGE} (proxy {proxy}, dns {dns})", said)
        self.assertNotIn("leaves the cluster unusable", said)
        apply.retire_removed(self.state, out)
        self.assertEqual(self.check(doc, "n", existing=[])[0], {})

    def test_only_the_nodes_with_an_instance_are_asked_about(self):
        doc = without(without(self.doc, EDGE), WORKER)
        out, said, prompts = self.check(doc, "y", existing=[WORKER])
        self.assertEqual(sorted(out), sorted([EDGE, WORKER]))     # both retired
        self.assertEqual(len(prompts), 1)
        warning = said[said.index("warning: "):]
        self.assertIn(f"boxd-ctl node drain worker-{self.ids[f'{WORKER}/worker']}", warning)
        self.assertNotIn(EDGE, warning)
        self.assertIn(f"nothing to take out: {EDGE} ", said)
        # A no still stops everything, the node without an instance included.
        with self.assertRaises(InstallError):
            self.check(doc, "n", existing=[WORKER])

    def test_a_role_dropped_from_a_node_that_stays_is_asked_about(self):
        doc = copy.deepcopy(self.doc)
        next(n for n in doc["nodes"] if n["name"] == EDGE)["roles"] = ["dns:ns2"]
        _, _, prompts = self.check(doc, "y", existing=[EDGE, WORKER])
        self.assertEqual(len(prompts), 1)

    def test_terraform_is_not_asked_when_nothing_was_removed(self):
        def existing():
            raise AssertionError("looked up instances")
        self.assertEqual(apply.check_removed_members(self.state, config.Config(self.doc),
                                                     existing=existing), {})

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
        apply.retire_removed(self.state, removals)
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
                mock.patch.object(apply.sys.stdin, "isatty", return_value=True), \
                mock.patch("builtins.input", return_value="n") as ask, \
                mock.patch.object(State, "ensure_keypairs") as keys, \
                mock.patch.object(tf, "init"), \
                mock.patch.object(tf, "instance_types", return_value={EDGE: "e2-small"}), \
                mock.patch.object(tf, "plan") as plan, \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(InstallError):
                apply._apply(config.Config(without(doc, EDGE)), args)
        ask.assert_called_once()
        keys.assert_not_called()
        # terraform was only read, to see which dropped nodes have an instance.
        plan.assert_not_called()

    def test_a_destroyed_cluster_coming_back_smaller_is_not_asked(self):
        """What `destroy` then `apply` with fewer nodes hits: no instance
        left, so no question, and the apply goes on (here, to its keys)."""
        doc = two_edge_doc()
        state_dir = tempfile.mkdtemp()
        st = State(state_dir)
        st.allocate(config.Config(doc))
        st.save()
        args = types.SimpleNamespace(state_dir=state_dir, out_dir=tempfile.mkdtemp(),
                                     cache_dir=tempfile.mkdtemp(), auto_approve=False)
        class Reached(Exception):
            pass
        err = io.StringIO()
        with mock.patch.object(license, "key_path", return_value=None), \
                mock.patch.object(telemetry, "errors", return_value=[]), \
                mock.patch.object(apply.sys.stdin, "isatty", return_value=False), \
                mock.patch("builtins.input", side_effect=AssertionError("asked")), \
                mock.patch.object(State, "ensure_keypairs", side_effect=Reached), \
                mock.patch.object(tf, "init"), \
                mock.patch.object(tf, "instance_types", return_value={}), \
                contextlib.redirect_stderr(err):
            with self.assertRaises(Reached):
                apply._apply(config.Config(without(doc, EDGE)), args)
        self.assertIn(f"nothing to take out: {EDGE} ", err.getvalue())

    def test_bare_metal_is_always_asked(self):
        """No terraform there: the hosts are the operator's, so every dropped
        node is asked about, as before."""
        src = inspect.getsource(apply._preflight)
        self.assertIn("if clouds.provisions(cfg) else None", src)

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
