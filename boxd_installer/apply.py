"""apply, in its five phases, and plan: the same steps up to terraform's plan
(docs/ARCHITECTURE.md). `run` is apply on every target; the checks the phases
make are at the end. The commands themselves are main.py."""
import os
import shutil
import sys

from . import (ansible, assets, clouds, cluster, composio, config, control_carry, license,
               private_services, remote_state, render, storage_engine, telemetry, tf, windows)
from .state import Allocation, State, generate_egress_ca
from .util import InstallError, log

# apply's five phases, in order, on every target (docs/ARCHITECTURE.md).
PHASES = ("preflight", "infrastructure", "render", "converge", "finish")

def _phase(name: str, what: str) -> None:
    log(f"phase {PHASES.index(name) + 1}/{len(PHASES)}, {name}: {what}")

def run(cfg, args) -> int:
    """apply, with the installer state already pulled (main.cmd_apply)."""
    if clouds.kubernetes(cfg):
        return _apply_gke(cfg, args)
    return _apply(cfg, args)

def _apply(cfg, args) -> int:
    """apply on instances (GCP, AWS) and on bare metal: the five phases, one
    function each. Nothing in the cloud or on a node changes before
    infrastructure."""
    _phase("preflight", "checks, before anything changes")
    state, key, removals, assets_root, storage = _preflight(cfg, args)
    _phase("infrastructure", "terraform" if clouds.provisions(cfg)
           else "the hosts exist, so there is nothing to create")
    tf_out = _infrastructure(cfg, args, state, removals)
    _phase("render", "every node's config")
    r = _render_configs(cfg, args, state, tf_out, assets_root)
    _phase("converge", "ansible on every node, then the nodes join the cluster")
    _converge(cfg, args, state, r, storage, assets_root)
    _phase("finish", "settings on the running cluster")
    return _finish(cfg, args, state, r, key, storage, assets_root)

def _preflight(cfg, args):
    """Phase 1: every check that needs no node and no terraform apply, the
    keys, and the assets bundle. Returns (state, license key path, the removals
    confirmed, assets root, storage guard) for the later phases."""
    # Before anything else: a [license].key_file that is missing or not a
    # key stops the apply; it never falls back to the free tier.
    key = license.key_path(cfg)
    if errs := telemetry.errors(cfg) + windows.errors(cfg):
        raise InstallError("; ".join(errs))
    check_existing_vpc(cfg)
    state = State(args.state_dir)
    # Before terraform, which would delete a dropped node's instance. On a
    # cloud, only the dropped nodes that have one are asked about.
    removals = check_removed_members(
        state, cfg, auto_approve=args.auto_approve,
        existing=(lambda: instance_names(cfg)) if clouds.provisions(cfg) else None)
    _check_oidc(cfg, state)
    state.ensure_keypairs()
    cluster.set_keys_dir(state.keys_dir)
    os.makedirs(args.out_dir, exist_ok=True)
    if not clouds.provisions(cfg):
        # Terraform puts the deploy key on cloud instances; on metal the
        # operator does, so this stops a first apply until they have.
        cluster.check_hosts(cfg, state.deploy_pubkey())

    assets_root = assets.ensure(cfg, args.cache_dir, state)
    check_storage_bundle(cfg, assets_root)
    check_relay_bundle(cfg, assets_root)
    windows.check_bundle(cfg, assets_root)
    check_private_service_bridges(cfg, state)
    # Before terraform: once the storage catalog is on, the engine and its
    # bucket are one-way (storage_engine.check_guard).
    storage = storage_engine.check_guard(cfg, state, assets_root)
    assets.console_tarball(assets_root)
    return state, key, removals, assets_root, storage

def _infrastructure(cfg, args, state, removals) -> dict:
    """Phase 2: terraform makes the network, the instances and the buckets
    (terraform/<cloud>). Returns its outputs."""
    # metal: the hosts exist, so there is nothing to plan or replace.
    if clouds.provisions(cfg):
        path = _plan_instances(cfg, state, args.out_dir)
        # Before the apply: a replaced control node takes its state with it.
        control_carry.save(cfg, state, control_carry.plan_json(cfg, path))
        tf.apply_plan(cfg, path, before=lambda: _checkpoint(cfg, args))
    # The removed nodes' instances are gone now (on metal, out of the config).
    retire_removed(state, removals)
    tf_out = tf.read_outputs(cfg, state)
    cluster.set_tf_outputs(tf_out)
    return tf_out

