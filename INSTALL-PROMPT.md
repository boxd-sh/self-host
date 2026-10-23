# Install boxd with Claude Code

A prompt for Claude Code that installs a self-hosted boxd cluster on GCP or
AWS by following this repository's README step by step, stopping after each
step for your OK, and then checks the cluster end to end. It adds no steps of
its own: the README is the procedure, and this prompt is how to walk it.

Start Claude Code in a clone of this repository. You need everything listed
in the README's "Before you start". Fill in the inputs, then paste everything
from "Prompt" down.

---

## Prompt

Install a self-hosted boxd cluster with the installer in this repository by
following README.md, then verify it. Stop after each step, tell me what
happened, and wait for my OK before the next one.

### My inputs
- Cloud: `<gcp | aws>`. Walkthrough: README "Install on GCP" or "Install on AWS".
- GCP project / AWS profile: `<…>`; region `<…>`; zone or AZ `<…>`
- Cluster name: `<CLUSTER>` (node names start with it)
- DNS zone: `<ZONE>`. I own its parent zone and add the delegation records myself.
- Certificate contact email: `<EMAIL>`
- Machine sizes my users need, if not the defaults: `<…>`
- Sign-in: `<the walkthrough's step 6 | single sign-on with Entra ID, Okta, Google Workspace or Keycloak>`
  (README "Single sign-on"; I set it up at the provider). With single sign-on,
  `bootstrap_admin` is `<MY EMAIL>`.

### Ground rules
1. **The README is the procedure.** Before each step, quote which README step
   you're on. Run commands as the README writes them, through `./boxd-install`.
   Don't install terraform, aws, gcloud or ansible on this machine.
2. **One step at a time:** do it, report, wait. If the README is unclear,
   wrong or missing something, say so and suggest the fix. Don't silently
   work around it.
3. **Secrets never show up in chat, in a command line or in a log:** the
   state key, the license key, cloud keys, OAuth secrets. The state key lives
   in `~/.boxd-<CLUSTER>.env` (mode 600, README "Your state key"), and every
   command sources it first. A license key, if I have one, lives in
   `license.key` (README "License key"). Check values by shape, e.g. 64 hex
   characters, without printing them.
4. **Anything interactive is mine:** cloud logins, `providers edit`, saving
   the license key, the cloud console, my DNS provider. Give me the exact
   command or the exact values, then wait.
5. **Long commands** (`apply`, `destroy`) run in the background, logged to
   `apply.log`. Report each phase as it completes: terraform, node access,
   ansible, the worker reboot, control plane, worker registration, templates.
   If one fails, show me the error and your diagnosis before re-running.
   Re-running `apply` is safe.
6. **Never** run `destroy`, delete a bucket or touch state unless I ask in this
   conversation. Don't change installer code without asking.

### After the walkthrough: verify
Once the zone is delegated and `https://app.<ZONE>` answers, check each item
and give me a table of pass/fail with evidence:

1. A worker, over the README's SSH-to-nodes path: `uname -r` is the boxd host
   kernel; `/dev/kvm` exists; nested virtualization is on
   (`/sys/module/kvm_intel/parameters/nested` is `Y`).
2. On a control node: `boxd-ctl status` shows a leader and every worker;
   `boxd-ctl template list` shows every size ready; `boxd-ctl machine-sizes show`
   matches my sizes.
3. `https://app.<ZONE>` returns 200 with a valid certificate.
4. I sign in to the console; we do this together.
5. The CLI (README "The CLI your users install") installs as `boxd` in
   `~/.local/bin` and **replaces** any existing `boxd`. Ask me first, or use a
   clean container (`docker run -it ubuntu:24.04`). Then `boxd auth login`,
   which I approve in my browser.
6. `boxd new t1`; reach it with `boxd connect t1`, with `ssh t1.boxd`, and over
   HTTPS at `https://t1.<ZONE>` (serve something on port 8000 first).
7. A VM can't reach the cloud metadata service: `curl -m3 http://169.254.169.254/`
   inside t1 fails.
8. A second `apply` with no changes: terraform reports no changes and no boxd
   service restarts.

Then delete `t1`, and write `HANDOVER.md` (untracked): the URLs, where the env
file and `cluster.toml` live (and that the state key must be in a password
manager), the `Assets:` version installed, how to update the installer and
upgrade boxd (README "Day-2 operations"), the verification table, and a rough
hourly cost of what's running. Leave the cluster running.
