"""The GKE target's render (kube_render.py): every role's TOML in the
boxd-config Secret, and the Kubernetes objects. Run:
python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import base64
import os
import shutil
import tempfile
import tomllib
import unittest

from boxd_installer import config, kube_render
from boxd_installer.clouds import gke
from boxd_installer.state import State
from boxd_installer.util import InstallError

from test_gke_config import existing_doc, gke_doc

TF_OUT = {"gke": {"name": "mycluster", "location": "europe-west4-a",
                  "node_cidr": "10.60.0.0/20", "pods_cidr": "172.20.0.0/16",
                  "services_cidr": "172.24.0.0/20", "vpc_mtu": 8896},
          "edges": [{"ip": "34.1.1.1"}, {"ip": "34.1.1.2"}],
          "backups": None, "worker_instance_groups": []}
MANAGED = "app.kubernetes.io/managed-by"

def make_state() -> State:
    d = tempfile.mkdtemp(prefix="gke-render-")
    st = State(d)
    with open(os.path.join(st.keys_dir, "proxy_host_ed25519"), "w") as fh:
        fh.write("-----BEGIN OPENSSH PRIVATE KEY-----\nx\n-----END OPENSSH PRIVATE KEY-----\n")
    st.acme_eab = lambda *a, **k: {"kid": "kid", "hmac": "hmac"}
    return st

def renderer(doc=None, tf_out=None, state=None, pull=None):
    cfg = config.Config(doc or gke_doc())
    st = state or make_state()
    alloc = st.allocate(cfg)
    return kube_render.KubeRenderer(cfg, alloc, tf_out or TF_OUT, st, pull_config=pull)

def toml(files, name):
    return tomllib.loads(files[name])

def by_kind(objs, kind, name=None):
    out = [o for o in objs if o["kind"] == kind and (name is None or o["metadata"]["name"] == name)]
    return out[0] if name else out

class Configs(unittest.TestCase):
    def setUp(self):
        self.r = renderer()
        self.addCleanup(shutil.rmtree, self.r.state.dir, True)
        self.files, self.hashes = self.r.config_files(range(1))

    def test_secret_keys(self):
        self.assertEqual(sorted(self.files), [
            "clickhouse-password", "console.toml", "control-0.toml", "control-1.toml",
            "control-2.toml", "control.toml", "dns-0.toml", "dns-1.toml", "guest-fw-proxies",
            "proxy-0.toml", "proxy-1.toml", "proxy_host_ed25519", "worker-0.toml"])
        self.assertEqual(self.files["control.toml"], self.files["control-0.toml"])
        # boxd-control refuses a [control] without socket_path; the example has none.
        for i in range(3):
            self.assertIn('socket_path = "/run/boxd/control.sock"', self.files[f"control-{i}.toml"])
        # The guest firewall's --proxy addresses: each edge's proxy in the pool, .254 downwards.
        self.assertEqual(self.files["guest-fw-proxies"], "10.120.0.254 10.120.0.253\n")
        secret = self.r.secret(self.files)
        self.assertEqual((secret["kind"], secret["metadata"]["name"]), ("Secret", "boxd-config"))
        self.assertEqual(base64.b64decode(secret["data"]["worker-0.toml"]).decode(),
                         self.files["worker-0.toml"])

    def test_raft_members_by_dns_name(self):
        c1 = toml(self.files, "control-1.toml")
        self.assertEqual(c1["node_id"], 2)
        self.assertEqual([p["addr"] for p in c1["cluster"]["peers"]], [
            f"boxd-control-{i}.boxd-control.boxd.svc.cluster.local:7000" for i in range(3)])
        learners = {l["addr"] for l in c1["cluster"]["learners"]}
        self.assertIn("boxd-worker-0.boxd-worker.boxd.svc.cluster.local:7000", learners)
        self.assertIn("boxd-proxy-1-0.boxd-proxy-1.boxd.svc.cluster.local:7001", learners)
        self.assertIn("boxd-dns-0-0.boxd-dns-0.boxd.svc.cluster.local:7002", learners)
        self.assertEqual(c1["vrack"], {"interface": "ens4", "subnet": "10.60.0.0/20"})
        self.assertEqual(c1["names"]["reserved"][-2:], ["p0", "p1"])
        self.assertTrue(c1["self_hosted"])

    def test_proxy(self):
        p1 = toml(self.files, "proxy-1.toml")
        self.assertEqual(p1["proxy"]["public_ips"], ["34.1.1.2"])
        self.assertEqual(p1["proxy"]["public_host"], "p1.boxd.example.com")
        self.assertEqual(p1["proxy"]["ssh_port"], 2222)
        self.assertEqual(p1["network"]["routed"], {"host_ip": "10.120.0.253", "underlay_ip": "@NODE_IP@"})
        self.assertEqual(p1["proxy"]["internal_services"][0],
                         {"hostname": "app.boxd.example.com", "target_ip": "172.24.0.80",
                          "target_port": 8080})

    def test_dns(self):
        d0 = toml(self.files, "dns-0.toml")["dns"]
        self.assertEqual(d0["listen"], "34.1.1.1:53")
        self.assertEqual(d0["nameservers"], ["ns1.boxd.example.com", "ns2.boxd.example.com"])
        records = {(r["name"], r["value"]) for r in d0["static_records"]}
        self.assertTrue({("ns1", "34.1.1.1"), ("ns2", "34.1.1.2"), ("p1", "34.1.1.2"),
                         ("app", "34.1.1.1"), ("app", "34.1.1.2")} <= records)
        self.assertEqual(toml(self.files, "dns-1.toml")["dns"]["listen"], "34.1.1.2:53")

    def test_worker(self):
        w = toml(self.files, "worker-0.toml")
        self.assertEqual(w["worker"]["cgroup_root"], "@CGROUP_ROOT@")
        self.assertEqual(w["network"]["routed"]["underlay_ip"], "@NODE_IP@")
        self.assertEqual(w["network"]["guest_mtu"], 0)
        self.assertEqual(w["storage"], {"raft_dir": "/mnt/boxd-state/worker/raft",
                                        "state_db": "/mnt/boxd-state/worker/state.db"})
        self.assertEqual(w["paths"]["data_dir"], "/mnt/boxd-data")
        self.assertEqual(w["network"]["routed"]["host_ip"], f"10.120.0.{10 + w['node_id']}")

    def test_console(self):
        c = toml(self.files, "console.toml")
        self.assertNotIn("learners", c["cluster"])
        self.assertEqual(c["storage"]["state_db"], "/var/lib/boxd/console-replica/state.db")

    def test_membership_changes_restart_nothing(self):
        """A worker added changes control's learners, not any role's hash."""
        doc = gke_doc()
        doc["gke"]["pools"]["worker"]["nodes"] = 2
        r2 = renderer(doc, state=self.r.state)
        files2, hashes2 = r2.config_files(range(2))
        self.assertNotEqual(files2["control-0.toml"], self.files["control-0.toml"])
        for k in ("control", "console", "proxy-0", "dns-1", "worker-0"):
            self.assertEqual(hashes2[k], self.hashes[k], k)
        self.assertIn("worker-1", hashes2)

    def test_a_worker_being_removed_keeps_its_config(self):
        doc = gke_doc()
        doc["gke"]["pools"]["worker"]["nodes"] = 2
        st = self.r.state
        st.allocate(config.Config(doc))
        files, _ = renderer(gke_doc(), state=st).config_files(range(2))
        self.assertIn("worker-1.toml", files)
        control = toml(files, "control-0.toml")
        self.assertNotIn(st.data["node_ids"]["worker-1/worker"],
                         [l["id"] for l in control["cluster"]["learners"]])

    def test_guest_mtu_from_the_vpc(self):
        out = {**TF_OUT, "gke": {**TF_OUT["gke"], "vpc_mtu": 1460}}
        files, _ = renderer(existing_doc(), tf_out=out).config_files(range(1))
        self.assertEqual(toml(files, "worker-0.toml")["network"]["guest_mtu"], 1410)
        doc = existing_doc()
        doc["network"]["guest_mtu"] = 1300
        files, _ = renderer(doc, tf_out=out).config_files(range(1))
        self.assertEqual(toml(files, "worker-0.toml")["network"]["guest_mtu"], 1300)

    def test_files_read_by_path_come_from_the_mount(self):
        st = make_state()
        for name in ("local-ca.pem", "local-ca.key", "github-app.pem"):
            with open(os.path.join(st.keys_dir, name), "w") as fh:
                fh.write(f"-----BEGIN {name}-----\n")
        doc = gke_doc(ingress="internal")
        doc["acme"] = {"ca": "local"}
        doc["github"] = {"client_id": "id", "client_secret": "s", "app_id": 1}
        files, hashes = renderer(doc, state=st).config_files(range(1))
        acme = toml(files, "control-0.toml")["acme"]
        self.assertEqual(acme["local_ca"], {"cert_path": "/etc/boxd-src/local-ca.pem",
                                            "key_path": "/etc/boxd-src/local-ca.key"})
        self.assertEqual(toml(files, "console.toml")["github"]["app_private_key_path"],
                         "/etc/boxd-src/github-app.pem")
        for name in ("local-ca.pem", "local-ca.key", "github-app.pem"):
            self.assertIn(name, files)

    def test_pull_secret_and_bucket(self):
        st = make_state()
        doc = gke_doc()
        doc["storage_engine"] = {"default_vm_storage": "file"}
        out = {**TF_OUT, "storage_bucket": {"kind": "gcs", "bucket": "p-c-storage"}}
        files, _ = renderer(doc, tf_out=out, state=st, pull='{"auths": {}}').config_files(range(1))
        self.assertEqual(files["docker-config.json"], '{"auths": {}}')
        self.assertEqual(toml(files, "bucket.toml")["bucket"], "p-c-storage")
        self.assertEqual(toml(files, "worker-0.toml")["storage_engine"]["bucket"]["config"],
                         "/etc/boxd/bucket.toml")

    def test_needs_the_edges(self):
        with self.assertRaisesRegex(InstallError, "no edge IP 1"):
            renderer(tf_out={**TF_OUT, "edges": [{"ip": "34.1.1.1"}]}).config_files(range(1))

class Objects(unittest.TestCase):
    def setUp(self):
        self.r = renderer()
        self.addCleanup(shutil.rmtree, self.r.state.dir, True)
        self.files, self.hashes = self.r.config_files(range(1))
        self.objs = (self.r.base_objects({"downloads/install.sh": "#!/bin/sh"})
                     + self.r.control_objects(self.hashes["control"])
                     + self.r.console_objects(self.hashes["console"],
                                              downloads={"downloads/install.sh": "#!/bin/sh"})
                     + self.r.edge_objects(0, self.hashes) + self.r.edge_objects(1, self.hashes)
                     + self.r.worker_objects(1) + self.r.pod_monitorings())

    def test_every_object_is_labelled_but_no_pod_or_claim(self):
        for o in self.objs:
            self.assertEqual(o["metadata"]["labels"][MANAGED], "boxd-install", o["metadata"]["name"])
            if "namespace" in o["metadata"]:
                self.assertEqual(o["metadata"]["namespace"], "boxd")
            spec = o.get("spec", {})
            tpl = spec.get("template", {}).get("metadata", {})
            self.assertNotIn(MANAGED, tpl.get("labels", {}), o["metadata"]["name"])
            for claim in spec.get("volumeClaimTemplates", []):
                self.assertNotIn("labels", claim["metadata"])

    def test_namespace_is_privileged(self):
        ns = by_kind(self.objs, "Namespace", "boxd")
        self.assertEqual(ns["metadata"]["labels"]["pod-security.kubernetes.io/enforce"], "privileged")

    def test_control(self):
        sts = by_kind(self.objs, "StatefulSet", "boxd-control")
        self.assertEqual(sts["spec"]["replicas"], 3)
        pod = sts["spec"]["template"]["spec"]
        c = pod["containers"][0]
        self.assertIn('control-${POD_NAME##*-}.toml', c["args"][0])
        self.assertEqual(c["image"], f"{gke.DEFAULT_REGISTRY}/boxd:{self.r.version}")
        self.assertEqual(pod["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
                         [0]["topologyKey"], "kubernetes.io/hostname")
        self.assertNotIn("topologySpreadConstraints", pod)
        self.assertEqual(sts["spec"]["template"]["metadata"]["annotations"][kube_render.CONFIG_HASH],
                         self.hashes["control"])
        self.assertEqual(by_kind(self.objs, "PodDisruptionBudget", "boxd-control")["spec"]["maxUnavailable"], 1)

    def test_control_spreads_over_zones(self):
        doc = gke_doc(zones=["europe-west4-a", "europe-west4-b", "europe-west4-c"])
        r = renderer(doc)
        pod = r.control_objects("h")[0]["spec"]["template"]["spec"]
        self.assertEqual(pod["topologySpreadConstraints"][0]["topologyKey"], "topology.kubernetes.io/zone")

    def test_console(self):
        sts = by_kind(self.objs, "StatefulSet", "boxd-console")
        env = {e["name"]: e.get("value") for e in sts["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(env["BOXD_CONSOLE_CONTROL"], ",".join(
            f"http://boxd-control-{i}.boxd-control.boxd.svc.cluster.local:7100" for i in range(3)))
        self.assertEqual(env["BOXD_CONSOLE_API"], "http://boxd-proxy-0-0.boxd-proxy-0.boxd.svc.cluster.local:9443")
        svc = by_kind(self.objs, "Service", "boxd-console")
        self.assertEqual(svc["spec"]["clusterIP"], "172.24.0.80")
        self.assertEqual(by_kind(self.objs, "ConfigMap", "boxd-downloads")["data"], {"install.sh": "#!/bin/sh"})

    def test_edges(self):
        for i, ip in enumerate(("34.1.1.1", "34.1.1.2")):
            for role in ("proxy", "dns"):
                sts = by_kind(self.objs, "StatefulSet", f"boxd-{role}-{i}")
                pod = sts["spec"]["template"]["spec"]
                self.assertTrue(pod["hostNetwork"])
                self.assertEqual(pod["nodeSelector"], {"boxd.sh/pool": "edge", "boxd.sh/edge": str(i)})
                env = {e["name"]: e.get("value") for e in pod["containers"][0]["env"]}
                self.assertEqual(env["EDGE_IP"], ip)
                self.assertEqual(env["BOXD_METRICS_LISTEN"],
                                 f"127.0.0.1:{kube_render.METRICS[role]}")
                self.assertEqual(sts["spec"]["serviceName"], f"boxd-{role}-{i}")
            self.assertEqual(by_kind(self.objs, "Service", f"boxd-proxy-{i}")["spec"]["clusterIP"], "None")

    def test_worker(self):
        sts = by_kind(self.objs, "StatefulSet", "boxd-worker")
        self.assertEqual(sts["spec"]["updateStrategy"], {"type": "OnDelete"})
        pod = sts["spec"]["template"]["spec"]
        self.assertTrue(pod["hostNetwork"] and pod["hostPID"])
        self.assertEqual(pod["priorityClassName"], "boxd-boxd-worker")
        self.assertEqual(pod["nodeSelector"], {"boxd.sh/pool": "worker", "boxd.sh/disk": "local-ssd",
                                               "boxd.sh/node-ready": "true"})
        worker, dockerd = pod["containers"]
        self.assertTrue(worker["securityContext"]["privileged"])
        self.assertNotIn("limits", worker["resources"])
        env = {e["name"]: e.get("value") for e in worker["env"]}
        self.assertEqual(env["LOCAL_SSD"], "1")
        self.assertEqual(env["DOCKER_HOST"], "unix:///run/boxd-docker/docker.sock")
        data = next(m for m in worker["volumeMounts"] if m["name"] == "data")
        self.assertEqual(data["mountPropagation"], "HostToContainer")
        self.assertIn("--bridge=none", dockerd["args"])
        self.assertIn("--data-root=/mnt/boxd-data/docker", dockerd["args"])
        self.assertIn("--log-level=warn", dockerd["args"])
        # dockerd itself: the dind entrypoint's cgroup setup reaches the node's cgroups.
        self.assertEqual(dockerd["command"], ["dockerd"])
        self.assertTrue(all(a.startswith("--") for a in dockerd["args"]), dockerd["args"])
        vol = next(v for v in pod["volumes"] if v["name"] == "data")
        self.assertEqual(vol["hostPath"]["path"], gke.DATA_MOUNT)
        self.assertEqual([c["metadata"]["name"] for c in sts["spec"]["volumeClaimTemplates"]], ["state"])
        self.assertEqual(by_kind(self.objs, "PodDisruptionBudget", "boxd-worker")["spec"]["maxUnavailable"], 0)
        # No DNS rate limit on self-hosted workers (it throttled the worker's own lookups).
        self.assertNotIn("boxd-worker-hooks", [o["metadata"]["name"] for o in self.objs])
        self.assertNotIn("hooks", [v["name"] for v in pod["volumes"]])

    def test_worker_on_a_persistent_disk(self):
        doc = gke_doc()
        doc["gke"]["pools"]["worker"].update(local_ssd=0, data_disk_gb=500)
        r = renderer(doc)
        sts = r.worker_objects(2)[0]
        claims = {c["metadata"]["name"]: c for c in sts["spec"]["volumeClaimTemplates"]}
        self.assertEqual(claims["data"]["spec"]["resources"]["requests"]["storage"], "500Gi")
        self.assertEqual(claims["data"]["spec"]["storageClassName"], "boxd-boxd-xfs")
        pod = sts["spec"]["template"]["spec"]
        self.assertEqual(pod["nodeSelector"]["boxd.sh/disk"], "pd")
        self.assertFalse(any(v["name"] == "data" for v in pod["volumes"]))
        self.assertTrue(by_kind(r.base_objects(), "StorageClass"))

    def test_spec_hash_ignores_images(self):
        h = self.r.worker_spec_hash()
        doc = gke_doc()
        doc["cluster"]["assets_version"] = "f" * 40
        other = renderer(doc)
        self.assertNotEqual(other.worker_images(), self.r.worker_images())
        self.assertEqual(other.worker_spec_hash(), h)
        doc = gke_doc()
        doc["images"] = {"pull_secret": "boxd-pull"}
        pulled = renderer(doc)
        self.assertNotEqual(pulled.worker_spec_hash(), h)
        pod = pulled.worker_objects(1)[0]["spec"]["template"]["spec"]
        self.assertEqual(pod["imagePullSecrets"], [{"name": "boxd-pull"}])

    def test_node_prep(self):
        ds = by_kind(self.objs, "DaemonSet", "boxd-node-prep")
        self.assertEqual(ds["spec"]["updateStrategy"], {"type": "OnDelete"})
        c = ds["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(c["image"], f"{gke.DEFAULT_REGISTRY}/boxd-node-prep:{self.r.version}")
        env = {e["name"]: e.get("value") for e in c["env"]}
        self.assertEqual(env["BOXD_LOCAL_SSD"], "1")
        role = by_kind(self.objs, "ClusterRole", "boxd-boxd-node-prep")
        self.assertEqual(role["rules"], [{"apiGroups": [""], "resources": ["nodes"],
                                          "verbs": ["get", "patch"]}])

    def test_node_prep_installs_the_bundles_kernel(self):
        import json
        root = tempfile.mkdtemp(prefix="gke-assets-")
        self.addCleanup(shutil.rmtree, root, True)
        with open(os.path.join(root, "MANIFEST.json"), "w") as fh:
            json.dump({"git_sha": "a" * 40, "files": {}, "host_kernel": {"release": "6.17.0-boxd+"}}, fh)
        cfg = config.Config(gke_doc())
        st = make_state()
        r = kube_render.KubeRenderer(cfg, st.allocate(cfg), TF_OUT, st, assets_root=root)
        ds = by_kind(r.base_objects(), "DaemonSet", "boxd-node-prep")
        env = {e["name"]: e.get("value") for e in ds["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(env["BOXD_HOST_KERNEL"], "6.17.0-boxd+")

    def test_node_prep_stock_kernel(self):
        r = renderer(gke_doc(host_kernel="stock"))
        ds = by_kind(r.base_objects(), "DaemonSet", "boxd-node-prep")
        env = {e["name"]: e.get("value") for e in ds["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(env["BOXD_HOST_KERNEL"], "")

    def test_monitoring(self):
        mons = [o for o in self.objs if o["kind"] == "PodMonitoring"]
        self.assertEqual({o["metadata"]["name"]: o["spec"]["endpoints"][0]["port"] for o in mons},
                         {"boxd-control": 9101, "boxd-proxy": 9102, "boxd-dns": 9103, "boxd-worker": 9104})
        self.assertEqual(renderer(gke_doc(observability=False)).pod_monitorings(), [])

if __name__ == "__main__":
    unittest.main()

class Analytics(unittest.TestCase):
    """[gke] analytics: ClickHouse in the cluster for the usage graphs, as the metrics node."""

    def r(self, **gke_keys):
        doc = gke_doc()
        doc["gke"].update(gke_keys)
        r = renderer(doc)
        self.addCleanup(shutil.rmtree, r.state.dir, True)
        return r

    def test_default_runs_clickhouse_and_points_every_role_at_it(self):
        r = self.r()
        files, hashes = r.config_files(range(1))
        endpoint = "http://boxd-clickhouse.boxd.svc.cluster.local:8123"
        for name in ("control-0.toml", "console.toml", "proxy-0.toml", "worker-0.toml"):
            a = toml(files, name)["analytics"]
            self.assertEqual((a["endpoint"], a["user"]), (endpoint, "default"), name)
            self.assertEqual(a["password"], files["clickhouse-password"], name)
        self.assertNotIn("analytics", toml(files, "dns-0.toml"))
        objs = r.clickhouse_objects(hashes["clickhouse"])
        sts = by_kind(objs, "StatefulSet", "boxd-clickhouse")
        claim = sts["spec"]["volumeClaimTemplates"][0]["spec"]
        self.assertEqual((claim["storageClassName"], claim["resources"]["requests"]["storage"]),
                         ("premium-rwo", "100Gi"))
        pod = sts["spec"]["template"]["spec"]
        self.assertEqual(pod["nodeSelector"]["boxd.sh/pool"], "system")
        env = {e["name"]: e for e in pod["containers"][0]["env"]}
        self.assertEqual(env["BOXD_CLICKHOUSE_PASSWORD"]["valueFrom"]["secretKeyRef"],
                         {"name": "boxd-config", "key": "clickhouse-password"})
        self.assertTrue(pod["containers"][0]["image"].endswith("/boxd-clickhouse:" + r.version))
        self.assertIn('from_env="BOXD_CLICKHOUSE_PASSWORD"',
                      by_kind(objs, "ConfigMap", "boxd-clickhouse")["data"]["boxd-users.xml"])
        self.assertEqual([p["port"] for p in by_kind(objs, "Service", "boxd-clickhouse")["spec"]["ports"]],
                         [8123, 9000])

    def test_disk_size(self):
        r = self.r(analytics_disk_gb=250)
        files, hashes = r.config_files(range(1))
        sts = by_kind(r.clickhouse_objects(hashes["clickhouse"]), "StatefulSet", "boxd-clickhouse")
        self.assertEqual(sts["spec"]["volumeClaimTemplates"][0]["spec"]["resources"]["requests"]
                         ["storage"], "250Gi")

    def test_off(self):
        r = self.r(analytics=False)
        files, hashes = r.config_files(range(1))
        self.assertNotIn("clickhouse-password", files)
        self.assertNotIn("analytics", toml(files, "worker-0.toml"))
        self.assertEqual(r.clickhouse_objects(""), [])

    def test_an_explicit_analytics_table_wins(self):
        doc = gke_doc()
        doc["analytics"] = {"endpoint": "https://ch.example.com:8443", "user": "boxd",
                            "password": "x"}
        r = renderer(doc)
        self.addCleanup(shutil.rmtree, r.state.dir, True)
        files, hashes = r.config_files(range(1))
        self.assertEqual(toml(files, "worker-0.toml")["analytics"]["endpoint"],
                         "https://ch.example.com:8443")
        self.assertNotIn("clickhouse-password", files)
        self.assertEqual(r.clickhouse_objects(""), [])

class Downloads(unittest.TestCase):
    """The CLI downloads come from the image; the zone's files (the installer script,
    version and manifests) are mounted into it, on the console (app.<zone>/downloads)
    and on every proxy (<zone>/downloads, the URL the console hands out)."""
    DOWNLOADS = {"downloads/install.sh": "#!/bin/sh\n",
                 "downloads/cli/cli-version.txt": "0.3.4\n",
                 "downloads/cli/latest-linux-amd64.json": "{}\n"}
    WANT = {"/usr/local/share/boxd/downloads/install.sh": "install.sh",
            "/usr/local/share/boxd/downloads/cli/cli-version.txt": "cli-version.txt",
            "/usr/local/share/boxd/downloads/cli/latest-linux-amd64.json": "latest-linux-amd64.json"}

    def setUp(self):
        self.r = renderer()
        self.addCleanup(shutil.rmtree, self.r.state.dir, True)
        self.files, self.hashes = self.r.config_files(range(1))

    def mounted(self, pod) -> dict[str, str]:
        return {m["mountPath"]: m["subPath"] for m in pod["containers"][0]["volumeMounts"]
                if m["name"] == "downloads"}

    def test_the_proxies_mount_the_zone_files_into_the_image(self):
        for i in (0, 1):
            objs = self.r.edge_objects(i, self.hashes, downloads=self.DOWNLOADS)
            pod = by_kind(objs, "StatefulSet", f"boxd-proxy-{i}")["spec"]["template"]["spec"]
            self.assertEqual(self.mounted(pod), self.WANT)
            self.assertIn({"name": "downloads", "configMap": {"name": "boxd-downloads"}},
                          pod["volumes"])
            dns = by_kind(objs, "StatefulSet", f"boxd-dns-{i}")["spec"]["template"]["spec"]
            self.assertNotIn("downloads", [v["name"] for v in dns["volumes"]])
        bare = by_kind(self.r.edge_objects(0, self.hashes), "StatefulSet", "boxd-proxy-0")
        self.assertNotIn("downloads",
                         [v["name"] for v in bare["spec"]["template"]["spec"]["volumes"]])

    def test_the_console_serves_the_image_directory(self):
        sts = by_kind(self.r.console_objects(self.hashes["console"], downloads=self.DOWNLOADS),
                      "StatefulSet", "boxd-console")
        pod = sts["spec"]["template"]["spec"]
        self.assertEqual(self.mounted(pod), self.WANT)
        env = {e["name"]: e.get("value") for e in pod["containers"][0]["env"]}
        self.assertEqual(env["BOXD_CONSOLE_DOWNLOADS_DIR"], "/usr/local/share/boxd/downloads")
