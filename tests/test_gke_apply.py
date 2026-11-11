"""The GKE target's apply: the order of kube.deploy's steps, and where
main._apply_gke puts the checks, terraform, the deploy and the post-install
steps. Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import contextlib
import io
import shutil
import tempfile
import unittest
from unittest import mock

from boxd_installer import config, kube, main
from boxd_installer.util import InstallError

from test_gke_config import gke_doc
from test_gke_render import TF_OUT, renderer

class DeployOrder(unittest.TestCase):
    def test_order(self):
        r = renderer()
        self.addCleanup(shutil.rmtree, r.state.dir, True)
        cfg = r.cfg
        calls = mock.Mock()
        calls.sts.return_value = 1
        calls.crd.return_value = True
        calls.pods.return_value = {}
        applied = []

        def apply(cfg, objs, out_dir, name):
            applied.append(name)
            calls.apply(name)
        with mock.patch.multiple(kube, apply=apply, rollout=calls.rollout,
                                 wait_for_file=calls.wait_for_file,
                                 remove_workers=calls.remove_workers,
                                 update_workers=calls.update_workers,
                                 wait_workers_ready=calls.wait_workers_ready,
                                 prune=calls.prune, verify_registration=calls.verify_registration,
                                 _sts_replicas=calls.sts, _pods=calls.pods, _wait_crd=calls.crd,
                                 clickhouse_schema=calls.clickhouse_schema), \
                mock.patch.multiple(kube.ansible, wait_for_leader=calls.wait_for_leader,
                                    sync_config=calls.sync_config):
            kube.deploy(cfg, r, r.state, "/tmp/out", TF_OUT)
        self.assertEqual(applied, ["base", "secret", "clickhouse", "control", "console", "edge-0",
                                   "edge-1", "worker", "monitoring"])
        names = [c[0] for c in calls.mock_calls]
        at = names.index
        # ClickHouse and its schema before the roles that write to it.
        self.assertLess(at("clickhouse_schema"), names.index("apply", at("clickhouse_schema")))
        self.assertLess(at("wait_for_leader"), at("wait_for_file"))
        self.assertLess(at("wait_for_file"), at("sync_config"))
        # New learners are members before their pods start.
        self.assertLess(at("sync_config"), names.index("apply", at("sync_config")))
        self.assertLess(at("remove_workers"), names.index("update_workers"))
        self.assertLess(at("wait_workers_ready"), at("prune"))
        self.assertLess(at("prune"), at("verify_registration"))
        # One wait per control pod, on its own file.
        self.assertEqual([c.args[1] for c in calls.wait_for_file.call_args_list],
                         ["boxd-control-0", "boxd-control-1", "boxd-control-2"])
        rolled = [c.args[1] for c in calls.rollout.call_args_list]
        self.assertEqual(rolled, ["boxd-clickhouse", "boxd-control", "boxd-console", "boxd-proxy-0",
                                  "boxd-dns-0", "boxd-proxy-1", "boxd-dns-1"])

class ApplyGke(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gke-apply-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def run_apply(self, applied=False, plan_problems=(), live=([], [], {})):
        cfg = config.Config(gke_doc(), self.dir)
        calls = mock.Mock()
        args = mock.Mock(state_dir=self.dir, out_dir=self.dir, cache_dir=self.dir)
        state = mock.Mock()
        state.data = {"applied_assets": {"sha": "x"}} if applied else {}
        state.load_providers.return_value = {}
        state.egress_ca.return_value = ("c", "k")
        with mock.patch.multiple(main, State=mock.Mock(return_value=state), assets=mock.DEFAULT,
                                 composio=mock.DEFAULT, storage_engine=mock.DEFAULT,
                                 check_storage_bundle=mock.DEFAULT, log=mock.DEFAULT), \
             mock.patch.multiple(main.clouds.gke, live_checks=mock.Mock(return_value=live),
                                 write_tfvars=calls.write_tfvars, record=calls.record,
                                 plan_problems=mock.Mock(return_value=list(plan_problems))), \
             mock.patch.multiple(main.tf, init=mock.DEFAULT, plan=calls.tf_plan,
                                 apply_plan=calls.tf_apply, read_outputs=mock.Mock(return_value=TF_OUT),
                                 show_json=mock.Mock(return_value="{}")), \
             mock.patch.multiple(kube, connect=calls.connect, deploy=calls.deploy,
                                 live_worker_replicas=mock.Mock(return_value=2),
                                 pull_config=mock.Mock(return_value=None)), \
             mock.patch("boxd_installer.kube_render.KubeRenderer") as renderer_cls, \
             mock.patch.multiple(main.ansible, set_keys_dir=mock.DEFAULT, set_tf_outputs=mock.DEFAULT,
                                 pause_machine_specs=calls.pause, worker_egress_ca=mock.DEFAULT,
                                 init_cluster_id=calls.init_cluster_id, turn_on_machine_specs=mock.DEFAULT,
                                 install_license=mock.DEFAULT, set_telemetry=mock.DEFAULT,
                                 seed_templates=calls.seed_templates, configure_orgs=mock.DEFAULT,
                                 acme_eab_spent=mock.Mock(return_value=False)), \
             mock.patch.object(main.render, "glue_records", return_value=""), \
             contextlib.redirect_stdout(io.StringIO()):
            main.assets.computer_image.return_value = ("computer", "v1")
            main.storage_engine.template_storage.return_value = "file"
            renderer_cls.return_value.alloc = mock.Mock()
            rc = main._apply_gke(cfg, args)
        return rc, calls

    def test_order(self):
        rc, calls = self.run_apply()
        self.assertEqual(rc, 0)
        names = [c[0] for c in calls.mock_calls]
        at = names.index
        self.assertLess(at("write_tfvars"), at("tf_plan"))
        self.assertLess(at("tf_apply"), at("connect"))
        self.assertLess(at("connect"), at("deploy"))
        self.assertLess(at("deploy"), at("record"))
        self.assertLess(at("record"), at("init_cluster_id"))
        self.assertLess(at("init_cluster_id"), at("seed_templates"))
        # The live worker count reaches terraform.
        self.assertEqual(calls.write_tfvars.call_args.args[3], 2)
        self.assertEqual(calls.seed_templates.call_args.args[1], "computer")
        calls.pause.assert_not_called()   # a fresh cluster

    def test_refusals_stop_before_terraform(self):
        with self.assertRaisesRegex(InstallError, "this apply can't go on"):
            self.run_apply(live=(["policy says no"], [], {}))

    def test_plan_guard(self):
        with self.assertRaisesRegex(InstallError, "refusing the terraform plan"):
            self.run_apply(applied=True, plan_problems=["replaces the worker pool"])

    def test_upgrade_pauses_machine_specs(self):
        _, calls = self.run_apply(applied=True)
        calls.pause.assert_called_once()

class ValidateGke(unittest.TestCase):
    def test_errors_and_ok(self):
        cfg = config.Config(gke_doc())
        with mock.patch.object(main.config, "load", return_value=cfg), \
                mock.patch.object(main.clouds.gke, "live_checks", return_value=(["no"], ["careful"], {})), \
                mock.patch.object(main, "log") as log, \
                self.assertRaisesRegex(InstallError, "can't be installed on GKE:\n  no"):
            main.cmd_validate(mock.Mock(cluster="cluster.toml"))
        self.assertIn("careful", str(log.call_args_list))
        out = io.StringIO()
        with mock.patch.object(main.config, "load", return_value=cfg), \
                mock.patch.object(main.clouds.gke, "live_checks", return_value=([], [], {})), \
                mock.patch.object(main, "log"), contextlib.redirect_stdout(out):
            self.assertEqual(main.cmd_validate(mock.Mock(cluster="cluster.toml")), 0)
        self.assertIn("cluster.toml OK", out.getvalue())

    def test_license_is_an_error(self):
        doc = gke_doc()
        doc["gke"]["pools"]["worker"]["nodes"] = 3
        cfg = config.Config(doc)
        with mock.patch.object(main.config, "load", return_value=cfg), \
                mock.patch.object(main.clouds.gke, "live_checks", return_value=([], [], {})), \
                mock.patch.object(main, "log"), \
                self.assertRaisesRegex(InstallError, "48 vCPUs"):
            main.cmd_validate(mock.Mock(cluster="cluster.toml"))

class RenderGke(unittest.TestCase):
    def test_render_writes_the_secret_files_and_the_manifests(self):
        import json
        import os
        d = tempfile.mkdtemp(prefix="gke-render-cmd-")
        self.addCleanup(shutil.rmtree, d, True)
        fake = os.path.join(d, "tf.json")
        with open(fake, "w") as fh:
            json.dump(TF_OUT, fh)
        cfg = config.Config(gke_doc())
        args = mock.Mock(cluster="c", fake_tf=fake, state_dir=os.path.join(d, "state"),
                         out_dir=os.path.join(d, "out"))
        with mock.patch.object(main.config, "load", return_value=cfg), \
                mock.patch("boxd_installer.state.State.acme_eab",
                           return_value={"kid": "k", "hmac": "h"}), \
                mock.patch("boxd_installer.state.State.ensure_keypairs",
                           lambda st: open(os.path.join(st.keys_dir, "proxy_host_ed25519"), "w").write("k\n")), \
                mock.patch.object(main, "log"):
            self.assertEqual(main.cmd_render(args), 0)
        with open(os.path.join(d, "out", "manifests.json")) as fh:
            kinds = {o["kind"] for o in json.load(fh)["items"]}
        self.assertTrue({"Namespace", "StatefulSet", "DaemonSet", "PodMonitoring"} <= kinds)
        self.assertTrue(os.path.exists(os.path.join(d, "out", "boxd-config", "worker-0.toml")))

class DestroyGke(unittest.TestCase):
    def test_existing_cluster_objects_go_first(self):
        doc = gke_doc(cluster="projects/my-gcp-project/locations/europe-west4/clusters/platform")
        for k in ("admin_cidrs", "vpc_cidr", "pods_cidr", "services_cidr"):
            doc["gke"].pop(k)
        cfg = config.Config(doc)
        state = mock.Mock(data={"gke": {"workers": 1}}, dir=tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, state.dir, True)
        calls = mock.Mock()
        with mock.patch.object(main.config, "load", return_value=cfg), \
                mock.patch.object(main.remote_state, "synced"), \
                mock.patch.object(main, "State", return_value=state), \
                mock.patch.object(main.tf, "init"), \
                mock.patch.object(main.tf, "destroy", calls.destroy), \
                mock.patch.object(main.tf, "cached_outputs", return_value=TF_OUT), \
                mock.patch.object(main.clouds.gke, "write_tfvars", calls.write_tfvars), \
                mock.patch.object(main.clouds.gke, "live_checks", return_value=([], [], {"vpc_mtu": 1500})), \
                mock.patch.multiple(kube, connect=mock.DEFAULT, teardown=calls.teardown,
                                    live_worker_replicas=mock.Mock(return_value=1)), \
                contextlib.redirect_stdout(io.StringIO()):
            main.cmd_destroy(mock.Mock(cluster="c", state_dir=tempfile.mkdtemp(), out_dir=tempfile.mkdtemp()))
        names = [c[0] for c in calls.mock_calls]
        self.assertLess(names.index("teardown"), names.index("destroy"))
        self.assertEqual(calls.write_tfvars.call_args.args[4], {"vpc_mtu": 1500})
        self.assertNotIn("gke", state.data)

class RecreateWorker(unittest.TestCase):
    """A pod spec change drains the worker, then recreates its pod. A drain that fails (machines
    that can't move) puts the worker back in placement and leaves its pod alone."""
    def recreate(self, drain_error=None):
        d = tempfile.mkdtemp(prefix="gke-recreate-")
        self.addCleanup(shutil.rmtree, d, True)
        cfg = config.Config(gke_doc(), d)
        ctl, kubectl = mock.Mock(), mock.Mock()
        drain = mock.Mock(side_effect=drain_error)
        with mock.patch.multiple(kube, drain=drain, kubectl=kubectl, _wait=mock.Mock(),
                                 _worker_pod=mock.Mock(return_value={"metadata": {"uid": "u1"}}),
                                 _registered=mock.Mock(return_value={"worker-5"})), \
                mock.patch.object(kube.ansible, "ctl", ctl):
            try:
                kube.recreate_worker(cfg, 0, "worker-5")
            finally:
                self.ctl = [c.args[1] for c in ctl.call_args_list]
                self.deleted = any(c.args[1:3] == ("delete", "pod") for c in kubectl.call_args_list)

    def test_a_failed_drain_puts_the_worker_back(self):
        failed = InstallError("boxd-ctl node drain worker-5 failed, so the worker stays: 2 left")
        with self.assertRaisesRegex(InstallError, "(?s)2 left.*worker-5 is back in placement"):
            self.recreate(failed)
        self.assertEqual(self.ctl, ["node exclude worker-5", "node include worker-5"])
        self.assertFalse(self.deleted, "the pod is not recreated")

    def test_a_drained_worker_gets_its_new_pod(self):
        self.recreate()
        self.assertEqual(self.ctl, ["node exclude worker-5", "node include worker-5"])
        self.assertTrue(self.deleted)

if __name__ == "__main__":
    unittest.main()


class ApplyPlanCheckpoint(unittest.TestCase):
    def test_before_runs_ahead_of_terraform(self):
        from boxd_installer import tf
        order = []
        with mock.patch.object(tf, "_tf", side_effect=lambda *a, **k: order.append("terraform")):
            tf.apply_plan(object(), "tf.plan", before=lambda: order.append("checkpoint"))
        self.assertEqual(order, ["checkpoint", "terraform"])
