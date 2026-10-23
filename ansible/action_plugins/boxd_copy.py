"""`copy` for assets files, minus the trip through the operator's ssh tunnel.

When the node has its own verified copy of the assets bundle
(boxd-common/tasks/fetch-assets.yml set `boxd_assets_on_node`) and `src` is
one of the bundle's files (`boxd_asset_hashes`), this copies it from there,
on the node (copy with remote_src), checksum-compared just like a normal
copy. The SSM tunnel moves ~0.26 MB/s, and a worker gets ~250 MB of
binaries and kernel. Anything else, or a node-side copy that fails, goes
through the ordinary copy action. Takes copy's arguments."""
import os

from ansible.plugins.action import ActionBase
from ansible.utils.display import Display

display = Display()

class ActionModule(ActionBase):

    def run(self, tmp=None, task_vars=None):
        task_vars = task_vars or {}
        src = self._task.args.get("src")
        on_node = self._on_node(src, task_vars) if src else None
        if on_node:
            result = self._copy(task_vars, src=on_node, remote_src=True)
            if not result.get("failed"):
                return result
            display.warning(f"{os.path.basename(src)}: copy from the node's assets failed "
                            f"({result.get('msg', '?')}); copying over ssh instead")
        return self._copy(task_vars)

    def _on_node(self, src, task_vars):
        """The node-side path of `src`, or None."""
        tv = lambda name, default: self._templar.template(task_vars.get(name, default))
        if not tv("boxd_assets_on_node", False):
            return None
        local = os.path.realpath(tv("boxd_assets_dir", ""))
        rel = os.path.relpath(os.path.realpath(src), local)
        if rel not in (tv("boxd_asset_hashes", None) or {}):
            return None
        return f"{tv('boxd_node_assets_root', '')}/{tv('boxd_assets_sha', '')}/{rel}"

    def _copy(self, task_vars, **override):
        task = self._task.copy()
        task.args.update(override)
        copy = self._shared_loader_obj.action_loader.get(
            "ansible.legacy.copy", task=task, connection=self._connection,
            play_context=self._play_context, loader=self._loader,
            templar=self._templar, shared_loader_obj=self._shared_loader_obj)
        return copy.run(task_vars=task_vars)