def _render_configs(cfg, args, state, tf_out: dict, assets_root: str) -> render.Renderer:
    """Phase 3: every node's service configs and the ansible inventory, from
    cluster.toml, the providers config, the installer state and terraform's
    outputs, written under the out dir (render.py)."""
    cfg.apply_overlay(state.load_providers())
    # The sign-in clients live in the providers overlay, not cfg.doc.
    if inert := config.inert_sign_in({t: cfg.table(t) for t in ("oidc", "google", "github")
                                      if cfg.table(t)}):
        log(f"warning: {inert}")
    for warning in (config.network_warnings(cfg) + config.data_disk_warnings(cfg)
                    + config.storage_bucket_warnings(cfg) + windows.registry_warnings(cfg)):
        log(f"warning: {warning}")
    composio.sync_auth_configs(cfg, state, assets_root)
    ensure_egress_ca(cfg, state)
    storage_engine.adopt_prefix(cfg, state)
    alloc = state.allocate(cfg)
    r = render.Renderer(cfg, alloc, tf_out, state, assets_root=assets_root)
    write_out(r.render_all(), args.out_dir)
    return r

def _converge(cfg, args, state, r, storage, assets_root: str) -> None:
    """Phase 4: the last checks, which need the nodes (cluster.py), then
    ansible installs the bundle and the rendered configs on every node
    (ansible.py), then the nodes join the cluster."""
    cluster.preflight(cfg)
    check_network_mode(cfg, state)
    storage_engine.check_identity(cfg, storage, r)
    if not clouds.provisions(cfg):
        for warning in license.free_tier_warnings(cfg, counted=cluster.worker_vcpus(cfg)):
            log(f"warning: {warning}")
    # Last before the converge, so a check that refuses the apply leaves them on.
    if bundle_changes(state, cfg):
        cluster.pause_machine_specs(cfg)
    ansible.converge(cfg, args.out_dir, assets_root, state.keys_dir,
                     assets.host_kernel_release(assets_root),
                     control_carry.carry_dir(state))

    cluster.wait_for_leader(cfg)
    # A node added to cluster.toml joins a running cluster only through the
    # leader's sync-config (a no-op on a fresh cluster and on a re-apply).
    cluster.sync_config(cfg)
    expected = len(cfg.nodes_with("worker"))
    gave_up = cluster.learners_gave_up(cfg)
    restarted = gave_up or (cluster._registered_nodes(cfg) or 0) < expected
    if restarted:
        if gave_up:
            # Expected on a fresh cluster: they started before the raft
            # leader existed. The restart below fixes it.
            log(f"restarting services that gave up registering (they started before "
                f"the raft leader): {', '.join(gave_up)}")
        cluster.restart_learners(cfg)
    cluster.verify_registration(cfg, expected=expected)
    if restarted:
        log("after the restart, every node registered")
    control_carry.clear(state)

