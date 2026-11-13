import argparse
import os
import shutil
import sys
import tempfile

import shlex
import shutil as _shutil
import subprocess

from . import (ansible, assets, clouds, composio, config, control_carry, license, local_ca,
               oidc, private_services, remote_state, render, storage_engine, telemetry, tf,
               windows, windows_image)
from .state import Allocation, State, generate_egress_ca
from .util import InstallError, log

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# `state pull` lands here by default: inside the checkout, so it survives
# ./boxd-install's throwaway container; gitignored; holds decrypted secrets.
PULLED_STATE = os.path.join(ROOT, "pulled-state")

def _write_out(files: dict[str, str], out_dir: str) -> None:
    for rel, content in sorted(files.items()):
        path = os.path.join(out_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(content)
    log(f"wrote {len(files)} files under {out_dir}/")

def _renderer(cfg, state, fake_tf: str | None) -> render.Renderer:
    cfg.apply_overlay(state.load_providers())
    tf_out = tf.cached_outputs(state, fake_tf)
    alloc = state.allocate(cfg)
    return render.Renderer(cfg, alloc, tf_out, state)

def _render_files(cfg, state, fake_tf: str | None) -> dict[str, str]:
    """What `render` writes: the per-host configs and ansible inventory, or on
    GKE the boxd-config Secret's files and every Kubernetes object."""
    if not clouds.kubernetes(cfg):
        return _renderer(cfg, state, fake_tf).render_all()
    import json
    from . import kube_render
    from .clouds import gke
    if fake_tf:
        state.ensure_keypairs()
    cfg.apply_overlay(state.load_providers())
    r = kube_render.KubeRenderer(cfg, state.allocate(cfg), tf.cached_outputs(state, fake_tf), state)
    n = gke.settings(cfg.doc)["pools"]["worker"]["nodes"]
    files, hashes = r.config_files(range(n))
    downloads = r.downloads()
    # What kube.deploy applies, in its order.
    objs = (r.base_objects(downloads) + r.clickhouse_objects(hashes.get("clickhouse", ""))
            + r.control_objects(hashes["control"])
            + r.console_objects(hashes["console"], downloads=downloads))
    for node in cfg.nodes_with("proxy"):
        objs += r.edge_objects(gke.ordinal(node.name), hashes, downloads=downloads)
    objs += r.worker_objects(n) + r.pod_monitorings()
    out = {f"boxd-config/{k}": v for k, v in files.items()}
    out["manifests.json"] = json.dumps({"apiVersion": "v1", "kind": "List", "items": objs},
                                       indent=1) + "\n"
    return out

def cmd_validate(args) -> int:
    cfg = config.load(args.cluster)
    if clouds.kubernetes(cfg):
        return _validate_gke(cfg)
    check_existing_vpc(cfg)
    # A warning, not an error: renaming a node replaces its instance, so a
    # running cluster keeps the names it has.
    if odd := [n.name for n in cfg.nodes if not n.name.startswith(f"{cfg.name}-")]:
        log(f"warning: node names don't start with the cluster name {cfg.name!r}: "
            f"{', '.join(odd)}. They become instance names, so two clusters in one "
            "project or account can collide; rename them before the first apply.")
    # A [license].key_file that is missing or not a key stops here. No
    # terraform state here: workers without machine_type count as new ones.
    if errs := telemetry.errors(cfg) + windows.errors(cfg):
        raise InstallError("; ".join(errs))
    warnings = (license.free_tier_warnings(cfg, assume_new=True) + telemetry.warnings(cfg)
                + config.network_warnings(cfg) + config.data_disk_warnings(cfg)
                + config.storage_bucket_warnings(cfg))
    for warning in warnings:
        log(f"warning: {warning}")
    print("cluster.toml OK")
    print(telemetry.validate_line(cfg))
    if "oidc" in cfg.doc:
        print(oidc.check_discovery(cfg.doc["oidc"]))
    return 0

def check_existing_vpc(cfg) -> None:
    """[aws] vpc_id: the routes, DNS and S3 endpoint the nodes need (validate
    and apply only, not every command that loads cluster.toml)."""
    if cfg.cloud_name == "aws" and "vpc_id" in cfg.cloud:
        if errs := clouds.CLOUDS["aws"].network_errors(cfg.cloud):
            raise InstallError("the existing VPC can't take the cluster:\n  " + "\n  ".join(errs))

def _validate_gke(cfg) -> int:
    """[gke]: the file, the worker pool against the license, and what can
    only be read from the project (the org policy; an existing cluster)."""
    from .clouds import gke
    errs = telemetry.errors(cfg) + gke.license_errors(cfg)
    live_errs, live_warns, _ = gke.live_checks(cfg)
    for warning in (gke.warnings(cfg) + live_warns + telemetry.warnings(cfg)
                    + config.network_warnings(cfg)):
        log(f"warning: {warning}")
    if errs + live_errs:
        raise InstallError("\n  ".join(["cluster.toml can't be installed on GKE:"] + errs + live_errs))
    print("cluster.toml OK")
    print(telemetry.validate_line(cfg))
    if "oidc" in cfg.doc:
        print(oidc.check_discovery(cfg.doc["oidc"]))
    return 0

def cmd_render(args) -> int:
    cfg = config.load(args.cluster)
    if args.fake_tf:
        state = State(args.state_dir)
        _write_out(_render_files(cfg, state, args.fake_tf), args.out_dir)
        return 0
    with remote_state.synced(cfg, args.state_dir, rescue_dir=PULLED_STATE):
        state = State(args.state_dir)
        _write_out(_render_files(cfg, state, None), args.out_dir)
    return 0

def _checkpoint(cfg, args) -> None:
    """Push the state before terraform creates anything from it: the values made for this
    cluster on its first apply (the KMS key ring's suffix, keys, the egress CA) must be in
    the state bucket before resources carry them, or a run cut short makes them anew on the
    next apply and terraform replaces what the first one created."""
    remote_state.push(cfg, args.state_dir, delete_local=False)

def cmd_apply(args) -> int:
    cfg = config.load(args.cluster)
    with remote_state.synced(cfg, args.state_dir, rescue_dir=PULLED_STATE):
        if clouds.kubernetes(cfg):
            return _apply_gke(cfg, args)
        return _apply(cfg, args)

def _apply(cfg, args) -> int:
    # Before anything else: a [license].key_file that is missing or not a
    # key stops the apply; it never falls back to the free tier.
    key = license.key_path(cfg)
    if errs := telemetry.errors(cfg) + windows.errors(cfg):
        raise InstallError("; ".join(errs))
    check_existing_vpc(cfg)
    state = State(args.state_dir)
    # The render would refuse a half-configured [oidc] only after terraform.
    oidc_table = {**cfg.doc.get("oidc", {}), **state.load_providers().get("oidc", {})}
    if oidc_table and (why := render.oidc_render_error(oidc_table)):
        raise InstallError(why)
    state.ensure_keypairs()
    ansible.set_keys_dir(state.keys_dir)
    os.makedirs(args.out_dir, exist_ok=True)
    if not clouds.provisions(cfg):
        # Terraform puts the deploy key on cloud instances; on metal the
        # operator does, so this stops a first apply until they have.
        ansible.check_hosts(cfg, state.deploy_pubkey())

    assets_root = assets.ensure(cfg, args.cache_dir, state)
    check_storage_bundle(cfg, assets_root)
    check_relay_bundle(cfg, assets_root)
    windows.check_bundle(cfg, assets_root)
    check_private_service_bridges(cfg, state)
    # Before terraform: once the storage catalog is on, the engine and its
    # bucket are one-way (storage_engine.check_guard).
    storage = storage_engine.check_guard(cfg, state, assets_root)
    assets.console_tarball(assets_root)

    # metal: the hosts exist, so there is nothing to plan or replace.
    if clouds.provisions(cfg):
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
        tfvars = tf.write_tfvars(cfg, state, args.out_dir, existing)
        plan = os.path.join(args.out_dir, "tf.plan")
        tf.plan(cfg, tfvars, plan)
        # Before the apply: a replaced control node takes its state with it.
        control_carry.save(cfg, state, control_carry.plan_json(cfg, plan))
        tf.apply_plan(cfg, plan, before=lambda: _checkpoint(cfg, args))
    tf_out = tf.read_outputs(cfg, state)
    ansible.set_tf_outputs(tf_out)

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
    _write_out(r.render_all(), args.out_dir)

    ansible.preflight(cfg)
    check_network_mode(cfg, state)
    storage_engine.check_identity(cfg, storage, r)
    if not clouds.provisions(cfg):
        for warning in license.free_tier_warnings(cfg, counted=ansible.worker_vcpus(cfg)):
            log(f"warning: {warning}")
    # Last before the converge, so a check that refuses the apply leaves them on.
    if bundle_changes(state, cfg):
        ansible.pause_machine_specs(cfg)
    ansible.converge(cfg, args.out_dir, assets_root,
                     assets.host_kernel_release(assets_root),
                     control_carry.carry_dir(state))

    ansible.wait_for_leader(cfg)
    # A node added to cluster.toml joins a running cluster only through the
    # leader's sync-config (a no-op on a fresh cluster and on a re-apply).
    ansible.sync_config(cfg)
    expected = len(cfg.nodes_with("worker"))
    gave_up = ansible.learners_gave_up(cfg)
    restarted = gave_up or (ansible._registered_nodes(cfg) or 0) < expected
    if restarted:
        if gave_up:
            # Expected on a fresh cluster: they started before the raft
            # leader existed. The restart below fixes it.
            log(f"restarting services that gave up registering (they started before "
                f"the raft leader): {', '.join(gave_up)}")
        ansible.restart_learners(cfg)
    ansible.verify_registration(cfg, expected=expected)
    if restarted:
        log("after the restart, every node registered")
    control_carry.clear(state)
    return _finish(cfg, args, state, r, key, storage, assets_root)

def _finish(cfg, args, state, r, key, storage, assets_root: str,
            image_override=None) -> int:
    """Every target, once every node runs the new bundle and every worker
    registered: the bucket, the cluster id, machine specs, the license, the
    storage switches, telemetry, templates and orgs, then the state record
    and the DNS delegation."""
    # Before any switch: a worker that can't open its bucket fails the apply.
    storage_engine.wait_bucket_open(cfg)

    # Every node (control, proxy, console) runs the new bundle now, so the
    # cluster ID can be made (once; a no-op after). Before the key: a bound
    # key needs it.
    ansible.init_cluster_id(cfg)
    # For the same reason: an older node would skip their entries for good.
    ansible.turn_on_machine_specs(cfg)
    # Before the templates, so the key is in place before any machine is made.
    if key:
        ansible.install_license(cfg, key)
    # After the key: a bucket default needs its bucket-storage feature. The
    # guard is pushed to the state bucket the moment the catalog is on.
    storage_engine.set_switches(
        cfg, state, storage, r,
        push=lambda: remote_state.push(cfg, args.state_dir, delete_local=False))
    value, source = telemetry.setting(cfg)
    if value is None:
        log("telemetry: [telemetry] enabled is not set; the cluster's setting is left as it is")
    else:
        ansible.set_telemetry(cfg, value, explicit=source == telemetry.CLUSTER_TOML)
    # Every worker runs the new release and has the firmware now ([windows]).
    windows.configure(cfg, r.alloc)

    image, tag = assets.computer_image(assets_root)
    if image_override:
        image = image_override(image)
    ansible.seed_templates(cfg, image, tag, cfg.assets_version,
                           storage=storage_engine.template_storage(cfg))
    ansible.configure_orgs(cfg, cfg.machine_sizes())

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
    if cfg.acme_ca == "gts" and ansible.acme_eab_spent(cfg):
        raise InstallError(
            "boxd-control can't register its ACME account: Google Trust Services says the "
            "EAB key was already used, so no certificate will ever be issued. Mint a new "
            "key and re-apply (docs/REFERENCE.md, Troubleshooting, \"Unknown external account binding\"):\n"
            "  ./boxd-install state reset-eab\n  ./boxd-install apply")
    return 0

def _apply_gke(cfg, args) -> int:
    """apply on GKE ([gke]): terraform/gke for the cloud side, then the
    Kubernetes objects (kube.deploy), then the same post-install steps as on
    instances. An upgrade is an apply with a new assets_version: control,
    console and the edges roll; workers are updated in place."""
    from . import kube, kube_render
    from .clouds import gke
    key = license.key_path(cfg)
    if errs := telemetry.errors(cfg) + gke.license_errors(cfg):
        raise InstallError("; ".join(errs))
    state = State(args.state_dir)
    oidc_table = {**cfg.doc.get("oidc", {}), **state.load_providers().get("oidc", {})}
    if oidc_table and (why := render.oidc_render_error(oidc_table)):
        raise InstallError(why)
    errs, warns, facts = gke.live_checks(cfg)
    errs += gke.state_errors(cfg, state)
    for warning in gke.warnings(cfg) + warns:
        log(f"warning: {warning}")
    if errs:
        raise InstallError("\n  ".join(["this apply can't go on:"] + errs))
    state.ensure_keypairs()
    ansible.set_keys_dir(state.keys_dir)
    os.makedirs(args.out_dir, exist_ok=True)

    assets_root = assets.ensure(cfg, args.cache_dir, state)
    check_storage_bundle(cfg, assets_root)
    # The guard asks a cluster that exists through the previous apply's outputs.
    storage = storage_engine.check_guard(cfg, state, assets_root)

    tf.init(cfg)
    # The pool never shrinks under a running worker: kube.deploy drains and
    # removes workers (and their nodes) itself.
    live = kube.live_worker_replicas(cfg, state)
    tfvars = gke.write_tfvars(cfg, state, args.out_dir, live, facts)
    plan = os.path.join(args.out_dir, "tf.plan")
    tf.plan(cfg, tfvars, plan)
    # Every plan, the first included: a state that was lost looks like a first apply, and its
    # plan is the one that would replace what the cluster already has.
    import json
    if problems := gke.plan_problems(json.loads(tf.show_json(cfg, plan))):
        raise InstallError("\n  ".join(["refusing the terraform plan:"] + problems))
    tf.apply_plan(cfg, plan, before=lambda: _checkpoint(cfg, args))
    tf_out = tf.read_outputs(cfg, state)
    ansible.set_tf_outputs(tf_out)
    kube.connect(cfg, tf_out)

    cfg.apply_overlay(state.load_providers())
    if inert := config.inert_sign_in({t: cfg.table(t) for t in ("oidc", "google", "github")
                                      if cfg.table(t)}):
        log(f"warning: {inert}")
    composio.sync_auth_configs(cfg, state, assets_root)
    ensure_egress_ca(cfg, state)
    storage_engine.adopt_prefix(cfg, state)
    alloc = state.allocate(cfg)
    r = kube_render.KubeRenderer(cfg, alloc, tf_out, state, assets_root=assets_root,
                                 pull_config=kube.pull_config(cfg))
    storage_engine.check_identity(cfg, storage, r)
    if state.data.get("applied_assets") and bundle_changes(state, cfg):
        ansible.pause_machine_specs(cfg)
    kube.deploy(cfg, r, state, args.out_dir, tf_out)
    gke.record(cfg, state)
    return _finish(cfg, args, state, r, key, storage, assets_root,
                   image_override=lambda image: gke.computer_image(cfg.doc, image))

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
    deployed = ansible.deployed_network_modes(cfg)
    differ = sorted(n for n, mode in deployed.items() if mode != want)
    recorded = state.data.get("network_mode")
    if not differ and (deployed or recorded in (None, want)):
        return
    count = ansible.machine_count(cfg)
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
    found = ansible.worker_egress_ca(cfg) if state.data.get("applied_assets") else None
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

def cmd_destroy(args) -> int:
    cfg = config.load(args.cluster)
    if not clouds.provisions(cfg):
        raise InstallError("destroy removes what terraform created; on [metal] the installer "
                           "created no hosts, so there is nothing for it to destroy. Reinstall "
                           "or wipe the hosts yourself (docs/INSTALL.md \"Install on bare metal\")")
    with remote_state.synced(cfg, args.state_dir, rescue_dir=PULLED_STATE):
        state = State(args.state_dir)
        tfvars = os.path.join(args.out_dir, "terraform.tfvars.json")
        tf.init(cfg)
        if clouds.kubernetes(cfg):
            _destroy_gke_prepare(cfg, state, args.out_dir)
        elif not os.path.exists(tfvars):
            # No license check: destroying never depends on the key file.
            tfvars = tf.write_tfvars(cfg, state, args.out_dir, tf.instance_types(cfg),
                                     for_destroy=True)
        try:
            tf.destroy(cfg, tfvars)
        except InstallError:
            kept = storage_engine.kept_buckets(tf.state_list(cfg))
            if kept is None:
                raise
            log(kept_buckets_message(state, kept))
        control_carry.clear(state)
        # The instances are gone: the next apply is a new cluster, with a new
        # storage prefix and no guard.
        state.clear_storage_engine()
        if clouds.kubernetes(cfg):
            # A key ring outlives destroy: the next cluster gets a new one.
            state.data.pop("gke", None)
            state.save()
    print("infra destroyed. Encrypted state (ids, keys) kept in the state "
          "bucket — delete the installer-state.enc object only if this "
          "cluster is gone for good.")
    return 0

def _destroy_gke_prepare(cfg, state, out_dir: str) -> None:
    """GKE: the tfvars of the cluster as it is (every pool at its live size),
    and in a cluster boxd didn't make, boxd's namespace and cluster-scoped
    objects removed first (terraform takes the pools, not the objects)."""
    from . import kube
    from .clouds import gke
    os.makedirs(out_dir, exist_ok=True)
    live = kube.live_worker_replicas(cfg, state)
    facts = {}
    if not gke.settings(cfg.doc)["create"]:
        try:
            facts = gke.live_checks(cfg)[2]
        except InstallError as e:
            log(f"warning: can't read the cluster ({e}); destroying with defaults")
    gke.write_tfvars(cfg, state, out_dir, live, facts)
    if not gke.settings(cfg.doc)["create"] and state.data.get("gke"):
        try:
            kube.connect(cfg, tf.cached_outputs(state))
            kube.teardown(cfg)
        except InstallError as e:
            log(f"warning: boxd's Kubernetes objects were not removed ({e}); delete the "
                f"namespace {gke.settings(cfg.doc)['namespace']} by hand")

def cmd_ctl(args) -> int:
    """`./boxd-install ctl -- <boxd-ctl args>`: boxd-ctl on the cluster's
    control node (over ssh, or on GKE in the control-0 pod). Stdin is passed
    through (`ctl -- license install < license.key`)."""
    cfg = config.load(args.cluster)
    argv = list(args.ctl_args)
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        raise InstallError("ctl needs boxd-ctl's arguments, e.g. ./boxd-install ctl -- status")
    data = None if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read()
    with remote_state.synced(cfg, args.state_dir, rescue_dir=PULLED_STATE):
        state = State(args.state_dir)
        ansible.set_keys_dir(state.keys_dir)
        ansible.set_tf_outputs(tf.cached_outputs(state))
        res = ansible.ctl(cfg, shlex.join(argv), check=False, quiet=True, input=data or None)
    sys.stdout.write(res.stdout or "")
    sys.stderr.write(res.stderr or "")
    return res.returncode

def kept_buckets_message(state, kept: list[str]) -> str:
    """A destroy that failed only on buckets with objects in them
    (force_destroy is false on the storage, backups and archive buckets): the
    rest is gone. They stay in terraform state, so an apply under the same
    cluster name keeps using them; deleting them is the operator's call."""
    try:
        outputs = tf.cached_outputs(state)
    except InstallError:
        outputs = {}
    names = []
    for addr in kept:
        res = addr.split(".")[1].split("[")[0]
        out = {"storage": "storage_bucket"}.get(res, res)
        bucket = (outputs.get(out) or {}).get("bucket")
        names.append(f"{bucket} ({addr})" if bucket else addr)
    return ("kept " + ", ".join(names) + ": not empty, and terraform never deletes a bucket "
            "with objects in it. An apply of this cluster keeps using it; delete it by hand "
            "only if this cluster is gone for good")

PROVIDERS_TEMPLATE = """\
# Secret provider tables — encrypted at rest in the state bucket, merged over cluster.toml
# at render time (values here win). Uncomment what you use; delete the rest.

# Google sign-in (console). OAuth web client, redirect URI:
#   https://app.<zone>/api/v1/auth/google/callback
# [google]
# client_id = "….apps.googleusercontent.com"
# client_secret = ""

# GitHub sign-in (proxy + console; worker gets only app_* fields). OAuth app
# callback: https://app.<zone>/api/v1/auth/github/callback
# The app_* block is a SEPARATE GitHub App for org repo access — optional.
# client_id/client_secret are REQUIRED whenever [github] is set (a GitHub
# App's own client credentials work for sign-in too).
# [github]
# client_id = ""
# client_secret = ""
# app_id = 0
# app_slug = ""
# (App private key: ./install.py providers set-github-key <key>.pem — not a field here.)
# app_client_id = ""
# app_client_secret = ""
# app_webhook_secret = ""

# Single sign-on (console) through your OpenID Connect provider: Entra ID,
# Okta, Google Workspace, Keycloak. Its other keys go in cluster.toml's [oidc]
# (docs/REFERENCE.md "Single sign-on"); only the client secret goes here. Redirect URI:
#   https://app.<zone>/api/v1/auth/oidc/callback
# With [oidc], GitHub and Google sign-in are off.
# [oidc]
# client_secret = ""

# Resend transactional email.
# [resend]
# api_key = "re_…"
# from = "boxd <hello@app.<zone>>"
# reply_to = ""
# webhook_secret = "whsec_…"
# segment_id = ""

# ClickHouse analytics sink.
# [analytics]
# endpoint = "http://<ip>:8123"
# user = "default"
# password = ""

# S3-compatible backups. AUTO-PROVISIONED by default (GCS bucket + HMAC key,
# or S3 bucket + IAM access key, via terraform) — set this only to use
# EXTERNAL storage instead; it wins over the auto credentials.
# [gcp]/[aws] auto_backups = false disables the auto provisioning entirely.
# [backups]
# endpoint = "https://storage.googleapis.com"   # or https://s3.<region>.amazonaws.com
# bucket = ""
# region = "auto"
# access_key = ""
# secret_key = ""

# Snapshot archive store (`boxd-ctl snapshot archive`). Turned on by
# [snapshot_archive] in cluster.toml; on AWS the installer creates the bucket
# and key. Set these only for YOUR OWN bucket (on GCP, or with
# [aws] auto_snapshot_archive = false); they win over the auto credentials.
# The bucket needs default encryption and a lifecycle rule that aborts
# incomplete multipart uploads.
# [snapshot_archive]
# endpoint = "https://s3.<region>.amazonaws.com"
# bucket = ""
# region = ""
# access_key = ""
# secret_key = ""

# The storage engine's bucket (machines' disks). Turned on by [storage_engine]
# in cluster.toml; on AWS and GCP the installer creates the bucket. Set these
# only for YOUR OWN S3-compatible bucket: on [metal], or with [aws]/[gcp]
# auto_storage_bucket = false (refused while the installer makes the bucket).
# The bucket needs a lifecycle rule that aborts incomplete multipart uploads.
# conditional_put: leave it out (probe) so apply can test the bucket opens.
# [storage_engine]
# endpoint = "https://s3.<region>.amazonaws.com"
# region = ""
# bucket = ""
# access_key = ""
# secret_key = ""
# virtual_hosted_style = false

# The private registry your Windows image is in ([windows] in cluster.toml;
# win-img/README.md). Every worker gets these pull credentials. host is the
# registry's host as the image reference names it. Artifact Registry: username
# "_json_key_base64" and a service account's JSON key, base64, as password.
# [windows_registry]
# host = "registry.example.com"
# username = ""
# password = ""

# Composio integrations. auth_configs/webhook_secret/connect_domain are
# derived by the installer — only the keys below belong here.
# [automations]
# composio_api_key = "ak_…"
# anthropic_api_key = "sk-ant-…"
"""

def cmd_providers(args) -> int:
    """Secret provider tables live in the encrypted state, never on disk in
    the repo. get prints, set replaces from a file, edit opens $EDITOR —
    set/edit validate before pushing. set-github-key stores the GitHub App
    private-key PEM (a file, so it rides in state's keys/ alongside the TLS
    material rather than in the TOML overlay). set-tls-ca stores the
    intermediate CA cert + key boxd-control signs with when [acme].ca =
    "local", the same way."""
    cfg = config.load(args.cluster)
    if args.action == "set-tls-ca":
        if not (args.file and args.key):
            raise InstallError("providers set-tls-ca needs <cert.pem> <key.pem> "
                               "(the intermediate CA, optionally followed by its root)")
        # Before touching state: a bad pair fails without a pull.
        ca_chain, ca_key = local_ca.validate(args.file, args.key)
    remote_state.pull(cfg, args.state_dir)
    state = State(args.state_dir)
    path = state.providers_path()
    try:
        if args.action == "set-github-key":
            if not args.file:
                raise InstallError("providers set-github-key needs a PEM file argument")
            with open(args.file) as fh:
                pem = fh.read()
            if "PRIVATE KEY-----" not in pem:
                raise InstallError(f"{args.file} does not look like a PEM private key")
            dest = os.path.join(state.keys_dir, "github-app.pem")
            with open(dest, "w") as fh:
                fh.write(pem)
            os.chmod(dest, 0o600)
            remote_state.push(cfg, args.state_dir)
            print("GitHub App key stored in state — set the [github] app_* fields via "
                  "`providers edit`, then `apply` to roll it out")
            return 0
        if args.action == "set-tls-ca":
            local_ca.write(state.keys_dir, ca_chain, ca_key)
            remote_state.push(cfg, args.state_dir)
            print('local CA stored in state — set [acme] ca = "local" in cluster.toml, '
                  "then `apply` to roll it out")
            return 0
        if args.action == "get":
            if os.path.exists(path):
                with open(path) as fh:
                    print(fh.read(), end="")
            else:
                print("# no providers config set — see `providers set/edit`")
            _shutil.rmtree(args.state_dir)
            return 0
        if args.action == "set":
            if not args.file:
                raise InstallError("providers set needs a TOML file argument (through "
                                   "./boxd-install, a path inside this directory)")
            doc = _load_providers(args.file)
        else:
            if os.path.exists(path):
                _shutil.copy(path, path + ".edit")
            else:
                with open(path + ".edit", "w") as fh:
                    fh.write(PROVIDERS_TEMPLATE)
            doc = _edit_until_valid(path + ".edit")
        src = args.file if args.action == "set" else path + ".edit"
        _shutil.copy(src, path)
        os.chmod(path, 0o600)
        if os.path.exists(path + ".edit"):
            os.remove(path + ".edit")
        remote_state.push(cfg, args.state_dir)
        if inert := config.inert_sign_in({**cfg.doc, **doc}):
            log(f"warning: {inert}")
        print(f"providers config stored ({', '.join(sorted(doc)) or 'empty'}) — "
              "run `apply` (or `render`+converge) to roll it out")
        return 0
    except Exception:
        _shutil.rmtree(args.state_dir, ignore_errors=True)
        raise

def _load_providers(path: str) -> dict:
    """Parsed and validated, or an InstallError a person can act on."""
    import tomllib
    try:
        with open(path, "rb") as fh:
            doc = tomllib.load(fh)
    except tomllib.TOMLDecodeError as e:
        raise InstallError(f"not valid TOML: {e}\n  Values are quoted strings, e.g. "
                           'client_id = "1234-abc.apps.googleusercontent.com"') from None
    config.validate_providers(doc)
    return doc

def _edit_until_valid(path: str) -> dict:
    """$EDITOR on `path` until it parses and validates — a typo reopens the
    same text instead of losing it (like `kubectl edit`)."""
    while True:
        subprocess.run([os.environ.get("EDITOR", "vi"), path], check=True)
        try:
            return _load_providers(path)
        except InstallError as e:
            print(f"error: {e}", file=sys.stderr)
            if not sys.stdin.isatty():
                raise
            if input("Edit again? [Y/n] ").strip().lower() in ("n", "no"):
                raise InstallError("providers config not changed") from None

def cmd_state(args) -> int:
    cfg = config.load(args.cluster)
    rel = os.path.relpath(args.state_dir, ROOT)
    if args.action == "reset-eab":
        if os.path.isdir(PULLED_STATE) and os.listdir(PULLED_STATE):
            raise InstallError("./pulled-state hasn't been pushed: push or delete it first "
                               "(pushing it later would bring the old key back)")
        # Its own throwaway dir: never mixes with a pulled copy being edited.
        args.state_dir = tempfile.mkdtemp(prefix="boxd-installer-state-")
        with remote_state.synced(cfg, args.state_dir, rescue_dir=PULLED_STATE):
            n = State(args.state_dir).drop_acme_eab()
        print(f"removed {n} stored ACME EAB key(s); the next `apply` mints a new one "
              "and restarts boxd-control with it" if n else "no stored ACME EAB key to remove")
        return 0
    if args.action == "reset-service-addresses":
        if os.path.isdir(PULLED_STATE) and os.listdir(PULLED_STATE):
            raise InstallError("./pulled-state hasn't been pushed: push or delete it first "
                               "(pushing it later would bring the old addresses back)")
        args.state_dir = tempfile.mkdtemp(prefix="boxd-installer-state-")
        with remote_state.synced(cfg, args.state_dir, rescue_dir=PULLED_STATE):
            n = State(args.state_dir).drop_service_addresses()
        print(f"forgot {n} private service address(es); the next `apply` allocates them again "
              "from [network].service_range (boxes get the new ones on their next lookup)"
              if n else "no private service addresses to forget")
        return 0
    if args.action == "pull":
        os.makedirs(args.state_dir, mode=0o700, exist_ok=True)
        remote_state.pull(cfg, args.state_dir)
        print(f"state decrypted into ./{rel} (secrets: keep it private)")
        print("push it back when done (this deletes it):  ./boxd-install "
              + ("" if args.state_dir == PULLED_STATE else f"--state-dir {rel} ") + "state push")
    else:
        remote_state.push(cfg, args.state_dir)
        print(f"state pushed from ./{rel}; local copy deleted")
    return 0

def cmd_windows_image(args) -> int:
    """`windows-image kit`: the part of `./boxd-install windows-image build` that runs here
    (windows_image.py). Prints `key=value` lines on stdout for boxd-install, which runs the build
    on the host."""
    cfg = config.load(args.cluster)
    if clouds.kubernetes(cfg):
        raise InstallError("[windows] is not supported on [gke] yet")
    with remote_state.synced(cfg, args.state_dir, rescue_dir=PULLED_STATE):
        state = State(args.state_dir)
        cfg.apply_overlay(state.load_providers())
        # The license key first, as apply checks it: an image is no use without Windows guests.
        license.key_path(cfg)
        if errs := windows.errors(cfg):
            raise InstallError("; ".join(errs))
        assets_root = assets.ensure(cfg, args.cache_dir, state)
        info = windows_image.prepare(cfg, args.cache_dir, assets_root)
    for k, v in info.items():
        if k in ("kit", "docker_config") and v:
            v = os.path.relpath(v, ROOT)
        print(f"{k}={v}")
    return 0

def main() -> int:
    p = argparse.ArgumentParser(prog="install.py")
    p.add_argument("--cluster", default=os.path.join(ROOT, "cluster.toml"))
    p.add_argument("--state-dir", help="default: a fresh random dir under /tmp")
    p.add_argument("--out-dir", help="default: a fresh random dir under /tmp; "
                   "apply/destroy delete it when done (rendered configs carry secrets)")
    p.add_argument("--cache-dir", default=os.path.join(ROOT, "cache"))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate")
    pr = sub.add_parser("render")
    pr.add_argument("--fake-tf", help="JSON file standing in for terraform outputs")
    sub.add_parser("apply")
    sub.add_parser("destroy")
    pc = sub.add_parser("ctl", help="run boxd-ctl on the cluster: ctl -- <args>",
                        description="boxd-ctl on a control node (on GKE, in the control-0 pod), "
                                    "e.g. ./boxd-install ctl -- status")
    pc.add_argument("ctl_args", nargs=argparse.REMAINDER)
    ps = sub.add_parser("state")
    ps.add_argument("action", choices=["pull", "push", "reset-eab", "reset-service-addresses"],
                    help="pull: decrypt into ./pulled-state; push: encrypt it back and delete it; "
                         "reset-eab: forget the stored ACME EAB key so the next apply mints a new one; "
                         "reset-service-addresses: forget the private services' addresses so the "
                         "next apply allocates them again")
    pp = sub.add_parser("providers", formatter_class=argparse.RawDescriptionHelpFormatter,
                        description="Sign-in, email, backups and integration secrets, kept "
                                    "encrypted in the state bucket (never on disk here).",
                        epilog="""actions:
  edit                    open $EDITOR on the current config (a commented template the
                          first time); saved when it parses and validates, else reopens
  get                     print the current config (it contains secrets)
  set FILE                REPLACE the whole config with FILE (no merge); through
                          ./boxd-install, FILE must be inside this directory
  set-github-key PEM      store the GitHub App private key
  set-tls-ca CERT KEY     store the intermediate CA for [acme] ca = "local"

Changes take effect on the next `apply`.""")
    pp.add_argument("action", choices=["get", "set", "edit", "set-github-key", "set-tls-ca"])
    pp.add_argument("file", nargs="?",
                    help="TOML file (for `set`) / PEM file (for `set-github-key`) / "
                         "CA cert PEM (for `set-tls-ca`)")
    pp.add_argument("key", nargs="?", help="CA private key PEM (for `set-tls-ca`)")
    pw = sub.add_parser("windows-image",
                        help="the tools-image part of ./boxd-install windows-image build")
    pw.add_argument("action", choices=["kit"])

    args = p.parse_args()
    # SIGTERM (`docker stop`, a cancelled CI job) unwinds like Ctrl-C: the state push in
    # remote_state.synced's finally runs. Python's default would exit without it.
    import signal
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(128 + signum))
    legacy_out = os.path.join(ROOT, "out")
    if os.path.isdir(legacy_out):
        shutil.rmtree(legacy_out)
        log(f"removed legacy {legacy_out} (regenerable; contained rendered secrets)")
    # Local decrypted state the bucket doesn't have yet: anything but `state`
    # (which pushes it) and validate would work from the bucket's copy and
    # then be overwritten by, or overwrite, the local edits.
    for local in (os.path.join(ROOT, "state"), PULLED_STATE):
        if os.path.isdir(local) and os.listdir(local) and args.cmd not in ("validate", "state"):
            rel = os.path.relpath(local, ROOT)
            print(f"error: local state in ./{rel} hasn't been pushed. Push it "
                  f"(./boxd-install --state-dir {rel} state push) or, if the bucket's "
                  f"copy is the one to keep, delete ./{rel}", file=sys.stderr)
            return 1
    # cluster.toml is git-ignored: a fresh clone has only the example.
    if not os.path.exists(args.cluster):
        if args.cluster == os.path.join(ROOT, "cluster.toml"):
            print("No cluster.toml. Copy cluster.example.toml to cluster.toml and edit it "
                  "(on AWS, start from examples/aws/cluster.toml).", file=sys.stderr)
        else:
            print(f"No {args.cluster}.", file=sys.stderr)
        return 2
    # Only now: an early return above would leave these behind.
    if not args.state_dir:
        args.state_dir = PULLED_STATE if args.cmd == "state" else \
            tempfile.mkdtemp(prefix="boxd-installer-state-")
    args.state_dir = os.path.abspath(args.state_dir)
    tmp_out = False
    if not args.out_dir:
        args.out_dir = tempfile.mkdtemp(prefix="boxd-installer-out-")
        tmp_out = True
    try:
        rc = {"validate": cmd_validate, "render": cmd_render,
              "apply": cmd_apply, "destroy": cmd_destroy, "ctl": cmd_ctl,
              "state": cmd_state, "providers": cmd_providers,
              "windows-image": cmd_windows_image}[args.cmd](args)
    except InstallError as e:
        print(f"error: {e}", file=sys.stderr)
        rc = 1
    finally:
        if tmp_out and args.cmd != "render":
            shutil.rmtree(args.out_dir, ignore_errors=True)
        gv = os.path.join(ROOT, "ansible", "group_vars")
        for n in os.listdir(gv) if os.path.isdir(gv) else []:
            p = os.path.join(gv, n)
            if os.path.islink(p) and not os.path.exists(p):
                os.remove(p)
        try:
            os.rmdir(args.state_dir)
        except OSError:
            pass
    if args.cmd == "render":
        print(f"rendered to {args.out_dir} — contains secrets; delete when done")
    return rc
