import argparse
import os
import shutil
import sys
import tempfile

import shutil as _shutil
import subprocess

from . import (ansible, assets, composio, config, control_carry, license, local_ca,
               oidc, remote_state, render, telemetry, tf)
from .state import State
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

def cmd_validate(args) -> int:
    cfg = config.load(args.cluster)
    # A warning, not an error: renaming a node replaces its instance, so a
    # running cluster keeps the names it has.
    if odd := [n.name for n in cfg.nodes if not n.name.startswith(f"{cfg.name}-")]:
        log(f"warning: node names don't start with the cluster name {cfg.name!r}: "
            f"{', '.join(odd)}. They become instance names, so two clusters in one "
            "project or account can collide; rename them before the first apply.")
    # A [license].key_file that is missing or not a key stops here. No
    # terraform state here: workers without machine_type count as new ones.
    warnings = license.free_tier_warnings(cfg, assume_new=True) + telemetry.warnings(cfg)
    for warning in warnings:
        log(f"warning: {warning}")
    print("cluster.toml OK")
    print(telemetry.validate_line(cfg))
    if "oidc" in cfg.doc:
        print(oidc.check_discovery(cfg.doc["oidc"]))
    return 0

def cmd_render(args) -> int:
    cfg = config.load(args.cluster)
    if args.fake_tf:
        state = State(args.state_dir)
        _write_out(_renderer(cfg, state, args.fake_tf).render_all(), args.out_dir)
        return 0
    with remote_state.synced(cfg, args.state_dir, rescue_dir=PULLED_STATE):
        state = State(args.state_dir)
        _write_out(_renderer(cfg, state, None).render_all(), args.out_dir)
    return 0

def cmd_apply(args) -> int:
    cfg = config.load(args.cluster)
    with remote_state.synced(cfg, args.state_dir, rescue_dir=PULLED_STATE):
        return _apply(cfg, args)

def _apply(cfg, args) -> int:
    # Before anything else: a [license].key_file that is missing or not a
    # key stops the apply; it never falls back to the free tier.
    key = license.key_path(cfg)
    state = State(args.state_dir)
    # The render would refuse a half-configured [oidc] only after terraform.
    oidc_table = {**cfg.doc.get("oidc", {}), **state.load_providers().get("oidc", {})}
    if oidc_table and (why := render.oidc_render_error(oidc_table)):
        raise InstallError(why)
    state.ensure_keypairs()
    ansible.set_keys_dir(state.keys_dir)
    os.makedirs(args.out_dir, exist_ok=True)

    assets_root = assets.ensure(cfg, args.cache_dir, state)
    assets.console_tarball(assets_root)

    tf.init(cfg)
    # A worker that exists keeps its type: the license never resizes one.
    existing = tf.instance_types(cfg)
    for warning in license.free_tier_warnings(cfg, existing):
        log(f"warning: {warning}")
    tfvars = tf.write_tfvars(cfg, state, args.out_dir, existing)
    plan = os.path.join(args.out_dir, "tf.plan")
    tf.plan(cfg, tfvars, plan)
    # Before the apply: a replaced control node takes its state with it.
    control_carry.save(cfg, state, control_carry.plan_json(cfg, plan))
    tf.apply_plan(cfg, plan)
    tf_out = tf.read_outputs(cfg, state)
    ansible.set_tf_outputs(tf_out)

    cfg.apply_overlay(state.load_providers())
    # The sign-in clients live in the providers overlay, not cfg.doc.
    if inert := config.inert_sign_in({t: cfg.table(t) for t in ("oidc", "google", "github")
                                      if cfg.table(t)}):
        log(f"warning: {inert}")
    composio.sync_auth_configs(cfg, state, assets_root)
    alloc = state.allocate(cfg)
    r = render.Renderer(cfg, alloc, tf_out, state, assets_root=assets_root)
    _write_out(r.render_all(), args.out_dir)

    ansible.preflight(cfg)
    ansible.converge(cfg, args.out_dir, assets_root,
                     assets.host_kernel_release(assets_root),
                     control_carry.carry_dir(state))

    ansible.wait_for_leader(cfg)
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

    # Every node (control, proxy, console) runs the new bundle now, so the
    # cluster ID can be made (once; a no-op after). Before the key: a bound
    # key needs it.
    ansible.init_cluster_id(cfg)
    # Before the templates, so the key is in place before any machine is made.
    if key:
        ansible.install_license(cfg, key)
    value, source = telemetry.setting(cfg)
    if value is None:
        log("telemetry: [telemetry] enabled is not set; the cluster's setting is left as it is")
    else:
        ansible.set_telemetry(cfg, value, explicit=source == telemetry.CLUSTER_TOML)

    image, tag = assets.computer_image(assets_root)
    ansible.seed_templates(cfg, image, tag, cfg.assets_version)
    ansible.configure_orgs(cfg, cfg.machine_sizes())

    # The exact bundle this cluster now runs — with a channel, the only
    # record of what to pin to come back here (README "Upgrades and rollback").
    state.data["applied_assets"] = {"sha": cfg.assets_version, "channel": cfg.assets_channel}
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
            "key and re-apply (README Troubleshooting, \"Unknown external account binding\"):\n"
            "  ./boxd-install state reset-eab\n  ./boxd-install apply")
    return 0

def cmd_destroy(args) -> int:
    cfg = config.load(args.cluster)
    with remote_state.synced(cfg, args.state_dir, rescue_dir=PULLED_STATE):
        state = State(args.state_dir)
        tfvars = os.path.join(args.out_dir, "terraform.tfvars.json")
        tf.init(cfg)
        if not os.path.exists(tfvars):
            # No license check: destroying never depends on the key file.
            tfvars = tf.write_tfvars(cfg, state, args.out_dir, tf.instance_types(cfg),
                                     for_destroy=True)
        tf.destroy(cfg, tfvars)
        control_carry.clear(state)
    print("infra destroyed. Encrypted state (ids, keys) kept in the state "
          "bucket — delete the installer-state.enc object only if this "
          "cluster is gone for good.")
    return 0

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
# (README "Single sign-on"); only the client secret goes here. Redirect URI:
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
    ps = sub.add_parser("state")
    ps.add_argument("action", choices=["pull", "push", "reset-eab"],
                    help="pull: decrypt into ./pulled-state; push: encrypt it back and delete it; "
                         "reset-eab: forget the stored ACME EAB key so the next apply mints a new one")
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

    args = p.parse_args()
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
              "apply": cmd_apply, "destroy": cmd_destroy,
              "state": cmd_state, "providers": cmd_providers}[args.cmd](args)
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