def _finish(cfg, args, state, r, key, storage, assets_root: str,
            image_override=None) -> int:
    """Phase 5, on every target, once every node runs the new bundle and every
    worker registered: the bucket, the cluster id, machine specs, the license,
    the storage switches, telemetry, templates and orgs (all boxd-ctl, through
    cluster.py), then the state record and the DNS delegation."""
    # Before any switch: a worker that can't open its bucket fails the apply.
    storage_engine.wait_bucket_open(cfg)

    # Every node (control, proxy, console) runs the new bundle now, so the
    # cluster ID can be made (once; a no-op after). Before the key: a bound
    # key needs it.
    cluster.init_cluster_id(cfg)
    # For the same reason: an older node would skip their entries for good.
    cluster.turn_on_machine_specs(cfg)
    # Before the templates, so the key is in place before any machine is made.
    if key:
        cluster.install_license(cfg, key)
    # After the key: a bucket default needs its bucket-storage feature. The
    # guard is pushed to the state bucket the moment the catalog is on.
    storage_engine.set_switches(
        cfg, state, storage, r,
        push=lambda: remote_state.push(cfg, args.state_dir, delete_local=False))
    value, source = telemetry.setting(cfg)
    if value is None:
        log("telemetry: [telemetry] enabled is not set; the cluster's setting is left as it is")
    else:
        cluster.set_telemetry(cfg, value, explicit=source == telemetry.CLUSTER_TOML)
    # Every worker runs the new release and has the firmware now ([windows]).
    windows.configure(cfg, r.alloc)

    image, tag = assets.computer_image(assets_root)
    if image_override:
        image = image_override(image)
    cluster.seed_templates(cfg, image, tag, cfg.assets_version,
                           storage=storage_engine.template_storage(cfg))
    cluster.configure_orgs(cfg, cfg.machine_sizes())

    # The exact bundle this cluster now runs — with a channel, the only
    # record of what to pin to come back here (docs/REFERENCE.md "Upgrades and rollback").
    state.data["applied_assets"] = {"sha": cfg.assets_version, "channel": cfg.assets_channel}
    state.data["network_mode"] = cfg.network_mode
    state.save()

    print()
    print(render.glue_records(cfg, r))
    after = "the forwarder is in place" if cfg.ingress == "private" else "glue records propagate"
    print(f"\nConsole: https://app.{cfg.zone}  (after {after})")
    print(f"Assets: {cfg.assets_version}" + (f" (channel {cfg.assets_channel})" if cfg.assets_channel else ""))
    if cfg.acme_ca == "gts" and cluster.acme_eab_spent(cfg):
        raise InstallError(
            "boxd-control can't register its ACME account: Google Trust Services says the "
            "EAB key was already used, so no certificate will ever be issued. Mint a new "
            "key and re-apply (docs/REFERENCE.md, Troubleshooting, \"Unknown external account binding\"):\n"
            "  ./boxd-install state reset-eab\n  ./boxd-install apply")
    return 0

def _apply_gke(cfg, args) -> int:
    """apply on GKE ([gke]), in the same five phases: terraform/gke for the
    cloud side, and the converge is Kubernetes objects (kube.deploy), not
    ansible. An upgrade is an apply with a new assets_version: control,
    console and the edges roll; workers are updated in place."""
    from .clouds import gke
    _phase("preflight", "checks, before anything changes")
    state, key, facts, assets_root, storage = _gke_preflight(cfg, args)
    _phase("infrastructure", "terraform")
    tf_out = _gke_infrastructure(cfg, args, state, facts)
    _phase("render", "the cluster's config")
    r = _gke_render(cfg, state, tf_out, assets_root)
    _phase("converge", "the Kubernetes objects, then the pods join the cluster")
    _gke_deploy(cfg, args, state, r, storage, tf_out)
    _phase("finish", "settings on the running cluster")
    return _finish(cfg, args, state, r, key, storage, assets_root,
                   image_override=lambda image: gke.computer_image(cfg.doc, image))

def _gke_preflight(cfg, args):
    """Phase 1 on GKE. Returns (state, license key path, live_checks' facts,
    assets root, storage guard)."""
    key = license.key_path(cfg)
    state = State(args.state_dir)
    facts = _gke_checks(cfg, state)
    state.ensure_keypairs()
    cluster.set_keys_dir(state.keys_dir)
    os.makedirs(args.out_dir, exist_ok=True)

    assets_root = assets.ensure(cfg, args.cache_dir, state)
    check_storage_bundle(cfg, assets_root)
    # The guard asks a cluster that exists through the previous apply's outputs.
    storage = storage_engine.check_guard(cfg, state, assets_root)
    return state, key, facts, assets_root, storage

def _gke_infrastructure(cfg, args, state, facts: dict) -> dict:
    """Phase 2 on GKE: terraform/gke makes (or adopts) the GKE cluster, its
    node pools and the buckets. Returns its outputs, with kubectl connected."""
    from . import kube
    path = _plan_gke(cfg, state, args.out_dir, facts)
    tf.apply_plan(cfg, path, before=lambda: _checkpoint(cfg, args))
    tf_out = tf.read_outputs(cfg, state)
    cluster.set_tf_outputs(tf_out)
    kube.connect(cfg, tf_out)
    return tf_out

def _gke_render(cfg, state, tf_out: dict, assets_root: str):
    """Phase 3 on GKE: the renderer of the boxd-config Secret and every
    Kubernetes object (kube_render.py); kube.deploy applies them."""
    from . import kube, kube_render
    cfg.apply_overlay(state.load_providers())
    if inert := config.inert_sign_in({t: cfg.table(t) for t in ("oidc", "google", "github")
                                      if cfg.table(t)}):
        log(f"warning: {inert}")
    composio.sync_auth_configs(cfg, state, assets_root)
    ensure_egress_ca(cfg, state)
    storage_engine.adopt_prefix(cfg, state)
    alloc = state.allocate(cfg)
    return kube_render.KubeRenderer(cfg, alloc, tf_out, state, assets_root=assets_root,
                                    pull_config=kube.pull_config(cfg))

