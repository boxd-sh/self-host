"""Render boxd on Kubernetes ([gke]): every role's TOML, packed into the
boxd-config Secret, and every Kubernetes object, from cluster.toml, the
node-id allocation and the terraform outputs. kube.py applies them.

The TOMLs come from render.Renderer with what differs on Kubernetes
overridden: Raft members are addressed by their StatefulSet DNS names (the
synthetic nodes' internal_ip, clouds/gke.py), the routed network's underlay
address and the worker's cgroup root are placeholders the boxd image's
entrypoint fills on the node (@NODE_IP@, @CGROUP_ROOT@), workers keep Raft
state on their own disk and machines' data on node-prep's local SSD RAID,
and files the roles read by path (local CA, GitHub App key) are read from
the Secret's mount.

The contract with the boxd image (built by boxd's release, tagged with the
release id): `boxd-entrypoint <role>`; the Secret mounted at /etc/boxd-src
with control-<i>.toml, console.toml, proxy-<i>.toml, dns-<i>.toml,
worker-<i>.toml, bucket.toml, proxy_host_ed25519, docker-config.json (with
[images] pull_secret); POD_NAME picks a pod's own file. Control does not go
through the entrypoint: each voter has its own node_id, so its pod runs
boxd-control on control-<ordinal>.toml straight from the mount, which is
also what lets `boxd-ctl sync-config` read a Secret change without a restart.

Every object carries app.kubernetes.io/managed-by=boxd-install and
boxd.sh/install=<namespace>: kube.py prunes what is labelled and no longer
rendered. Pod templates and volume claim templates never carry those labels
(a pruned pod or claim would take a running worker, or its state, with it)."""
import base64
import copy
import hashlib
import json
import os

from . import config, render, toml_emit
from .clouds import gke
from .util import InstallError

MANAGED_BY = "boxd-install"
SECRET = "boxd-config"
SRC = "/etc/boxd-src"
# The CLI downloads live in the boxd image (the release's binaries and skills under
# cli/); the installer adds only the files it renders for the zone (install.sh,
# cli-version.txt, the latest-<platform>.json manifests), mounted one by one from the
# boxd-downloads ConfigMap. The console serves the directory at app.<zone>/downloads
# (BOXD_CONSOLE_DOWNLOADS_DIR), the proxy at <zone>/downloads (its entrypoint links
# the proxy's fixed /var/lib/boxd/downloads here). Nothing is copied, nothing is on a disk.
DOWNLOADS_DIR = "/usr/local/share/boxd/downloads"
WORKER_STATE = "/mnt/boxd-state"
DATA_DIR = "/mnt/boxd-data"
CONFIG_HASH = "boxd.sh/config-hash"
SPEC_HASH = "boxd.sh/spec-hash"
POOL_LABEL = "boxd.sh/pool"
EDGE_LABEL = "boxd.sh/edge"
READY_LABEL = "boxd.sh/node-ready"
DISK_LABEL = "boxd.sh/disk"
ROLE_LABEL = "boxd.sh/role"
# BOXD_METRICS_LISTEN per role, as the systemd units have them (proxy and dns
# share an edge node, so each role keeps its own port).
METRICS = {"control": 9101, "proxy": 9102, "dns": 9103, "worker": 9104, "console": 9105}
RAFT = {"control": 7000, "worker": 7000, "proxy": 7001, "dns": 7002}
STATE_CLASS = "standard-rwo"   # GKE's default pd-balanced class
# [gke] analytics: ClickHouse in the cluster for the console's usage graphs, as
# the metrics node on the other targets (roles/boxd-clickhouse): its data on a
# pd-ssd disk (GKE's premium-rwo), its schema the role's migrations.
CLICKHOUSE = "boxd-clickhouse"
CLICKHOUSE_CLASS = "premium-rwo"
CLICKHOUSE_MIGRATIONS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                     "ansible", "roles", "boxd-clickhouse", "files", "migrations")
# The server config (roles/boxd-clickhouse's clickhouse-config.xml.j2 for a pod):
# warnings to the console only (GKE files stderr as ERROR), every system log
# with a TTL, the profiler's logs off. IPv4 only (a pod has no IPv6: the image's
# "::" warned at every start), HTTP and native only; the Service keeps it inside
# the cluster.
CLICKHOUSE_CONFIG = """<clickhouse>
    <listen_host replace="replace">0.0.0.0</listen_host>
    <mysql_port remove="true"/>
    <postgresql_port remove="true"/>
    <logger replace="replace">
        <level>warning</level>
        <console>1</console>
    </logger>
    <max_server_memory_usage_to_ram_ratio>0.8</max_server_memory_usage_to_ram_ratio>
    <interserver_http_port remove="true"/>
    <interserver_https_port remove="true"/>
    <query_log><ttl>event_date + INTERVAL 7 DAY DELETE</ttl></query_log>
    <trace_log remove="true"/>
    <part_log><ttl>event_date + INTERVAL 7 DAY DELETE</ttl></part_log>
    <processors_profile_log remove="true"/>
    <metric_log><ttl>event_date + INTERVAL 7 DAY DELETE</ttl></metric_log>
    <asynchronous_metric_log><ttl>event_date + INTERVAL 7 DAY DELETE</ttl></asynchronous_metric_log>
    <asynchronous_insert_log><ttl>event_date + INTERVAL 7 DAY DELETE</ttl></asynchronous_insert_log>
    <query_metric_log><ttl>event_date + INTERVAL 7 DAY DELETE</ttl></query_metric_log>
    <background_schedule_pool_log><ttl>event_date + INTERVAL 7 DAY DELETE</ttl></background_schedule_pool_log>
    <text_log><ttl>event_date + INTERVAL 7 DAY DELETE</ttl></text_log>
    <error_log><ttl>event_date + INTERVAL 7 DAY DELETE</ttl></error_log>
</clickhouse>
"""
# The default user, its password from the pod's environment (the Secret), from
# anywhere in the cluster: the Service is its only way in.
CLICKHOUSE_USERS = """<clickhouse>
    <users>
        <default replace="replace">
            <password from_env="BOXD_CLICKHOUSE_PASSWORD"/>
            <networks><ip>::/0</ip></networks>
            <profile>default</profile>
            <quota>default</quota>
        </default>
    </users>
    <profiles>
        <default>
            <max_memory_usage>4000000000</max_memory_usage>
        </default>
    </profiles>
</clickhouse>
"""
STATE_GB = {"control": 20, "console": 10, "proxy": 10, "dns": 5, "worker": 20}
DOCKER_SOCK = "/run/boxd-docker"
TOLERATE_POOLS = [{"key": POOL_LABEL, "operator": "Exists", "effect": "NoSchedule"}]
# Control runs boxd-control itself (module docstring): what the entrypoint
# would set up for it, then its own voter's config.
CONTROL_SOCKET = "/run/boxd/control.sock"
CONTROL_START = ('mkdir -p /var/lib/boxd/control /run/boxd /etc/boxd && '
                 'ulimit -n 1048576 2>/dev/null; ulimit -l unlimited 2>/dev/null; '
                 'exec boxd-control --config="/etc/boxd-src/control-${POD_NAME##*-}.toml"')
