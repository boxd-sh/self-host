"""An existing VPC on AWS ([aws] vpc_id, private_subnet_id, public_subnet_id,
s3_endpoint_id): the CIDRs read from AWS, the config rules, and the tfvars.
Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import contextlib
import io
import os
import tempfile
import tomllib
import unittest
from unittest import mock

from boxd_installer import config, main, render, tf
from boxd_installer.clouds import aws
from boxd_installer.util import InstallError

from test_machine_sizes import FakeAlloc, FakeState, aws_doc, validate

VPC, PRIV, PUB, VPCE = "vpc-0abc", "subnet-0priv", "subnet-0pub", "vpce-0s3"
PRIV_RTB, PUB_RTB = "rtb-0priv", "rtb-0pub"

NAT_ROUTE = {"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": "nat-0abc", "State": "active"}
IGW_ROUTE = {"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-0abc", "State": "active"}
LOCAL_ROUTE = {"DestinationCidrBlock": "10.60.0.0/16", "GatewayId": "local", "State": "active"}

class FakeEc2:
    """aws ec2 describe-* for a VPC 10.60.0.0/16 in us-east-1 (the example's
    region), its subnets, route tables and S3 gateway endpoint."""
    def __init__(self, subnet_az="us-east-1a", subnet_vpc=VPC, endpoint_tables=(PRIV_RTB, PUB_RTB),
                 endpoint_service="com.amazonaws.us-east-1.s3", missing=(), denied=(),
                 priv_cidr="10.60.4.0/22", secondary=(), priv_routes=(NAT_ROUTE,),
                 pub_routes=(IGW_ROUTE,), priv_associated=True, dns=True,
                 endpoint_state="available"):
        self.calls, self.missing, self.denied = [], missing, denied
        self.subnets = {
            PRIV: {"SubnetId": PRIV, "VpcId": subnet_vpc, "CidrBlock": priv_cidr,
                   "AvailabilityZone": subnet_az},
            PUB: {"SubnetId": PUB, "VpcId": VPC, "CidrBlock": "10.60.0.0/24",
                  "AvailabilityZone": "us-east-1a"},
        }
        self.endpoint = {"VpcEndpointId": VPCE, "VpcId": VPC, "VpcEndpointType": "Gateway",
                         "ServiceName": endpoint_service, "RouteTableIds": list(endpoint_tables),
                         "State": endpoint_state}
        # (cidr, state) beyond the primary.
        self.secondary = secondary
        self.tables = [
            {"RouteTableId": PRIV_RTB, "Associations": [{"SubnetId": PRIV}] if priv_associated else [],
             "Routes": [LOCAL_ROUTE, *priv_routes]},
            {"RouteTableId": PUB_RTB, "Associations": [{"SubnetId": PUB}],
             "Routes": [LOCAL_ROUTE, *pub_routes]},
            {"RouteTableId": "rtb-0main", "Associations": [{"Main": True}],
             "Routes": [LOCAL_ROUTE]},
        ]
        self.dns = dns

    def __call__(self, region, op, *args):
        self.calls.append((region, op, *args))
        if set(args) & set(self.denied):
            raise InstallError(f"command failed (254): aws ec2 {op} ...\nAn error occurred "
                               f"(UnauthorizedOperation) when calling the {op} operation")
        if set(args) & set(self.missing):
            return {"Vpcs": [], "Subnets": [], "VpcEndpoints": []}
        if op == "describe-vpcs":
            assoc = [{"CidrBlock": "10.60.0.0/16", "CidrBlockState": {"State": "associated"}}]
            assoc += [{"CidrBlock": c, "CidrBlockState": {"State": st}} for c, st in self.secondary]
            return {"Vpcs": [{"VpcId": VPC, "CidrBlock": "10.60.0.0/16",
                              "CidrBlockAssociationSet": assoc}]}
        if op == "describe-subnets":
            return {"Subnets": [self.subnets[s] for s in args[1:]]}
        if op == "describe-vpc-endpoints":
            return {"VpcEndpoints": [self.endpoint]}
        if op == "describe-route-tables":
            return {"RouteTables": self.tables}
        if op == "describe-vpc-attribute":
            return {"VpcId": VPC, "EnableDnsSupport": {"Value": self.dns}}
        raise AssertionError(op)

def existing_doc(ingress="private", **extra) -> dict:
    """examples/aws/cluster.toml moved into the existing VPC: both nodes at
    the top of the private subnet (10.60.4.0/22)."""
    doc = aws_doc()
    for k in aws.NETWORK_CIDRS:
        del doc["aws"][k]
    doc["aws"].update({"vpc_id": VPC, "private_subnet_id": PRIV, **extra})
    if ingress == "private":
        doc["aws"]["ingress"] = "private"
        doc["acme"] = {"ca": "local"}
        doc["nodes"][0]["internal_ip"] = "10.60.7.250"
    else:
        doc["aws"].setdefault("public_subnet_id", PUB)
        doc["nodes"][0]["internal_ip"] = "10.60.0.250"
    doc["nodes"][1]["internal_ip"] = "10.60.7.251"
    return doc

def resolve(doc: dict, ec2=None) -> str:
    """existing_network then validation, as config.load runs them: errors as
    one string ("" when valid)."""
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            aws.existing_network(doc["aws"], ec2 or FakeEc2())
    except InstallError as e:
        return str(e)
    return validate(doc)

def check(doc: dict, ec2=None) -> str:
    """resolve, then network_errors as validate and apply run it."""
    ec2 = ec2 or FakeEc2()
    if err := resolve(doc, ec2):
        return err
    try:
        return "\n".join(aws.network_errors(doc["aws"], ec2))
    except InstallError as e:
        return str(e)

class ExistingNetwork(unittest.TestCase):
    def test_private_reads_the_cidrs(self):
        doc, ec2 = existing_doc(), FakeEc2()
        self.assertEqual(resolve(doc, ec2), "")
        self.assertEqual(doc["aws"]["vpc_cidr"], "10.60.0.0/16")
        self.assertEqual(doc["aws"]["private_subnet_cidr"], "10.60.4.0/22")
        self.assertNotIn("public_subnet_cidr", doc["aws"])
        # load reads only these; network_errors (validate, apply) the rest.
        self.assertEqual([c[:2] for c in ec2.calls], [("us-east-1", "describe-vpcs"),
                                                      ("us-east-1", "describe-subnets")])
        # The VPC is what the proxy admits by default; every node is in the private subnet.
        self.assertEqual(aws.ingress_cidrs(doc["aws"]), ["10.60.0.0/16"])
        self.assertEqual(config.Config(doc).vpc_cidr, "10.60.0.0/16")
        self.assertEqual(config.Config(doc).node_cidr, "10.60.4.0/22")

    def test_public_reads_both_subnets(self):
        doc = existing_doc("public")
        self.assertEqual(resolve(doc), "")
        self.assertEqual(doc["aws"]["public_subnet_cidr"], "10.60.0.0/24")
        self.assertEqual(aws.ingress_cidrs(doc["aws"]), ["0.0.0.0/0"])

    def test_refuses_cidrs(self):
        for k, v in (("vpc_cidr", "10.60.0.0/16"), ("private_subnet_cidr", "10.60.4.0/22"),
                     ("public_subnet_cidr", "10.60.0.0/24")):
            doc = existing_doc(**{k: v})
            self.assertIn(f"[aws].{k} comes from vpc_id's VPC and subnets: remove it",
                          resolve(doc), k)

    def test_same_or_overlapping_subnets_refused(self):
        err = resolve(existing_doc("public", public_subnet_id=PRIV))
        self.assertIn("[aws].public_subnet_id and private_subnet_id must be two different "
                      "subnets", err)
        self.assertNotIn("public_subnet_cidr", err)
        ec2 = FakeEc2(priv_cidr="10.60.0.0/22")
        self.assertIn("must be two different subnets", resolve(existing_doc("public"), ec2))

    def test_private_needs_only_the_private_subnet(self):
        doc = existing_doc()
        del doc["aws"]["private_subnet_id"]
        self.assertIn("[aws].vpc_id needs private_subnet_id", resolve(doc))
        self.assertIn('public_subnet_id is only for ingress = "public"',
                      resolve(existing_doc(public_subnet_id=PUB)))

    def test_needs_region_and_zone(self):
        for k in ("region", "availability_zone"):
            doc = existing_doc()
            del doc["aws"][k]
            self.assertIn(f"[aws].vpc_id needs {k}", resolve(doc), k)

    def test_public_needs_the_public_subnet(self):
        doc = existing_doc("public")
        del doc["aws"]["public_subnet_id"]
        self.assertIn('needs public_subnet_id with ingress = "public"', resolve(doc))

    def test_ids_are_strings(self):
        self.assertIn("[aws].private_subnet_id must be a quoted id string",
                      resolve(existing_doc(private_subnet_id=7)))

    def test_node_must_be_in_its_subnet(self):
        doc = existing_doc()
        doc["nodes"][1]["internal_ip"] = "10.60.8.10"
        self.assertIn("internal_ip 10.60.8.10 must be in the private subnet 10.60.4.0/22",
                      resolve(doc))
        doc = existing_doc("public")
        doc["nodes"][0]["internal_ip"] = "10.60.7.250"
        self.assertIn("internal_ip 10.60.7.250 must be in the public subnet 10.60.0.0/24",
                      resolve(doc))
        doc = existing_doc()
        doc["nodes"][1]["internal_ip"] = "10.60.7.255"
        self.assertIn("is AWS-reserved", resolve(doc))

    def test_subnet_in_another_vpc_or_zone(self):
        self.assertIn(f"private_subnet_id {PRIV} is in vpc-0other, not vpc_id {VPC}",
                      resolve(existing_doc(), FakeEc2(subnet_vpc="vpc-0other")))
        self.assertIn(f"private_subnet_id {PRIV} is in us-east-1b, not availability_zone us-east-1a",
                      resolve(existing_doc(), FakeEc2(subnet_az="us-east-1b")))

    def test_ids_need_vpc_id(self):
        doc = aws_doc()
        doc["aws"]["private_subnet_id"] = PRIV
        self.assertIn("[aws].private_subnet_id is for an existing VPC: it needs vpc_id",
                      validate(doc))

    def test_load_reads_the_vpc(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "examples", "aws",
                               "cluster.toml")) as fh:
            doc_toml = fh.read()
        for line in ('vpc_cidr            = "10.70.0.0/16"\n',
                     'public_subnet_cidr  = "10.70.0.0/24"\n',
                     'private_subnet_cidr = "10.70.1.0/24"\n'):
            doc_toml = doc_toml.replace(line, "")
        doc_toml = doc_toml.replace("[aws]\n", f'[aws]\nvpc_id = "{VPC}"\n'
                                    f'public_subnet_id = "{PUB}"\nprivate_subnet_id = "{PRIV}"\n')
        doc_toml = doc_toml.replace('"10.70.0.10"', '"10.60.0.250"').replace('"10.70.1.10"',
                                                                           '"10.60.7.251"')
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cluster.toml")
            with open(path, "w") as fh:
                fh.write(doc_toml)
            with mock.patch.object(aws, "_describe", FakeEc2()), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                cfg = config.load(path)
        self.assertEqual(cfg.vpc_cidr, "10.60.0.0/16")
        self.assertIn(f"[aws] vpc_id {VPC}: VPC 10.60.0.0/16 (resolver 10.60.0.2)", err.getvalue())

class SecondaryCidr(unittest.TestCase):
    """Only subnets in the VPC's primary CIDR: vpc_cidr, the resolver and the
    default ingress_cidrs all come from it."""
    def test_subnet_in_a_secondary_cidr_refused(self):
        ec2 = FakeEc2(priv_cidr="100.64.4.0/22", secondary=(("100.64.0.0/16", "associated"),))
        err = resolve(existing_doc(), ec2)
        self.assertIn(f"[aws].private_subnet_id {PRIV} (100.64.4.0/22) is in the VPC's secondary "
                      "CIDR 100.64.0.0/16, not its primary 10.60.0.0/16", err)
        self.assertNotIn("outside vpc_cidr", err)

    def test_secondary_cidrs_elsewhere_are_fine(self):
        ec2 = FakeEc2(secondary=(("100.64.0.0/16", "associated"),))
        self.assertEqual(resolve(existing_doc(), ec2), "")

    def test_disassociated_cidr_is_ignored(self):
        ec2 = FakeEc2(priv_cidr="100.64.4.0/22", secondary=(("100.64.0.0/16", "disassociated"),))
        err = resolve(existing_doc(), ec2)
        self.assertNotIn("secondary", err)
        self.assertIn("outside vpc_cidr", err)

class Routes(unittest.TestCase):
    """The internet routes and DNS the nodes need, checked by validate and apply."""
    def test_good_network_passes(self):
        ec2 = FakeEc2()
        self.assertEqual(check(existing_doc("public"), ec2), "")
        # Read whatever bucket_access is.
        self.assertIn("describe-route-tables", [c[1] for c in ec2.calls])
        self.assertIn(("us-east-1", "describe-vpc-attribute", "--vpc-id", VPC, "--attribute",
                       "enableDnsSupport"), ec2.calls)

    def test_private_subnet_needs_a_default_route(self):
        err = check(existing_doc(), FakeEc2(priv_routes=()))
        self.assertIn(f"[aws].private_subnet_id {PRIV}: its route table {PRIV_RTB} has no "
                      "0.0.0.0/0 route", err)

    def test_blackhole_route_does_not_count(self):
        dead = {**NAT_ROUTE, "State": "blackhole"}
        self.assertIn("has no 0.0.0.0/0 route", check(existing_doc(), FakeEc2(priv_routes=(dead,))))

    def test_any_default_route_target_does_for_private(self):
        tgw = {"DestinationCidrBlock": "0.0.0.0/0", "TransitGatewayId": "tgw-0abc",
               "State": "active"}
        self.assertEqual(check(existing_doc(), FakeEc2(priv_routes=(tgw,))), "")

    def test_unassociated_subnet_uses_the_main_table(self):
        err = check(existing_doc(), FakeEc2(priv_associated=False))
        self.assertIn(f"{PRIV}: its route table rtb-0main has no 0.0.0.0/0 route", err)

    def test_public_subnet_needs_an_internet_gateway(self):
        err = check(existing_doc("public"), FakeEc2(pub_routes=(NAT_ROUTE,)))
        self.assertIn(f"[aws].public_subnet_id {PUB}: its route table {PUB_RTB} sends 0.0.0.0/0 "
                      "to nat-0abc, not an internet gateway", err)
        err = check(existing_doc("public"), FakeEc2(pub_routes=()))
        self.assertIn(f"[aws].public_subnet_id {PUB}: its route table {PUB_RTB} needs a 0.0.0.0/0 "
                      "route to an internet gateway (igw-…)", err)
        self.assertIn("Elastic IPs", err)
        self.assertNotIn("download the release", err)

    def test_dns_support_must_be_on(self):
        self.assertIn(f"[aws].vpc_id {VPC} has DNS support (enableDnsSupport) off",
                      check(existing_doc(), FakeEc2(dns=False)))

    def test_denied_read_names_the_call(self):
        ec2 = FakeEc2(denied=("enableDnsSupport",))
        self.assertIn(f"can't read {VPC}'s DNS support (aws ec2 describe-vpc-attribute): An error "
                      "occurred (UnauthorizedOperation)", check(existing_doc(), ec2))

    def test_validate_and_apply_refuse(self):
        doc = existing_doc()
        resolve(doc)
        cfg = config.Config(doc)
        with mock.patch.object(aws, "_describe", FakeEc2(priv_routes=())), \
                self.assertRaisesRegex(InstallError, "the existing VPC can't take the cluster:\n"
                                       ".*has no 0.0.0.0/0 route"):
            main.check_existing_vpc(cfg)
        with mock.patch.object(aws, "_describe") as describe:
            main.check_existing_vpc(config.Config(aws_doc()))
        describe.assert_not_called()

    def test_cmd_validate_checks(self):
        doc = existing_doc()
        resolve(doc)
        with mock.patch.object(main.config, "load", return_value=config.Config(doc)), \
                mock.patch.object(aws, "_describe", FakeEc2(dns=False)), \
                self.assertRaisesRegex(InstallError, "enableDnsSupport"):
            main.cmd_validate(mock.Mock(cluster="cluster.toml"))

class Alloc(FakeAlloc):
    vnis = {"boxd-aws-demo-worker-0": 100}

class NodeCidr(unittest.TestCase):
    """vrack's subnet and ClickHouse's allowed network: the private subnet
    when every node is in it, else the VPC."""
    def render(self, doc):
        cfg = config.Config(doc)
        tf_out = {"nodes": {n.name: {"external_ip": "203.0.113.1", "instance_id": "i-0"}
                            for n in cfg.nodes}}
        r = render.Renderer(cfg, Alloc(), tf_out, FakeState())
        subnets = {tomllib.loads(b).get("vrack", {}).get("subnet")
                   for b in r.deploy_configs().values()} - {None}
        allowed = [ln for ln in r.group_vars().splitlines()
                   if ln.startswith("clickhouse_allowed_network:")]
        return subnets, allowed

    def test_default_is_the_vpc(self):
        self.assertEqual(self.render(aws_doc()),
                         ({"10.70.0.0/16"}, ["clickhouse_allowed_network: 10.70.0.0/16"]))

    def test_existing_private_is_the_private_subnet(self):
        doc = existing_doc()
        resolve(doc)
        doc["acme"] = aws_doc()["acme"]  # the example's ZeroSSL: no local CA to render with
        self.assertEqual(self.render(doc),
                         ({"10.60.4.0/22"}, ["clickhouse_allowed_network: 10.60.4.0/22"]))

    def test_existing_public_is_the_vpc(self):
        """The proxy, console and metrics nodes are in the public subnet."""
        doc = existing_doc("public")
        resolve(doc)
        self.assertEqual(self.render(doc),
                         ({"10.60.0.0/16"}, ["clickhouse_allowed_network: 10.60.0.0/16"]))

class Lookups(unittest.TestCase):
    def test_not_found(self):
        for key, ident, extra in (("vpc_id", VPC, {}), ("private_subnet_id", PRIV, {}),
                                  ("s3_endpoint_id", VPCE,
                                   {"bucket_access": "vpc", "s3_endpoint_id": VPCE})):
            err = check(existing_doc(**extra), FakeEc2(missing=(ident,)))
            self.assertIn(f"[aws].{key} {ident}: not found in us-east-1", err, key)

    def test_denied_or_unknown_id(self):
        err = resolve(existing_doc("public"), FakeEc2(denied=(PUB,)))
        self.assertIn(f"[aws].public_subnet_id {PUB}: AWS can't find it in us-east-1, or this "
                      "login may not describe it (An error occurred (UnauthorizedOperation)", err)

class NetworkSwitch(unittest.TestCase):
    """A cluster that exists stays in the kind of VPC it has."""
    def setUp(self):
        self.created = config.Config(aws_doc())
        doc = existing_doc()
        resolve(doc)
        self.existing = config.Config(doc)

    def test_same_mode_or_new_cluster_passes(self):
        for mode in (None, "created"):
            aws.check_network_switch(self.created, mode)
        for mode in (None, "existing"):
            aws.check_network_switch(self.existing, mode)

    def test_created_to_vpc_id_refused(self):
        with self.assertRaisesRegex(InstallError, "runs in the VPC apply made, and cluster.toml "
                                    "now sets \\[aws\\] vpc_id"):
            aws.check_network_switch(self.existing, "created")

    def test_vpc_id_to_created_refused(self):
        with self.assertRaisesRegex(InstallError, "runs in an existing VPC .* no longer sets it"):
            aws.check_network_switch(self.created, "existing")

    def test_swapping_vpc_or_subnet_refused(self):
        held = {"vpc_id": VPC, "private_subnet_id": PRIV}
        aws.check_network_switch(self.existing, "existing", held)
        for key, was in (("vpc_id", "vpc-0old"), ("private_subnet_id", "subnet-0old")):
            with self.assertRaisesRegex(InstallError, f"\\[aws\\].{key} \\S+ \\(the cluster is in "
                                        f"{was}\\).*install it as a new cluster"):
                aws.check_network_switch(self.existing, "existing", {**held, key: was})

    def test_public_subnet_swap_refused(self):
        doc = existing_doc("public")
        resolve(doc)
        cfg = config.Config(doc)
        with self.assertRaisesRegex(InstallError, f"public_subnet_id {PUB} \\(the cluster is in "
                                    "subnet-0old\\)"):
            aws.check_network_switch(cfg, "existing", {"vpc_id": VPC, "private_subnet_id": PRIV,
                                                       "public_subnet_id": "subnet-0old"})

    def test_public_subnet_coming_or_going_is_not_a_swap(self):
        """Private <-> public ingress adds or drops the public subnet."""
        aws.check_network_switch(self.existing, "existing", {
            "vpc_id": VPC, "private_subnet_id": PRIV, "public_subnet_id": PUB})
        doc = existing_doc("public")
        resolve(doc)
        aws.check_network_switch(config.Config(doc), "existing",
                                 {"vpc_id": VPC, "private_subnet_id": PRIV})

    def test_state_network_ids(self):
        def data(typ, name, ident):
            return {"mode": "data", "type": typ, "name": name,
                    "instances": [{"index_key": 0, "attributes": {"id": ident}}]}
        raw = {"resources": [data("aws_vpc", "existing", VPC), data("aws_subnet", "private", PRIV),
                             data("aws_subnet", "public", PUB),
                             {"mode": "managed", "type": "aws_subnet", "name": "private",
                              "instances": [{"index_key": 0, "attributes": {"id": "subnet-0m"}}]}]}
        self.assertEqual(tf.state_network_ids(raw), {"vpc_id": VPC, "private_subnet_id": PRIV,
                                                     "public_subnet_id": PUB})
        self.assertEqual(tf.state_network_ids({}), {})

    def test_state_network(self):
        made = {"resources": [{"mode": "managed", "type": "aws_vpc", "name": "vpc",
                               "instances": [{"index_key": 0}]}]}
        old = {"resources": [{"mode": "managed", "type": "aws_vpc", "name": "vpc",
                              "instances": [{}]}]}
        given = {"resources": [{"mode": "data", "type": "aws_vpc", "name": "existing",
                                "instances": [{"index_key": 0}]},
                               {"mode": "managed", "type": "aws_instance", "name": "node",
                                "instances": [{"index_key": "w-0"}]}]}
        self.assertEqual(tf.state_network(made), "created")
        self.assertEqual(tf.state_network(old), "created")
        self.assertEqual(tf.state_network(given), "existing")
        self.assertIsNone(tf.state_network({}))
        self.assertIsNone(tf.state_network({"resources": [
            {"mode": "managed", "type": "aws_vpc", "name": "vpc", "instances": []}]}))

class S3Endpoint(unittest.TestCase):
    def test_bucket_access_vpc_needs_the_endpoint(self):
        self.assertIn('bucket_access = "vpc" with vpc_id needs s3_endpoint_id',
                      resolve(existing_doc(bucket_access="vpc")))
        self.assertIn('s3_endpoint_id is only for bucket_access = "vpc"',
                      resolve(existing_doc(s3_endpoint_id=VPCE)))

    def test_endpoint_on_every_cluster_subnet(self):
        self.assertEqual(check(existing_doc(bucket_access="vpc", s3_endpoint_id=VPCE)), "")
        self.assertEqual(check(existing_doc("public", bucket_access="vpc",
                                            s3_endpoint_id=VPCE)), "")
        err = check(existing_doc("public", bucket_access="vpc", s3_endpoint_id=VPCE),
                    FakeEc2(endpoint_tables=(PRIV_RTB,)))
        self.assertIn(f"{VPCE} is not on {PUB}'s route table {PUB_RTB}", err)

    def test_endpoint_must_be_s3_gateway(self):
        err = check(existing_doc(bucket_access="vpc", s3_endpoint_id=VPCE),
                    FakeEc2(endpoint_service="com.amazonaws.us-east-1.dynamodb"))
        self.assertIn(f"{VPCE} must be an S3 gateway endpoint", err)

    def test_endpoint_must_be_available(self):
        for state in ("pending", "Deleting", "failed"):
            err = check(existing_doc(bucket_access="vpc", s3_endpoint_id=VPCE),
                        FakeEc2(endpoint_state=state))
            self.assertIn(f"[aws].s3_endpoint_id {VPCE} is {state}, not available", err, state)
        self.assertEqual(check(existing_doc(bucket_access="vpc", s3_endpoint_id=VPCE),
                               FakeEc2(endpoint_state="Available")), "")

    def test_not_read_by_load(self):
        """A broken endpoint doesn't stop destroy or state: load never looks."""
        ec2 = FakeEc2(endpoint_state="deleted")
        self.assertEqual(resolve(existing_doc(bucket_access="vpc", s3_endpoint_id=VPCE), ec2), "")
        self.assertNotIn("describe-vpc-endpoints", [c[1] for c in ec2.calls])

