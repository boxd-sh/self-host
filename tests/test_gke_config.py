"""The GKE target ([gke]): validation, the synthetic topology, the license
check on the worker pool, the checks of an existing cluster, terraform's
variables and the plan guard. Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import json
import os
import subprocess
import tempfile
import tomllib
import unittest
from unittest import mock

from boxd_installer import clouds, config, license
from boxd_installer.clouds import gke
from boxd_installer.util import InstallError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXISTING = "projects/my-gcp-project/locations/europe-west4/clusters/platform"

def gke_doc(**gke_keys) -> dict:
    with open(os.path.join(ROOT, "examples", "gke", "cluster.toml"), "rb") as fh:
        doc = tomllib.load(fh)
    doc["gke"].update(gke_keys)
    return doc

def existing_doc(**gke_keys) -> dict:
    doc = gke_doc(**{"cluster": EXISTING, **gke_keys})
    for k in gke.CREATE_ONLY:
        doc["gke"].pop(k, None)
    return doc

def validate(doc: dict) -> str:
    try:
        config._validate(doc)
    except InstallError as e:
        return str(e)
    return ""

class FakeState:
    def __init__(self, data=None):
        self.data = data or {}
        self.saved = 0
    def save(self):
        self.saved += 1

class Example(unittest.TestCase):
    def test_the_example_is_valid(self):
        self.assertEqual(validate(gke_doc()), "")

    def test_an_existing_cluster_is_valid(self):
        self.assertEqual(validate(existing_doc()), "")

    def test_the_cloud_stays_gcp(self):
        cfg = config.Config(gke_doc())
        self.assertEqual((cfg.cloud_name, cfg.target), ("gcp", "gke"))
        self.assertIs(clouds.get(cfg), gke)
        self.assertTrue(clouds.kubernetes(cfg))
        self.assertTrue(clouds.provisions(cfg))
        self.assertEqual(cfg.acme_ca, "gts")
        self.assertEqual(cfg.network_mode, "routed")
        self.assertEqual(cfg.network["vni_range_start"], 100)

    def test_other_targets_are_unchanged(self):
        with open(os.path.join(ROOT, "examples", "gcp", "cluster.toml"), "rb") as fh:
            cfg = config.Config(tomllib.load(fh))
        self.assertIsNone(cfg.target)
        self.assertFalse(clouds.kubernetes(cfg))
        self.assertIs(clouds.get(cfg), clouds.gcp)

class Topology(unittest.TestCase):
    def test_one_node_per_pod(self):
        cfg = config.Config(gke_doc())
        names = [n.name for n in cfg.nodes]
        self.assertEqual(names, ["control-0", "control-1", "control-2", "console",
                                 "proxy-0", "dns-0", "proxy-1", "dns-1", "worker-0"])
        self.assertEqual(cfg.nodes[1].internal_ip,
                         "boxd-control-1.boxd-control.boxd.svc.cluster.local")
        self.assertEqual(cfg.nodes_with("proxy")[1].internal_ip,
                         "boxd-proxy-1-0.boxd-proxy-1.boxd.svc.cluster.local")
        self.assertEqual([n.qualifiers["dns"] for n in cfg.nodes_with("dns")], ["ns1", "ns2"])
        self.assertEqual(cfg.nodes_with("worker")[0].machine_type, "n2-standard-16")

    def test_internal_ingress_is_private(self):
        doc = gke_doc(ingress="internal")
        doc["acme"] = {"ca": "local"}
        self.assertEqual(validate(doc), "")
        self.assertEqual(config.Config(doc).ingress, "private")

    def test_system_pool_default_holds_every_control(self):
        doc = gke_doc(zones=["europe-west4-a", "europe-west4-b"])
        del doc["gke"]["pools"]["system"]["nodes"]
        self.assertEqual(gke.settings(doc)["pools"]["system"]["nodes"], 4)
        self.assertEqual(validate(doc), "")

class Validation(unittest.TestCase):
    def assertError(self, doc, text):
        self.assertIn(text, validate(doc))

    def test_no_nodes_list(self):
        doc = gke_doc()
        doc["nodes"] = [{"name": "x", "roles": ["worker"], "internal_ip": "10.60.0.2"}]
        self.assertError(doc, "[[nodes]] is not used with [gke]")

    def test_gcp_instance_fields_move(self):
        doc = gke_doc()
        doc["gcp"]["zone"] = "europe-west4-a"
        doc["gcp"]["vpc_cidr"] = "10.60.0.0/16"
        doc["gcp"]["ingress"] = "private"
        err = validate(doc)
        self.assertIn("[gcp].zone is not used with [gke]: set [gke] zones", err)
        self.assertIn("[gcp].vpc_cidr is not used with [gke]: set [gke] vpc_cidr", err)
        self.assertIn("[gcp].ingress is not a [gcp] key with [gke]", err)

    def test_needs_gcp(self):
        doc = gke_doc()
        doc["aws"] = {"region": "x"}
        del doc["gcp"]
        self.assertError(doc, "[gke] needs [gcp] beside it")

    def test_images_only_with_gke(self):
        with open(os.path.join(ROOT, "examples", "gcp", "cluster.toml"), "rb") as fh:
            doc = tomllib.load(fh)
        doc["images"] = {"registry": "x"}
        self.assertError(doc, "[images] is only for [gke]")

    def test_cluster_value(self):
        self.assertError(gke_doc(cluster="mine"), '[gke].cluster must be "create" or')
        doc = gke_doc()
        del doc["gke"]["cluster"]
        self.assertError(doc, "[gke].cluster required")
        self.assertError(existing_doc(cluster="projects/other-project/locations/europe-west4/clusters/c"),
                         "is in project other-project")
        self.assertError(existing_doc(cluster="projects/my-gcp-project/locations/us-east1-b/clusters/c"),
                         "outside [gcp].region europe-west4")

    def test_create_only_keys(self):
        self.assertError(gke_doc(cluster=EXISTING), "[gke].admin_cidrs is only for cluster = \"create\"")
        doc = gke_doc()
        del doc["gke"]["admin_cidrs"]
        self.assertError(doc, "[gke].admin_cidrs required")
        doc = gke_doc()
        del doc["gke"]["vpc_cidr"]
        self.assertError(doc, "[gke].vpc_cidr required")
        self.assertError(gke_doc(vpc_cidr="172.18.0.0/20"), "[gke].vpc_cidr must be inside 10.0.0.0/8")

    def test_zones(self):
        doc = gke_doc()
        del doc["gke"]["zones"]
        self.assertError(doc, "[gke].zones required")
        self.assertError(gke_doc(zones=["us-east1-b"]), "is not a zone of [gcp].region")
        self.assertError(gke_doc(zones=["europe-west4-a", "europe-west4-a"]), "duplicates")

    def test_control_replicas(self):
        self.assertError(gke_doc(control_replicas=2), "must be odd")
        self.assertError(gke_doc(control_replicas=5), "more than 3 is not supported yet")
        self.assertError(gke_doc(control_replicas=0), "must be 1 or 3")
        doc = gke_doc(control_replicas=1)
        doc["gke"]["pools"]["system"]["nodes"] = 1
        self.assertEqual(validate(doc), "")

    def test_analytics(self):
        self.assertError(gke_doc(analytics="yes"), "[gke].analytics must be true or false")
        self.assertError(gke_doc(analytics_disk_gb=5), "analytics_disk_gb must be a whole number")
        self.assertError(gke_doc(analytics_disk_gb=100.5), "analytics_disk_gb must be a whole number")
        self.assertEqual(validate(gke_doc(analytics=False)), "")
        self.assertEqual(validate(gke_doc(analytics_disk_gb=500)), "")

    def test_system_pool(self):
        doc = gke_doc(zones=["europe-west4-a", "europe-west4-b"])
        self.assertError(doc, "must be a multiple of the 2 zones")
        doc = gke_doc()
        doc["gke"]["pools"]["system"]["nodes"] = 2
        self.assertError(doc, "must be at least control_replicas (3)")

    def test_no_autoscaling(self):
        doc = gke_doc()
        doc["gke"]["pools"]["worker"]["max_nodes"] = 5
        self.assertError(doc, "boxd's pools don't autoscale")
        doc = gke_doc()
        doc["gke"]["pools"]["gpu"] = {"nodes": 1}
        self.assertError(doc, "[gke.pools.gpu]: unknown pool")

    def test_worker_pool(self):
        doc = gke_doc()
        doc["gke"]["pools"]["worker"]["machine_type"] = "n2d-standard-16"
        self.assertError(doc, "AMD, Arm and E2 machines have no nested virtualization")
        doc = gke_doc()
        del doc["gke"]["pools"]["worker"]["nodes"]
        self.assertError(doc, "[gke.pools.worker].nodes required")
        doc = gke_doc()
        doc["gke"]["pools"]["worker"]["local_ssd"] = 3
        self.assertError(doc, "local_ssd must be one of")
        doc = gke_doc()
        doc["gke"]["pools"]["worker"]["local_ssd"] = 0
        self.assertError(doc, "data_disk_gb required with local_ssd = 0")
        doc["gke"]["pools"]["worker"]["data_disk_gb"] = 500
        self.assertEqual(validate(doc), "")
        doc = gke_doc()
        doc["gke"]["pools"]["worker"]["data_disk_gb"] = 500
        self.assertError(doc, "data_disk_gb is only for local_ssd = 0")

    def test_edge_pool(self):
        doc = gke_doc()
        doc["gke"]["pools"]["edge"]["nodes"] = 0
        self.assertError(doc, "[gke.pools.edge].nodes must be a positive integer")

    def test_internal_ingress(self):
        self.assertError(gke_doc(ingress="internal"), 'requires [acme] ca = "local"')
        self.assertError(gke_doc(internal_ips=["10.60.0.250"]), "only for ingress = \"internal\"")
        self.assertError(gke_doc(ingress="private"), "[gke].ingress must be one of public, internal")
        self.assertError(gke_doc(host_kernel="custom"), "[gke].host_kernel must be one of boxd, stock")

    def test_network(self):
        doc = gke_doc()
        doc["network"]["mode"] = "bridge"
        self.assertError(doc, '[network].mode must be "routed" with [gke]')
        doc = gke_doc()
        doc["network"]["pool"] = "10.60.0.0/16"
        self.assertError(doc, "overlaps vpc_cidr")
        doc = gke_doc(pods_cidr="10.120.0.0/14")
        self.assertError(doc, "[network].pool 10.120.0.0/16 overlaps [gke].pods_cidr")
        self.assertError(gke_doc(services_cidr="172.17.0.0/20"), "Docker's default bridge")
        self.assertError(gke_doc(pods_cidr="172.24.0.0/16"), "[gke].pods_cidr 172.24.0.0/16 overlaps [gke].services_cidr")
        doc = gke_doc()
        del doc["network"]["pool"]
        self.assertError(doc, "[network] missing: pool")

    def test_tables_the_installer_sets(self):
        doc = gke_doc()
        doc["proxy"]["ssh_port"] = 22
        self.assertError(doc, "[proxy].ssh_port can't be 22 on GKE")
        doc = gke_doc()
        doc["dns"] = {"listen": "0.0.0.0:53"}
        self.assertError(doc, "[dns].listen is set by the installer on GKE")
        doc = gke_doc()
        doc["worker"]["swap_gb"] = [64]
        self.assertError(doc, "[worker].swap_gb is not supported on GKE")

    def test_images(self):
        doc = gke_doc()
        doc["images"] = {"registry": "https://x/y"}
        self.assertError(doc, "[images].registry must be a registry path")
        doc["images"] = {"registry": "europe-west4-docker.pkg.dev/p/boxd", "pull_secret": "Bad_Name"}
        self.assertError(doc, "[images].pull_secret must be the name of a Secret")
        doc["images"] = {"mirror": "x"}
        self.assertError(doc, "[images].mirror: unknown key")
        doc["images"] = {"registry": "europe-west4-docker.pkg.dev/p/boxd", "pull_secret": "boxd-pull"}
        self.assertEqual(validate(doc), "")

    def test_name_lengths(self):
        doc = gke_doc()
        doc["cluster"]["name"] = "a" * 23
        self.assertError(doc, "[cluster].name must be <= 22 chars on GKE")

    def test_unknown_key_and_namespace(self):
        self.assertError(gke_doc(node_upgrades="installer"), "[gke].node_upgrades: unknown key")
        self.assertError(gke_doc(namespace="kube-boxd"), "[gke].namespace must be")
        self.assertError(gke_doc(namespace="Boxd"), "[gke].namespace must be")

class Warnings(unittest.TestCase):
    def test_recommended_topology(self):
        doc = gke_doc(control_replicas=1)
        doc["gke"]["pools"]["system"]["nodes"] = 1
        doc["gke"]["pools"]["edge"]["nodes"] = 1
        out = " ".join(gke.warnings(config.Config(doc)))
        self.assertIn("control_replicas = 1", out)
        self.assertIn("[gke.pools.edge].nodes = 1", out)
        self.assertEqual(gke.warnings(config.Config(gke_doc())), [])

    def test_ranges_in_10_8(self):
        out = gke.warnings(config.Config(gke_doc(pods_cidr="10.200.0.0/16")))
        self.assertIn("[gke].pods_cidr 10.200.0.0/16 is inside 10.0.0.0/8", out[0])

class License(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gke-license-")

    def cfg(self, nodes, mt="n2-standard-16", key=None):
        doc = gke_doc()
        doc["gke"]["pools"]["worker"].update(nodes=nodes, machine_type=mt)
        if key:
            with open(os.path.join(self.dir, "license.key"), "w") as fh:
                fh.write(key + "\n")
            doc["license"] = {"key_file": "license.key"}
        return config.Config(doc, self.dir)

    def test_free_tier(self):
        self.assertEqual(gke.license_errors(self.cfg(1)), [])
        err = gke.license_errors(self.cfg(2))
        self.assertIn("the worker pool has 32 vCPUs (2 x n2-standard-16)", err[0])
        self.assertIn("boxd allows 16", err[0])

    def test_unknown_type(self):
        self.assertIn("can't count the vCPUs", gke.license_errors(self.cfg(1, "n2-custom-8-32768"))[0])

    def test_a_key_that_lifts_it(self):
        with mock.patch.object(license, "key_features", return_value={gke.UNLIMITED}):
            self.assertEqual(gke.license_errors(self.cfg(4, key="boxd-lic1_abc")), [])
        with mock.patch.object(license, "key_features", return_value={"bucket-storage"}):
            self.assertTrue(gke.license_errors(self.cfg(4, key="boxd-lic1_abc")))

def cluster_json(**over) -> dict:
    c = {"name": "platform", "location": "europe-west4", "currentMasterVersion": "1.31.2-gke.100",
         "locations": ["europe-west4-a", "europe-west4-b", "europe-west4-c"],
         "networkConfig": {"network": "projects/my-gcp-project/global/networks/platform",
                           "subnetwork": "projects/my-gcp-project/regions/europe-west4/subnetworks/nodes",
                           "datapathProvider": "LEGACY_DATAPATH"},
         "ipAllocationPolicy": {"useIpAliases": True, "clusterIpv4CidrBlock": "172.30.0.0/16",
                                "servicesIpv4CidrBlock": "172.31.0.0/20"},
         "releaseChannel": {"channel": "REGULAR"},
         "maintenancePolicy": {"window": {"maintenanceExclusions": {
             "hold": {"maintenanceExclusionOptions": {"scope": "NO_MINOR_OR_NODE_UPGRADES"}}}}},
         "monitoringConfig": {"managedPrometheusConfig": {"enabled": True}},
         "databaseEncryption": {"state": "ENCRYPTED"}}
    c.update(over)
    return c

NETWORK = {"mtu": 8896}
SUBNET = {"name": "nodes", "ipCidrRange": "10.80.0.0/20", "privateIpGoogleAccess": True}

class ExistingCluster(unittest.TestCase):
    def problems(self, c=None, network=NETWORK, subnet=SUBNET, doc=None):
        cfg = config.Config(doc or existing_doc())
        return gke.existing_cluster_problems(cfg, c or cluster_json(), network, subnet)

    def test_a_good_cluster(self):
        self.assertEqual(self.problems(), ([], []))

    def test_refusals(self):
        cases = [
            (cluster_json(autopilot={"enabled": True}), "Autopilot"),
            (cluster_json(currentMasterVersion="1.28.3-gke.1"), "boxd needs 1.28.4 or later"),
            (cluster_json(networkConfig={**cluster_json()["networkConfig"],
                                         "datapathProvider": "ADVANCED_DATAPATH"}), "Dataplane V2"),
            (cluster_json(ipAllocationPolicy={"useIpAliases": False}), "not VPC-native"),
            (cluster_json(locations=["europe-west4-b"]), "europe-west4-a is not one of"),
            (cluster_json(networkConfig={"network": "projects/host/global/networks/shared"}), "Shared VPC"),
            (cluster_json(monitoringConfig={}), "Managed Service for Prometheus off"),
            (cluster_json(ipAllocationPolicy={"useIpAliases": True,
                                              "clusterIpv4CidrBlock": "10.120.0.0/14"}),
             "overlaps platform's pod range"),
        ]
        for c, text in cases:
            errs, _ = self.problems(c)
            self.assertTrue(any(text in e for e in errs), (text, errs))

    def test_subnet_and_mtu(self):
        errs, _ = self.problems(subnet={**SUBNET, "ipCidrRange": "172.18.0.0/20"})
        self.assertIn("outside 10.0.0.0/8", errs[0])
        errs, _ = self.problems(subnet={**SUBNET, "privateIpGoogleAccess": False})
        self.assertIn("--enable-private-ip-google-access", errs[0])
        errs, warns = self.problems(network={"mtu": 1460})
        self.assertEqual(errs, [])
        self.assertIn("guests get an MTU of 1410", warns[0])
        errs, _ = self.problems(network={"mtu": 1300})
        self.assertIn("less than 1280", errs[0])
        doc = existing_doc()
        doc["network"]["guest_mtu"] = 1300
        self.assertEqual(self.problems(network={"mtu": 1300}, doc=doc), ([], []))

    def test_registry_outside_google_needs_no_private_access(self):
        doc = existing_doc()
        doc["images"] = {"registry": "registry.example.com/boxd"}
        errs, _ = self.problems(subnet={**SUBNET, "privateIpGoogleAccess": False}, doc=doc)
        self.assertEqual(errs, [])

    def test_warnings(self):
        _, warns = self.problems(cluster_json(maintenancePolicy={}, databaseEncryption={}))
        self.assertTrue(any("no maintenance exclusion on node upgrades" in w for w in warns))
        self.assertTrue(any("application-layer encryption" in w for w in warns))

    def test_live_checks_read_with_gcloud(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if cmd[1:3] == ["resource-manager", "org-policies"]:
                return subprocess.CompletedProcess(cmd, 0, json.dumps(
                    {"booleanPolicy": {"enforced": True}}), "")
            if cmd[1:4] == ["container", "clusters", "describe"]:
                return subprocess.CompletedProcess(cmd, 0, json.dumps(cluster_json()), "")
            if cmd[1:4] == ["compute", "networks", "describe"]:
                return subprocess.CompletedProcess(cmd, 0, json.dumps({"mtu": 1500}), "")
            return subprocess.CompletedProcess(cmd, 0, json.dumps(SUBNET), "")
        with mock.patch.object(gke, "run", fake_run):
            errs, warns, facts = gke.live_checks(config.Config(existing_doc()))
        self.assertIn('host_kernel = "boxd" needs Secure Boot off', errs[0])
        self.assertIn('set [gke] host_kernel = "stock"', errs[0].replace("Set", "set"))
        self.assertEqual(facts, {"vpc_mtu": 1500})
        self.assertIn("--region=europe-west4", calls[-1])
        self.assertTrue(all(c[0] == "gcloud" for c in calls))

    def test_stock_kernel_skips_the_policy(self):
        with mock.patch.object(gke, "run") as run:
            self.assertEqual(gke.live_checks(config.Config(gke_doc(host_kernel="stock"))), ([], [], {}))
        run.assert_not_called()

    def test_unreadable_policy_warns(self):
        with mock.patch.object(gke, "run", return_value=subprocess.CompletedProcess([], 1, "", "denied")):
            errs, warns, _ = gke.live_checks(config.Config(gke_doc()))
        self.assertEqual(errs, [])
        self.assertIn("could not read the org policy", warns[0])

class Helpers(unittest.TestCase):
    def test_guest_mtu(self):
        self.assertEqual([gke.guest_mtu(m) for m in (8896, 1550, 1500, 1460)], [0, 0, 1450, 1410])

    def test_console_ip(self):
        self.assertEqual(gke.console_cluster_ip("172.24.0.0/20"), "172.24.0.80")
        self.assertEqual(gke.console_cluster_ip("172.24.0.0/27"), "172.24.0.16")

    def test_parse_cluster(self):
        self.assertEqual(gke.parse_cluster(EXISTING), ("my-gcp-project", "europe-west4", "platform"))
        self.assertIsNone(gke.parse_cluster("create"))

    def test_images(self):
        doc = gke_doc()
        self.assertEqual(gke.image(doc, "boxd", "abc"), f"{gke.DEFAULT_REGISTRY}/boxd:abc")
        self.assertEqual(gke.computer_image(doc, "computer"), "computer")
        doc["images"] = {"registry": "r.example.com/m"}
        self.assertEqual(gke.image(doc, "boxd-node-prep", "abc"), "r.example.com/m/boxd-node-prep:abc")
        self.assertEqual(gke.computer_image(doc, "computer"), "r.example.com/m/computer")

class StateRecord(unittest.TestCase):
    def test_what_cant_change(self):
        cfg = config.Config(gke_doc())
        st = FakeState()
        gke.record(cfg, st)
        self.assertEqual(gke.state_errors(cfg, st), [])
        doc = gke_doc(control_replicas=1)
        doc["gke"]["pools"]["edge"]["nodes"] = 1
        doc["gke"]["namespace"] = "other"
        doc["gke"]["pools"]["worker"].update(local_ssd=0, data_disk_gb=500)
        errs = " ".join(gke.state_errors(config.Config(doc), st))
        for text in ("control_replicas was 3", "edges can only be added", "namespace was 'boxd'",
                     "local_ssd switched"):
            self.assertIn(text, errs)
        self.assertIn("analytics_disk_gb was 100",
                      " ".join(gke.state_errors(config.Config(gke_doc(analytics_disk_gb=200)), st)))
        more_edges = gke_doc()
        more_edges["gke"]["pools"]["edge"]["nodes"] = 3
        self.assertEqual(gke.state_errors(config.Config(more_edges), st), [])
        self.assertIn("can't move between GKE clusters",
                      gke.state_errors(config.Config(existing_doc()), st)[0])

class Tfvars(unittest.TestCase):
    def test_create(self):
        st = FakeState()
        v = gke.tfvars(config.Config(gke_doc()), st, live_workers=0)
        self.assertTrue(v["create_cluster"])
        self.assertEqual((v["system_nodes_per_zone"], v["system_tainted"]), (3, False))
        self.assertEqual((v["edge_count"], v["worker_nodes"], v["worker_local_ssd"]), (2, 1, 4))
        self.assertFalse(v["worker_secure_boot"])
        self.assertEqual(v["edge_source_ranges"], ["0.0.0.0/0"])
        self.assertIn("10000-30000", v["edge_tcp_ports"])
        self.assertTrue(v["kms_suffix"] and v["hold_start"])
        self.assertEqual(json.loads(v["dashboard_json"])["displayName"],
                         "boxd on GKE (mycluster/boxd)")
        # Stable across applies.
        self.assertEqual(gke.tfvars(config.Config(gke_doc()), st)["kms_suffix"], v["kms_suffix"])

    def test_the_pool_never_shrinks_under_a_running_worker(self):
        doc = gke_doc()
        doc["gke"]["pools"]["worker"]["nodes"] = 1
        self.assertEqual(gke.tfvars(config.Config(doc), FakeState(), live_workers=3)["worker_nodes"], 3)

    def test_existing_internal_stock(self):
        doc = existing_doc(ingress="internal", host_kernel="stock", observability=False)
        doc["acme"] = {"ca": "local"}
        doc["gke"]["pools"]["system"]["nodes"] = 3
        v = gke.tfvars(config.Config(doc), FakeState(), facts={"vpc_mtu": 1500})
        self.assertFalse(v["create_cluster"])
        self.assertEqual((v["existing_cluster_name"], v["existing_cluster_location"]),
                         ("platform", "europe-west4"))
        self.assertTrue(v["system_tainted"])
        self.assertTrue(v["worker_secure_boot"])
        self.assertEqual(v["existing_vpc_mtu"], 1500)
        self.assertEqual(v["dashboard_json"], "")
        self.assertEqual(v["kms_suffix"], "")
        self.assertIn("192.168.0.0/16", v["edge_source_ranges"])

class PlanGuard(unittest.TestCase):
    def change(self, kind, name, actions, before=None, after=None, unknown=None):
        return {"type": kind, "name": name, "change": {
            "actions": actions, "before": before, "after": after, "after_unknown": unknown or {}}}

    def test_worker_pool(self):
        plan = {"resource_changes": [self.change("google_container_node_pool", "worker", ["delete", "create"])]}
        self.assertIn("replaces the worker pool", gke.plan_problems(plan)[0])
        resize = self.change("google_container_node_pool", "worker", ["update"],
                             {"node_count": 2, "node_config": [{"machine_type": "a"}]},
                             {"node_count": 3, "node_config": [{"machine_type": "a"}]})
        self.assertEqual(gke.plan_problems({"resource_changes": [resize]}), [])

    def test_a_new_kms_key_under_a_running_cluster(self):
        key = {"type": "google_kms_crypto_key", "name": "secrets",
               "change": {"actions": ["create", "delete"]}}
        running = {"type": "google_container_cluster", "name": "boxd",
                   "change": {"actions": ["update"], "before": {}, "after": {}}}
        self.assertIn("KMS key the GKE cluster's Secrets are encrypted with",
                      gke.plan_problems({"resource_changes": [running, key]})[0])
        # A cluster this plan creates has no Secrets yet.
        new = {"type": "google_container_cluster", "name": "boxd", "change": {"actions": ["create"]}}
        self.assertEqual(gke.plan_problems({"resource_changes": [new, key]}), [])
        retype = self.change("google_container_node_pool", "worker", ["update"],
                             {"node_count": 2, "node_config": [{"machine_type": "a"}]},
                             {"node_count": 2, "node_config": [{"machine_type": "b"}]})
        self.assertIn("changes the worker pool's node_config", gke.plan_problems(
            {"resource_changes": [retype]})[0])

    def test_cluster_and_other_pools(self):
        plan = {"resource_changes": [
            self.change("google_container_cluster", "boxd", ["delete", "create"]),
            self.change("google_container_node_pool", "edge", ["delete", "create"])]}
        out = gke.plan_problems(plan)
        self.assertEqual(len(out), 1)
        self.assertIn("replaces the GKE cluster", out[0])

class DeleteInstance(unittest.TestCase):
    GROUP = ("https://www.googleapis.com/compute/v1/projects/p/zones/europe-west4-a/"
             "instanceGroupManagers/gke-mycluster-mycluster-w-1a2b3c4d-grp")

    def test_deletes_from_its_group(self):
        cfg = config.Config(gke_doc())
        with mock.patch.object(gke, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            gke.delete_worker_instance(cfg, "gke-mycluster-mycluster-w-1a2b3c4d-x7k2",
                                       [self.GROUP])
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[:6], ["gcloud", "compute", "instance-groups", "managed",
                                   "delete-instances", "gke-mycluster-mycluster-w-1a2b3c4d-grp"])
        self.assertIn("--instances=gke-mycluster-mycluster-w-1a2b3c4d-x7k2", cmd)
        self.assertIn("--zone=europe-west4-a", cmd)

    def test_gone_already_is_fine_and_failure_is_not(self):
        cfg = config.Config(gke_doc())
        with mock.patch.object(gke, "run", return_value=subprocess.CompletedProcess(
                [], 1, "", "ERROR: The resource 'x' was not found")):
            gke.delete_worker_instance(cfg, "n", [self.GROUP])
        with mock.patch.object(gke, "run", return_value=subprocess.CompletedProcess([], 1, "", "denied")), \
                self.assertRaisesRegex(InstallError, "deleting the worker node n"):
            gke.delete_worker_instance(cfg, "n", [self.GROUP])
        with self.assertRaisesRegex(InstallError, "can't tell which instance group"):
            gke.delete_worker_instance(cfg, "n", [])

if __name__ == "__main__":
    unittest.main()

class Dashboard(unittest.TestCase):
    def test_no_zero_positions(self):
        """The Monitoring API leaves out a tile's zero xPos/yPos, so a dashboard that sent
        them showed as changed on every terraform plan."""
        v = gke.tfvars(config.Config(gke_doc()), FakeState(), live_workers=0)
        tiles = json.loads(v["dashboard_json"])["mosaicLayout"]["tiles"]
        for t in tiles:
            self.assertNotEqual(t.get("xPos"), 0, t)
            self.assertNotEqual(t.get("yPos"), 0, t)
        self.assertTrue(any("xPos" not in t and "yPos" not in t for t in tiles))
