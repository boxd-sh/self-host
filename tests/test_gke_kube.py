"""The GKE target's driver (kube.py) against a fake kubectl: the boxd-ctl
transport, apply and prune, worker removal through `node drain`, workers
updated in place, membership waits. Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import base64
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import unittest
from unittest import mock

from boxd_installer import ansible, config, kube, kube_render, main
from boxd_installer.util import InstallError

from test_gke_config import gke_doc
from test_gke_render import TF_OUT, make_state, renderer

def ok(out: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0, out, "")

def fail(err: str, out: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 1, out, err)

class FakeKube:
    """Just enough of a cluster for kube.py: objects by (resource, name),
    worker pods with container statuses, mounted Secret files, logs."""
    KIND_RESOURCE = {"StatefulSet": "statefulset", "Service": "service", "ConfigMap": "configmap",
                     "Secret": "secret", "PodMonitoring": "podmonitoring"}

    def __init__(self):
        self.objects: dict[tuple[str, str], dict] = {}
        self.pods: dict[str, dict] = {}
        self.files: dict[tuple[str, str], str] = {}
        self.logs: dict[str, str] = {}
        self.calls: list[list[str]] = []
        self.ctl = []
        self.uid = 0

    def add_worker_pod(self, i, images, annotations=None, node=None):
        self.uid += 1
        self.pods[f"boxd-worker-{i}"] = {
            "metadata": {"name": f"boxd-worker-{i}", "uid": f"uid-{self.uid}",
                         "annotations": dict(annotations or {})},
            "spec": {"nodeName": node or f"gke-n-{i}",
                     "containers": [{"name": n, "image": img} for n, img in images.items()]},
            "status": {"conditions": [{"type": "Ready", "status": "True"}],
                       "containerStatuses": [{"name": n, "image": img, "ready": True,
                                              "restartCount": 0, "state": {"running": {}}}
                                             for n, img in images.items()]}}

    def set_sts(self, replicas):
        self.objects[("statefulset", "boxd-worker")] = {"spec": {"replicas": replicas},
                                                        "status": {"readyReplicas": replicas}}

    def __call__(self, cfg, *args, check=True, quiet=True, input=None):
        args = list(args)
        self.calls.append(args)
        verb = args[0]
        if verb == "get":
            resource, rest = args[1], args[2:]
            name = rest[0] if rest and not rest[0].startswith("-") else None
            if resource == "pod":
                obj = self.pods.get(name)
            elif resource == "pods":
                return ok(json.dumps({"items": list(self.pods.values())}))
            elif resource == "crd":
                return ok("{}")
            elif name is None:
                items = [o for (r, _), o in self.objects.items() if r == resource]
                return ok(json.dumps({"items": items}))
            else:
                obj = self.objects.get((resource, name))
            if obj is None:
                return fail(f'Error from server (NotFound): {resource} "{name}" not found')
            return ok(json.dumps(obj))
        if verb == "scale":
            n = int(args[2].split("=")[1])
            self.objects[("statefulset", "boxd-worker")]["spec"]["replicas"] = n
            for name in [p for p in self.pods if int(p.rsplit("-", 1)[1]) >= n]:
                del self.pods[name]
            return ok()
        if verb == "delete":
            resource, name = args[1], args[2]
            if resource == "pod" and name not in self.pods:
                return ok()
            if resource == "pod":
                old = self.pods.pop(name)
                images = {c["name"]: c["image"] for c in old["spec"]["containers"]}
                self.add_worker_pod(int(name.rsplit("-", 1)[1]), images,
                                    {kube_render.SPEC_HASH: "new-template"})
            else:
                self.objects.pop((resource, name), None)
            return ok()
        if verb == "patch":
            pod = self.pods[args[2]]
            patch = json.loads(args[args.index("-p") + 1])
            for c in patch["spec"]["containers"]:
                for spec_c in pod["spec"]["containers"]:
                    if spec_c["name"] == c["name"]:
                        spec_c["image"] = c["image"]
                for st in pod["status"]["containerStatuses"]:
                    if st["name"] == c["name"]:
                        st["image"] = c["image"]
                        st["restartCount"] += 1
            return ok()
        if verb == "annotate":
            key, _, value = args[3].partition("=")
            if args[2] in self.pods:
                self.pods[args[2]]["metadata"].setdefault("annotations", {})[key] = value
            return ok()
        if verb == "exec":
            dash = args.index("--")
            pod, cmd = args[1], args[dash + 1:]
            if args[1] == "-i":
                pod = args[2]
            if cmd[0] == "sha256sum":
                body = self.files.get((pod, cmd[1]))
                return ok(f"{hashlib.sha256(body.encode()).hexdigest()}  {cmd[1]}") if body is not None \
                    else fail("No such file")
            if cmd[0] == "pkill":
                for st in self.pods[pod]["status"]["containerStatuses"]:
                    if st["name"] == "worker":
                        st["restartCount"] += 1
                return ok()
            if cmd[0] == "boxd-ctl":
                self.ctl.append((pod, cmd[1:], input))
                return ok("ctl ok\n")
        if verb == "logs":
            return ok(self.logs.get(args[1], ""))
        if verb in ("apply", "rollout"):
            return ok()
        raise AssertionError(f"unexpected kubectl {args}")

class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeKube()
        patches = [mock.patch.object(kube, "kubectl", self.fake),
                   mock.patch.object(kube.time, "sleep"),
                   mock.patch.object(kube, "log")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.cfg = config.Config(gke_doc())

class Transport(Base):
    def test_ctl_execs_in_the_control_pod(self):
        node = self.cfg.nodes_with("control")[1]
        kube.ctl(self.cfg, node, "template create --image 'computer:v 1' --vcpu 2", quiet=True)
        self.assertEqual(self.fake.ctl, [("boxd-control-1", ["template", "create", "--image",
                                                              "computer:v 1", "--vcpu", "2"], None)])
        self.assertEqual(self.fake.calls[-1][:2], ["exec", "boxd-control-1"])

    def test_stdin_goes_through(self):
        ansible.ctl(self.cfg, "license install", check=False, input="boxd-lic1_x\n")
        self.assertEqual(self.fake.calls[-1][:3], ["exec", "-i", "boxd-control-0"])
        self.assertEqual(self.fake.ctl[-1], ("boxd-control-0", ["license", "install"], "boxd-lic1_x\n"))

    def test_every_ctl_helper_uses_it(self):
        import os
        import tempfile
        key = os.path.join(tempfile.mkdtemp(), "license.key")
        self.addCleanup(shutil.rmtree, os.path.dirname(key), True)
        with open(key, "w") as fh:
            fh.write("boxd-lic1_x\n")
        leader = "Leader: node 1 (x:7000) — this node\nNodes:  1\n"
        real = self.fake.__call__

        def answer(cfg, *args, **kw):
            if args[0] == "exec" and args[-1] == "status":
                return ok(leader)
            if args[0] == "exec" and args[-1] == "sync-config":
                return ok("No new learners to add\n")
            return real(cfg, *args, **kw)
        with mock.patch.object(ansible, "_ssh") as ssh, mock.patch.object(kube, "kubectl", answer), \
                mock.patch.object(ansible, "log"):
            self.assertEqual(ansible.leader_node(self.cfg).name, "control-0")
            ansible.sync_config(self.cfg)
            ansible.install_license(self.cfg, key)
            ansible.wait_for_leader(self.cfg)
        ssh.assert_not_called()

    def test_registered_ids(self):
        out = "Leader: node 1 (x:7000) — this node\nNodes:  2\nVMs:    0\n\nPeers:\n  worker-4 — active\n  worker-9 — active\n"
        self.assertEqual(kube.registered_ids(out), {"worker-4", "worker-9"})
        self.assertEqual(kube.registered_ids("Leader: (unknown)\nNodes:  0\n"), set())

class ApplyPrune(Base):
    def test_apply_writes_a_private_list(self):
        import os
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        written = {}
        real = self.fake.__call__

        def spy(cfg, *args, **kw):
            if args[0] == "apply":
                path = args[args.index("-f") + 1]
                written["mode"] = os.stat(path).st_mode & 0o777
                with open(path) as fh:
                    written["doc"] = json.load(fh)
            return real(cfg, *args, **kw)
        with mock.patch.object(kube, "kubectl", spy):
            kube.apply(self.cfg, [{"kind": "Secret", "metadata": {"name": "boxd-config"}}], d, "secret")
        self.assertEqual(written["mode"], 0o600)
        self.assertEqual(written["doc"]["kind"], "List")
        self.assertIn("--server-side", self.fake.calls[-1])
        self.assertIn("--force-conflicts", self.fake.calls[-1])
        self.assertEqual(os.listdir(d), [])

    def test_prune_deletes_only_what_is_no_longer_rendered(self):
        self.fake.objects[("services", "boxd-proxy-0")] = {"metadata": {"name": "boxd-proxy-0"}}
        self.fake.objects[("services", "boxd-proxy-1")] = {"metadata": {"name": "boxd-proxy-1"}}
        rendered = [{"kind": "Service", "metadata": {"name": "boxd-proxy-0"}}]
        deleted = kube.prune(self.cfg, rendered)
        self.assertEqual(deleted, ["Service/boxd-proxy-1"])
        gets = [c for c in self.fake.calls if c[0] == "get"]
        self.assertTrue(all("-l" in c and "boxd.sh/install=boxd" in c[c.index("-l") + 1] for c in gets))
        self.assertFalse(any(c[1] in ("pods", "persistentvolumeclaims", "secrets", "namespaces")
                             for c in gets))

    def test_prune_never_takes_the_workers(self):
        self.fake.objects[("statefulsets.apps", "boxd-worker")] = {"metadata": {"name": "boxd-worker"}}
        with self.assertRaisesRegex(InstallError, "refusing to prune StatefulSet boxd-worker"):
            kube.prune(self.cfg, [])

    def test_prune_skips_a_missing_crd(self):
        def no_crd(cfg, *args, **kw):
            if args[:2] == ("get", "podmonitorings.monitoring.googleapis.com"):
                return fail('error: the server doesn\'t have a resource type "podmonitorings"')
            return self.fake(cfg, *args, **kw)
        with mock.patch.object(kube, "kubectl", no_crd):
            self.assertEqual(kube.prune(self.cfg, []), [])

class Removal(Base):
    IMAGES = {"worker": "r/boxd:old", "dockerd": "r/boxd-dockerd:old"}

    def setUp(self):
        super().setUp()
        doc = gke_doc()
        doc["gke"]["pools"]["worker"]["nodes"] = 3
        self.st = make_state()
        self.addCleanup(shutil.rmtree, self.st.dir, True)
        self.st.allocate(config.Config(doc))
        self.ids = dict(self.st.data["node_ids"])
        for i in range(3):
            self.fake.add_worker_pod(i, self.IMAGES)
        self.fake.set_sts(3)
        self.ctl_calls = []
        self.results = {}

        def ctl(cfg, command, check=True, quiet=False, input=None):
            self.ctl_calls.append(command)
            if command == "status":
                return ok("Peers:\n" + "".join(f"  worker-{v} — active\n" for k, v in self.ids.items()
                                               if k.startswith("worker-")))
            res = self.results.get(command.split(" worker-")[0], ok())
            if check and res.returncode:
                raise InstallError(res.stderr)
            return res
        for p in (mock.patch.object(ansible, "ctl", ctl),
                  mock.patch.object(ansible, "leader_node", return_value="leader"),
                  mock.patch.object(ansible, "ctl_on",
                                    lambda cfg, node, command, **kw: self.ctl_calls.append(command) or ok())):
            p.start()
            self.addCleanup(p.stop)
        self.cfg = config.Config(gke_doc())   # one worker now

    def test_scale_down_drains_removes_and_retires(self):
        with mock.patch.object(kube.gke, "delete_worker_instance") as delete:
            kube.remove_workers(self.cfg, self.st, 1, ["group"])
        w1, w2 = self.ids["worker-1/worker"], self.ids["worker-2/worker"]
        for wid in (w2, w1):
            for verb in ("node exclude", "node drain", "node mark-dead"):
                self.assertIn(f"{verb} worker-{wid}", self.ctl_calls)
            self.assertIn(f"remove-peer {wid}", self.ctl_calls)
        # Highest first, each fully out before the next.
        self.assertLess(self.ctl_calls.index(f"remove-peer {w2}"),
                        self.ctl_calls.index(f"node exclude worker-{w1}"))
        self.assertEqual(sorted(self.fake.pods), ["boxd-worker-0"])
        self.assertEqual(self.fake.objects[("statefulset", "boxd-worker")]["spec"]["replicas"], 1)
        pvcs = [c[2] for c in self.fake.calls if c[:2] == ["delete", "pvc"]]
        self.assertEqual(pvcs, ["state-boxd-worker-2", "data-boxd-worker-2",
                                "state-boxd-worker-1", "data-boxd-worker-1"])
        self.assertEqual([c.args[1] for c in delete.call_args_list], ["gke-n-2", "gke-n-1"])
        self.assertEqual(kube.removals(self.st), {})
        self.assertNotIn("worker-1/worker", self.st.data["node_ids"])
        self.assertEqual(sorted(self.st.data["retired_node_ids"]), sorted([w1, w2]))

    def test_a_retired_id_is_never_handed_out_again(self):
        with mock.patch.object(kube.gke, "delete_worker_instance"):
            kube.remove_workers(self.cfg, self.st, 1, [])
        doc = gke_doc()
        doc["gke"]["pools"]["worker"]["nodes"] = 3
        alloc = self.st.allocate(config.Config(doc))
        new = alloc.node_id("worker-2", "worker")
        self.assertGreater(new, max(self.ids.values()))
        self.assertNotIn(new, self.st.data["retired_node_ids"])

    def test_drain_that_leaves_machines_stops_it(self):
        self.results["node drain"] = fail("", "worker-6: 1 machine left\n")
        with self.assertRaisesRegex(InstallError, "node drain worker-.* failed, so the worker stays"):
            kube.remove_workers(self.cfg, self.st, 2, [])
        self.assertIn("boxd-worker-2", self.fake.pods)
        self.assertEqual(kube.removals(self.st)["2"]["node"], "gke-n-2")

    def test_a_release_without_drain(self):
        self.results["node drain"] = fail("error: unrecognized subcommand 'drain'")
        with self.assertRaisesRegex(InstallError, "has no `node drain`"):
            kube.remove_workers(self.cfg, self.st, 2, [])

    def test_mark_dead_refused(self):
        self.results["node mark-dead"] = fail("worker holds 1 non-destroyed VM")
        with self.assertRaisesRegex(InstallError, "mark-dead .* refused"):
            kube.remove_workers(self.cfg, self.st, 2, [])

    def test_an_interrupted_removal_finishes(self):
        kube.removals(self.st)["2"] = {"id": self.ids["worker-2/worker"], "node": "gke-n-2"}
        del self.fake.pods["boxd-worker-2"]
        self.fake.set_sts(2)
        self.ids.pop("worker-2/worker")   # no longer registered
        with mock.patch.object(kube.gke, "delete_worker_instance") as delete:
            kube.remove_workers(self.cfg, self.st, 2, [])
        delete.assert_called_once()
        self.assertFalse(any(c.startswith("node drain") for c in self.ctl_calls))
        self.assertEqual(kube.removals(self.st), {})

class InPlace(Base):
    def setUp(self):
        super().setUp()
        self.r = renderer()
        self.addCleanup(shutil.rmtree, self.r.state.dir, True)
        self.files, self.hashes = self.r.config_files(range(1))
        self.images = self.r.worker_images()
        self.spec = self.r.worker_spec_hash()
        self.fake.files[("boxd-worker-0", f"{kube_render.SRC}/worker-0.toml")] = self.files["worker-0.toml"]
        self.ctl_calls = []
        p = mock.patch.object(ansible, "ctl", lambda cfg, c, **kw: self.ctl_calls.append(c) or ok(
            f"Peers:\n  worker-{self.r.alloc.ids['worker-0/worker']} — active\n"))
        p.start()
        self.addCleanup(p.stop)

    def pod(self):
        return self.fake.pods["boxd-worker-0"]

    def update(self):
        existing = {n: p["metadata"]["uid"] for n, p in self.fake.pods.items()}
        kube.update_workers(self.cfg, self.r, self.files, self.hashes, existing, 1)

    def test_new_image_is_patched_into_the_running_pod(self):
        self.fake.add_worker_pod(0, {"worker": "r/boxd:old", "dockerd": "r/boxd-dockerd:old"},
                                 {kube_render.SPEC_HASH: self.spec})
        uid = self.pod()["metadata"]["uid"]
        self.update()
        self.assertEqual({c["name"]: c["image"] for c in self.pod()["spec"]["containers"]}, self.images)
        self.assertEqual(self.pod()["metadata"]["uid"], uid)    # the same pod: machines keep running
        self.assertEqual(self.pod()["metadata"]["annotations"][kube_render.CONFIG_HASH],
                         self.hashes["worker-0"])
        self.assertEqual(self.ctl_calls, [])

    def test_config_change_restarts_the_worker_process(self):
        self.fake.add_worker_pod(0, self.images, {kube_render.SPEC_HASH: self.spec,
                                                  kube_render.CONFIG_HASH: "old"})
        self.update()
        execs = [c for c in self.fake.calls if c[0] == "exec"]
        self.assertTrue(any(c[-3:] == ["-TERM", "-x", "boxd-worker"] for c in execs))
        self.assertEqual(self.pod()["metadata"]["annotations"][kube_render.CONFIG_HASH],
                         self.hashes["worker-0"])

    def test_up_to_date_is_left_alone(self):
        self.fake.add_worker_pod(0, self.images, {kube_render.SPEC_HASH: self.spec,
                                                  kube_render.CONFIG_HASH: self.hashes["worker-0"]})
        self.update()
        self.assertFalse(any(c[0] in ("exec", "patch", "delete", "annotate") for c in self.fake.calls))

    def test_spec_change_drains_and_recreates(self):
        self.fake.add_worker_pod(0, self.images, {kube_render.SPEC_HASH: "older-template"})
        uid = self.pod()["metadata"]["uid"]
        self.update()
        wid = f"worker-{self.r.alloc.ids['worker-0/worker']}"
        self.ctl_calls = [c for c in self.ctl_calls if c != "status"]
        self.assertEqual(self.ctl_calls[0], f"node exclude {wid}")
        self.assertIn(f"node drain {wid}", self.ctl_calls)
        self.assertEqual(self.ctl_calls[-1], f"node include {wid}")
        self.assertNotIn(f"node mark-dead {wid}", self.ctl_calls)
        self.assertNotEqual(self.pod()["metadata"]["uid"], uid)

    def test_a_pod_made_by_this_apply_is_only_annotated(self):
        self.fake.add_worker_pod(0, self.images, {kube_render.SPEC_HASH: self.spec})
        kube.update_workers(self.cfg, self.r, self.files, self.hashes, {}, 1)
        self.assertFalse(any(c[0] in ("exec", "patch", "delete") for c in self.fake.calls))
        self.assertEqual(self.pod()["metadata"]["annotations"][kube_render.CONFIG_HASH],
                         self.hashes["worker-0"])

class Probes(Base):
    def test_deployed_bucket(self):
        body = 'kind = "gcs"\nbucket = "p-c-storage"\nprefix = "engine/ab"\n'
        self.fake.objects[("secret", "boxd-config")] = {
            "data": {"bucket.toml": base64.b64encode(body.encode()).decode()}}
        self.assertEqual(kube.deployed_bucket_identities(self.cfg), {"boxd-config": {
            "kind": "gcs", "bucket": "p-c-storage", "endpoint": "", "prefix": "engine/ab"}})
        del self.fake.objects[("secret", "boxd-config")]
        self.assertEqual(kube.deployed_bucket_identities(self.cfg), {})

    def test_worker_bucket_line(self):
        self.fake.logs["boxd-worker-0"] = ("x\nstorage engine: bucket mode is off on this worker: e\n"
                                           "storage engine: the bucket is open (gcs)\ny\n")
        line = kube.worker_bucket_line(self.cfg, self.cfg.nodes_with("worker")[0])
        self.assertEqual(line, "storage engine: the bucket is open (gcs)")
        self.assertTrue(ansible.worker_bucket_line(self.cfg, self.cfg.nodes_with("worker")[0]))

    def test_wait_for_file_nudges_the_kubelet(self):
        pod, path = "boxd-control-0", "/etc/boxd-src/control-0.toml"
        self.fake.files[(pod, path)] = "old"
        seen = []
        real = self.fake.__call__

        def updating(cfg, *args, **kw):
            if args[0] == "annotate":
                self.fake.files[(pod, path)] = "new"
                seen.append(args)
            return real(cfg, *args, **kw)
        with mock.patch.object(kube, "kubectl", updating):
            kube.wait_for_file(self.cfg, pod, "control", path, "new")
        self.assertEqual(len(seen), 1)

    def test_wait_for_file_times_out(self):
        clock = iter(range(0, 10_000, 50))
        with mock.patch.object(kube.time, "time", lambda: next(clock)), \
                self.assertRaisesRegex(InstallError, "did not see the new"):
            kube.wait_for_file(self.cfg, "boxd-control-0", "control", "/x", "new")

    def test_pull_config(self):
        doc = gke_doc()
        doc["images"] = {"pull_secret": "boxd-pull"}
        cfg = config.Config(doc)
        with self.assertRaisesRegex(InstallError, "no such Secret"):
            kube.pull_config(cfg)
        self.fake.objects[("secret", "boxd-pull")] = {"type": "Opaque", "data": {}}
        with self.assertRaisesRegex(InstallError, "not a kubernetes.io/dockerconfigjson"):
            kube.pull_config(cfg)
        self.fake.objects[("secret", "boxd-pull")] = {
            "type": "kubernetes.io/dockerconfigjson",
            "data": {".dockerconfigjson": base64.b64encode(b'{"auths":{}}').decode()}}
        self.assertEqual(kube.pull_config(cfg), '{"auths":{}}')
        self.assertIsNone(kube.pull_config(self.cfg))

    def test_live_worker_replicas(self):
        st = make_state()
        self.addCleanup(shutil.rmtree, st.dir, True)
        self.assertEqual(kube.live_worker_replicas(self.cfg, st), 0)   # no outputs yet
        import json as _json
        import os
        with open(os.path.join(st.dir, "tf-outputs.json"), "w") as fh:
            _json.dump(TF_OUT, fh)
        st.data["gke"] = {"workers": 2, "removing": {"4": {"id": 9}}}
        self.fake.set_sts(3)
        with mock.patch.object(kube, "connect"):
            self.assertEqual(kube.live_worker_replicas(self.cfg, st), 5)
            st.data["gke"]["removing"] = {}
            self.assertEqual(kube.live_worker_replicas(self.cfg, st), 3)
        with mock.patch.object(kube, "connect", side_effect=InstallError("unreachable")):
            self.assertEqual(kube.live_worker_replicas(self.cfg, st), 2)

    def test_learners_that_gave_up_are_restarted(self):
        self.fake.logs["boxd-proxy-1-0"] = "registration failed after 30 attempts\n"
        self.fake.add_worker_pod(0, {"worker": "w", "dockerd": "d"})
        self.fake.logs["boxd-worker-0"] = "registration failed after 30 attempts\n"
        self.assertEqual(kube.learners_gave_up(self.cfg), ["boxd-proxy-1-0/proxy", "boxd-worker-0/worker"])
        kube.restart_learners(self.cfg)
        self.assertIn(["delete", "pod", "boxd-proxy-1-0", "--wait=false"], self.fake.calls)
        self.assertTrue(any(c[0] == "exec" and c[1] == "boxd-worker-0" and "pkill" in c
                            for c in self.fake.calls))
        self.assertFalse(any(c[:3] == ["delete", "pod", "boxd-worker-0"] for c in self.fake.calls))

    def test_verify_registration_by_node_id(self):
        cfg, st = self.cfg, make_state()
        self.addCleanup(shutil.rmtree, st.dir, True)
        alloc = st.allocate(cfg)
        wid = f"worker-{alloc.node_id('worker-0', 'worker')}"
        answers = iter(["Peers:\n  worker-99 — active\n", f"Peers:\n  {wid} — active\n"])
        with mock.patch.object(ansible, "ctl", lambda *a, **k: ok(next(answers))), \
                mock.patch.object(kube, "learners_gave_up", return_value=[]):
            kube.verify_registration(cfg, alloc)

class Ctl(unittest.TestCase):
    def test_ctl_command(self):
        import io
        cfg = config.Config(gke_doc())
        args = mock.Mock(cluster="cluster.toml", state_dir="/tmp/x", ctl_args=["--", "vm", "list", "a b"])
        with mock.patch.object(main.config, "load", return_value=cfg), \
                mock.patch.object(main.remote_state, "synced"), \
                mock.patch.object(main, "State"), \
                mock.patch.object(main.tf, "cached_outputs", return_value=TF_OUT), \
                mock.patch.object(main.ansible, "set_keys_dir"), \
                mock.patch.object(main.ansible, "ctl", return_value=ok("no vms\n")) as ctl, \
                mock.patch.object(main.sys, "stdin", io.StringIO("")), \
                mock.patch.object(main.sys, "stdout", io.StringIO()) as out:
            self.assertEqual(main.cmd_ctl(args), 0)
        self.assertEqual(shlex.split(ctl.call_args.args[1]), ["vm", "list", "a b"])
        self.assertEqual(out.getvalue(), "no vms\n")
        with self.assertRaisesRegex(InstallError, "ctl needs boxd-ctl's arguments"), \
                mock.patch.object(main.config, "load", return_value=cfg):
            main.cmd_ctl(mock.Mock(cluster="c", ctl_args=["--"]))

if __name__ == "__main__":
    unittest.main()

class ClickhouseSchema(unittest.TestCase):
    """Each migration once: run again in order, the later ones break the earlier ones."""

    def test_applies_only_the_new_ones_and_records_them(self):
        from boxd_installer.kube_render import CLICKHOUSE_MIGRATIONS
        files = sorted(f for f in os.listdir(CLICKHOUSE_MIGRATIONS) if f.endswith(".sql"))
        scripts, inputs = [], []

        def fake(cfg, *args, check=True, quiet=True, input=None):
            script = args[-1]
            scripts.append(script)
            if input is not None:
                inputs.append(input)
            out = "\n".join(files[:3]) + "\n" if "SELECT name FROM default.boxd_migrations" in script else ""
            return mock.Mock(returncode=0, stdout=out, stderr="")
        with mock.patch.object(kube, "kubectl", side_effect=fake), mock.patch.object(kube, "log"):
            kube.clickhouse_schema(object())
        recorded = [s for s in scripts if "INSERT INTO default.boxd_migrations" in s]
        self.assertEqual(len(inputs), len(files) - 3)
        self.assertEqual(len(recorded), len(files) - 3)
        self.assertIn(f"VALUES ('{files[3]}')", recorded[0])
        self.assertTrue(all("$BOXD_CLICKHOUSE_PASSWORD" in s for s in scripts))

    def test_a_failed_migration_stops_the_apply_unrecorded(self):
        scripts = []

        def fake(cfg, *args, check=True, quiet=True, input=None):
            scripts.append(args[-1])
            return mock.Mock(returncode=1 if input is not None else 0, stdout="", stderr="boom")
        with mock.patch.object(kube, "kubectl", side_effect=fake), mock.patch.object(kube, "log"):
            with self.assertRaisesRegex(InstallError, "ClickHouse migration 001_events.sql failed: boom"):
                kube.clickhouse_schema(object())
        self.assertFalse([s for s in scripts if "INSERT INTO" in s])

class WaitForFile(unittest.TestCase):
    def test_a_container_that_isnt_running_is_not_waited_for(self):
        """A crash-looping worker (an image whose entrypoint fails) can't be exec'd into:
        the update that follows the wait (a new image) is what fixes it."""
        pod = {"status": {"containerStatuses": [
            {"name": "worker", "state": {"waiting": {"reason": "CrashLoopBackOff"}}},
            {"name": "dockerd", "state": {"running": {}}}]}}
        with mock.patch.object(kube, "_file_sha", return_value=None), \
                mock.patch.object(kube, "get", return_value=pod), \
                mock.patch.object(kube, "kubectl") as k, mock.patch.object(kube, "_wait") as w, \
                mock.patch.object(kube, "log"):
            kube.wait_for_file(object(), "boxd-worker-0", "worker", "/etc/boxd-src/worker-0.toml", "x")
        k.assert_not_called()
        w.assert_not_called()

    def test_a_running_container_is_waited_for(self):
        pod = {"status": {"containerStatuses": [{"name": "worker", "state": {"running": {}}}]}}
        with mock.patch.object(kube, "_file_sha", return_value=None), \
                mock.patch.object(kube, "get", return_value=pod), \
                mock.patch.object(kube, "kubectl"), mock.patch.object(kube, "_wait") as w, \
                mock.patch.object(kube, "namespace", return_value="boxd"), \
                mock.patch.object(kube, "log"):
            kube.wait_for_file(object(), "boxd-worker-0", "worker", "/etc/boxd-src/worker-0.toml", "x")
        w.assert_called_once()