def tfvars(doc: dict) -> dict:
    doc["aws"]["ami"] = "ami-0123456789abcdef0"  # skip the SSM lookup
    return tf.build_tfvars(config.Config(doc), FakeState())

class Tfvars(unittest.TestCase):
    def test_existing_vpc_passes_ids_not_cidrs(self):
        doc = existing_doc(bucket_access="vpc", s3_endpoint_id=VPCE)
        self.assertEqual(resolve(doc), "")
        tv = tfvars(doc)
        self.assertEqual({k: tv[k] for k in aws.EXISTING_VPC_KEYS},
                         {"vpc_id": VPC, "private_subnet_id": PRIV, "public_subnet_id": None,
                          "s3_endpoint_id": VPCE})
        for k in aws.NETWORK_CIDRS:
            self.assertNotIn(k, tv)
        self.assertEqual(tv["ingress_cidrs"], ["10.60.0.0/16"])

    def test_default_unchanged(self):
        tv = tfvars(aws_doc())
        self.assertEqual({k: tv[k] for k in aws.NETWORK_CIDRS},
                         {"vpc_cidr": "10.70.0.0/16", "public_subnet_cidr": "10.70.0.0/24",
                          "private_subnet_cidr": "10.70.1.0/24"})
        for k in aws.EXISTING_VPC_KEYS:
            self.assertNotIn(k, tv)

if __name__ == "__main__":
    unittest.main()
