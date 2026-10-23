"""[templates].sizes: validation, the terraform tfvar, and pushing the list to
the cluster after seeding. Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import contextlib
import io
import os
import subprocess
import tomllib
import unittest
from unittest import mock

from boxd_installer import ansible, config, render, tf
from boxd_installer.util import InstallError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT = [{"vcpu": 1, "memory_gb": 4}, {"vcpu": 2, "memory_gb": 8},
           {"vcpu": 4, "memory_gb": 16}]
CUSTOM = DEFAULT + [{"vcpu": 1, "memory_gb": 32}]

def aws_doc(templates=None) -> dict:
    with open(os.path.join(ROOT, "examples", "aws", "cluster.toml"), "rb") as fh:
        doc = tomllib.load(fh)
    if templates is not None:
        doc["templates"] = templates
    return doc

def validate(doc: dict) -> str:
    """Validation errors as one string ("" when valid); warnings go to stderr."""
    try:
        config._validate(doc)
    except InstallError as e:
        return str(e)
    return ""

class TemplatesValidation(unittest.TestCase):
    def test_absent_is_valid(self):
        self.assertEqual(validate(aws_doc()), "")

    def test_custom_list_is_valid(self):
        self.assertEqual(validate(aws_doc({"sizes": CUSTOM, "image": "computer", "tag": "v1"})), "")

    def test_empty_or_not_a_list(self):
        for sizes in ([], "1x4", {"vcpu": 1, "memory_gb": 4}):
            self.assertIn("non-empty list", validate(aws_doc({"sizes": sizes})), sizes)

    def test_entries_need_positive_ints(self):
        for entry, field in (({"vcpu": 0, "memory_gb": 4}, "vcpu"),
                             ({"vcpu": 1, "memory_gb": -4}, "memory_gb"),
                             ({"vcpu": 1.5, "memory_gb": 4}, "vcpu"),
                             ({"vcpu": True, "memory_gb": 4}, "vcpu"),
                             ({"vcpu": 1, "memory_gb": "32G"}, "memory_gb"),
                             ({"vcpu": 1}, "memory_gb")):
            err = validate(aws_doc({"sizes": [entry]}))
            self.assertIn(f"sizes[0].{field} must be a positive integer", err, entry)

    def test_entry_must_be_table_without_extra_keys(self):
        self.assertIn("must be a { vcpu", validate(aws_doc({"sizes": [4]})))
        err = validate(aws_doc({"sizes": [{"vcpu": 1, "memory_gb": 4, "memory": 4}]}))
        self.assertIn("unknown key(s) memory", err)

    def test_duplicates(self):
        err = validate(aws_doc({"sizes": DEFAULT + [{"vcpu": 2, "memory_gb": 8}]}))
        self.assertIn("sizes[3]: duplicate size 2x8", err)

    def test_image_and_tag_are_strings(self):
        self.assertIn("[templates].image must be a non-empty string",
                      validate(aws_doc({"image": 3})))
        self.assertIn("[templates].tag must be a non-empty string",
                      validate(aws_doc({"tag": ""})))

    def test_unknown_key(self):
        self.assertIn("[templates].size: unknown key", validate(aws_doc({"size": DEFAULT})))

class FakeAlloc:
    def node_id(self, name, role): return 1
    def peers(self): return []
    def learners(self): return []
    def bridge_subnet(self, name): return "10.100.1.0/24"

class FakeState:
    keys_dir = "/nonexistent"
    data: dict = {}
    def secrets_kek(self): return "kek"
    def jwt_secret(self): return "jwt"
    def deploy_pubkey(self): return "ssh-ed25519 AAAA"
    def acme_eab(self, ca, project, email): return {"kid": "k", "hmac": "h"}
    def clickhouse_password(self): return "pw"

class RenderMachineSizes(unittest.TestCase):
    """Sizes are cluster state (`boxd-ctl machine-sizes set`, see
    SeedTemplates), not node config: no rendered service file carries them."""
    def test_no_service_config_carries_sizes(self):
        cfg = config.Config(aws_doc({"sizes": CUSTOM}))
        tf_out = {"nodes": {n.name: {"external_ip": "203.0.113.1"} for n in cfg.nodes}}
        files = render.Renderer(cfg, FakeAlloc(), tf_out, FakeState()).deploy_configs()
        for path, body in files.items():
            self.assertNotIn("machine_sizes", tomllib.loads(body), path)

    def test_image_and_tag_are_not_part_of_a_size(self):
        cfg = config.Config(aws_doc({"sizes": CUSTOM, "image": "computer"}))
        self.assertEqual(cfg.machine_sizes(), CUSTOM)

class FakeCtl:
    """boxd-ctl on the control node: records every command; `template
    create` registers a template that reports ready on the next list."""
    def __init__(self, machine_sizes_rc=0, machine_sizes_err=""):
        self.commands: list[str] = []
        self.templates: dict[str, str] = {"tpl-old": "computer-2x8-old-00000000"}
        self.rc, self.err = machine_sizes_rc, machine_sizes_err

    def __call__(self, cfg, command, check=True, quiet=False):
        self.commands.append(command)
        if command.startswith("template create"):
            name = command.split("--name '")[1].rstrip("'")
            self.templates[f"tpl-{len(self.templates)}"] = name
        if command.startswith("template delete"):
            self.templates.pop(command.split()[-1], None)
        out = ""
        if command == "template list":
            out = "".join(f"{tid}  {name}  ready\n" for tid, name in self.templates.items())
        rc = self.rc if command.startswith("machine-sizes") else 0
        err = self.err if command.startswith("machine-sizes") else ""
        return subprocess.CompletedProcess(command, rc, out, err)

class SeedTemplates(unittest.TestCase):
    def seed(self, fake: FakeCtl, templates=None):
        cfg = config.Config(aws_doc(templates))
        with mock.patch.object(ansible, "ctl", fake), mock.patch.object(ansible.time, "sleep"), \
                contextlib.redirect_stderr(io.StringIO()) as log:
            ansible.seed_templates(cfg, "computer", "v1", "abcdef0123456789")
        return log.getvalue()

    def test_sizes_are_set_after_templates_are_ready_and_before_cleanup(self):
        fake = FakeCtl()
        self.seed(fake, {"sizes": CUSTOM})
        cmds = fake.commands
        sizes_at = cmds.index("machine-sizes set 1x4 2x8 4x16 1x32")
        last_default = max(i for i, c in enumerate(cmds) if c.startswith("template set-default"))
        first_delete = cmds.index("template delete tpl-old")
        self.assertLess(last_default, sizes_at)
        self.assertLess(sizes_at, first_delete)

    def test_default_sizes_are_set_too(self):
        fake = FakeCtl()
        self.seed(fake)
        self.assertIn("machine-sizes set 1x4 2x8 4x16", fake.commands)

    def test_old_ctl_without_the_command_only_warns(self):
        fake = FakeCtl(2, "error: unrecognized subcommand 'machine-sizes'")
        self.assertIn("no `machine-sizes`", self.seed(fake))
        self.assertIn("template delete tpl-old", fake.commands)

    def test_a_refused_list_fails_the_apply(self):
        fake = FakeCtl(1, "Error: status: InvalidArgument, message: \"duplicate machine size 1x4\"")
        with self.assertRaisesRegex(InstallError, "duplicate machine size 1x4"):
            self.seed(fake)

class ConfigureOrgs(unittest.TestCase):
    ORGS = ("ORG ID  NAME  SLUG  OWNER  MEMBERS  ADMINS  VMS  AUTOHIB\n"
            "org-a  Acme Inc  acme  u1  3  1  2  default\n"
            "org-b  Beta  beta  u2  1  1  0  never\n")

    def run_it(self, orgs: str, comped: str) -> list[str]:
        commands = []
        def fake(cfg, command, check=True, quiet=False):
            commands.append(command)
            out = {"org list": orgs, "org comp list": comped}.get(command, "")
            return subprocess.CompletedProcess(command, 0, out, "")
        with mock.patch.object(ansible, "ctl", fake), contextlib.redirect_stderr(io.StringIO()):
            ansible.configure_orgs(config.Config(aws_doc()), CUSTOM)
        return commands

    def test_every_org_gets_the_largest_cpu_and_memory_and_is_comped(self):
        cmds = self.run_it(self.ORGS, "No comped orgs.\n")
        self.assertEqual(cmds[2:], ["org set-quota org-a --cpu 4 --mem 32G", "org comp add org-a",
                                    "org set-quota org-b --cpu 4 --mem 32G", "org comp add org-b"])

    def test_already_comped_orgs_keep_their_comp(self):
        cmds = self.run_it(self.ORGS, "org-a  Acme Inc  owner=u1  (enterprise)\n")
        self.assertNotIn("org comp add org-a", cmds)
        self.assertIn("org comp add org-b", cmds)

    def test_no_orgs_is_a_no_op(self):
        self.assertEqual(self.run_it("no orgs\n", "No comped orgs.\n"), ["org list", "org comp list"])

    def test_the_system_org_is_named_as_such(self):
        # A fresh cluster: boxd's own org only, which nobody signed in to make.
        orgs = ("ORG ID  NAME  SLUG  OWNER  MEMBERS  ADMINS  VMS  AUTOHIB\n"
                "org-s  default  default-system  system  0  0  0  default\n")
        with mock.patch.object(ansible, "log") as log:
            cmds = self.run_it(orgs, "No comped orgs.\n")
        self.assertIn("org comp add org-s", cmds)
        lines = [c.args[0] for c in log.call_args_list]
        self.assertIn("comped org org-s (boxd's system org)", lines)
        self.assertTrue(lines[-1].endswith("(0 from sign-ins, plus boxd's system org)"))

class TerraformMachineSizes(unittest.TestCase):
    """The fit check against real worker specs lives in terraform
    (terraform/<cloud>/machine_sizes.tf, tested by its *.tftest.hcl); here
    only that the list reaches it."""
    def tfvars(self, templates=None) -> dict:
        doc = aws_doc(templates)
        doc["aws"]["ami"] = "ami-0123456789abcdef0"  # skip the SSM lookup
        return tf.build_tfvars(config.Config(doc), FakeState())

    def test_default_list(self):
        self.assertEqual(self.tfvars()["machine_sizes"], DEFAULT)

    def test_custom_list(self):
        self.assertEqual(self.tfvars({"sizes": CUSTOM})["machine_sizes"], CUSTOM)

if __name__ == "__main__":
    unittest.main()
