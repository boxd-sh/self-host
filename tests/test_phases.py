"""apply's five phases (docs/ARCHITECTURE.md): preflight, infrastructure,
render, converge, finish, in that order on every target, each handing the
next what it made. Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import inspect
import tomllib
import os
import unittest
from unittest import mock

from boxd_installer import ansible, cluster, config, main

from test_gke_config import gke_doc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def gcp_doc() -> dict:
    with open(os.path.join(ROOT, "examples", "gcp", "cluster.toml"), "rb") as fh:
        return tomllib.load(fh)

class Order(unittest.TestCase):
    def run_phases(self, apply, doc, names, preflight):
        calls = mock.Mock()
        logs = []
        patches = {n: getattr(calls, n) for n in names}
        getattr(calls, names[0]).return_value = preflight
        getattr(calls, names[1]).return_value = "tf_out"
        getattr(calls, names[2]).return_value = "renderer"
        calls._finish.return_value = 0
        with mock.patch.multiple(main, log=logs.append, **patches):
            self.assertEqual(apply(config.Config(doc), "args"), 0)
        return calls, [m.split(":")[0] for m in logs]

    def test_instances(self):
        names = ["_preflight", "_infrastructure", "_render_configs", "_converge", "_finish"]
        calls, logs = self.run_phases(main._apply, gcp_doc(), names,
                                      ("state", "key", "removals", "assets", "storage"))
        self.assertEqual([c[0] for c in calls.mock_calls], names)
        cfg = calls._preflight.call_args.args[0]
        calls._infrastructure.assert_called_once_with(cfg, "args", "state", "removals")
        calls._render_configs.assert_called_once_with(cfg, "args", "state", "tf_out", "assets")
        calls._converge.assert_called_once_with(cfg, "args", "state", "renderer", "storage",
                                                "assets")
        calls._finish.assert_called_once_with(cfg, "args", "state", "renderer", "key", "storage",
                                              "assets")
        self.assertEqual(logs, [f"phase {i}/5, {p}" for i, p in enumerate(main.PHASES, 1)])

    def test_gke(self):
        names = ["_gke_preflight", "_gke_infrastructure", "_gke_render", "_gke_deploy", "_finish"]
        calls, logs = self.run_phases(main._apply_gke, gke_doc(), names,
                                      ("state", "key", "facts", "assets", "storage"))
        self.assertEqual([c[0] for c in calls.mock_calls], names)
        cfg = calls._gke_preflight.call_args.args[0]
        calls._gke_infrastructure.assert_called_once_with(cfg, "args", "state", "facts")
        calls._gke_render.assert_called_once_with(cfg, "state", "tf_out", "assets")
        calls._gke_deploy.assert_called_once_with(cfg, "args", "state", "renderer", "storage",
                                                  "tf_out")
        self.assertEqual(calls._finish.call_args.args,
                         (cfg, "args", "state", "renderer", "key", "storage", "assets"))
        self.assertEqual(logs, [f"phase {i}/5, {p}" for i, p in enumerate(main.PHASES, 1)])

class Modules(unittest.TestCase):
    """ansible.py runs the playbook and nothing else; ssh and boxd-ctl are cluster.py."""
    def test_ansible_is_only_the_playbook(self):
        public = [n for n, f in inspect.getmembers(ansible, inspect.isfunction)
                  if f.__module__ == ansible.__name__]
        self.assertEqual(public, ["converge", "node_assets_vars"])
        self.assertNotIn("ansible-playbook", inspect.getsource(cluster))

    def test_converge_takes_the_keys_dir(self):
        cfg = mock.Mock(env_group="prod", assets_version="a" * 40)
        with mock.patch.object(ansible, "run") as run, \
             mock.patch.object(ansible, "node_assets_vars", return_value={}), \
             mock.patch.object(ansible.os, "symlink"), mock.patch.object(ansible.os, "remove"), \
             mock.patch.object(ansible.os, "open", return_value=3), \
             mock.patch.object(ansible.os, "fdopen", mock.mock_open()):
            ansible.converge(cfg, "/out", "/assets", "/state/keys")
        self.assertIn("boxd_tls_local_dir=/state/keys", run.call_args.args[0])

if __name__ == "__main__":
    unittest.main()
