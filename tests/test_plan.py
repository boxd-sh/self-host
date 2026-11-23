"""`plan`: apply's checks before terraform and the same terraform plan step
(_plan_instances on GCP and AWS, _plan_gke on GKE), then nothing else. The
installer state is read and thrown away, never pushed. Bare metal has no
terraform. Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import contextlib
import inspect
import io
import os
import shutil
import tempfile
import tomllib
import unittest
from unittest import mock

from boxd_installer import config, main
from boxd_installer.state import State
from boxd_installer.util import InstallError

from test_gke_config import gke_doc
from test_metal import metal_doc
from test_removed_members import WORKER, two_edge_doc, without

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def example(name: str) -> dict:
    with open(os.path.join(ROOT, "examples", name, "cluster.toml"), "rb") as fh:
        return tomllib.load(fh)

class Plan(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="plan-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.state_dir = os.path.join(self.dir, "state")
        self.out_dir = os.path.join(self.dir, "out")
        self.args = mock.Mock(cluster="unused", state_dir=self.state_dir, out_dir=self.out_dir)

    def run_plan(self, doc, prior=None, replaced=(), plan_gke=None):
        """cmd_plan on `doc`; `prior` makes the pulled state (the cluster as
        last applied). Returns (calls in order, stderr)."""
        calls = []
        def pull(cfg, state_dir):
            calls.append("pull")
            if prior is not None:
                s = State(state_dir)
                s.allocate(config.Config(prior))
                os.makedirs(s.keys_dir, exist_ok=True)
                with open(os.path.join(s.keys_dir, "boxd-deploy.pub"), "w") as fh:
                    fh.write("ssh-ed25519 AAAA real")
        def plan_instances(cfg, state, out_dir):
            calls.append("plan_instances")
            self.assertEqual(out_dir, self.out_dir)
            return os.path.join(out_dir, "tf.plan")
        def gke_plan(cfg, state, out_dir, facts):
            calls.append("plan_gke")
            self.assertEqual(facts, {"vpc_mtu": 1460})
            if plan_gke:
                plan_gke()
        cc = mock.Mock(plan_json=mock.Mock(return_value={}),
                       replaced_nodes=mock.Mock(return_value=list(replaced)))
        err = io.StringIO()
        with mock.patch.object(main.config, "load", return_value=config.Config(doc)), \
             mock.patch.multiple(main.remote_state, pull=pull,
                                 push=mock.Mock(side_effect=AssertionError("pushed")),
                                 synced=mock.Mock(side_effect=AssertionError("synced"))), \
             mock.patch.multiple(main, _plan_instances=plan_instances, _plan_gke=gke_plan,
                                 _gke_checks=lambda cfg, state: calls.append("gke_checks")
                                 or {"vpc_mtu": 1460},
                                 control_carry=cc,
                                 tf=mock.Mock(side_effect=AssertionError("tf"))), \
             mock.patch.object(State, "ensure_keypairs", lambda s: calls.append("keys")), \
             contextlib.redirect_stderr(err):
            self.assertEqual(main.cmd_plan(self.args), 0)
        # The pulled state holds secrets: gone after.
        self.assertFalse(os.path.exists(self.state_dir))
        return calls, err.getvalue()

    def test_gcp(self):
        calls, err = self.run_plan(example("gcp"), prior=example("gcp"))
        self.assertEqual(calls, ["pull", "keys", "plan_instances"])
        self.assertNotIn("placeholder", err)

    def test_aws(self):
        calls, _ = self.run_plan(example("aws"), prior=example("aws"))
        self.assertEqual(calls, ["pull", "keys", "plan_instances"])

    def test_gke(self):
        calls, _ = self.run_plan(gke_doc())
        self.assertEqual(calls, ["pull", "gke_checks", "keys", "plan_gke"])

    def test_gke_plan_refused_is_an_error_and_state_still_deleted(self):
        def refuse():
            raise InstallError("refusing the terraform plan: the worker pool is recreated")
        with self.assertRaisesRegex(InstallError, "refusing the terraform plan"):
            self.run_plan(gke_doc(), plan_gke=refuse)
        self.assertFalse(os.path.exists(self.state_dir))

    def test_fresh_cluster_says_the_key_is_a_placeholder(self):
        calls, err = self.run_plan(example("gcp"))
        self.assertIn("keys", calls)
        self.assertIn("placeholder", err)

    def test_removed_node_warns_without_asking(self):
        doc = two_edge_doc()
        with mock.patch("builtins.input", side_effect=AssertionError("asked")):
            calls, err = self.run_plan(without(doc, WORKER), prior=doc)
        self.assertIn("plan_instances", calls)
        self.assertIn("no longer lists", err)
        self.assertIn(WORKER, err)

    def test_replaced_control_node_noted(self):
        doc = example("gcp")
        control = next(n["name"] for n in doc["nodes"] if "control" in n["roles"])
        _, err = self.run_plan(doc, prior=doc, replaced=[control, WORKER])
        self.assertIn(f"replaces control node(s) {control};", err)

    def test_metal_refused_before_anything(self):
        with mock.patch.object(main.config, "load", return_value=config.Config(metal_doc())), \
             mock.patch.object(main.remote_state, "pull", side_effect=AssertionError("pulled")), \
             self.assertRaisesRegex(InstallError, "bare metal has no terraform"):
            main.cmd_plan(self.args)

class SameStepsAsApply(unittest.TestCase):
    """plan and apply share the terraform step, so a plan is the one apply makes."""
    def test_apply_uses_the_shared_steps(self):
        self.assertIn("_plan_instances(cfg, state, args.out_dir)",
                      inspect.getsource(main._infrastructure))
        self.assertIn("_gke_checks(cfg, state)", inspect.getsource(main._gke_preflight))
        self.assertIn("_plan_gke(cfg, state, args.out_dir, facts)",
                      inspect.getsource(main._gke_infrastructure))

if __name__ == "__main__":
    unittest.main()
