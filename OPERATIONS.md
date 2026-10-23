# Operating a boxd cluster

Day-to-day operations for a self-hosted boxd cluster: managing people and
orgs, capacity, machines and workers, and what to do when something goes
wrong. Installing, adding nodes and upgrading are in [README.md](README.md).

Everything here uses `boxd-ctl`, the operator CLI on every control node, plus
`systemctl` and `journalctl` on the nodes. `boxd-ctl <command> --help` is the
reference for every command and ends with worked examples; this document is
organised by task and adds the context the help can't.

## Contents

- [Getting started](#getting-started)
- [Which ids go where](#which-ids-go-where)
- [What the installer owns](#what-the-installer-owns)
- [People and orgs](#people-and-orgs)
- [Capacity and machine sizes](#capacity-and-machine-sizes)
- [Machines](#machines)
- [Workers](#workers)
- [The free tier and the license key](#the-free-tier-and-the-license-key)
- [Domains](#domains)
- [Certificates from your own CA](#certificates-from-your-own-ca)
- [Backups](#backups)
- [Snapshot archive](#snapshot-archive)
- [Troubleshooting](#troubleshooting)
- [Logs and support](#logs-and-support)
- [Ask boxd first](#ask-boxd-first)

## Getting started

Connect to a control node. There is no public SSH; on GCP go through IAP, on
AWS through SSM (see [README.md](README.md#ssh-to-nodes)):

```sh
./boxd-install gcloud compute ssh ubuntu@mycluster-control-0 --tunnel-through-iap \
    --project <project> --zone <gcp-zone>
./boxd-install aws ssm start-session --target <instance-id>      # AWS
```

`boxd-ctl` talks to the control socket, which belongs to the `boxd` group.
Join it once:

```sh
sudo usermod -aG boxd $USER
newgrp boxd        # or log out and back in
boxd-ctl status
```

`boxd-ctl status` names the leader. Most commands work from any control node,
but `sync-config` and `remove-peer` must run on the leader: on that node,
`status` prints `(this node)` after the leader's address.

**Run commands one at a time.** Scripts that fire many `boxd-ctl` calls in
parallel against one control node can trip its connection limit, and the
failures are easy to miss. Loop sequentially and check each result.

## Which ids go where

| Id | Looks like | Where to find it | Used by |
|---|---|---|---|
| Org id | as printed | `boxd-ctl org list` | every `org` command |
| User id | as printed | `boxd-ctl user list` | `org add-member`, `org set-owner`, `vm reassign`, `pin` |
| VM id | a UUID | `boxd-ctl vm list` (or `org vms <org-id>`) | every `vm` command |
| Node id | `worker-1` for a worker | `boxd-ctl node list` | `node exclude/include/remove/mark-dead`, `vm migrate --to`, `pin --worker-id` |
| Raft node id | a number, e.g. `7` | `node_id` at the top of the node's `/etc/boxd/*.toml` | `remove-peer` only |

Most `vm` commands take the VM id, not its name. Find it with:

```sh
boxd-ctl vm list | grep myapp
```

## What the installer owns

Some settings are rewritten by every `./boxd-install apply`. Change those in
`cluster.toml` and re-apply; a `boxd-ctl` change to them lasts only until the
next apply. Everything else lives only in the cluster's own state and is
changed with `boxd-ctl`.

| Owned by `cluster.toml` + `apply` | Owned by `boxd-ctl` |
|---|---|
| Machine sizes (`[templates].sizes` → `machine-sizes set`) | Org default machine size and `--max-vms` |
| Every org's vCPU / memory quota (the largest size) and its comp | |
| Default image (`image set`) | Org auto-hibernate and backup defaults |
| The `computer-*` templates; `apply` deletes any `computer-*` template that isn't one of its own | Members, admins and owners |
| Node configs in `/etc/boxd/*.toml` (never edit them in place) | Worker exclusions and pins |
| | Custom domains, Tailscale and NetBird, the signups gate |

Two consequences:

- Name your own templates anything but `computer-*`.
- Everything in the right-hand column is stored only in the control nodes'
  replicated state. It is not in `cluster.toml` and not in the installer's
  encrypted state, so `apply` cannot recreate it. Run three control nodes in
  production: a cluster with one control node loses these records with that
  node's disk. (When `apply` itself replaces a control node, for example
  for a subnet or `internal_ip` change, it carries the state across; see
  README "Switching an existing cluster". A lost disk it can't.)

## People and orgs

A self-hosted cluster has one working org. The first person to sign in names
it and owns it; everyone after joins it as a member. Members are normally
invited from the console; these commands are the operator's overrides.

```sh
boxd-ctl org list
boxd-ctl user list

boxd-ctl org set-owner <org-id> <user-id>     # transfer ownership
boxd-ctl org add-admin <org-id> <user-id>
boxd-ctl org remove-admin <org-id> <user-id>  # back to member
boxd-ctl org add-member <org-id> <user-id>
boxd-ctl org rm-member <org-id> <user-id>

boxd-ctl org invites <org-id>                 # pending, expired and accepted
boxd-ctl org revoke-invite <token>
```

**Closing sign-ups.** Anyone who can complete sign-in (your Google Workspace
with an Internal consent screen, any GitHub account if GitHub sign-in is
configured, or with single sign-on whoever your identity provider lets into
the application) gets an account and joins the org. To stop new accounts while
keeping existing users signed in:

```sh
boxd-ctl signups pause
boxd-ctl signups status
boxd-ctl signups resume
```

**Removing someone.** `org rm-member` is refused while they still own
machines in the org, and for the org's owner. Hand each machine to someone
who stays (or destroy it, after checking with its users), then remove them:

```sh
boxd-ctl vm list | grep <user-id>
boxd-ctl vm reassign <vm-id> --to-user <other-user-id>
boxd-ctl org rm-member <org-id> <user-id>
```

To remove the owner, transfer ownership first with `org set-owner`.

**When someone leaves**, offboard them. `user disable` refuses while they
own any machine, and lists each with the commands to choose from, so deal
with those first:

```sh
boxd-ctl vm list | grep <user-id>
boxd-ctl vm stop <vm-id>                                # a private machine: stop it first
boxd-ctl vm reassign <vm-id> --to-user <other-user-id>  # to a member of the machine's org
boxd-ctl vm destroy <vm-id>                             # or, after checking with its users
boxd-ctl user disable <user-id>
boxd-ctl user enable <user-id>                          # lets them back in
boxd-ctl org rm-member <org-id> <user-id>               # optional: out of an org for good
```

A private machine is served its owner's credentials, so stop it before
reassigning it, and review it before starting it again. The disable
suspends their credentials at once (CLI logins, SSH keys, API keys,
console sessions) and refuses their sign-in; org API keys, and the tokens
shared machines use, act for the org and keep working. It suspends rather
than revokes: `user enable` restores their CLI logins and SSH keys that
haven't expired, and they sign in to the console again. They stay a
member of their orgs until you remove them. An org they own stays theirs: give it a new owner with
`org set-owner`. boxd doesn't find out that someone left your identity
provider, so this is always an operator's step, and until then what they
hold keeps working. With the cluster reachable only from your network, they
lose their way in with their network access.

**Single sign-on and existing accounts** (README "Single sign-on"). A
person's first SSO sign-in gets a new account unless it links by email to
the one they have, for the domains in `[oidc] link_email_domains`. It doesn't
link when the provider says the email isn't verified, for an Entra ID guest
from another tenant, or when the email differs or is on two accounts. For
someone it didn't link, move their machines to the new account:

```sh
boxd-ctl user list | grep <email>                        # the old and the new account
boxd-ctl org add-member <org-id> <new-user-id>           # first, for a machine in another org
boxd-ctl vm reassign <vm-id> --to-user <new-user-id>
```

**Two commands destroy machines as a side effect**, permanently:

- `boxd-ctl user remove <user-id>` deletes the account **and destroys every
  machine the user owns, with its backups**. Reassign what should stay first.
- `boxd-ctl org destroy <org-id>` **destroys every machine in the org**. It is
  refused for the cluster's own org, but not for any other org you created.

Check `boxd-ctl vm list` or `boxd-ctl org vms <org-id>` before running
either.

## Capacity and machine sizes

Three settings decide how big a machine can be and what a user gets by
default:

1. **The cluster's size list**: the sizes anyone may request at all. It comes
   from `[templates].sizes` in `cluster.toml`, one prewarmed template per size.
   `boxd-ctl machine-sizes show` prints it.
2. **The org's quota**: the largest vCPU count and memory a single machine may
   have, and the number of machines.
3. **The org's default size**: what a machine gets when its creator names no
   size.

```sh
boxd-ctl machine-sizes show
boxd-ctl org get-machine-defaults <org-id>

boxd-ctl org set-quota <org-id> --cpu 4 --mem 32G      # largest machine
boxd-ctl org set-quota <org-id> --max-vms 100          # more machines
boxd-ctl org set-quota <org-id> --max-vms 0            # no new machines at all
boxd-ctl org set-machine-defaults <org-id> --vcpu 2 --memory 8G
```

To offer a new size, add it to `[templates].sizes` and re-apply (see
[README.md](README.md)). Every apply sets each org's vCPU / memory quota to
the largest listed vCPU count and memory, so a hand-set `--cpu`/`--mem`
lasts only until the next apply; `--max-vms` is left alone.

**Memory and hibernation.** A running machine keeps the host memory its guest
has touched until it hibernates or stops; the host does not reclaim it from a
running guest. Idle machines suspend and then hibernate on their own
(`[control] default_auto_suspend_secs` / `default_auto_hibernate_secs` in
`cluster.toml`), and hibernated machines wake on the next SSH or HTTPS
request. Keep auto-hibernate on for most machines: it is what keeps a worker's
memory and swap in check.

```sh
boxd-ctl org set-auto-hibernate <org-id> 3600   # new VMs: hibernate after 1 h idle
boxd-ctl org set-auto-hibernate <org-id> --clear
boxd-ctl vm auto-hibernate <vm-id> 0            # this VM never hibernates
```

The org setting applies to machines created afterwards; existing machines
keep theirs.

## Machines

```sh
boxd-ctl vm list                       # every VM: name, id, owner, worker, status
boxd-ctl org vms <org-id>

boxd-ctl vm stop <vm-id>
boxd-ctl vm start <vm-id>
boxd-ctl vm reboot <vm-id>
boxd-ctl vm hibernate <vm-id>
boxd-ctl vm wake <vm-id>
```

Changes that reboot a running machine (memory is lost, the disk is kept):

```sh
boxd-ctl vm resize <vm-id> --vcpu 4 --memory 16G   # must be on the size list
boxd-ctl vm grow-disk <vm-id> --add 50G            # disks only grow
boxd-ctl vm rename <vm-id> new-name                # URL and SSH follow the new name
```

A stopped machine picks the change up at its next start. Resizing or growing
a suspended or hibernated machine is refused: wake it first.

Handing a machine to someone else (they must already be in the org):

```sh
boxd-ctl vm reassign <vm-id> --to-user <user-id>
```

**Before destroying a machine**, check with its users. A machine with no
recent SSH sessions may still be serving HTTPS, running jobs, or simply
waiting for its owner to come back from holiday. `boxd-ctl vm destroy` is
permanent; restoring needs a backup (see [Backups](#backups)).

```sh
boxd-ctl vm destroy <vm-id>
```

## Workers

```sh
boxd-ctl node list            # NODE ID, roles, internal IP, status, placement
```

**Maintenance on a worker.** Stop new machines landing there; machines
already on it keep running:

```sh
boxd-ctl node exclude worker-1
# ... maintenance ...
boxd-ctl node include worker-1
```

Restarting the `boxd-worker` service does not stop the machines on it: each
machine runs in its own process, and the worker reattaches when it comes back.
Rebooting the host does stop them.

**Draining a worker.** Exclude it, then move each machine off:

```sh
boxd-ctl node exclude worker-1
boxd-ctl vm list | grep worker-1
boxd-ctl vm migrate <vm-id> --to worker-2
```

Each migration reboots the machine once. A hibernated or stopped machine
arrives running: hibernate or stop it again afterwards. Machines move only
between workers with the same CPU model, which is why all workers of a
cluster should be one instance family.

**Retiring a worker.** Once it is drained and excluded, mark it dead so the
cluster stops waiting on it (template builds wait for every healthy worker):

```sh
boxd-ctl node mark-dead worker-1
```

Taking the node out of the cluster for good (`remove-peer`, then deleting it
from `cluster.toml`) has steps that must happen in order on every node:
[ask boxd first](#ask-boxd-first).

**Pinning a user to a worker.** That user's new machines land only on the
pinned worker:

```sh
boxd-ctl pin add --user-id <user-id> --worker-id worker-2
boxd-ctl pin list
boxd-ctl pin remove --user-id <user-id> --worker-id worker-2
```

## The free tier and the license key

Without a license key that has the `unlimited-worker-vcpus` feature, a
cluster has the **free tier**: its workers may have at most **16 vCPUs** in
all. That is the sum of the logical CPUs each worker host reports. The vCPUs
your machines get don't count, and a worker marked dead doesn't count (but
see [Retiring a worker for the free tier](#retiring-a-worker-for-the-free-tier)).
Only a key with `unlimited-worker-vcpus` lifts the limit: `boxd-ctl license
status` lists a key's features, and a valid key without that feature still
leaves the limit in place. With `[license] key_file` set, the installer
installs the key on every `apply` (see [License key](README.md#license-key)
in the README).

Telemetry can be turned off (`[telemetry] enabled = false`) only with a key
that has the `telemetry-opt-out` feature. Once turned off it stays off, also
after that key expires. A cluster that never had such a key always sends boxd
its hourly usage report (see [Telemetry](README.md#telemetry) in the README).

**What happens over the limit.** boxd refuses every request that adds a
machine: a new machine, a fork, and a machine from a snapshot. It refuses
them from every surface: the console, the `boxd` CLI on a laptop or inside a
machine, the API and the SDKs. Everything else keeps working: machines that
exist start, stop, hibernate, wake, resize, migrate, restore checkpoints and
backups, and are deleted as usual. boxd never stops, pauses or deletes a
machine because of the license. There is no grace period. While you swap a
worker, with the old and the new one both counted, new machines wait until
the old one is retired as below. Marking it dead is not enough on its own:
until it is out of `cluster.toml`, it counts again after every `apply` or
reboot.

Users see this message:

```text
New machines are not allowed: this cluster has 24 worker vCPUs, and the boxd
free tier allows 16. Ask your cluster operator to add a license key or to
remove workers.
```

A counted worker that has never reported its CPU count makes the count
unknown, and without such a key that also refuses new machines. A worker
added on this boxd version gets 10 minutes to report first. A worker that
registered before boxd recorded registration times has no such window: if it
has not reported, it makes the count unknown at once. The message names the
worker:

```text
New machines are not allowed: worker worker-2 has not reported its CPU count,
so boxd cannot check this cluster against the free tier limit of 16 worker
vCPUs. Ask your cluster operator to check that worker, or to add a license key.
```

On that worker, check that `boxd-worker` runs and can reach the control
nodes (`systemctl status boxd-worker`, `journalctl -u boxd-worker`). A
running worker reports within a minute. If the host is gone for good, retire
it as below.

Owners and admins of a team org see a banner at the top of the console; on a
self-hosted cluster that is the cluster org. Owning a personal org (every
user does) is not enough, and plain members never see it. The banner shows
when the cluster is over the limit (it can't be closed while that lasts), and
when the key expires within 30 days or is in its grace period (these can be
snoozed for a day).

**The cluster ID.** A key can be bound to one cluster, by its random ID. The
ID is made once, by `boxd-ctl license init-cluster-id` on a control node,
after the whole deploy that brings the license support is done (every
control, proxy and console node on the new version). `apply` runs it for
you; it is a no-op once the ID exists. Until then `boxd-ctl license status`
shows `cluster id:    not set (run boxd-ctl license init-cluster-id after the
deploy)`, and a key bound to the cluster can't be installed.

The ID lives with the cluster's data on the control nodes. A cluster rebuilt
with `destroy` and `apply` starts empty and gets a new ID, so a key bound to
the old one is refused there: ask boxd for a key bound to the new ID, or use
an unbound one.

**Check the state** on a control node:

```sh
boxd-ctl license status
```

```text
cluster id:    0f9033df4a8aef67587648d542016c7b
license:       none
mode:          enforce
worker vCPUs:  24, limit 16: OVER THE LIMIT
refuses:       New machines are not allowed: this cluster has 24 worker vCPUs, ...
workers:
  worker-1  healthy      24
```

It shows the key's state (`none`, `valid`, `expiring soon`, `grace`,
`expired`, `rejected: <reason>`), its licensee, features and expiry, the
cluster ID a key can be bound to, the worker vCPU count and each worker. It
never shows the key.

**Get under the limit again**, either way:

- **Install a license key with `unlimited-worker-vcpus`.** With `[license]
  key_file` set, put the new key in that file and run `./boxd-install
  apply`: every `apply` installs the file again and replaces any key
  installed by hand. Only on a cluster without `key_file`, install it by
  hand on a control node, from stdin so it stays out of your shell history:

  ```sh
  boxd-ctl license install < license.key
  ```

  boxd checks the key's signature (and, for a bound key, the cluster ID)
  before it stores it, and refuses a key that doesn't verify. A new key
  replaces the old one. New machines are allowed at once.
- **Retire workers** until the rest have 16 vCPUs or fewer, as below.

### Retiring a worker for the free tier

A worker marked dead stops counting, but **it counts again as soon as its
`boxd-worker` starts**: the service registers the worker on every start,
which marks it healthy. Every `./boxd-install apply` enables and starts
`boxd-worker` on every worker listed in `cluster.toml`, and a reboot starts
it too. Stopping or disabling the service by hand doesn't last: the next
`apply` undoes it.

To keep a retired worker out of the count:

1. Drain it and exclude it ([Workers](#workers)).
2. Mark it dead: `boxd-ctl node mark-dead worker-1`.
3. Take it out of `cluster.toml`, as in [Workers](#workers): those steps
   must happen in order on every node, so [ask boxd first](#ask-boxd-first).

Until step 3 is done, the worker counts again after each `apply` or reboot.
`boxd-ctl license status` lists a retired worker as `dead`, `not counted`.
If it shows `healthy` again, its `boxd-worker` started.

### When a key expires

Its features keep working for its grace period (at most 30 days). `boxd-ctl
license status` says `expiring soon` from 30 days before the expiry, and the
control leader logs a `boxd::license` warning once a day. After the grace
period the cluster has the free tier again: a cluster with more than 16
worker vCPUs refuses new machines. Telemetry that the key let you turn off
stays off. Ask boxd for a new key in time.

### Signals

For your monitoring, from the control leader's metrics: `boxd_license_state`
(0 none, 1 valid, 2 expiring soon, 3 grace, 4 expired, 5 rejected),
`boxd_license_days_left`, `boxd_license_over_limit`,
`boxd_license_worker_vcpus` and `boxd_license_unknown_count_workers`. Every
node that answers requests counts its refusals in
`boxd_license_refused_total`, one per refused request.

## Domains

Every machine is reachable at `https://<name>.<zone>`. Two ways to add your
own domains:

```sh
# One domain for one machine
boxd-ctl domain add myapp example.com
boxd-ctl domain list                  # pending until DNS verifies and the certificate is issued
boxd-ctl domain rm example.com

# A wildcard for the whole org: <vm>.dev.example.com. Delegate the
# domain's NS records to the cluster's nameservers first.
boxd-ctl org set-domain <org-id> dev.example.com
boxd-ctl org clear-domain <org-id>
```

**Tailscale.** `boxd-ctl org tailscale enable` serves the org's machines over
your own tailnet and *only* there: they stop answering on the public address.
Read `boxd-ctl org tailscale enable --help` before using it; the auth key
must be reusable and tagged, and it expires.

**NetBird.** `boxd-ctl org netbird enable <org-id> --setup-key <key>` does
the same over the org's own NetBird network (`--management-url` for a
self-hosted NetBird). The setup key must be reusable and not ephemeral: every
proxy registers its own peer. `boxd-ctl org netbird status <org-id>` lists the
proxies that have registered. An org uses Tailscale or NetBird, not both.

Each proxy joins those networks under a name. Add an empty `[proxy.tailscale]`
or `[proxy.netbird]` table to `cluster.toml` and `apply` names every proxy
`boxd-<cluster>-<node>`, so two clusters on one network (staging and
production, say) don't collide. Without the table, boxd names each proxy
after its node id (`boxd-proxy-<n>`), which repeats from cluster to cluster.

## Certificates from your own CA

Only for clusters with `[acme] ca = "local"` (setup in
[README.md](README.md#tls-from-your-own-ca)). `boxd-control` signs every
certificate with your intermediate CA; certificates live 30 days and renew
with 10 days left. There is no revocation: a certificate that leaked stays
valid until it expires, or until you rotate the intermediate.

Domains work as above, with two differences, both checked against
`[acme].dns_verify_resolver` (your internal resolver):

- a custom domain also needs `_acme-challenge.<domain>` to be a CNAME to
  exactly `<machine>.<zone>`; the `*.<domain>` CNAME provides it. Until it
  resolves, `boxd-ctl domain list` shows the domain pending with the reason;
- an org domain is delegated in your **internal** zone: NS records for
  `dev.example.com` → `ns1.<zone>`, `ns2.<zone>`.

If the intermediate has name constraints, every custom domain and org domain
must fall inside them, or it stays pending.

**Check which CA signed a certificate:**

```sh
openssl s_client -connect app.<zone>:443 -servername app.<zone> -showcerts </dev/null \
    | openssl x509 -noout -issuer -enddate
```

**Rotate the intermediate** (before it expires, or after a key leak). From
the installer checkout:

```sh
./boxd-install providers set-tls-ca new-intermediate.pem new-intermediate.key
./boxd-install apply
```

`apply` replaces `/etc/boxd/ca/` on the control nodes and restarts
`boxd-control` one node at a time. The leader then re-signs every certificate
the new intermediate didn't issue, over its next sweeps (one a minute).
Laptops need no change as long as the new intermediate chains to the same
root. Keep the old intermediate valid until the check above shows the new
issuer everywhere. To move to a new **root**, distribute it to laptops first.

**The CA is broken** (expired, wrong key, wrong file):

- **What you see:** new machines, new custom domains and renewals stay
  without a certificate; existing certificates keep being served until they
  expire. `boxd-control` keeps running.
- **Confirm:** on the leader (`boxd-ctl status`),
  `journalctl -u boxd-control | grep "local CA"` shows
  `local CA unusable, not issuing certs` with the reason. Expired leaves show
  as an expired certificate in the browser and in the `openssl` check above.
- **Fix:** run `set-tls-ca` with a valid pair (it rejects what boxd-control
  would) and `apply`. The next sweep issues everything that is missing or
  due.
- **Don't** edit `/etc/boxd/ca/` by hand: the next `apply` puts the stored
  pair back. If you must, keep the files `root:boxd-ca 0640` and restart
  `boxd-control`, which reads the CA once and keeps it.

## Backups

The installer provisions a bucket for machine disk backups, but nothing is
backed up until a schedule is set. Set an org default for new machines:

```sh
# Daily, kept 7 days
boxd-ctl org set-backups-default <org-id> --period-secs 86400 --retention-secs 604800
```

Existing machines are unchanged, and a fork copies its source's schedule.
Users manage a machine's own schedule and restores from their CLI:

```sh
boxd machine config set myapp backups.period 24h
boxd machine config set myapp backups.retention 14d
boxd machine backup list myapp
boxd machine backup restore myapp <backup-id>
```

A restore rewrites the disk in place and cold-boots the machine; anything
written since the backup is gone. Backups cover disks only, not memory.

Machine backups do not cover the control plane: see
[What the installer owns](#what-the-installer-owns).

With `[aws] bucket_access = "vpc"` (README "Buckets only inside the VPC"),
backup objects can be read and deleted only from inside the VPC: run
`aws s3` commands against the bucket from a node, not from your laptop.

## Snapshot archive

Only for clusters with `[snapshot_archive]` in `cluster.toml` (setup in
[README.md](README.md#snapshot-archive)), on an assets version whose
`boxd-ctl snapshot --help` lists `archive`.

A snapshot's bytes (its disk and memory images) live on the workers' data
disks, often on several workers. Archiving moves them into the archive
bucket and frees every worker's copy; unarchiving brings them back onto one
worker. Use it for snapshots nobody has created from in a while, to free
worker disk without deleting them.

```sh
boxd-ctl snapshot list                         # least recently used first: SIZE, LAST USED, STORAGE, WORKER
boxd-ctl snapshot archive <snapshot>           # id, or name if unique in the cluster
boxd-ctl snapshot unarchive <snapshot>
boxd-ctl snapshot unarchive <snapshot> --worker worker-2   # exactly this worker, no fallback
```

`STORAGE` is where the bytes are:

| `STORAGE` | Bytes | Create machines from it, save it again |
|---|---|---|
| `local` | on workers' disks | yes |
| `archiving` | on disk, being uploaded | refused |
| `archived` | in the bucket only | refused |
| `unarchiving` | being restored onto one worker | refused |

Users see the same state; a create from a snapshot that isn't `local` is
refused with a message saying so. Unarchive it first.

- **Both run in the background.** The command returns once the transfer is
  assigned to a worker; watch `snapshot list` until `STORAGE` settles. There
  is no progress view, only `SINCE`. Transfers run one at a time per worker.
- **A failed transfer** goes back to where it started (`local` or
  `archived`), and `snapshot list` shows the reason in `REASON`. Fix what it
  names, then run the same command again. A short network outage fails a
  transfer; re-running is the fix.
- **Nothing times out.** If the worker doing a transfer dies or restarts and
  the state doesn't move for more than a few minutes, run the same command
  again: it reassigns the transfer to a new attempt.
- **Changed your mind during an upload?** `unarchive` cancels it; the
  workers' copies were never touched.
- **Unarchive needs disk** on the target worker: about the snapshot's size
  plus 5 % of that worker's disk. Without `--worker`, the cluster picks a
  healthy worker that has the room.
- **Disk comes back gradually.** Machines already running from the snapshot
  may still map its memory image; that space is freed when the last of them
  hibernates, stops or is destroyed.
- **Deleting an archived snapshot leaves its copy in the bucket.** Delete the
  folder from `snapshot list --json` yourself if you don't want it.
- **Before removing `[snapshot_archive]`** from `cluster.toml`, unarchive
  everything that is `archived`: those bytes exist only in the bucket, and on
  AWS the next `apply` tries to delete it (it stops if the bucket isn't
  empty).

## Troubleshooting

Each entry: what you see, how to confirm it, what to do, and what not to do.

### A machine is stuck in `starting` or `pending`

- **Confirm:** `boxd-ctl vm list | grep <name>` shows the status for more than
  a couple of minutes. Note the worker it is on.
- **Fix:** stop it, then start it. It cold-boots: memory is lost, the disk is
  kept.

  ```sh
  boxd-ctl vm stop <vm-id>
  boxd-ctl vm start <vm-id>
  ```
- **Don't:** destroy and recreate it. The disk is fine.
- If it happens again, collect the worker's log for that VM id (see
  [Logs and support](#logs-and-support)) and send it to boxd.

### A machine is stuck in `migrating`

- **Confirm:** `vm list` shows `migrating` long after the move should have
  finished, usually because a worker restarted mid-migration.
- **Fix:**

  ```sh
  boxd-ctl vm migration-cancel <vm-id> --reason "worker-1 restarted"
  boxd-ctl vm migration-cancel <vm-id> --to-stopped   # leave it stopped instead
  ```
- Cancelling doesn't stop a copy already in flight, so the target worker may
  keep a stale copy of the machine's files. Mention it to boxd.

### A worker's disk is filling up

Workers keep machine disks and their swap file on the data disk
(`/mnt/boxd-data`) and the cluster's own records on the boot disk
(`/var/lib/boxd`).

- **Confirm:** on the worker, `df -h / /mnt/boxd-data`.
- **Data disk:** exclude the worker (`node exclude`) so no new machines land
  there, then migrate machines off or add workers. Destroy machines only
  after checking with their users. With a
  [snapshot archive](#snapshot-archive), archiving snapshots nobody uses
  (`snapshot list`, least recently used first) frees their space on every
  worker.
- **Boot disk:** a full boot disk stops the worker from recording anything,
  while the service itself keeps running and looks healthy. Free space first
  (old logs in `/var/log/boxd`, `journalctl --vacuum-size=1G`), then contact
  boxd. **Don't restart `boxd-worker` on a full boot disk**: on restart it
  reconciles machines from records it could not update, and running machines
  can come back from stale state.
- Check disk space on every worker regularly. Nothing in the installed
  cluster alerts on it.

### A worker is short of memory

- **Confirm:** on the worker, `free -g` and `swapon --show`. Heavy swap use
  with many machines running is the sign.
- **Fix:** hibernate idle machines (`vm hibernate`), keep auto-hibernate on
  for the org, and spread load with `node exclude` and `vm migrate` or more
  workers.
- Under sustained swap pressure, a machine can freeze and fail to hibernate
  ("Vcpu is in unexpected state"). Stop and start it to recover it.

### `boxd-ctl status` shows no leader

- **Confirm:** `Leader: (unknown)`. On each control node,
  `systemctl status boxd-control` and `journalctl -u boxd-control -n 200`.
- The cluster needs a majority of its control nodes (2 of 3) to make any
  change; machines that are already running keep running. Bring the stopped
  control nodes back. If they don't come back, contact boxd.

### The console or sign-in is broken

See [Troubleshooting in README.md](README.md#troubleshooting): DNS
delegation, certificates, and missing sign-in buttons.

## Logs and support

Services are systemd units named after their role:

| Unit | On |
|---|---|
| `boxd-control` | control nodes |
| `boxd-worker` | workers |
| `boxd-proxy` | proxy nodes |
| `boxd-console` | the console node |
| `boxd-dns` | DNS nodes |
| `clickhouse-server` | the metrics node |

```sh
sudo journalctl -u boxd-worker --since "1 hour ago"
sudo journalctl -u boxd-worker --since "1 hour ago" | grep <vm-id>
```

When you contact boxd, include:

- the assets version you run (the `Assets:` line `./boxd-install apply` prints);
- `boxd-ctl status` and `boxd-ctl node list`;
- for a machine problem, its line from `boxd-ctl vm list` and the
  `boxd-worker` log from the worker it is on, filtered by its VM id;
- what you ran and what it printed.

## Ask boxd first

These are safe only in a particular order, or can't be undone:

- Removing any node from the cluster (`remove-peer`, `node remove`, or
  deleting it from `cluster.toml`). Raft node ids must never be reused, and
  peers keep dialling a removed node until restarted without it.
- Going back to an older `assets_version`. A newer version may have migrated
  stored state that an older one can't read.
- Restarting `boxd-worker` on a worker whose boot disk is full.
- Editing anything under `/etc/boxd` or `/var/lib/boxd` by hand.
- `user remove` and `org destroy` while you are unsure what they own: both
  destroy machines permanently (see [People and orgs](#people-and-orgs)).
- Any `boxd-ctl` command that isn't in `boxd-ctl --help`. A few levers are
  kept for boxd's own use and run only on our instruction.