def _gke_deploy(cfg, args, state, r, storage, tf_out: dict) -> None:
    """Phase 4 on GKE: the Kubernetes objects, in order, until every pod runs
    the new bundle and every worker registered (kube.py)."""
    from . import kube
    from .clouds import gke
    storage_engine.check_identity(cfg, storage, r)
    if state.data.get("applied_assets") and bundle_changes(state, cfg):
        cluster.pause_machine_specs(cfg)
    kube.deploy(cfg, r, state, args.out_dir, tf_out)
    gke.record(cfg, state)

def plan(cfg, args) -> int:
    """What the next apply's terraform would change, changing nothing: apply's
    checks before terraform, the tfvars it would write, and `terraform plan`
    on them (GCP, AWS and GKE; bare metal has no terraform). The installer
    state is read from the bucket and deleted after, never pushed back;
    terraform state is only read. Checks that need the assets bundle are
    apply's alone. With --out-dir, terraform.tfvars.json and tf.plan stay."""
    if not clouds.provisions(cfg):
        raise InstallError("bare metal has no terraform to plan: the hosts exist. "
                           "`./boxd-install render` writes the configs an apply installs.")
    on_gke = clouds.kubernetes(cfg)
    license.key_path(cfg)
    if not on_gke:
        if errs := telemetry.errors(cfg) + windows.errors(cfg):
            raise InstallError("; ".join(errs))
        check_existing_vpc(cfg)
    remote_state.pull(cfg, args.state_dir)
    try:
        state = State(args.state_dir)
        if on_gke:
            facts = _gke_checks(cfg, state)
        else:
            # apply asks here; a plan only says what the apply would ask about.
            if removed := state.removed_members(cfg):
                live, gone = split_removed(removed, lambda: instance_names(cfg))
                if gone:
                    log(_gone_note(gone))
                if live:
                    log(f"warning: {_removal_warning(cfg, live)}")
            _check_oidc(cfg, state)
        fresh = not os.path.exists(os.path.join(state.keys_dir, "boxd-deploy.pub"))
        # On a fresh cluster these are thrown away with the rest of this
        # state; apply makes the real ones.
        state.ensure_keypairs()
        if fresh and not on_gke:
            log("no installer state yet (a fresh cluster): the plan's deploy SSH key is a "
                "placeholder")
        os.makedirs(args.out_dir, exist_ok=True)
        if on_gke:
            _plan_gke(cfg, state, args.out_dir, facts)
        else:
            check_private_service_bridges(cfg, state)
            path = _plan_instances(cfg, state, args.out_dir)
            control = {n.name for n in cfg.nodes_with("control")}
            if replaced := [n for n in
                            control_carry.replaced_nodes(control_carry.plan_json(cfg, path))
                            if n in control]:
                log(f"note: this plan replaces control node(s) {', '.join(replaced)}; apply "
                    "carries their state over to the new instances")
        # terraform ends with a `terraform apply "<plan file>"` hint. That file
        # is in the out dir, which is deleted after unless --out-dir was given.
        log("nothing was changed. To make these changes run `./boxd-install apply`, which "
            "plans again; terraform's own `terraform apply` hint above does not apply here "
            "(the plan file is kept only with --out-dir)")
    finally:
        shutil.rmtree(args.state_dir, ignore_errors=True)
    return 0

def _check_oidc(cfg, state) -> None:
    """The render would refuse a half-configured [oidc] only after terraform."""
    oidc_table = {**cfg.doc.get("oidc", {}), **state.load_providers().get("oidc", {})}
    if oidc_table and (why := render.oidc_render_error(oidc_table)):
        raise InstallError(why)