def sha(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode())
        h.update(b"\0")
    return h.hexdigest()[:16]

def strip_learners(doc: dict) -> dict:
    """A config without [cluster] learners: what a restart hinges on. A
    learner added or removed reaches control through sync-config and no
    other role reads the list, so membership changes restart nothing."""
    out = copy.deepcopy(doc)
    out.get("cluster", {}).pop("learners", None)
    return out

class KubeRenderer(render.Renderer):
    def __init__(self, cfg, alloc, tf_out: dict, state, assets_root: str | None = None,
                 pull_config: str | None = None):
        super().__init__(cfg, alloc, tf_out, state, assets_root=assets_root)
        self.s = gke.settings(cfg.doc)
        self.ns = self.s["namespace"]
        if "gke" not in tf_out:
            raise InstallError("terraform outputs have no 'gke': run apply (terraform/gke)")
        self.out = tf_out["gke"]
        self.edges = tf_out.get("edges") or []
        self.pull_config = pull_config
        self.version = cfg.assets_version
        self.labels = {"app.kubernetes.io/managed-by": MANAGED_BY,
                       "app.kubernetes.io/part-of": "boxd", "boxd.sh/install": self.ns}

    # ---------------------------------------------------------------- TOMLs

    def _advertised_ip(self, node) -> str:
        """proxy-<i> and dns-<i> advertise edge <i>'s load balancer IP."""
        i = gke.ordinal(node.name)
        if i >= len(self.edges) or not (self.edges[i] or {}).get("ip"):
            raise InstallError(f"{node.name}: no edge IP {i} in the terraform outputs")
        return self.edges[i]["ip"]

    def _iface(self) -> str:
        return gke.IFACE

    def _base(self, node, role: str, storage_role: str | None = None) -> dict:
        doc = super()._base(node, role, storage_role)
        doc["vrack"]["subnet"] = self.out["node_cidr"]
        if role == "worker":
            # Raft state on the pod's own disk (it follows the node id); the
            # data dir is the node's local SSD RAID.
            doc["storage"] = {"raft_dir": f"{WORKER_STATE}/worker/raft",
                              "state_db": f"{WORKER_STATE}/worker/state.db"}
        return doc

    def _routed_network(self, node, role: str) -> dict:
        net = super()._routed_network(node, role)
        net["routed"]["underlay_ip"] = "@NODE_IP@"
        return net

    def _guest_mtu(self) -> int | None:
        if "guest_mtu" in self.cfg.network:
            return self.cfg.network["guest_mtu"]
        return gke.guest_mtu(int(self.out["vpc_mtu"]))

    def analytics_in_cluster(self) -> bool:
        """[gke] analytics, unless [analytics] (cluster.toml or the providers
        config) names a ClickHouse of its own."""
        return bool(self.s["analytics"]) and not self.cfg.table("analytics")

    def _auto_analytics(self) -> dict:
        """[analytics] for control, proxies, workers and the console: the
        cluster's own ClickHouse (render.py fills it only without an explicit
        table)."""
        if not self.analytics_in_cluster():
            return {}
        return {"endpoint": f"http://{CLICKHOUSE}.{self.ns}.svc.cluster.local:8123",
                "user": "default", "password": self.state.clickhouse_password()}

    def clickhouse_objects(self, config_hash: str) -> list[dict]:
        """ClickHouse for the usage graphs ([gke] analytics): its config, its
        Service and a one-replica StatefulSet on the system pool with a pd-ssd
        disk. None without it (prune takes them; the disk stays)."""
        if not self.analytics_in_cluster():
            return []
        cm = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": self._meta(CLICKHOUSE),
              "data": {"boxd.xml": CLICKHOUSE_CONFIG, "boxd-users.xml": CLICKHOUSE_USERS}}
        svc = {"apiVersion": "v1", "kind": "Service", "metadata": self._meta(CLICKHOUSE),
               "spec": {"selector": {"app": CLICKHOUSE},
                        "ports": [{"name": "http", "port": 8123}, {"name": "native", "port": 9000}]}}
        pod = self._pod_spec(
            "system", terminationGracePeriodSeconds=60,
            containers=[{
                "name": "clickhouse", "image": self.image(CLICKHOUSE),
                "env": [{"name": "BOXD_CLICKHOUSE_PASSWORD", "valueFrom": {"secretKeyRef": {
                            "name": SECRET, "key": "clickhouse-password"}}},
                        # The image's own user setup stays out: boxd-users.xml has the user.
                        self._env("CLICKHOUSE_SKIP_USER_SETUP", "1")],
                "ports": [{"name": "http", "containerPort": 8123},
                          {"name": "native", "containerPort": 9000}],
                "readinessProbe": {"httpGet": {"path": "/ping", "port": 8123},
                                   "periodSeconds": 10, "failureThreshold": 30},
                "resources": {"requests": {"cpu": "500m", "memory": "2Gi"},
                              "limits": {"memory": "6Gi"}},
                "volumeMounts": [
                    {"name": "data", "mountPath": "/var/lib/clickhouse"},
                    {"name": "config", "mountPath": "/etc/clickhouse-server/config.d/boxd.xml",
                     "subPath": "boxd.xml", "readOnly": True},
                    {"name": "config", "mountPath": "/etc/clickhouse-server/users.d/boxd-users.xml",
                     "subPath": "boxd-users.xml", "readOnly": True}]}],
            volumes=[{"name": "config", "configMap": {"name": CLICKHOUSE}}])
        return [cm, svc, self._sts(CLICKHOUSE, CLICKHOUSE, "clickhouse", 1, pod,
                                   [self._claim("data", self.s["analytics_disk_gb"],
                                                CLICKHOUSE_CLASS)],
                                   annotations={CONFIG_HASH: config_hash})]

    def console_ip(self) -> str:
        return gke.console_cluster_ip(self.out["services_cidr"])

    def _routes(self) -> list[dict]:
        ip = self.console_ip()
        routes = [{"hostname": f"{h}.{self.cfg.zone}", "target_ip": ip, "target_port": 8080}
                  for h in ("app", "connect")]
        routes.extend(self.cfg.table("proxy").get("internal_services", []))
        return routes

    def _acme_table(self) -> dict:
        acme = super()._acme_table()
        if "local_ca" in acme:
            acme["local_ca"] = {"cert_path": f"{SRC}/local-ca.pem",
                                "key_path": f"{SRC}/local-ca.key"}
        return acme

    def _github_table(self) -> dict:
        t = super()._github_table()
        if t.get("app_private_key_path") == "/etc/boxd/tls/github-app.pem":
            t["app_private_key_path"] = f"{SRC}/github-app.pem"
        return t

    def control_doc(self, node) -> dict:
        doc = super().control_doc(node)
        # The operator socket boxd-ctl reaches in the control pod (`ctl`, `kubectl exec`):
        # always the default path, so [control] in cluster.toml need not name it.
        doc["control"]["socket_path"] = CONTROL_SOCKET
        doc["names"] = self._names_table()
        return doc

    def proxy_doc(self, node) -> dict:
        doc = super().proxy_doc(node)
        # Not 22: the edge node's own sshd holds it (validate refuses 22).
        doc["proxy"].setdefault("ssh_port", 2222)
        doc["proxy"].setdefault("host_key", "/etc/boxd/tls/proxy_host_ed25519")
        doc["names"] = self._names_table()
        return doc

    def dns_doc(self, node) -> dict:
        doc = super().dns_doc(node)
        # Its edge's IP, not 0.0.0.0: the node's resolved stub holds 127.0.0.53:53.
        doc["dns"]["listen"] = f"{self._advertised_ip(node)}:53"
        n = len(self.cfg.nodes_with("dns"))
        doc["dns"]["nameservers"] = [f"ns{i}.{self.cfg.zone}" for i in range(1, n + 1)]
        return doc

    def worker_doc(self, node) -> dict:
        doc = super().worker_doc(node)
        # VMMs go in the worker pod's own slice: they survive a container
        # restart (an in-place update) and die with the pod.
        doc["worker"]["cgroup_root"] = "@CGROUP_ROOT@"
        return doc

    def worker_node(self, i: int):
        """worker-<i>, also one the config no longer lists but that still runs
        (being drained): it keeps its config until it is gone."""
        for n in self.cfg.nodes_with("worker"):
            if n.name == f"worker-{i}":
                return n
        return config.Node({"name": f"worker-{i}", "roles": ["worker"],
                            "internal_ip": gke.pod_dns(self.ns, "boxd-worker", i),
                            "machine_type": self.s["pools"]["worker"]["machine_type"]})

    def _key_file(self, name: str) -> str | None:
        path = os.path.join(self.state.keys_dir, name)
        if not os.path.exists(path):
            return None
        with open(path) as fh:
            return fh.read()

    def config_files(self, worker_ordinals) -> tuple[dict[str, str], dict[str, str]]:
        """(the Secret's files, each pod's config hash). worker_ordinals:
        every worker pod that runs or will, the ones being removed included."""
        dump = lambda d: toml_emit.dumps(d, header=render.HEADER)  # noqa: E731
        files: dict[str, str] = {}
        hashes: dict[str, str] = {}
        extra: dict[str, str] = {}
        if self.analytics_in_cluster():
            files["clickhouse-password"] = self.state.clickhouse_password()
            hashes["clickhouse"] = sha(CLICKHOUSE_CONFIG, CLICKHOUSE_USERS,
                                       files["clickhouse-password"])
        if host := self._key_file("proxy_host_ed25519"):
            extra["proxy_host_ed25519"] = host if host.endswith("\n") else host + "\n"
        else:
            raise InstallError("state has no proxy_host_ed25519 (ensure_keypairs runs first)")
        if self.cfg.acme_ca == "local":
            from . import local_ca
            for name in (local_ca.CERT_FILE, local_ca.KEY_FILE):
                if (body := self._key_file(name)) is not None:
                    extra[name] = body
        if self._github_key_stored():
            extra["github-app.pem"] = self._key_file("github-app.pem")
        if store := self.storage_store():
            extra["bucket.toml"] = dump(store)
        if self.pull_config:
            extra["docker-config.json"] = self.pull_config
        files.update(extra)
        get = lambda *names: [extra.get(n, "") for n in names]  # noqa: E731

        control = []
        for n in self.cfg.nodes_with("control"):
            doc = self.control_doc(n)
            files[f"control-{gke.ordinal(n.name)}.toml"] = dump(doc)
            control.append(dump(strip_learners(doc)))
        files["control.toml"] = files["control-0.toml"]
        hashes["control"] = sha(*control, *get("local-ca.pem", "local-ca.key"))
        console = self.cfg.nodes_with("console")[0]
        files["console.toml"] = dump(self.console_doc(console))
        hashes["console"] = sha(files["console.toml"], *get("github-app.pem"))
        for n in self.cfg.nodes_with("proxy"):
            i = gke.ordinal(n.name)
            doc = self.proxy_doc(n)
            files[f"proxy-{i}.toml"] = dump(doc)
            hashes[f"proxy-{i}"] = sha(dump(strip_learners(doc)),
                                       *get("proxy_host_ed25519", "github-app.pem"))
        for n in self.cfg.nodes_with("dns"):
            i = gke.ordinal(n.name)
            doc = self.dns_doc(n)
            files[f"dns-{i}.toml"] = dump(doc)
            hashes[f"dns-{i}"] = sha(dump(strip_learners(doc)))
        # The guest firewall's proxies (boxd-guest-fw --proxy): each proxy's address in the
        # pool, .254 downwards, as roles/boxd-worker computes them on a host. The worker's
        # entrypoint reads them at its start, so an added edge restarts the workers' process.
        files["guest-fw-proxies"] = " ".join(
            self._pool_ip(254 - i) for i in range(len(self.cfg.nodes_with("proxy")))) + "\n"
        for i in sorted(worker_ordinals):
            doc = self.worker_doc(self.worker_node(i))
            files[f"worker-{i}.toml"] = dump(doc)
            hashes[f"worker-{i}"] = sha(dump(strip_learners(doc)), files["guest-fw-proxies"],
                                        *get("bucket.toml", "docker-config.json", "github-app.pem"))
        return files, hashes

    def secret(self, files: dict[str, str]) -> dict:
        return {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                "metadata": self._meta(SECRET),
                "data": {k: base64.b64encode(v.encode()).decode() for k, v in sorted(files.items())}}

    # -------------------------------------------------------------- objects

    def image(self, name: str = "boxd") -> str:
        return gke.image(self.cfg.doc, name, self.version)

    def _meta(self, name: str, namespaced: bool = True, labels: dict | None = None,
              annotations: dict | None = None) -> dict:
        m = {"name": name, "labels": {**self.labels, **(labels or {})}}
        if namespaced:
            m["namespace"] = self.ns
        if annotations:
            m["annotations"] = annotations
        return m

    def cluster_name(self, what: str) -> str:
        """A cluster-scoped object's name: one per installation."""
        return f"boxd-{self.ns}-{what}"

    def _pod_spec(self, pool: str, **extra) -> dict:
        spec = {"nodeSelector": {POOL_LABEL: pool}, "tolerations": TOLERATE_POOLS}
        if ps := (self.cfg.doc.get("images") or {}).get("pull_secret"):
            spec["imagePullSecrets"] = [{"name": ps}]
        spec.update(extra)
        return spec

    @staticmethod
    def _common_volumes() -> list[dict]:
        return [{"name": "config", "secret": {"secretName": SECRET, "defaultMode": 0o400}},
                {"name": "machine-id", "hostPath": {"path": "/etc/machine-id", "type": "File"}}]

    @staticmethod
    def _common_mounts() -> list[dict]:
        return [{"name": "state", "mountPath": "/var/lib/boxd"},
                {"name": "config", "mountPath": SRC, "readOnly": True},
                {"name": "machine-id", "mountPath": "/etc/machine-id", "readOnly": True}]

    @staticmethod
    def _env(name: str, value=None, field: str | None = None) -> dict:
        if field:
            return {"name": name, "valueFrom": {"fieldRef": {"fieldPath": field}}}
        return {"name": name, "value": str(value)}

    def _claim(self, name: str, gb: int, storage_class: str = STATE_CLASS) -> dict:
        # No labels here: the claims a StatefulSet makes from this must never
        # match the prune selector.
        return {"metadata": {"name": name},
                "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": storage_class,
                         "resources": {"requests": {"storage": f"{gb}Gi"}}}}

    def _headless(self, name: str, app: str, ports: dict[str, int]) -> dict:
        return {"apiVersion": "v1", "kind": "Service", "metadata": self._meta(name),
                "spec": {"clusterIP": "None", "publishNotReadyAddresses": True,
                         "selector": {"app": app},
                         "ports": [{"name": k, "port": v} for k, v in ports.items()]}}

    def _sts(self, name: str, app: str, role: str, replicas: int, pod: dict,
             claims: list[dict], annotations: dict | None = None, service: str | None = None,
             strategy: str = "RollingUpdate") -> dict:
        tpl_meta = {"labels": {"app": app, ROLE_LABEL: role}}
        if annotations:
            tpl_meta["annotations"] = annotations
        return {"apiVersion": "apps/v1", "kind": "StatefulSet",
                "metadata": self._meta(name, labels={ROLE_LABEL: role}),
                "spec": {"serviceName": service or name, "replicas": replicas,
                         "podManagementPolicy": "Parallel",
                         "updateStrategy": {"type": strategy},
                         "selector": {"matchLabels": {"app": app}},
                         "template": {"metadata": tpl_meta, "spec": pod},
                         "volumeClaimTemplates": claims}}

    def control_dns(self) -> list[str]:
        return [n.internal_ip for n in self.cfg.nodes_with("control")]

    def base_objects(self, downloads: dict[str, str] | None = None) -> list[dict]:
        """Everything but the Secret, the role StatefulSets and the
        PodMonitorings: namespace, classes, RBAC, Services, disruption
        budgets, ConfigMaps, node-prep."""
        ns = {"apiVersion": "v1", "kind": "Namespace",
              "metadata": {"name": self.ns, "labels": {
                  **self.labels,
                  # The workers and edges are privileged and on the host
                  # network: Pod Security admits them only here.
                  "pod-security.kubernetes.io/enforce": "privileged",
                  "pod-security.kubernetes.io/audit": "privileged",
                  "pod-security.kubernetes.io/warn": "privileged"}}}
        objs = [ns,
                {"apiVersion": "scheduling.k8s.io/v1", "kind": "PriorityClass",
                 "metadata": self._meta(self.cluster_name("worker"), namespaced=False),
                 "value": 1000000, "globalDefault": False,
                 "description": "boxd workers and node prep: they hold running machines"},
                self._headless("boxd-control", "boxd-control",
                               {"raft": RAFT["control"], "forward": 8000, "edge": 7100}),
                {"apiVersion": "v1", "kind": "Service", "metadata": self._meta("boxd-console"),
                 # Pinned: the proxies' internal_services take an IP.
                 "spec": {"clusterIP": self.console_ip(), "selector": {"app": "boxd-console"},
                          "ports": [{"name": "http", "port": 8080}]}},
                self._headless("boxd-worker", "boxd-worker",
                               {"raft": RAFT["worker"], "grpc": 9010}),
                {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget",
                 "metadata": self._meta("boxd-control"),
                 "spec": {"maxUnavailable": 1,
                          "selector": {"matchLabels": {"app": "boxd-control"}}}},
                # No eviction ever takes a worker (a GKE upgrade's drain, an
                # eviction call): only the installer's drain removes one.
                {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget",
                 "metadata": self._meta("boxd-worker"),
                 "spec": {"maxUnavailable": 0,
                          "selector": {"matchLabels": {"app": "boxd-worker"}}}}]
        for i in range(len(self.cfg.nodes_with("proxy"))):
            objs.append(self._headless(f"boxd-proxy-{i}", f"boxd-proxy-{i}",
                                       {"raft": RAFT["proxy"], "api": 9443}))
            objs.append(self._headless(f"boxd-dns-{i}", f"boxd-dns-{i}", {"raft": RAFT["dns"]}))
        if self.s["pools"]["worker"]["local_ssd"] == 0:
            objs.append({"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass",
                         "metadata": self._meta(self.cluster_name("xfs"), namespaced=False),
                         "provisioner": "pd.csi.storage.gke.io",
                         "parameters": {"type": "pd-balanced",
                                        "csi.storage.k8s.io/fstype": "xfs"},
                         "volumeBindingMode": "WaitForFirstConsumer",
                         "allowVolumeExpansion": True, "reclaimPolicy": "Delete"})
        if downloads:
            # The CLI download manifests for app.<zone>/downloads; the console's
            # entrypoint copies them next to the CLI binaries the image holds.
            objs.append({"apiVersion": "v1", "kind": "ConfigMap",
                         "metadata": self._meta("boxd-downloads"),
                         "data": {os.path.basename(k): v for k, v in sorted(downloads.items())}})
        objs += self.node_prep_objects()
        return objs

    def node_prep_objects(self) -> list[dict]:
        """The DaemonSet that readies a worker node (boxd's node-prep image):
        boxd's host kernel and one reboot (host_kernel = "boxd"), the local
        SSDs as one RAID 0 with XFS at gke.DATA_MOUNT, then the
        boxd.sh/node-ready label the worker waits for. OnDelete: a new release
        never reboots a node that runs a worker; new nodes get it.

        RBAC: a node can only be patched through a ClusterRole (nodes are
        cluster-scoped, and RBAC has no rule for "the node this pod runs on"),
        so the role is held to get and patch on nodes, and the script patches
        only its own node's ready label."""
        name = self.cluster_name("node-prep")
        sa = {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": self._meta("boxd-node-prep")}
        role = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
                "metadata": self._meta(name, namespaced=False),
                "rules": [{"apiGroups": [""], "resources": ["nodes"], "verbs": ["get", "patch"]}]}
        binding = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
                   "metadata": self._meta(name, namespaced=False),
                   "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole",
                               "name": name},
                   "subjects": [{"kind": "ServiceAccount", "name": "boxd-node-prep",
                                 "namespace": self.ns}]}
        lssd = self.s["pools"]["worker"]["local_ssd"]
        kernel = ""
        if self.s["host_kernel"] == "boxd":
            from . import assets
            kernel = assets.host_kernel_release(self.assets_root) if self.assets_root else ""
        pod = self._pod_spec(
            "worker", serviceAccountName="boxd-node-prep", hostNetwork=True, hostPID=True,
            priorityClassName=self.cluster_name("worker"), terminationGracePeriodSeconds=5,
            containers=[{
                "name": "prep", "image": self.image("boxd-node-prep"),
                "env": [self._env("NODE_NAME", field="spec.nodeName"),
                        # "" = GKE's own kernel: no kernel step, no reboot.
                        self._env("BOXD_HOST_KERNEL", kernel),
                        self._env("BOXD_LOCAL_SSD", "1" if lssd else "0"),
                        self._env("BOXD_DATA_MOUNT", gke.DATA_MOUNT)],
                "securityContext": {"privileged": True},
                "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}},
                "volumeMounts": [{"name": "host", "mountPath": "/host"},
                                 {"name": "dev", "mountPath": "/dev"}]}],
            volumes=[{"name": "host", "hostPath": {"path": "/"}},
                     {"name": "dev", "hostPath": {"path": "/dev"}}])
        ds = {"apiVersion": "apps/v1", "kind": "DaemonSet",
              "metadata": self._meta("boxd-node-prep", labels={ROLE_LABEL: "node-prep"}),
              "spec": {"selector": {"matchLabels": {"app": "boxd-node-prep"}},
                       "updateStrategy": {"type": "OnDelete"},
                       "template": {"metadata": {"labels": {"app": "boxd-node-prep",
                                                            ROLE_LABEL: "node-prep"}},
                                    "spec": pod}}}
        return [sa, role, binding, ds]

    def pod_monitorings(self) -> list[dict]:
        """Managed Service for Prometheus scrapes each role's /metrics (the
        entrypoint moves a host-network role's listener to its node's IP).
        None with [gke] observability = false."""
        if not self.s["observability"]:
            return []
        return [{"apiVersion": "monitoring.googleapis.com/v1", "kind": "PodMonitoring",
                 "metadata": self._meta(f"boxd-{role}"),
                 "spec": {"selector": {"matchLabels": {ROLE_LABEL: role}},
                          "endpoints": [{"port": METRICS[role], "interval": "30s"}]}}
                for role in ("control", "proxy", "dns", "worker")]

    def control_objects(self, config_hash: str) -> list[dict]:
        n = len(self.cfg.nodes_with("control"))
        select = {"matchLabels": {"app": "boxd-control"}}
        affinity = {"podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [
            {"labelSelector": select, "topologyKey": "kubernetes.io/hostname"}]}}
        pod = self._pod_spec(
            "system", affinity=affinity, terminationGracePeriodSeconds=30,
            containers=[{
                "name": "control", "image": self.image(),
                "command": ["/bin/sh", "-c"], "args": [CONTROL_START],
                "env": [self._env("POD_NAME", field="metadata.name"),
                        self._env("BOXD_METRICS_LISTEN", f"0.0.0.0:{METRICS['control']}")],
                "readinessProbe": {"tcpSocket": {"port": RAFT["control"]},
                                   "periodSeconds": 10, "failureThreshold": 30},
                "resources": {"requests": {"cpu": "500m", "memory": "1Gi"}},
                "volumeMounts": self._common_mounts()}],
            volumes=self._common_volumes())
        if len(self.s["zones"]) > 1:
            pod["topologySpreadConstraints"] = [{
                "maxSkew": 1, "topologyKey": "topology.kubernetes.io/zone",
                "whenUnsatisfiable": "DoNotSchedule", "labelSelector": select}]
        return [self._sts("boxd-control", "boxd-control", "control", n, pod,
                          [self._claim("state", STATE_GB["control"])],
                          annotations={CONFIG_HASH: config_hash})]

    @staticmethod
    def _mount_downloads(downloads: dict[str, str] | None, mounts: list[dict],
                         volumes: list[dict]) -> None:
        """The zone's download files (render.downloads(): downloads/install.sh,
        downloads/cli/<file>) into DOWNLOADS_DIR in the image, each from the
        boxd-downloads ConfigMap (keyed by file name) at its own path."""
        if not downloads:
            return
        for path in sorted(downloads):
            name = os.path.basename(path)
            sub = "install.sh" if name == "install.sh" else f"cli/{name}"
            mounts.append({"name": "downloads", "mountPath": f"{DOWNLOADS_DIR}/{sub}",
                           "subPath": name, "readOnly": True})
        volumes.append({"name": "downloads", "configMap": {"name": "boxd-downloads"}})

    def console_objects(self, config_hash: str, downloads: dict[str, str] | None = None) -> list[dict]:
        proxy0 = self.cfg.nodes_with("proxy")[0].internal_ip
        mounts = self._common_mounts()
        volumes = self._common_volumes()
        self._mount_downloads(downloads, mounts, volumes)
        pod = self._pod_spec(
            "system", terminationGracePeriodSeconds=30,
            containers=[{
                "name": "console", "image": self.image(), "args": ["console"],
                "env": [self._env("POD_NAME", field="metadata.name"),
                        self._env("BOXD_METRICS_LISTEN", f"127.0.0.1:{METRICS['console']}"),
                        self._env("BOXD_CONSOLE_CONTROL",
                                  ",".join(f"http://{h}:7100" for h in self.control_dns())),
                        self._env("BOXD_CONSOLE_STATE_DB",
                                  "/var/lib/boxd/console-replica/state.db"),
                        self._env("BOXD_CONSOLE_DIR", "/usr/local/share/boxd/console"),
                        self._env("BOXD_CONSOLE_DOWNLOADS_DIR", DOWNLOADS_DIR),
                        self._env("BOXD_CONSOLE_LISTEN", "0.0.0.0:8080"),
                        self._env("BOXD_CONSOLE_API", f"http://{proxy0}:9443")],
                "readinessProbe": {"tcpSocket": {"port": 8080}, "periodSeconds": 10,
                                   "failureThreshold": 30},
                "resources": {"requests": {"cpu": "250m", "memory": "512Mi"}},
                "volumeMounts": mounts}],
            volumes=volumes)
        return [self._sts("boxd-console", "boxd-console", "console", 1, pod,
                          [self._claim("state", STATE_GB["console"])],
                          annotations={CONFIG_HASH: config_hash})]

    def _edge_pod(self, i: int, role: str, privileged: bool,
                  downloads: dict[str, str] | None = None) -> dict:
        env = [self._env("POD_NAME", field="metadata.name"),
               self._env("NODE_IP", field="status.hostIP"),
               self._env("EDGE_IP", self.edges[i]["ip"] if i < len(self.edges) else ""),
               self._env("BOXD_METRICS_LISTEN", f"127.0.0.1:{METRICS[role]}")]
        mounts = self._common_mounts()
        volumes = self._common_volumes()
        container = {"name": role, "image": self.image(), "args": [role], "env": env,
                     "resources": {"requests": {"cpu": "500m" if role == "proxy" else "100m",
                                                "memory": "1Gi" if role == "proxy" else "256Mi"}},
                     "volumeMounts": mounts}
        if role == "proxy":
            env.append(self._env("SSH_PORT", self.cfg.table("proxy").get("ssh_port", 2222)))
            # <zone>/downloads, the URL the console hands out.
            self._mount_downloads(downloads, mounts, volumes)
            mounts.append({"name": "bpf", "mountPath": "/sys/fs/bpf"})
            volumes.append({"name": "bpf", "hostPath": {"path": "/sys/fs/bpf"}})
        if privileged:
            container["securityContext"] = {"privileged": True}
        pod = self._pod_spec("edge", hostNetwork=True, dnsPolicy="ClusterFirstWithHostNet",
                             terminationGracePeriodSeconds=30, containers=[container],
                             volumes=volumes)
        pod["nodeSelector"][EDGE_LABEL] = str(i)
        return pod

    def edge_objects(self, i: int, hashes: dict[str, str],
                     downloads: dict[str, str] | None = None) -> list[dict]:
        """Edge <i>: its proxy and its dns, each a one-replica StatefulSet on
        edge pool <i>'s node, behind that pool's own load balancer IP."""
        return [self._sts(f"boxd-proxy-{i}", f"boxd-proxy-{i}", "proxy", 1,
                          self._edge_pod(i, "proxy", privileged=True, downloads=downloads),
                          [self._claim("state", STATE_GB["proxy"])],
                          annotations={CONFIG_HASH: hashes[f"proxy-{i}"]}),
                self._sts(f"boxd-dns-{i}", f"boxd-dns-{i}", "dns", 1,
                          self._edge_pod(i, "dns", privileged=False),
                          [self._claim("state", STATE_GB["dns"])],
                          annotations={CONFIG_HASH: hashes[f"dns-{i}"]})]

    def worker_pod(self) -> dict:
        lssd = self.s["pools"]["worker"]["local_ssd"]
        data_mount = {"name": "data", "mountPath": DATA_DIR}
        if lssd:
            data_mount["mountPropagation"] = "HostToContainer"
        env = [self._env("POD_NAME", field="metadata.name"),
               self._env("NODE_IP", field="status.hostIP"),
               self._env("BOXD_METRICS_LISTEN", f"127.0.0.1:{METRICS['worker']}"),
               self._env("DOCKER_HOST", f"unix://{DOCKER_SOCK}/docker.sock"),
               # For the docker credential helper the entrypoint configures.
               self._env("BOXD_IMAGE_REGISTRY", gke.registry(self.cfg.doc))]
        if lssd:
            env.append(self._env("LOCAL_SSD", "1"))
        worker = {
            "name": "worker", "image": self.image(), "args": ["worker"], "env": env,
            "securityContext": {"privileged": True},
            # Requests only: a limit would cap the VMMs in the pod's slice.
            "resources": {"requests": {"cpu": "1", "memory": "2Gi"}},
            "volumeMounts": [
                data_mount,
                {"name": "state", "mountPath": WORKER_STATE},
                {"name": "config", "mountPath": SRC, "readOnly": True},
                {"name": "machine-id", "mountPath": "/etc/machine-id", "readOnly": True},
                {"name": "cgroup", "mountPath": "/sys/fs/cgroup"},
                {"name": "bpf", "mountPath": "/sys/fs/bpf"},
                {"name": "run", "mountPath": "/run/boxd"},
                {"name": "docker-sock", "mountPath": DOCKER_SOCK}]}
        dockerd_mount = dict(data_mount)
        dockerd = {
            "name": "dockerd", "image": self.image("boxd-dockerd"),
            # dockerd itself, not the image's entrypoint: the dind script makes an "init"
            # cgroup at the root of the cgroups it sees and moves every process it can
            # into it, and a privileged pod here sees the node's (it moved the node's
            # kswapd, khugepaged, the journal threads...). The sidecar only pulls and
            # exports, which needs none of the script's setup.
            "command": ["dockerd"],
            # Warnings and up only: GKE files every stderr line as an ERROR.
            "args": [f"--host=unix://{DOCKER_SOCK}/docker.sock", "--bridge=none",
                     "--iptables=false", "--ip6tables=false", f"--data-root={DATA_DIR}/docker",
                     "--log-level=warn"],
            "securityContext": {"privileged": True},
            "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}},
            "volumeMounts": [dockerd_mount, {"name": "docker-sock", "mountPath": DOCKER_SOCK}]}
        volumes = self._common_volumes() + [
            {"name": "cgroup", "hostPath": {"path": "/sys/fs/cgroup"}},
            {"name": "bpf", "hostPath": {"path": "/sys/fs/bpf"}},
            {"name": "run", "hostPath": {"path": "/run/boxd", "type": "DirectoryOrCreate"}},
            {"name": "docker-sock", "emptyDir": {}}]
        if lssd:
            volumes.append({"name": "data", "hostPath": {"path": gke.DATA_MOUNT,
                                                         "type": "DirectoryOrCreate"}})
        select = {"matchLabels": {"app": "boxd-worker"}}
        pod = self._pod_spec(
            "worker",
            # One per node: two host-network workers would collide on every port.
            affinity={"podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [
                {"labelSelector": select, "topologyKey": "kubernetes.io/hostname"}]}},
            priorityClassName=self.cluster_name("worker"), hostNetwork=True, hostPID=True,
            dnsPolicy="ClusterFirstWithHostNet", terminationGracePeriodSeconds=60,
            containers=[worker, dockerd], volumes=volumes)
        pod["nodeSelector"].update({DISK_LABEL: "local-ssd" if lssd else "pd",
                                    READY_LABEL: "true"})
        return pod

    def worker_spec_hash(self, pod: dict | None = None) -> str:
        """The worker pod's spec without its images: a change here needs the
        pod recreated (after a drain); an image alone is patched in place."""
        pod = copy.deepcopy(pod or self.worker_pod())
        for c in pod.get("containers", []):
            c.pop("image", None)
        return sha(json.dumps(pod, sort_keys=True))

    def worker_objects(self, replicas: int) -> list[dict]:
        pod = self.worker_pod()
        claims = [self._claim("state", STATE_GB["worker"])]
        w = self.s["pools"]["worker"]
        if not w["local_ssd"]:
            claims.append(self._claim("data", w["data_disk_gb"], self.cluster_name("xfs")))
        return [self._sts("boxd-worker", "boxd-worker", "worker", replicas, pod, claims,
                          annotations={SPEC_HASH: self.worker_spec_hash(pod)},
                          strategy="OnDelete")]

    def worker_images(self) -> dict[str, str]:
        return {c["name"]: c["image"] for c in self.worker_pod()["containers"]}