def _plan_instances(cfg, state, out_dir: str) -> str:
    """terraform/<cloud> on GCP or AWS: init, the checks against what exists,
    the tfvars, and a saved plan (its path), for apply and plan alike."""
    tf.init(cfg)
    # A worker that exists keeps its type: the license never resizes one.
    existing = tf.instance_types(cfg)
    # A cluster that exists stays in the kind of VPC it has.
    if cfg.cloud_name == "aws":
        clouds.CLOUDS["aws"].check_network_switch(cfg, *tf.network(cfg))
    # A worker that exists keeps its kind of data disk (EBS or instance store).
    if cfg.cloud_name == "aws" and any(n.name in existing for n in cfg.nodes_with("worker")):
        clouds.CLOUDS["aws"].check_data_disk_switch(cfg, set(existing), tf.data_volumes(cfg))
    for warning in license.free_tier_warnings(cfg, existing):
        log(f"warning: {warning}")
    tfvars = tf.write_tfvars(cfg, state, out_dir, existing)
    path = os.path.join(out_dir, "tf.plan")
    tf.plan(cfg, tfvars, path)
    return path

def _gke_checks(cfg, state) -> dict:
    """[gke], before terraform: the license and telemetry, [oidc], and what
    the project and the state say can't change. Returns live_checks' facts."""
    from .clouds import gke
    if errs := telemetry.errors(cfg) + gke.license_errors(cfg):
        raise InstallError("; ".join(errs))
    _check_oidc(cfg, state)
    errs, warns, facts = gke.live_checks(cfg)
    errs += gke.state_errors(cfg, state)
    for warning in gke.warnings(cfg) + warns:
        log(f"warning: {warning}")
    if errs:
        raise InstallError("\n  ".join(["this apply can't go on:"] + errs))
    return facts

def _plan_gke(cfg, state, out_dir: str, facts: dict) -> str:
    """terraform/gke: init, the tfvars and a saved plan (its path), refused
    when it would replace the cluster or recreate worker nodes."""
    import json
    from . import kube
    from .clouds import gke
    tf.init(cfg)
    # The pool never shrinks under a running worker: kube.deploy drains and
    # removes workers (and their nodes) itself.
    live = kube.live_worker_replicas(cfg, state)
    tfvars = gke.write_tfvars(cfg, state, out_dir, live, facts)
    path = os.path.join(out_dir, "tf.plan")
    tf.plan(cfg, tfvars, path)
    # Every plan, the first included: a state that was lost looks like a first apply, and its
    # plan is the one that would replace what the cluster already has.
    if problems := gke.plan_problems(json.loads(tf.show_json(cfg, path))):
        raise InstallError("\n  ".join(["refusing the terraform plan:"] + problems))
    return path

def _checkpoint(cfg, args) -> None:
    """Push the state before terraform creates anything from it: the values made for this
    cluster on its first apply (the KMS key ring's suffix, keys, the egress CA) must be in
    the state bucket before resources carry them, or a run cut short makes them anew on the
    next apply and terraform replaces what the first one created."""
    remote_state.push(cfg, args.state_dir, delete_local=False)

def write_out(files: dict[str, str], out_dir: str) -> None:
    for rel, content in sorted(files.items()):
        path = os.path.join(out_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(content)
    log(f"wrote {len(files)} files under {out_dir}/")

def check_existing_vpc(cfg) -> None:
    """[aws] vpc_id: the routes, DNS and S3 endpoint the nodes need (validate
    and apply only, not every command that loads cluster.toml)."""
    if cfg.cloud_name == "aws" and "vpc_id" in cfg.cloud:
        if errs := clouds.CLOUDS["aws"].network_errors(cfg.cloud):
            raise InstallError("the existing VPC can't take the cluster:\n  " + "\n  ".join(errs))

def _removal_steps(role: str, node_id: int) -> list[str]:
    """What taking one Raft member out does to the cluster, and what must be
    run before the apply that drops it (check_removed_members)."""
    if role == "worker":
        wid = f"worker-{node_id}"
        return [f"worker {wid}: every machine on it goes with it. Move them off and retire it "
                "first:",
                f"    boxd-ctl node drain {wid}",
                f"    boxd-ctl node mark-dead {wid}",
                f"    boxd-ctl remove-peer {node_id}      (on the leader)"]
    if role == "proxy":
        return [f"proxy proxy-{node_id}: every machine placed on it resolves to it and has its "
                "SSH port there. boxd can't move machines to another proxy yet, so they become "
                "unreachable, and new machines keep landing on it. There is no procedure for "
                "this yet: put the node back and ask boxd."]
    if role == "dns":
        return ["nameserver: it is in your zone's delegation. Take its NS record and its glue "
                "(A) record out at your DNS provider and wait out their TTL first, then:",
                f"    boxd-ctl remove-peer {node_id}      (on the leader)"]
    return [f"control (Raft voter {node_id}): if the voters left lose their quorum, the whole "
            "cluster stops. Ask boxd first; the voter leaves with:",
            f"    boxd-ctl remove-peer {node_id}      (on the leader)"]

def check_removed_members(state, cfg, ask=None, auto_approve: bool = False,
                          existing=None) -> dict[str, dict[str, int]]:
    """An apply whose cluster.toml has dropped a Raft member (a node, or one
    of its roles) takes it out of the cluster, and on a cloud terraform
    deletes its instance. Nothing in the cluster forgets it on its own, so
    say what that breaks and what to run first, and go on only on a yes.
    Returns every removal, for retire_removed. `ask` is the prompt
    (input() at a terminal); without a terminal the apply stops, unless
    --auto-approve answered yes up front (the warning still goes to the log).

    `existing` (cloud targets) is called for the names of the nodes terraform
    has an instance for. A dropped node with none is not asked about: there
    is no instance to delete and no node to drain. That is every node after
    a destroy, whose state keeps the ids, when the cluster comes back with
    fewer nodes. Its ids are retired like the others'."""
    removed = state.removed_members(cfg)
    if not removed:
        return {}
    live, gone = split_removed(removed, existing)
    if gone:
        log(_gone_note(gone))
    if not live:
        return removed
    warning = _removal_warning(cfg, live)
    if auto_approve:
        print(f"warning: {warning}\n--auto-approve: taking them out", file=sys.stderr)
        return removed
    if ask is None:
        if not sys.stdin.isatty():
            raise InstallError(warning + "\nThis apply needs a yes at a terminal, or "
                               "--auto-approve; nothing was changed.")
        ask = input
    print(f"warning: {warning}", file=sys.stderr)
    answer = ask("Have you run these steps, and should this apply take them out? [y/N] ")
    if answer.strip().lower() not in ("y", "yes"):
        raise InstallError("apply stopped; nothing was changed")
    return removed

def instance_names(cfg) -> set[str]:
    """The nodes terraform has an instance for; none once a cluster is destroyed."""
    tf.init(cfg)
    return set(tf.instance_types(cfg))

def split_removed(removed: dict[str, dict[str, int]], existing) -> tuple[dict, dict]:
    """(the removals whose node has an instance, the ones whose node has none).
    Without `existing` (bare metal: the hosts are the operator's) all count as
    having one."""
    if existing is None:
        return removed, {}
    have = existing()
    return ({n: r for n, r in removed.items() if n in have},
            {n: r for n, r in removed.items() if n not in have})

def _gone_note(gone: dict[str, dict[str, int]]) -> str:
    names = ", ".join(f"{name} ({', '.join(f'{role} {i}' for role, i in roles.items())})"
                      for name, roles in gone.items())
    return ("cluster.toml no longer lists these nodes, and terraform has no instance for "
            f"them, so there is nothing to take out: {names}. apply only forgets their ids. "
            "If a running cluster still shows one as a peer (`boxd-ctl status`), remove it "
            "with `boxd-ctl remove-peer <id>` on the leader.")

def _removal_warning(cfg, removed: dict[str, dict[str, int]]) -> str:
    lines = ["cluster.toml no longer lists these nodes (or roles), so this apply takes them "
             "out of the cluster" + (", and terraform deletes their instances"
                                     if clouds.provisions(cfg) else "") + ".",
             "Until the steps below are done, that leaves the cluster unusable:"]
    for name, roles in removed.items():
        lines.append(f"  {name}")
        lines += [f"    {line}" for role, node_id in roles.items()
                  for line in _removal_steps(role, node_id)]
    lines += ["Run boxd-ctl on a control node; remove-peer only on the leader, which "
              "`boxd-ctl status` names (OPERATIONS.md, \"Getting started\").",
              "Not removing them? Put them back in cluster.toml with the same name, roles and "
              "internal_ip."]
    return "\n".join(lines)

def retire_removed(state, removals: dict[str, dict[str, int]]) -> None:
    """Forget the ids of the nodes the apply took out, for good: the next
    apply doesn't ask about them again, and no node gets their ids."""
    for name, roles in removals.items():
        for role in roles:
            if (node_id := state.retire_node_id(f"{name}/{role}")) is not None:
                log(f"{name} ({role}) removed; node id {node_id} retired")

def check_storage_bundle(cfg, assets_root: str) -> None:
    """[storage_engine] needs a bundle built with the storage engine (its
    manifest's features list "bucket"). Fail closed: a bundle that doesn't say
    so is refused, every bundle from before the field included, so a cluster
    meant to keep machines in a bucket never starts on file-only workers."""
    if "storage_engine" not in cfg.doc:
        return
    if "bucket" not in assets.features(assets_root):
        raise InstallError(
            f"[storage_engine] needs a release with bucket storage, and {cfg.assets_version[:12]} "
            "is one without it (its manifest lists no \"bucket\" feature). Bucket storage "
            "comes with a license key that includes it: ask your boxd contact, or remove "
            "[storage_engine]")

def check_relay_bundle(cfg, assets_root: str) -> None:
    """[[worker.private_services]] needs a bundle that ships the relay (bin/boxd-relay).
    Fail closed: an older bundle's worker would ignore the tables without a word, and boxes
    would get no answer for the names."""
    if not private_services.declared(cfg.doc):
        return
    if not assets.has_file(assets_root, "bin/boxd-relay"):
        raise InstallError(
            f"[[worker.private_services]] needs a release with the relay for private services, "
            f"and {cfg.assets_version[:12]} is one without it (its manifest lists no "
            "bin/boxd-relay): set [cluster].assets_version to a newer release, or remove the "
            "[[worker.private_services]] tables")

def check_private_service_bridges(cfg, state) -> None:
    """Before terraform: no private service target inside a worker's bridge /16, the /16s this
    apply gives new workers included. Found only at render, a new worker's instance would exist
    and its index be saved, and every later apply would stop there."""
    declared = private_services.declared(cfg.doc)
    if not declared or cfg.network_mode == "routed":
        return
    alloc = Allocation(cfg, {}, {}, state.bridge_indexes_for(cfg))
    subnets = {w: alloc.bridge_subnet(w) for w in alloc.bridge_indexes}
    if why := private_services.bridge_overlap_error(declared, subnets):
        raise InstallError(why)

def bundle_changes(state, cfg) -> bool:
    """Whether this apply installs another bundle than the last successful
    one. The record is written only at the end of an apply, so an upgrade
    that stopped half-way still counts as a change on the re-run."""
    return (state.data.get("applied_assets") or {}).get("sha") != cfg.assets_version

def check_network_mode(cfg, state) -> None:
    """Refuse to switch an existing cluster's [network] mode (bridge <->
    routed) while it has machines: each one's captured RAM (snapshots,
    hibernation, templates' copies) holds its old gateway and address, which
    the other mode doesn't route. The deployed workers' configs say what the
    cluster runs (a cluster switched by hand counts as switched); the state's
    record covers a cluster whose workers were all replaced."""
    if not state.data.get("applied_assets"):
        return
    want = cfg.network_mode
    deployed = cluster.deployed_network_modes(cfg)
    differ = sorted(n for n, mode in deployed.items() if mode != want)
    recorded = state.data.get("network_mode")
    if not differ and (deployed or recorded in (None, want)):
        return
    count = cluster.machine_count(cfg)
    where = ", ".join(differ) if differ else "the cluster (installer state)"
    if count:
        raise InstallError(
            f'[network] mode = "{want}", but {where} runs the other mode, and the cluster has '
            f"{count} machine(s). Machines keep their old network in their saved memory, so the "
            "mode only changes on a cluster without machines: remove them (`boxd-ctl vm list`), "
            "or put mode back")
    log(f'network: switching {where} to mode = "{want}" (no machines on the cluster)')

def ensure_egress_ca(cfg, state) -> None:
    """One egress CA for the cluster, in state, before the render ships it to
    every worker. A cluster applied before this release has none in state but
    workers that made their own: adopt the first one found, so its machines
    keep trusting it. Otherwise make one."""
    if state.egress_ca() is not None or not cfg.nodes_with("worker"):
        return
    found = cluster.worker_egress_ca(cfg) if state.data.get("applied_assets") else None
    if found:
        name, cert, key = found
        state.store_egress_ca(cert, key)
        others = [n.name for n in cfg.nodes_with("worker") if n.name != name]
        log(f"egress CA: adopted {name}'s own pair for the whole cluster"
            + (f"; machines on {', '.join(others)} get it at their next boot, restore or "
               "fork (until then their HTTPS through the egress proxy fails)" if others else ""))
    else:
        state.store_egress_ca(*generate_egress_ca())
        log("egress CA: made one for the cluster")
