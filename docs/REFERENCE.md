# boxd self-host reference

Configuration, day-2 operations and troubleshooting for a cluster installed
with [INSTALL.md](INSTALL.md). Running the cluster with `boxd-ctl` is in
[OPERATIONS.md](../OPERATIONS.md).

## Configuration

### Nodes and roles

Roles: `control` (raft voter, so use an odd count), `worker`, `proxy`,
`console`, `dns:nsN` (numbered nameserver slots), and `metrics` (ClickHouse:
powers the console's per-VM CPU, memory and disk graphs; fine on the control
node). Roles can share a node.

- **Node names** become instance names: start them with the cluster name, so
  two clusters in one project or account can't collide (`validate` warns
  otherwise). Renaming a node later replaces its instance.
- **Machine types** have defaults per role. `machine_type` and `boot_disk_gb`
  are per-node overrides. Workers must set `data_disk_gb`, because VM disks
  live there. On AWS a worker can use its local NVMe drives instead:
  `data_disk = "instance-store"` ([below](#instance-store-data-disk-aws)).
- **The network interface** boxd uses on the private network is, on each
  node, the device that holds its `internal_ip`, read from the host at every
  apply: instance types name it differently (AWS: `ens5`, `enp39s0` on m8i;
  GCP: `ens4`, `ens3` with gVNIC). `interface` under `[aws]` or `[gcp]` sets
  one name for every node instead; on bare metal it is required. A rendered
  config (`render`) shows `@boxd_node_interface@` where each node's name goes.
- **Nameservers:** the examples have one (`dns:ns1`). For redundancy, give a
  second node `dns:ns2`; `apply` then prints glue records for both. Today the
  zone's own NS answer always lists `ns1` and `ns2`, even with one
  nameserver. Resolvers that try the missing `ns2` fall back to `ns1`, so
  lookups work, just slower now and then. A boxd release will fix this. For
  production, run two.

### Instance-store data disk (AWS)

On an EC2 type with instance-store NVMe (a `d` after the generation, such as
`m8id`, `c8id` and `r8id`, or the storage-optimized `i` families), a worker
can keep its data on those local drives instead of an EBS volume:

```toml
[[nodes]]
name         = "mycluster-worker-0"
roles        = ["worker"]
internal_ip  = "10.70.1.10"
machine_type = "m8id.metal-48xl"
data_disk    = "instance-store"   # RAID 0 over every local NVMe drive; no EBS volume
```

Local NVMe reads take about 0.05–0.1 ms against about 1 ms on gp3, for
everything a worker keeps on its data disk: machines' disks, the memory files
a wake maps, and swap.

**The trade-off is durability.** Instance store is wiped on stop, hibernate,
terminate or host failure; a reboot keeps it. Every machine disk, memory
file, checkpoint, snapshot replica and swap file on that worker is lost then.
`validate` and `apply` warn about every instance-store worker. Bucket storage
(`[storage_engine]`) keeps the frozen layers of machines' disks in the bucket,
but boxd can't yet bring back the machines of a worker whose disk was wiped:
treat a wipe as losing them there too. So:

- Never stop or hibernate the instance from EC2. Reboot instead.
- Run more than one worker, so snapshots have replicas elsewhere, and turn on
  [VM backups](#vm-backups).
- If a worker's instance store is wiped anyway, contact boxd before you
  apply or start machines on it.

**What `apply` does.** No EBS data volume. The worker gets
`boxd-instance-store.service`, which runs at every boot before `boxd-worker`:
it assembles the RAID 0 array (`/dev/md/boxd-data`; a lone drive is used as
it is), or, when the drives are blank, creates it and an XFS file system
labelled `boxd-data`, then mounts it at `/mnt/boxd-data`. It never formats a
drive that holds another array or file system. `boxd-worker` requires the
service, so it never starts on the bare mount point if the disk isn't there.

**A worker keeps its kind of data disk.** `apply` refuses to switch a worker
that exists between `data_disk_gb` and `data_disk = "instance-store"`: either
way its machines would be left on the old disk. Move the machines off, remove
the worker and apply, then add it back under a new name with the new setting.

### Machine sizes (`[templates]`)

The sizes your users can pick are the prewarmed templates the installer
seeds. Without a `[templates]` table they are **1 vCPU · 4 GiB, 2 · 8 and
4 · 16**. To change the set, list *every* size you want:

```toml
[templates]
sizes = [
    { vcpu = 1, memory_gb = 4 },
    { vcpu = 2, memory_gb = 8 },
    { vcpu = 4, memory_gb = 16 },
    { vcpu = 1, memory_gb = 32 },   # memory-heavy: see below
]
# image = "computer"   # optional; default: the assets bundle's computer image
# tag   = "…"          # optional; default: that image's tag
```

- The list **replaces** the defaults, it doesn't extend them. `apply`
  deletes every `computer-*` template that isn't in it, so leaving out
  2 · 8 removes that size from the cluster.
- Once every template is ready, `apply` pushes the same list to the
  cluster (`boxd-ctl machine-sizes set 1x4 2x8 4x16 1x32`). The API then
  accepts exactly these sizes and rejects others with an error listing
  them; the console's size pickers show the same list. It is a replicated
  cluster setting, so no service restarts. `boxd-ctl machine-sizes show`
  prints it. An assets bundle whose `boxd-ctl` predates the command keeps
  its built-in sizes; `apply` warns about it.
- After seeding, `apply` sets **every** org's per-machine quota to the
  largest listed vCPU count and the largest listed memory (e.g. 4 vCPU /
  32 GiB, `boxd-ctl org set-quota`), and comps every org that isn't comped
  yet (`boxd-ctl org comp add`; an existing comp keeps its source). A fresh
  cluster has only boxd's own system org (`default-system`, owned by
  `system`, not a person; `apply` names it as such). The cluster's org
  appears at the first sign-in, so this reaches it on the next apply. A
  quota changed by hand lasts until then. The cluster org's
  default size is 2 · 8 if listed, else the smallest; on a cluster whose org
  already exists, change it by hand:

  ```sh
  boxd-ctl org set-machine-defaults <org-id> --vcpu 2 --memory 8G
  ```
- `validate` rejects duplicates and non-positive values. During `apply`,
  terraform looks up each worker type's real vCPU and RAM from the cloud
  and fails the plan if a size has more vCPUs than any worker. A size with
  more memory than a worker only gets a warning, because workers
  overcommit memory. Still, **memory-heavy sizes need memory-optimised
  workers**: `n2-highmem-*` on GCP, R7i/R8i (or their `*.metal` types) on
  AWS.
- Several sizes can share a vCPU count (1 · 4 and 1 · 32). A request that
  names only `--vcpu 1` gets the smallest memory for that count.

### Network modes

`[network] mode` picks how machines are networked:

- **`bridge`** (the default): each worker has its own `/16` for its machines
  (from `bridge_subnet_base`: `10.100.0.0/16`, `10.101.0.0/16`, …) on a
  `boxd-br0` bridge, and a VXLAN mesh joins the workers and proxies. A machine
  that moves to another worker gets a new address there.
- **`routed`**: one pool for the whole cluster, no bridge and no VXLAN. The
  boxd binaries route each machine's traffic over Geneve (UDP 6081) to
  wherever it runs, so a machine keeps its address for life. This is what live
  migration needs (`boxd-ctl vm migrate <vm> --to <worker> --live`), which
  moves only machines on boxd's storage engine. The installer doesn't set up
  the storage engine yet, so `validate` warns when routed mode is used
  without it.

```toml
[network]
vni_range_start = 100     # still required; unused in routed mode
vni_range_end   = 10000
mode = "routed"
pool = "10.120.0.0/16"    # instead of bridge_subnet_base
# guest_mtu = 0           # optional, see below
```

- **`pool`** is a `/16` inside `10.0.0.0/8`. It must not overlap the VPC or
  `10.96.0.0/12` and `10.244.0.0/16` (Kubernetes ranges people run inside
  their machines; a machine's netmask is the whole pool). Its `x.y.0.0/24`
  holds the hosts, never machines: `x.y.0.1` is every worker's gateway, a
  worker is `x.y.0.(10 + its node id)`, and proxies are `x.y.0.254`
  downwards.
- **`guest_mtu`** is the MTU a newly booted machine gets, `0` for its own
  1500, or 1280 to 1500. Geneve adds 50 bytes. The default is `0` on AWS
  and GCP: the VPCs the installer makes carry 9001 and 8896. On bare metal
  it is boxd's default, 1450, which suits a private network of 1500. Raise
  the network's MTU to at least 1550 and set `0` if you can. `guest_mtu` is
  also taken in bridge mode.
- **Switching** an existing cluster between modes is refused while it has
  machines, in either direction. A machine's saved memory (snapshots,
  hibernation, templates) keeps the gateway and address it booted with, and
  the other mode doesn't route those. Remove the machines first. The switch
  itself retires the bridge and the VXLAN tunnels on workers and proxies, and
  each worker refuses to go on while a machine's tap is still on its bridge.

Both modes run boxd's guest firewall on every worker (nftables, keyed on
each machine's tap). Machines reach the gateway's services, each other, the
proxies' device ingress and the internet. They can't reach the hosts'
other ports, the VPC or other private ranges, or the cloud metadata service.
`boxd-worker` won't start without the firewall's tables loaded.

### DNS for boxes (`[worker] dns_upstream`)

A box's only resolver is its gateway: the worker answers `<name>.boxd` itself
and forwards every other name, to `8.8.8.8` unless you say otherwise. Set the
resolvers when boxes must resolve names only your DNS knows, or when DNS to
the internet is blocked:

```toml
[worker]
dns_upstream = [
    "10.70.0.2",                       # the default, for every other name (IP or IP:port)
    "corp.example.com=10.80.0.53",     # names in corp.example.com go here (longest zone wins)
]
```

- Exactly one entry without a zone; any number of `zone=resolver`.
  Resolvers are IP addresses (with an optional port, `[IPv6]:port`), not
  hostnames. On AWS the VPC's resolver is its base address + 2
  (`10.70.0.2` for `10.70.0.0/16`).
- It applies to every worker, and an `apply` that changes it restarts the
  workers. On hosts their machines keep running through it. On `[gke]` it is
  in the worker pod's environment, so the apply drains each worker (its
  machines move to the others) and recreates its pod, one at a time; a worker
  whose machines can't move stops the apply there, back in placement with its
  old pod.
- A box with an egress allowlist still gets only the names its allowlist
  covers: the others are refused before anything is forwarded.
- [Private services](#private-services) don't need it: their names are
  answered by the worker, and the relay resolves `target_subnets` names with
  the worker host's own resolver.

### Private services

Boxes can't reach the VPC or any other private address. To let them call a
service there (an internal load balancer, a database), declare it: boxes then
reach that service, by its own hostname and port, and nothing else of the
private network.

```toml
# A load balancer whose addresses change (an AWS internal ALB): the name is
# resolved on the worker hosts, and only addresses in these subnets are used.
[[worker.private_services]]
name           = "platform.internal.example.com"
port           = 443
target_subnets = ["10.80.1.0/24", "10.80.2.0/24"]

# A service on one fixed address; the name doesn't have to exist in DNS.
[[worker.private_services]]
name   = "db.internal.example.com"
port   = 5432
target = "10.80.1.50"

[network]
# service_range = "198.18.0.0/24"   # optional; this is the default
```

How it works: a box resolves the name through its gateway (the worker's
resolver) and gets a **service address** from `service_range`, the same on
every worker. Its connection to that address and port goes to a relay on
the gateway (`boxd-relay`, a systemd unit of its own on each worker, user
`boxd-relay`, no privileges), which connects onward **from the worker
host**. TLS runs end to end between the box and the service. A box that
dials the target's real address is still dropped.

- **`name`** is a fully qualified hostname, compared without case. A name
  may be declared on several ports, one table each, and two names may share
  a port.
- **`port`** is the port boxes dial and the target's port.
- **`target`** (one IPv4) or **`target_subnets`** (IPv4 CIDRs, none wider
  than `/20`), exactly one. With `target_subnets` the worker hosts resolve
  `name` with their own resolver (the VPC's), every 5 seconds, and use only
  the addresses inside the subnets. For a load balancer, give the subnets
  it is attached to. Whoever controls that DNS record can point boxes at any
  host in those subnets, on that port, so keep them narrow.
- Targets may not hold a node's address, overlap the box network
  (`bridge_subnet_base`'s `/16`s or the routed `pool`) or `service_range`,
  or be loopback, link-local or multicast.
- **`service_range`** must be a `/16` to `/24` inside `198.18.0.0/15`,
  `172.16.0.0/12`, `192.168.0.0/16` or `100.64.0.0/10`. Boxes can't use
  these addresses for anything else. Pick another block if your network
  uses this one.
- **Service addresses are the installer's.** It allocates one per name and
  remembers it in its state: a name keeps its address for good, and an
  address is never given to another name, because boxes and their snapshots
  hold it. Don't write `address` yourself. `./boxd-install state
  reset-service-addresses` starts the allocation over (boxes get the new
  addresses on their next lookup).
- A release from before this feature is refused: its workers would ignore
  the tables.

What to know before you use it:

- **The service sees the worker's address, not the box's.** Untrusted code
  shares a box with whatever is meant to call the service, so the service
  must authenticate every caller itself. Its firewall (a security group on
  AWS) must admit the workers.
- **A box must resolve the name through its gateway.** A box using another
  resolver (its own `8.8.8.8`, DNS over HTTPS, Tailscale split DNS for the
  same domain) gets the real address, which is dropped. If you used
  Tailscale in boxes to reach the service, remove its split DNS for the
  domain.
- **Isolated boxes never reach a private service.** A box with an egress
  allowlist reaches one only if its allowlist names the service's hostname
  (a wildcard counts); `none` reaches none.
- **A refused connection looks like a connect, then a reset**, whatever the
  reason (no address answered, the box may not use it). The reason is in the
  relay's log. An isolated box's connect times out instead, and a stopped
  relay gives "connection refused".
- **The relay doesn't keep a box awake.** A box holding an idle connection
  suspends and hibernates on its timers; what the service sends wakes a
  suspended box. A hibernated box's connections are closed. A box that must
  sleep while the service keeps sending needs `idle.ignore-outbound`.
- TCP only. A restart of the relay itself (its upgrade, a crash) cuts its
  open connections; clients reconnect a second later. Worker restarts and
  deploys don't.
- Not on `[gke]` yet.

Not to be confused with `[[proxy.internal_services]]`, which is the other
direction: it routes ingress from users to your own internal services.

### License key

Without a license key, a boxd cluster has the **free tier**: its workers may
have at most **16 vCPUs** in all (the logical CPUs of the worker hosts; the
vCPUs your machines get don't count). A cluster over the limit keeps working
and keeps its machines, but refuses new ones until it is under the limit
again or has a key. A license key from boxd unlocks more.

Put the key in a file next to `cluster.toml` and name it:

```toml
[license]
key_file = "license.key"
```

`license.key` is git-ignored. **If you use another file name, add it to
`.gitignore` yourself**: the key must not be committed.

With a key, the installer downloads the releases boxd publishes for that key:
they are under a path made from the key, and only the key finds them. Keep
it as private as the state key. When the key changes (a renewal, or a key
with more features), boxd publishes your releases for the new key: replace
the file and apply. Without a key, the installer downloads from boxd's
release bucket.

Every `apply` installs it with `boxd-ctl license install` on a control node.
Before that, once every node runs the new bundle, it makes the cluster's ID
with `boxd-ctl license init-cluster-id` (a no-op when the ID exists); a key
bound to one cluster needs it.
boxd checks the key's signature before it stores it; a key that doesn't
verify stops the apply with the reason. To replace a key, replace the file
and apply. `boxd-ctl license status` on a control node shows the key's
state, its expiry and the worker vCPU count; it never shows the key.

When `key_file` is set, the file must exist and hold the key (one line that
starts with `boxd-lic`): otherwise `validate` and `apply` stop with an error
before anything changes. A cluster never drops to the free tier because of a
typo.

Without a key:

- a **new** worker with no `machine_type` gets a 16-vCPU type (`m8i.4xlarge`
  on AWS, `n2-standard-16` on GCP) instead of the 32-vCPU default, so one
  worker is free. A `machine_type` you set always wins.
- `validate` and `apply` warn when the workers have more than 16 vCPUs. The
  install still works. `validate` doesn't read the cluster's state, so it
  counts each worker without `machine_type` as a new one (its warning says
  so); on an existing cluster `apply` counts the types the workers have, and
  can warn when `validate` didn't.

A worker that already exists keeps the type it has, with or without a key:
the license never resizes a running worker (boxd never stops a machine
because of the license). Set `machine_type` to change it.

### Windows machines

With a license key that has Windows guests, users create Windows Server machines next to
Linux ones (`boxd machine new --os windows`). They reach them over RDP (each machine has
its own RDP port on the proxy; `boxd machine rdp reset-password` makes a password and shows
it once) and over `boxd machine connect` (a PowerShell session). Windows machines
hibernate and wake like Linux machines. Their snapshots, forks and checkpoints hold the disk
alone; a snapshot or fork is generalized, so every copy gets its own identity.

You build the cluster's Windows image yourself, from your own Windows media, and push it to
your registry: [win-img/README.md](../win-img/README.md) (where to get an ISO, what goes in
the image, the build). Then name it here:

```toml
[windows]
# The image you built and pushed (win-img/README.md).
image = "registry.example.com/boxd/windows-server-2025:2026-11"
# Optional. Your KMS host, host or host:port. Every Windows machine activates against it at
# boot (volume-licensing and SPLA images; an evaluation image can't).
# kms = "kms.example.com:1688"
# Optional. How you license Windows with Microsoft: "per-vm" (default: Windows machines go
# to any worker) or "per-host" (only to the workers in `hosts`, the ones you licensed).
# licensing = "per-vm"
# hosts = ["worker-1", "worker-2"]
```

The registry's pull credentials are a secret: `./boxd-install providers edit` stores them
(`[windows_registry]`, [Providers](#providers-secrets)), never `cluster.toml`. Every `apply` then:

- installs on each worker what Windows machines need from the release bundle: the UEFI
  firmware they boot, and the Windows builds of the in-VM `boxd` and `run` CLIs;
- writes the registry's credentials into each worker's config (`[[worker.registries]]`);
- sets the cluster's Windows image, KMS host and licensing mode (`boxd-ctl image
  set-windows`, `boxd-ctl cluster-config set windows_kms` and `windows_licensing`), and
  marks `hosts` as licensed Windows hosts (`boxd-ctl node windows-host`).

Without `[windows]` nothing changes, and a Windows create is refused with a sentence that
names what's missing.

**Workers.** A Windows machine boots UEFI firmware on the worker's KVM, like any machine. A
worker pulls the image (about 40 GB) at its first Windows machine, which takes some
minutes; later creates take about 30 seconds. A fresh machine reboots once inside Windows
Setup before it's ready.

### Telemetry

Every boxd cluster sends boxd usage reports over HTTPS, from the control
leader to `https://telemetry.boxd.sh/v1/report`. They have **no personal
data** and nothing that identifies your machines. A cluster that cannot reach
boxd keeps working, and nothing waits for a report.

- **Without usage billing** (the free tier, and license keys without it): one
  report an hour, with the counts below.
- **With usage billing** (a license key that includes `usage-billing`): a
  report every 5 minutes, signed with a token in the key, with the hosts the
  cluster runs on and the 5-minute periods each was up (see "Usage billing"
  below). The counts still go once an hour.

Releases before boxd v2 send report format 1: the counts only, once an hour.

What a report sends (the full list; a report has no other field):

| Field | What |
|---|---|
| `format` | The report's version, `2` (`1` before boxd v2) |
| `cluster_id` | The cluster's random ID (`boxd-ctl license status` shows it) |
| `boxd_sha` | The boxd version (its commit) |
| `sent_at` | When the report was built |
| `license` | `none`, or the license ID, licensee, state and features of the key |
| `counts.workers` | For each worker: its CPU count, its host kernel version and its average CPU use over the hour. No names, no IDs, no IPs |
| `counts.worker_vcpus` | The worker vCPUs the free tier counts |
| `counts.nodes` | How many control nodes (Raft voters) and proxies; `dns` counts only DNS nodes registered with `boxd-ctl node add` (0 on an installer cluster, whose DNS nodes do not register) |
| `counts.usage` | How many users, orgs, machines (all, running), and the vCPUs of the running machines |
| `counts.location` | The cloud and the region from `cluster.toml` |
| `hosts` | Each host that runs control, a proxy, DNS or a worker (once, however many of them it runs): the fields below |
| `hosts.host_key` | A 32-character key for the host: an HMAC of its instance ID (its `/etc/machine-id` off AWS) under a random salt that never leaves the cluster, so boxd cannot get the instance ID back from it |
| `hosts.roles` | Which of control, proxy, dns and worker it runs |
| `hosts.cloud`, `instance_type`, `partition`, `region`, `lifecycle` | On AWS: `aws`, the instance type (for example `m7i.2xlarge`), the partition (`aws`), the region, and `on-demand` or `spot`. Empty off AWS |
| `hosts.launched_at` | When the instance last started (from AWS's instance identity document; the rest of that document is not sent) |
| `hosts.imds_error` | When the host could not read these facts from the instance metadata service: a short code (`timeout`, `token`, ...) |
| `hosts.buckets` | The 5-minute periods in which the host was up since the last report boxd accepted (up to 72 hours back), with the instance type, partition and region of each |

`counts` is in one report an hour; the other reports carry only `hosts`.
A report never sends names, emails, user or org IDs, machine names, the zone,
IP or MAC addresses, the AWS account ID, instance IDs, AMI IDs, hostnames or
tags.

To see the exact report the cluster sends next, on a control node:

```bash
sudo boxd-ctl telemetry show          # the report, whether it is on, the last send, the hosts
sudo boxd-ctl telemetry show --json   # the report only
```

To turn it off you need a license key that includes telemetry opt-out. Then:

```toml
[telemetry]
enabled = false
```

`enabled = false` needs `[license] key_file`: `validate` refuses it
otherwise. With `enabled` set, every `apply` runs `boxd-ctl telemetry off`
(or `on` for `true`) after it installs the key; if control refuses `off` (the
key does not include telemetry opt-out), the apply stops with the message.
**Without `enabled`, `apply` leaves the cluster's setting as it is**: a new
cluster sends, and a cluster you turned off stays off. When the setting is off,
the installer also writes `[telemetry] default_enabled = false` into the
control nodes' config before it (re)starts them, so control does not send a
report before `apply` reaches `telemetry off`. On the free tier,
telemetry is always on. When a key with opt-out expires, telemetry stays off.

#### Usage billing

With a license key that includes `usage-billing`, boxd bills the cluster a
share of what its instances would cost on demand, from its reports: each host
that runs control, a proxy, DNS or a worker, for each 5-minute period it was
up, at the AWS on-demand Linux list price of its instance type and region (a
spot instance at the on-demand price too). EBS volumes are not billed.

- Telemetry can't be turned off: such a key never includes telemetry opt-out,
  and `validate` refuses `[telemetry] enabled = false` with it.
- Each node reads its instance type, partition, region and lifecycle from the
  instance metadata service (IMDSv2) at every report, and nothing else. The
  installer's terraform requires IMDSv2 tokens with a hop limit of 1, so the
  machines on your workers cannot reach it; keep it that way.
- Reports are signed with a token in the license key, so nobody else can send
  reports for your cluster.
- A cluster that can't reach boxd for a while loses nothing: it keeps the
  last 72 hours of 5-minute periods and sends them when it can again.
- `boxd-ctl telemetry show` lists each host, its instance type and how many
  periods the next report carries, and says whether reports are signed.

### Providers (secrets)

Credentials never go in `cluster.toml`. They live in the encrypted state,
managed with `./boxd-install providers`:

| Command | What it does |
|---|---|
| `providers edit` | opens `$EDITOR` on the current config (a commented template the first time); saved when it parses and validates, otherwise it reopens |
| `providers get` | prints the current config, secrets included |
| `providers set FILE` | **replaces** the whole config with `FILE` (no merge); `FILE` must be inside this directory |
| `providers set-github-key PEM` | stores the GitHub App's private key |
| `providers set-tls-ca CERT KEY` | stores your intermediate CA ([TLS from your own CA](#tls-from-your-own-ca)) |

Changes take effect on the next `apply`. Every table is optional; the console
hides sign-in buttons and features for anything unconfigured. Values are TOML
**quoted strings**.

- `[google]`: Google sign-in. A Web-application OAuth client with redirect URI
  `https://app.<zone>/api/v1/auth/google/callback`
  ([step 6 on GCP](INSTALL.md#6-console-sign-in-google)).
- `[github]` `client_id` / `client_secret`: GitHub sign-in. A GitHub OAuth App
  with callback `https://app.<zone>/api/v1/auth/github/callback`
  ([step 6 on AWS](INSTALL.md#6-console-sign-in-github-or-google)). A GitHub App's own client
  credentials work too.
- `[github]` `app_*`: the optional **GitHub App** integration, which gives
  orgs repository access. Create the App (github.com → Settings → Developer
  settings → GitHub Apps) with:

  - **Callback URL**: `https://app.<zone>/api/v1/auth/github/callback`
  - **Setup URL**: `https://app.<zone>/api/v1/org/github/setup`, and enable
    **"Request user authorization (OAuth) during installation"** — the setup
    handler requires the authorization code to prove the installer controls
    the installation.
  - **Webhook URL**: `https://app.<zone>/api/v1/webhooks/github`, active, with
    a webhook secret you generate — the same value goes in
    `app_webhook_secret`.
  - **Repository permissions**: actions **write**, checks **write**, contents
    **write**, deployments **write**, issues **write**, pull requests
    **write**, statuses **write**, workflows **write**, metadata **read**.
  - **Subscribed events**: check run, check suite, commit comment, create,
    delete, deployment, deployment review, deployment status, issues, issue
    comment, pull request, pull request review, pull request review comment,
    pull request review thread, push, release, status, workflow dispatch,
    workflow job, workflow run.

  Then fill `app_id`, `app_slug`, `app_client_id`, `app_client_secret`,
  `app_webhook_secret` from the App's settings page. The App's private key is
  a file, not a config value:

  ```sh
  ./boxd-install providers set-github-key <downloaded-key>.pem
  ```

- `[oidc]` `client_secret`: single sign-on's client secret. Its other
  settings go in `cluster.toml` ([Single sign-on](#single-sign-on-oidc)).
- `[resend]`: transactional email.
- `[automations]`: `composio_api_key` (+ optional `anthropic_api_key`).
  `apply` then creates the curated Composio auth configs automatically;
  custom-OAuth toolkits are the one thing you create by hand in Composio.
- `[backups]`: only to use your own S3-compatible storage. By default a
  bucket and credentials are provisioned for you (GCS + HMAC key on GCP,
  S3 + IAM access key on AWS); `auto_backups = false` in the cloud table
  turns that off.
- `[snapshot_archive]`: only to use your own bucket for the
  [snapshot archive](#snapshot-archive) (`endpoint`, `bucket`, `region`,
  `access_key`, `secret_key`).
- `[windows_registry]`: `host`, `username`, `password`, the pull credentials
  of the private registry your Windows image is in
  ([Windows machines](#windows-machines)). Every worker gets them.
- `[crowdstrike]`: `client_id`, `client_secret`, `cloud` (`us-1`, `us-2`,
  `eu-1` or `us-gov-1`), a CrowdStrike Falcon API client with the scope
  **Sensor download: Read**. `windows-image build` then downloads the Windows
  sensor and reads your customer ID (CID) through Falcon's API, and installs the
  sensor in the image ([win-img/README.md](../win-img/README.md), "CrowdStrike
  Falcon").

Who may sign up once sign-in works is in
[OPERATIONS.md](../OPERATIONS.md#people-and-orgs) (`boxd-ctl signups`).

### Single sign-on (OIDC)

People can sign in with your company's identity provider instead of Google
or GitHub: Entra ID, Okta, Google Workspace, Keycloak, or any other provider
that speaks OpenID Connect. A provider that speaks only SAML goes through
Keycloak ([below](#saml-only-providers)). Single sign-on covers the console,
`boxd auth login` (it opens the same sign-in page) and the phone page.

**It needs a boxd release that has single sign-on** (`assets_version`, see
[Upgrades](#upgrades-and-rollback)). A console from before it ignores
`[oidc]` without a word, or ignores `bootstrap_admin` and `link_email_domains`
and leaves Google and GitHub sign-in on where they're configured, so whoever
signs in first owns the cluster's org. Check a running cluster before you
add `[oidc]`: its console has single sign-on when

```sh
curl -s https://app.<zone>/api/v1/config | grep -o '"oidc":[^,}]*'
```

prints `"oidc":null` (or a provider once configured). Nothing printed means
an older release: move `assets_version` to `latest` and `apply` first.

**With `[oidc]` configured, it's the only way to sign in.** The console turns
Google and GitHub sign-in off on the server and shows one button, "Sign in
with `<display_name>`". A [GitHub App](#providers-secrets) for org repository
access keeps working, and so does `boxd-ctl token create`. A `[google]` or
`[github]` sign-in client left in the providers config stays stored but does
nothing; `apply` and `providers edit` say so.

**1. Register boxd at your provider** as a web application, a confidential
client with a client secret:

- **Redirect URI** `https://app.<zone>/api/v1/auth/oidc/callback`, exactly:
  providers compare it character by character.
- **The authorization code flow**, with the scopes `openid`, `email` and
  `profile`. The ID token must carry the person's `email`: boxd refuses a
  sign-in without one.
- **Assign the people or groups** who should have access. Who your provider
  lets into the application is who can get an account.

The steps for each provider are [below](#entra-id).

**2. Everything but the secret goes in `cluster.toml`:**

```toml
[oidc]
issuer = "https://login.microsoftonline.com/<tenant-id>/v2.0"
client_id = "…"
display_name = "Contoso"                   # the button: "Sign in with Contoso"
bootstrap_admin = "you@yourcompany.com"    # optional, see "The first sign-in"
link_email_domains = ["yourcompany.com"]   # optional, see "Existing accounts"
```

- **`issuer`**: the provider's issuer, exactly as its discovery document
  (`<issuer>/.well-known/openid-configuration`) states it, a trailing slash
  included. `validate` fetches that document and its signing keys as the
  console will, and prints what passed: the issuer, the endpoints, how many
  keys, and whether the provider lists PKCE (S256), which boxd always uses.
  When the machine you run it on can't reach the provider, it only warns:
  the provider may answer the cluster's network alone.
- **`ca_cert`** (optional): for a provider behind a private CA, typically one
  on your own premises, its root certificate as PEM text in a multi-line
  string (`ca_cert = """` on one line, the PEM, `"""` on the last). The public
  roots stay trusted. Those are the ones built into the console, not its
  machine's store, so a root your company installs on its own machines needs
  to be here too, even when `validate` passes without it on such a machine.

**3. The client secret goes in the providers config:**

```sh
./boxd-install providers edit
```

```toml
[oidc]
client_secret = "…"
```

Then `./boxd-install validate` and `apply`. Only the console gets `[oidc]`.
It needs outbound HTTPS to the provider (the NAT gateway or Cloud NAT
provides it; a provider on your premises needs a route from the VPC) and a
correct clock, since ID tokens are valid for minutes. The clouds' time
service keeps the nodes' clocks right.

**The first sign-in.** The first person to sign in to a new cluster owns its
org ([Users, orgs, sign-in](#users-orgs-sign-in)). Set `bootstrap_admin` to
your own email before the first `apply` with `[oidc]`: until that person has
signed in, everyone else is told the cluster isn't set up yet. The provider
must not mark that email unverified, or, on Entra ID, the account must not be
a guest's; the refusal says which. Once the cluster's org exists, the setting
does nothing.

**Existing accounts**, on a cluster that signed in with Google, GitHub or
another identity provider before. A person's first single sign-on creates a
new account unless it links by email to the one they have: the domain is in
`link_email_domains`, exactly one account has the email the provider sends,
and that account has no identity from this provider yet. List only domains
your provider controls, because whoever it lets in with an address in them
gets that address's account. Empty or absent: no link by email.

The link doesn't apply to a sign-in whose email the provider says isn't
verified, or to an Entra ID guest from another tenant. Those, and anyone
whose email differs or is on two accounts, get a new account; an operator
can move their machines to it ([OPERATIONS.md](../OPERATIONS.md#people-and-orgs)).

**Switching identity provider.** Point `[oidc]` at the new provider (its
issuer, client and secret) and `apply`. Everyone's next sign-in is then a new
identity from a new issuer, and with `link_email_domains` listing your
domain, it links by email to the account they have, as above, so keep
people's email addresses the same at both providers. On Entra ID, keep the
app registration rather than making a new one: a new registration gives
everyone a new identity under the same issuer, and no account links by email
to it.

**Sessions from before.** Console sessions and CLI logins made with Google or
GitHub keep working until they expire: 7 days for the console, 30 for the
CLI. SSH keys, and API keys made without an expiry date, keep working for
good. A console session from before can't mint anything new, though: it
can't approve a `boxd auth login`, create an API key or add an SSH key, and
is told to sign in with single sign-on again. A CLI login from before can
until it expires. To end someone's access at once, disable them (next
paragraph).

**Moving an existing cluster to single sign-on.** For a cluster where people
already sign in with Google or GitHub:

1. **Check the release.** Run the `/api/v1/config` check above. If it
   prints nothing, move `assets_version` to `latest` and `apply` first.
2. **Decide who keeps their account.** Each person's *first* single sign-on
   decides it: either it links to the account they have, or it creates a
   new, empty one. To keep accounts, set `link_email_domains` to your
   company's domains **before** the switch. An account links only if the
   email it signed up with is exactly the one your provider sends. For
   example, with

   ```toml
   [oidc]
   link_email_domains = ["contoso.com"]
   ```

   - Ana signed up with Google as `ana@contoso.com`. Her first Okta
     sign-in, also `ana@contoso.com`, gets her existing account, with her
     machines and orgs.
   - Ben signed up with GitHub, which gave boxd `ben.dev@gmail.com`. His
     Okta sign-in as `ben@contoso.com` matches no account, so he gets a new
     one. An account keeps the email it signed up with, so move his machines
     afterwards (step 5).
   - Cleo has two accounts, both `cleo@contoso.com`. Nothing links, because
     boxd can't tell which one is hers, so she gets a third. Before the
     switch, move the machines she wants to keep off the account she doesn't
     use (`boxd-ctl vm reassign`), then delete it with `boxd-ctl user remove
     <user-id>`. That **permanently destroys every machine still on it, with
     its backups**. Disabling it isn't enough: a disabled account still has
     the email, so nothing links.

   `boxd-ctl user list` shows each account's email. Without
   `link_email_domains`, everyone starts again with a new account.
3. **Register boxd at your provider, add `[oidc]` and the client secret,
   then `validate` and `apply`** (steps 1–3 above). Leave out
   `bootstrap_admin`: it only applies before the cluster's first sign-in.
   Only the console restarts, and machines keep running.
4. **Tell people what changes.** There's one button now, "Sign in with
   `<display_name>`". Their existing console sessions and CLI logins keep
   working until they expire, but creating an API key, adding an SSH key or
   approving a `boxd auth login` needs a fresh sign-in through single
   sign-on.
5. **Afterwards**, look for people who got a second account (`boxd-ctl user
   list`, the same email twice). Move their machines to the new account
   ([OPERATIONS.md](../OPERATIONS.md#people-and-orgs)), then disable the old
   one. Remove the Google or GitHub sign-in client with `providers edit` once
   you won't switch back. Until then `apply` reminds you it does nothing.

**Removing people is manual.** boxd doesn't find out that someone left your
provider: there is no directory sync (SCIM) and no background check of their
login. When someone leaves, an operator offboards them on a control node
([OPERATIONS.md](../OPERATIONS.md#people-and-orgs)):

1. **Their machines first.** `boxd-ctl user disable <user-id>` refuses while
   they own any machine that isn't destroyed, stopped ones included, and
   lists each. Reassign or destroy each one:
   `boxd-ctl vm reassign <vm-id> --to-user <user-id>` (to a member of the
   machine's org) or `boxd-ctl vm destroy <vm-id>`. A private machine is
   served its owner's credentials, so stop it first (`boxd-ctl vm stop
   <vm-id>`), and review it before you start it again.
2. **Then `boxd-ctl user disable <user-id>`.** It suspends their
   credentials at once: their CLI logins, SSH keys, API keys and console
   sessions are refused, and so is their sign-in. Org API keys, and the
   tokens shared machines use, act for the org and keep working. They stay a
   member of their orgs; `boxd-ctl org rm-member <org-id> <user-id>` removes
   them for good. `boxd-ctl user enable <user-id>` lets them back in, and
   restores their CLI logins and SSH keys that haven't expired; they sign in
   to the console again.
3. **Orgs they own** stay theirs: give each a new owner with
   `boxd-ctl org set-owner <org-id> <user-id>`.

Until an operator does this, what they hold keeps working: sessions and CLI
logins until they expire, SSH keys and API keys without an expiry date for
good. So this assumes the cluster is
reachable only from your network ([private ingress](INSTALL.md#private-ingress-on-aws),
or your firewalls): someone who leaves loses their network access first.

#### Entra ID

1. In the Microsoft Entra admin center, **App registrations → New
   registration**:
   - **Supported account types**: accounts in this organizational directory
     only (single tenant).
   - **Redirect URI**: platform **Web**,
     `https://app.<zone>/api/v1/auth/oidc/callback`.
2. **Certificates & secrets → New client secret.** Entra lets it live 24
   months at most. Before it expires, add a new one, put it in with
   `providers edit`, `apply`, then delete the old one.
3. `client_id` is the **Application (client) ID**. The issuer is
   `https://login.microsoftonline.com/<tenant-id>/v2.0`, with the **Directory
   (tenant) ID**, a GUID, not your domain name.
4. **Enterprise applications →** your app **→ Properties → Assignment
   required: Yes**, then **Users and groups → Add user/group** (assigning a
   group needs Entra ID P1 or P2). Without that, everyone in the tenant can
   sign in, guests included.
5. **App registrations →** your app **→ API permissions → Grant admin
   consent for** your tenant. With assignment required, Entra lets nobody
   consent for themselves, so without this every sign-in stops at "Need
   admin approval".

People need an email address on their Entra profile, which the `email` scope
puts in the token; no optional claim is needed. Entra marks a guest from
another tenant with the `idp` claim, and boxd never links a guest's sign-in
to an existing account.

#### Okta

1. In the Admin Console, **Applications → Applications → Create App
   Integration**. Where Okta opens its Integration Wizard, switch to the
   **Classic experience** tab. Then **OIDC - OpenID Connect**, **Web
   Application**.
2. **App integration name**: replace the default "My Web App" rather than
   typing after it. **Use Okta-generated client ID.**
3. Grant type **Authorization Code** only. **Sign-in redirect URI**
   `https://app.<zone>/api/v1/auth/oidc/callback`, in place of the localhost
   example. Remove the example sign-out URI: none is needed.
4. **Assignments**: **Limit access to selected groups**, the groups who
   should have access. **Allow everyone in your organization** lets every
   user of the Okta org in.
5. Save, then on the app's **General** tab, **Client Credentials → Edit**:
   keep **Client authentication: Client secret** and tick **Require PKCE as
   additional verification** (boxd always sends PKCE). `client_id` and the
   client secret are in that section.
6. The issuer is your Okta org, `https://<your-org>.okta.com`, or with a
   custom authorization server its issuer, such as
   `https://<your-org>.okta.com/oauth2/default`.

Okta's ID token carries `email` but not `email_verified`, and boxd trusts an
email unless the provider marks it unverified. So with `link_email_domains`,
whoever can edit a person's email in Okta decides which boxd account their
sign-in links to.

#### Google Workspace

This is your Workspace as the only way in, unlike the `[google]` table,
which adds Google sign-in next to the others.

1. In a Google Cloud project of your Workspace organization, **APIs &
   Services → OAuth consent screen**: audience **Internal**, so only your
   Workspace's accounts can sign in. **External would let any Google account
   in.**
2. **Credentials → Create credentials → OAuth client ID**: **Web
   application**, authorized redirect URI
   `https://app.<zone>/api/v1/auth/oidc/callback`.
3. The issuer is `https://accounts.google.com`; `client_id` and
   `client_secret` come from the client.

Every account in your Workspace can then sign in. To stop new accounts once
everyone who needs one has it, pause sign-ups
([OPERATIONS.md](../OPERATIONS.md#people-and-orgs)).

#### Keycloak

1. In your realm, **Clients → Create client**: **OpenID Connect**, a client
   ID such as `boxd`, **Client authentication** on, **Standard flow** on and
   the other flows off.
2. **Valid redirect URIs**: `https://app.<zone>/api/v1/auth/oidc/callback`.
3. `client_secret` is on the client's **Credentials** tab.
4. The issuer is `https://<keycloak-host>/realms/<realm>`
   (`/auth/realms/<realm>` before Keycloak 17).

Every user of the realm can sign in: give boxd's users a realm of their own,
or restrict the client with an authentication flow. Keycloak marks an email
unverified until it is verified, so turn on **Email verified** for users you
create by hand, the bootstrap admin above all. Behind a private CA, set
`ca_cert`.

#### SAML-only providers

For a provider that speaks only SAML (an older ADFS, Shibboleth), run
Keycloak in front of it as a broker. In the Keycloak realm, **Identity
providers → SAML v2.0**, import the provider's metadata, and point `[oidc]`
at Keycloak as above. Turn on **Trust Email** on that identity provider when
its emails are reliable; otherwise Keycloak marks them unverified, and no
existing account links. To skip Keycloak's own login page, set the default
identity provider of the **Identity Provider Redirector** in the realm's
browser flow.

### TLS from your own CA

For a cluster that lives inside a corporate network, boxd can sign its
certificates itself with an **intermediate CA you give it**, instead of
getting them from a public CA. No ACME, no public DNS: `boxd-control` signs
the zone and wildcard certs, every `*.<vm>.<zone>`, custom domains and org
domains locally. Works on GCP and AWS.

**What you provide.**

- An intermediate CA certificate and its private key, issued by your
  corporate root:
  - `basicConstraints = critical, CA:TRUE, pathlen:0`;
  - name constraints, if you use them, must permit `<zone>` **without** a
    leading dot (that covers the zone itself, e.g. `app.<zone>` and
    `<zone>`; `.<zone>` alone covers only names below it), plus every
    custom-domain namespace and every org domain the cluster should serve.
    boxd refuses to sign a name outside them;
  - the key unencrypted: ECDSA P-256/P-384, RSA (2048+ bits) or Ed25519, in
    any PEM form (the installer converts it to PKCS#8).
- Your root distributed to everything that talks to the cluster: laptops
  (MDM), CI runners, and VMs that call the cluster or each other over
  HTTPS. The `boxd` CLI uses the OS trust store, so nothing else to do
  there.
- Internal DNS that forwards `<zone>` to the cluster's nameservers (the same
  delegation as a public cluster, inside your resolvers).

**Setup.** Put the files inside this directory, then:

```sh
# the intermediate first, optionally followed by your root
./boxd-install providers set-tls-ca intermediate-chain.pem intermediate.key
```

```toml
[acme]
ca = "local"
dns_verify_resolver = "10.0.0.2"   # your internal resolver; IP[:port], default port 53
```

then `apply`. `set-tls-ca` checks the files before storing them in the
encrypted state: the first certificate is a CA, the key is its key and not
encrypted, and the cert file holds certificates only. `contact_email` isn't
needed. Ansible puts the pair in `/etc/boxd/ca/` on control nodes, readable
by `boxd-control` only (group `boxd-ca`). Delete the key file from this
directory once it's stored. `.gitignore` keeps `*.pem`, `*.key` and
`secrets/` at the top of this directory out of git meanwhile.

`dns_verify_resolver` is the resolver `boxd-control` asks when it verifies
custom domains and org domains. Point it at your internal resolver so it
sees your internal zones; left unset it asks `8.8.8.8`, which can't. On AWS
that's the VPC's resolver: `vpc_cidr`'s base address + 2 (`10.70.0.2` for
`10.70.0.0/16`).

**No corporate CA yet?** For an evaluation only, make a throwaway root and
an intermediate constrained to your zone, and trust the root on the laptops
that test (macOS: `security add-trusted-cert -r trustRoot -k
~/Library/Keychains/login.keychain-db secrets/root.pem`). Keep
`secrets/root.key` somewhere safe or delete it: anyone with it can make
certificates those laptops trust.

```sh
ZONE=boxd.example.com; mkdir -p secrets && cd secrets
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 365 \
  -subj "/CN=$ZONE test root (evaluation only)" -keyout root.key -out root.pem \
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign"
openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
  -subj "/CN=$ZONE test intermediate" -keyout intermediate.key -out intermediate.csr
printf 'basicConstraints=critical,CA:TRUE,pathlen:0\nkeyUsage=critical,keyCertSign,cRLSign\nnameConstraints=critical,permitted;DNS:%s\n' "$ZONE" > int.ext
openssl x509 -req -in intermediate.csr -CA root.pem -CAkey root.key -CAcreateserial \
  -days 180 -extfile int.ext -out intermediate.pem
cat intermediate.pem root.pem > intermediate-chain.pem && cd ..
./boxd-install providers set-tls-ca secrets/intermediate-chain.pem secrets/intermediate.key
```

**Domains.**

- *Custom domains* (`boxd-ctl domain add myapp app.corp.com`), in your
  internal DNS, as for a public cluster: `app.corp.com` A → the machine's
  IP, and `*.app.corp.com` CNAME → `myapp.<zone>`. In local mode
  `_acme-challenge.app.corp.com` must also resolve as a CNAME to exactly
  `myapp.<zone>`; the wildcard CNAME provides it. That binds the domain to
  the machine: without it, anyone could claim a name that points at someone
  else's machine and get a certificate your laptops trust.
- *Org domains* (`boxd-ctl org set-domain <org-id> engineering.corp.com`): in
  your internal `corp.com` zone, delegate `engineering.corp.com` to
  `ns1.<zone>` and `ns2.<zone>` (NS records; "New Delegation" in AD DNS).
  boxd checks the delegation through `dns_verify_resolver`. A subzone your
  DNS can only *forward* (no NS records in a zone you own) isn't supported.

**Lifetimes, rotation, revocation.** Certificates live 30 days and renew
with 10 days left; nothing is revoked (there is no CRL or OCSP), so a short
lifetime is the bound on a leaked key. To rotate the intermediate, run
`set-tls-ca` with the new pair and `apply`: `boxd-control` restarts and
re-signs every certificate over its next sweeps (one a minute). See
[OPERATIONS.md](../OPERATIONS.md#certificates-from-your-own-ca) for rotation and
a broken CA.

---

## GKE

With `[gke]` next to `[gcp]`, the installer runs boxd on Google Kubernetes
Engine instead of on Compute Engine instances
([Install on GKE](INSTALL.md#install-on-gke), `examples/gke/cluster.toml`).
`[gcp]` keeps the project, the region and the state bucket; `[gke]` replaces
`[gcp]`'s instance fields (`zone`, `vpc_cidr`, `image_family`) and
`[[nodes]]`. Everything else in `cluster.toml` works as on the other targets.

| Role | Runs as | Where |
|---|---|---|
| control | StatefulSet `boxd-control`, 1 or 3 Raft voters, pod network, a 20 GB disk each, a disruption budget of one | pool `system` |
| console | StatefulSet `boxd-console`, pod network, a pinned ClusterIP the proxies route `app.` and `connect.` to | pool `system` |
| proxy, dns | per edge `i`: StatefulSets `boxd-proxy-<i>` and `boxd-dns-<i>`, host network | pool `edge-<i>`, one node, its own load balancer IP |
| worker | StatefulSet `boxd-worker`, one per node, privileged, host network and PID, VMs in the pod's own cgroup slice | pool `worker` |
| node prep | DaemonSet `boxd-node-prep` on worker nodes: boxd's host kernel and one reboot, the local SSD RAID, then the label workers wait for | pool `worker` |

The installer renders every Kubernetes object itself (no Helm) and applies it
server-side; objects it labelled (`app.kubernetes.io/managed-by=boxd-install`,
`boxd.sh/install=<namespace>`) and no longer renders are deleted. Every
role's config is in the Secret `boxd-config`. Each proxy needs its own public
address (a machine's SSH port is allocated per proxy, and `<machine>.<zone>`
resolves to its proxy's IP), so the edge is one node pool of one node per
edge, each behind its own passthrough load balancer, and each edge is a
nameserver of the zone (`ns1` … `nsN`).

### `[gke]` keys

| Key | Default | |
|---|---|---|
| `cluster` | required | `"create"`, or an existing cluster: `"projects/<project>/locations/<location>/clusters/<name>"`, in `[gcp].project` and `[gcp].region` |
| `namespace` | `"boxd"` | boxd's namespace; can't change after the first apply |
| `zones` | required | the zones of `[gcp].region` the pools go in: one (a zonal cluster) or several (regional with `create`). The system pool and the edges spread over them; the worker pool is in the first |
| `admin_cidrs` | required with `create` | who may reach the cluster's control plane; the installer's address must be among them |
| `vpc_cidr`, `pods_cidr`, `services_cidr` | `vpc_cidr` required with `create`; `172.20.0.0/16`, `172.24.0.0/20` | `create` only: the nodes' subnet (inside `10.0.0.0/8`) and GKE's pod and service ranges (keep these outside 10/8) |
| `control_replicas` | `3` | 1 or 3 Raft voters; fixed after the first apply |
| `host_kernel` | `"boxd"` | `"boxd"` or `"stock"`: [Host kernel and Secure Boot](#host-kernel-and-secure-boot) |
| `ingress` | `"public"` | `"public"` or `"internal"`: [Internal ingress](#internal-ingress) |
| `internal_ips` | any free address | `internal` only: one address per edge, in the nodes' subnet |
| `observability` | `true` | [Observability on GKE](#observability-on-gke) |
| `analytics` | `true` | ClickHouse in the cluster for the console's usage graphs: [Usage graphs on GKE](#usage-graphs-on-gke) |
| `analytics_disk_gb` | `100` | ClickHouse's pd-ssd disk; fixed once it exists |
| `[gke.pools.system]` | `e2-standard-4`, a multiple of the zone count holding every control replica | `machine_type`, `nodes`. At least `control_replicas` nodes (one replica per node), a multiple of the zone count |
| `[gke.pools.edge]` | `n2-standard-4`, 2 | `machine_type`, `nodes` (one edge per node). Edges can be added, not removed |
| `[gke.pools.worker]` | required | `machine_type` (nested virtualization: Intel N2, N1, C2, C3), `nodes`, `local_ssd` (default 4, the local NVMe SSDs made one RAID 0 for the worker's data dir; 0 for a pd-balanced disk of `data_disk_gb` instead) |

`[images]` (GKE only): `registry`, where the cluster pulls boxd's images and
the VM image (default boxd's, `europe-docker.pkg.dev/boxd-prod-491613/releases`, public);
`pull_secret`, an existing `kubernetes.io/dockerconfigjson` Secret in the
namespace, used by every pod and by the workers' own pulls of VM images
(create the namespace and the Secret before the first apply). The cluster
pulls `boxd:<release>` (every role), `boxd-node-prep:<release>`,
`boxd-dockerd:<release>`, `boxd-clickhouse:<release>` (with `analytics`) and,
with `registry` set, `computer:<tag>` from
there: mirror those to run without internet access. Artifact Registry in the
project works through the nodes' service accounts, which the installer
grants `roles/artifactregistry.reader`.

Pools never autoscale (`autoscaling` keys are refused); `apply` sizes them.
`[network]` takes only `mode = "routed"` and a `pool` clear of every GKE
range. `[proxy] ssh_port` can't be 22 (the edge node's sshd holds it).
`[worker] ksm` and `swap_gb` are not supported (GKE nodes have no swap). The
license counts `nodes` × the worker type's vCPUs, and `validate` refuses a
pool over it (16 without a key that lifts the limit).

### Requirements of a cluster you run

`validate` and `apply` check each of these and say what to change:

- **GKE Standard** (Autopilot can't run privileged host-network workers with
  nested virtualization), **1.28.4 or later**, **VPC-native**, on the
  **legacy dataplane** (Dataplane V2 is refused: untested next to boxd's own
  eBPF programs).
- **Nodes in `10.0.0.0/8`**, and the node subnet, pod and service ranges
  clear of `[network] pool`. In the same project (no Shared VPC yet), in
  `[gcp].region`, with every `[gke] zones` entry among its node locations.
- **Private Google Access** on the node subnet while images come from
  Artifact Registry.
- **Managed Service for Prometheus** on, unless `observability = false`.
- A **maintenance exclusion** on node upgrades (warned when missing): GKE's
  automatic node upgrades recreate worker nodes, and every machine on a
  recreated worker is lost. The workers' disruption budget only delays it.
- Network reach from the installer to the control plane.

boxd's pools are dedicated: each is tainted `boxd.sh/pool=<pool>:NoSchedule`
and labelled `boxd.sh/pool=<pool>`; boxd's pods tolerate the taint and select
their pool; your workloads don't land there. (In a cluster the installer
creates, the system pool is not tainted: GKE's own system pods run there.)
boxd's namespace is labelled `pod-security.kubernetes.io/enforce=privileged`:
the workers and edges are privileged and on the host network, so Pod
Security admits them only there. Node prep holds a ClusterRole with `get`
and `patch` on nodes, because a node can only be patched through a
cluster-wide role (RBAC has no rule for "the node this pod runs on"); its
script patches only its own node's `boxd.sh/node-ready` label.

### Host kernel and Secure Boot

- **`host_kernel = "boxd"`** (default): node prep installs boxd's host
  kernel on each new worker node and reboots it once; the kubelet and GKE's
  system pods run on it. Forks of running machines carry their RAM and start
  in milliseconds. The kernel is not signed by Google, so the worker pool
  runs with **Secure Boot off**, and Google does not support nodes on another
  kernel. `validate` refuses it when the project enforces
  `constraints/compute.requireShieldedVm`.
- **`host_kernel = "stock"`**: GKE's Ubuntu kernel, Secure Boot on. A fork of
  a running machine snapshots it first: a longer pause of the source
  (hundreds of milliseconds). Everything else is the same.

A new release's kernel reaches new worker nodes only: node prep never
reboots a node that runs a worker.

### Network and MTU

Workers and proxies use boxd's routed network: Geneve (UDP 6081) between
nodes, no L2. Geneve takes 50 bytes, so a VPC MTU of 8896 (what `create`
makes) leaves guests their own 1500. On your cluster, the installer sets the
guests' MTU from the VPC's: 1460 gives 1410, 1500 gives 1450.

Firewall rules the installer adds: the edges' public ports (TCP 53, 80, 443,
9443, 10000-30000, 40000-60000; UDP 53) from the internet (or, internal, from
RFC 1918 ranges); Google's health checkers to the edges; between boxd's pools
UDP 6081 and TCP 7000-7002 (Raft), 7100, 8000, 9010 (the worker's gRPC, also
live migration) and 9101-9105 (metrics); and the pod range to the edges and
workers on those TCP ports and 9443.

### Internal ingress

`ingress = "internal"` puts each edge behind an internal passthrough load
balancer, with an address in the nodes' subnet (`internal_ips` pins them),
for clients on your VPN or interconnect. Public CAs can't validate a zone
served only inside the VPC, so it needs `[acme] ca = "local"`
([TLS from your own CA](#tls-from-your-own-ca)). `apply` prints the
conditional forwarder to set up on your resolvers: the zone → the edges'
addresses.

### Observability on GKE

With `observability = true` (default): the cluster's workload logs go to Cloud
Logging (with `create`), Managed Service for Prometheus scrapes every role
through PodMonitorings (control 9101, proxy 9102, dns 9103, worker 9104), and
terraform creates a Cloud Monitoring dashboard, *boxd on GKE (<cluster>/<namespace>)*:
roles up, Raft, workers, the busiest machines, GKE's node metrics and boxd's
warnings and errors. boxd's latency metrics are Prometheus summaries (a
`quantile` label), not histograms. `observability = false` turns all of it
off.

Your own Prometheus can scrape the same endpoints (a host-network role
serves on its node's IP, a pod-network one on its pod's):

```yaml
scrape_configs:
  - job_name: boxd
    kubernetes_sd_configs:
      - role: pod
        namespaces: { names: [boxd] }
    relabel_configs:
      - source_labels: [__meta_kubernetes_pod_label_boxd_sh_role]
        regex: control|proxy|dns|worker
        action: keep
      - source_labels: [__meta_kubernetes_pod_ip, __meta_kubernetes_pod_label_boxd_sh_role]
        regex: (.+);control
        target_label: __address__
        replacement: $1:9101
      - source_labels: [__meta_kubernetes_pod_ip, __meta_kubernetes_pod_label_boxd_sh_role]
        regex: (.+);proxy
        target_label: __address__
        replacement: $1:9102
      - source_labels: [__meta_kubernetes_pod_ip, __meta_kubernetes_pod_label_boxd_sh_role]
        regex: (.+);dns
        target_label: __address__
        replacement: $1:9103
      - source_labels: [__meta_kubernetes_pod_ip, __meta_kubernetes_pod_label_boxd_sh_role]
        regex: (.+);worker
        target_label: __address__
        replacement: $1:9104
      - source_labels: [__meta_kubernetes_pod_label_boxd_sh_role]
        target_label: boxd_role
      - source_labels: [__meta_kubernetes_pod_name]
        target_label: pod
```

### Usage graphs on GKE

The console's usage graphs (each machine's CPU, memory and disk over time)
come from ClickHouse, as on the other targets, where a metrics node runs it.
On GKE, `analytics = true` (default) runs it in the cluster: the
`boxd-clickhouse` StatefulSet, one pod on the system pool, its data on a
pd-ssd disk (`premium-rwo`) of `analytics_disk_gb` GB, reached through its
Service, its password generated in the installer state. Every apply applies
the schema migrations, as the metrics node's converge does. Control, the
proxies, the workers and the console get it as `[analytics]`.

An `[analytics]` of your own (cluster.toml or the providers config, a
ClickHouse Cloud service for instance) wins: the installer runs no
ClickHouse then. `analytics = false` runs none either; without an
`[analytics]` the graphs stay empty.

The disk size is fixed once the disk exists: the StatefulSet's volume
template can't change. To grow it, resize the claim
(`./boxd-install kubectl -n boxd edit pvc data-boxd-clickhouse-0`, a larger
`spec.resources.requests.storage`) and leave `analytics_disk_gb` as it is:
it only sizes the disk when it is made. Turning `analytics` off later removes
the pod and keeps the disk.

### Changing a GKE cluster

`apply` is the only way in, and an upgrade is an `apply` with a new
`assets_version`:

- **Control, console, edges** roll: a new image, or a config change other
  than membership, restarts their pods one at a time (each edge in turn).
  Membership changes restart nothing: `apply` writes the Secret, waits until
  every control pod sees it, then runs `boxd-ctl sync-config`.
- **Workers are updated in place.** A new image is patched into each running
  worker pod, one at a time: the containers restart, the pod stays, and so
  do its machines (the new worker adopts their VMMs). A config change
  restarts the worker's process the same way. Any other change to the
  worker's pod drains the worker first (`boxd-ctl node drain`), then
  recreates its pod.
- **More workers:** raise `[gke.pools.worker] nodes`. New workers join the
  cluster before their pods start; a new node first gets boxd's kernel.
- **Fewer workers:** lower `nodes`. The highest-numbered workers leave, one
  at a time: `boxd-ctl node exclude`, `node drain` (boxd moves every machine
  off: live for running bucket machines, relocation for hibernated ones, a
  stopped machine's move for the rest), `node mark-dead`, the pod removed,
  `remove-peer`, its Raft disk deleted, its node deleted from the pool. A
  drain that leaves machines behind stops the apply and the worker stays.
  A removed worker's node id is never used again. An apply that stops half
  way finishes the removal next time. Machines on file storage (without
  [bucket storage](#bucket-storage-storage_engine)) move only to a worker
  you name: drain those workers first
  (`./boxd-install ctl -- node drain worker-<id> --to worker-<id>`), then
  lower `nodes`. Bucket storage is what makes workers on GKE movable.
- **More edges:** raise `[gke.pools.edge] nodes`, then add the new
  nameserver to your zone's delegation. Edges can't be removed by `apply`.
- **Control replicas, the namespace, `cluster`, local SSD versus disk**
  can't change after the first apply.
- The worker pool's machine type, local SSD count and `host_kernel` can't
  change on a running cluster: GKE would recreate every worker node.
  `apply` refuses such a terraform plan.
- With `create`, GKE's node upgrades are held by a maintenance exclusion
  until the version's end of support; control-plane patches still land, at
  weekends.

### boxd-ctl and kubectl on GKE

`boxd-ctl` runs in the control pods. From the installer:

```sh
./boxd-install ctl -- status
./boxd-install ctl -- vm list
./boxd-install ctl -- license install < license.key      # stdin passes through
```

`kubectl` from the same image, with credentials kept in
`~/.config/boxd-install/kube`:

```sh
./boxd-install gcloud container clusters get-credentials <cluster> --location <location>
./boxd-install kubectl -n boxd get pods -o wide
./boxd-install kubectl -n boxd logs boxd-worker-0 -c worker
./boxd-install kubectl -n boxd logs ds/boxd-node-prep
```

Don't delete a worker pod by hand: its machines stop with it (the workers'
disruption budget blocks evictions, not deletes). Lower `nodes` instead.

### Secrets at rest

boxd's configs, keys among them, are in the Secret `boxd-config`. With
`create`, the cluster encrypts Secrets with a Cloud KMS key terraform makes
(application-layer Secrets encryption). On a cluster you run, turn it on
yourself: `gcloud container clusters update <cluster> --location <location>
--database-encryption-key projects/<p>/locations/<region>/keyRings/<ring>/cryptoKeys/<key>`,
after granting GKE's service agent
(`service-<project-number>@container-engine-robot.iam.gserviceaccount.com`)
`roles/cloudkms.cryptoKeyEncrypterDecrypter` on the key. `validate` warns
while it is off.

### Limitations on GKE

- Workers live in one zone (the first of `zones`).
- GKE nodes have no swap, so workers don't overcommit memory beyond their
  RAM; size the worker type for the machines it holds. Under node memory
  pressure the kubelet evicts pods, and a worker's machines go with it.
- Shared VPC, Dataplane V2 and Autopilot are not supported.
- `[[worker.private_services]]` is not supported (the relay needs a systemd unit on
  the worker host).

## Day-2 operations

### Updating the installer

This repo is released by boxd as tagged versions, each with a GitHub Release
that holds its notes (watch the repo → Custom → Releases to be notified).
`main` always holds the latest release; work in progress is on `dev`. To take
one:

```sh
git fetch --tags
git log --oneline HEAD..origin/main     # what's new
git pull --ff-only
source ~/.boxd-<cluster>.env
./boxd-install validate && ./boxd-install apply
```

- Your `cluster.toml` and env file aren't tracked, so a pull never touches them.
- **Received the installer as a zip?** Unzip the new release next to the old
  one, copy your `cluster.toml` into it, and run from the new folder. Its
  release notes come with it, in `CHANGELOG.md`.
- If the release moved to a new tools image, `./boxd-install` pulls it on its
  next run.
- An installer release changes how the cluster is installed (terraform,
  ansible, these docs). The boxd version it runs comes from `assets_version`:
  see the next section. Read the release notes before you apply: they say
  when a release needs anything from you.

### Upgrades and rollback

`assets_version` picks the boxd release the cluster runs:

- **`"latest"`** (the default): every `apply` installs the newest release boxd
  has published, so an `apply` you run to change a setting
  also upgrades boxd if a new release is out. boxd moves `latest` only after
  testing a release.
- **A 40-character version id** (a git sha): the cluster stays on exactly
  that release until you change it. Use this if every upgrade must be a
  planned change.

With a license key, `latest` is the newest release boxd has published for
that key, and the releases match what the key includes: with
[bucket storage](#bucket-storage-storage_engine), `latest` is a release with
it. `apply` refuses `[storage_engine]` with a release without it (the
release's manifest lists no `bucket` feature).

Every `apply` prints the version it installed (`Assets: <sha> (channel latest)`)
and records it in the installer state. Note it down: it's the exact value to
pin to stay on, or come back to, that release.

On GKE an upgrade rolls control, the console and the edges, and updates
workers in place: see [Changing a GKE cluster](#changing-a-gke-cluster).

An upgrade covers everything in the bundle: binaries, the boxd host kernel,
the console, the CLI your users download, and VM templates. Services restart
only where something actually changed, and `site.yml` converges control nodes
last with `serial: 1`, so replicas upgrade before the leader.

Machine specs (a machine created, restored or forked with a spec, and changes
to a machine's spec) need every node on a release that has them: a node on an
older release would miss what they record, for good. boxd keeps them behind a
switch, and `apply` handles it. Before it converges a new release, it turns
the switch off if it was on; once every node runs the new release, it turns
it on. In between, they are refused with *"machine specs aren't turned on on
this cluster yet"*. Machines that have a
spec keep running, and machines without one aren't affected. An `apply` that
installs the release the cluster already runs leaves the switch on.

To keep machine specs off, run `sudo boxd-ctl cluster-config set
machine_specs off` on a control node. `apply` leaves an `off` as it is and
says so; `sudo boxd-ctl cluster-config set machine_specs on` turns them back
on.

Treat a downgrade as a question for your boxd contact rather than an
assumption: re-applying an older release rolls the binaries back, but a
release that has already migrated on-disk or control-plane state may not
reverse cleanly.

#### Upgrading from a release before license keys

The first release with license keys also turns the free tier on: without a
key, a cluster whose workers have more than 16 vCPUs refuses new machines
([License key](#license-key)). If you ran boxd before that release, boxd
sends you a license key before your installer update. Install it in the same
`apply` that upgrades the cluster:

1. Save the key in `license.key` next to `cluster.toml` (one line that starts
   with `boxd-lic`). The file is git-ignored.
2. Add it to `cluster.toml`:

   ```toml
   [license]
   key_file = "license.key"
   ```

3. Update the installer (`git pull`, or the release zip from boxd).
4. `./boxd-install validate`: it checks the key file, and it prints no
   free-tier warning.
5. `./boxd-install apply`. After the nodes upgrade it prints
   `cluster id created: <id>` and `license key installed: valid`.
6. On a control node, `sudo boxd-ctl license status` shows the key, its
   expiry and `no limit`.

On a cluster with more than 16 worker vCPUs, new machines are refused for
about a minute during this `apply`: from the moment the proxies run the new
release until the key is installed, near the end. Machines that exist keep
running, and nothing else is refused. Upgrade at a quiet time. If you apply
without the key, new machines are refused until you add it and apply again.

If boxd told you that telemetry stays off for your cluster, your key includes
telemetry opt-out ([Telemetry](#telemetry)). A copy of the installer boxd
made for you already defaults to `[telemetry] enabled = false`; with any
other copy, set it in `cluster.toml`.

#### Clusters installed before the release bucket

If boxd gave you a product license (64 hex characters) to download releases,
your env file sets `BOXD_PRODUCT_LICENSE`. While it's set, the installer
downloads from your own release path, as before, and not from the release
bucket. Keep it set until your boxd contact tells you to switch. Then delete
the `BOXD_PRODUCT_LICENSE` line from `~/.boxd-<cluster>.env`, open a new
shell, and apply. Nothing else changes. A new cluster never needs it.

### boxd-ctl (operator CLI)

On GKE, `./boxd-install ctl -- <command>` runs it in the control pod
([boxd-ctl and kubectl on GKE](#boxd-ctl-and-kubectl-on-gke)).

Every control node has `boxd-ctl`, the operator CLI: orgs and members,
quotas and machine sizes, machines, workers, domains, cluster status. It
talks to the control socket (`/run/boxd/control.sock`), which is owned by the
`boxd` group. SSH to a control node ([below](#ssh-to-nodes)), add your user to
the group once and reload it:

```sh
sudo usermod -aG boxd $USER
newgrp boxd        # or log out and back in

boxd-ctl status
boxd-ctl org --help
```

**[OPERATIONS.md](../OPERATIONS.md)** covers running the cluster with it: people
and orgs, capacity, machines, draining workers, domains, backups, and a
troubleshooting runbook. `boxd-ctl <command> --help` ends with worked
examples for each command.

### SSH to nodes

On GCP and AWS there is no public SSH. The installer reaches nodes the same
way you do: through GCP's IAP tunnel or AWS Systems Manager. On bare metal
it connects directly, as `ubuntu`, to each node's `ssh_host` (or
`public_ip`, else `internal_ip`); use your own access there.

**GCP**, by node name:

```sh
./boxd-install gcloud compute ssh ubuntu@mycluster-control-0 --tunnel-through-iap \
    --project <project> --zone <gcp-zone>
```

**AWS**, by instance id:

```sh
./boxd-install aws ec2 describe-instances \
    --filters Name=tag:boxd:cluster,Values=<cluster-name> Name=instance-state-name,Values=running \
    --query 'Reservations[].Instances[].[Tags[?Key==`Name`]|[0].Value,InstanceId]' --output text
./boxd-install aws ssm start-session --target <instance-id>
```

`aws ssm start-session` gives a shell as `ssm-user` with sudo. For a one-off
command without a shell (a status check, a log excerpt), send it and read
the result:

```sh
id=$(./boxd-install aws ssm send-command --instance-ids <instance-id> \
    --document-name AWS-RunShellScript --parameters 'commands=["sudo boxd-ctl status"]' \
    --query Command.CommandId --output text)
./boxd-install aws ssm get-command-invocation --command-id "$id" \
    --instance-id <instance-id> --query StandardOutputContent --output text
```

Services are systemd units named after their role: `boxd-control`,
`boxd-worker`, `boxd-proxy`, `boxd-console`, `boxd-dns`, `clickhouse-server`.
Logs: `sudo journalctl -u boxd-console -f`. Configs: `/etc/boxd/*.toml`
(generated: change `cluster.toml` or providers and re-apply instead of
editing in place).

### Users, orgs, sign-in

The first person to sign in creates *the* cluster org — automatically comped
(unmetered) and sized 4 vCPU / 16 GiB per VM, 50 VMs. It's named
`[cluster] org_name` when that's set, and "<their first name>'s default org"
otherwise; rename it on the console's Org page. With `org_name`, the org's
slug (its handle in URLs and listings) follows it too, shortly after the
first sign-in. Setting `org_name` on a cluster
whose org exists already changes nothing. Everyone who signs in after is added to it as a member. Each `apply`
resets every org's quota to cover the largest size and comps it (see
[Machine sizes](#machine-sizes-templates)).

**Sign in first yourself.** Whoever gets there first owns the org — if that
turns out to be a contractor or someone mid-evaluation, their account is the
cluster's accountable owner. With single sign-on, `bootstrap_admin` makes
sure it's you ([Single sign-on](#single-sign-on-oidc)). If it happens anyway,
reassign from a control node:

```sh
boxd-ctl user list                          # find the right user_id
boxd-ctl org list                           # and the org_id
boxd-ctl org set-owner <org-id> <user-id>   # transfer ownership
boxd-ctl org add-admin <org-id> <user-id>   # additional admins
```

Day to day, members are invited from the console; `boxd-ctl org add-member`
is the operator override for adding someone directly.

### The CLI your users install

`https://app.<zone>` serves the boxd CLI for Linux and macOS, pre-configured
for your cluster — it already points at your `app.<zone>`, so there is no
endpoint to set:

```sh
curl -fsSL https://app.<zone>/downloads/install.sh | sh
boxd auth login        # opens the browser for a one-time device login
```

It installs a binary named `boxd` into `~/.local/bin`, replacing any `boxd`
CLI already there for another boxd service. One install sets up three things:

- **The binary.** Machines end to end: `boxd new` / `connect` / `list`,
  fork and snapshot, `machine exec`, file copy, HTTPS proxies and custom
  domains, raw TCP/UDP ports, env vars and secrets, checkpoints, org and
  billing. `boxd <cmd> --help` is the always-current reference; bare `boxd`
  drops into an interactive REPL with tab completion.
- **Client utilities** — the bridge back to the user's own computer, below.
  Off by default.
- **An agent skill.** The installer drops a `boxd-cli` skill for Claude Code,
  Codex and OpenCode, so a coding agent on their laptop can drive the CLI
  itself.

### Client utilities (VMs reaching the user's own computer)

Worth pointing your users at, because it is off by default and easy to miss.
The same binary carries a small background daemon that lets a machine reach
back to the computer the session was opened from: their **clipboard** (so
pasting images straight into Claude Code inside a VM works), **read-only**
access to files under their home directory, and a **Chrome on their own
screen with their own logins**, drivable over CDP from inside the VM.

Enabled per computer, as themselves (not `sudo`) — the daemon runs in their
login session:

```sh
boxd config set client-utils.enable true     # on their laptop
boxd config set client-utils.label "laptop"  # name it (default: hostname)
boxd config get client-utils.enable
boxd config set client-utils.enable false    # bridge off
boxd config set daemon.enable false          # stop + remove the background service
```

`client-utils.enable false` drops the bridge but leaves a reduced-mode agent
running that only syncs their `~/.ssh/config` entries. `daemon.enable false`
goes further: it stops the service and deletes its unit file (systemd user
unit on Linux, LaunchAgent plist on macOS), and the flag persists, so logging
in again does not quietly reinstall it. Neither command touches the `boxd`
binary itself.

Then, from inside a machine:

```sh
boxd local devices                    # which of their computers are reachable
boxd local ls Documents               # home directory only, read-only
boxd local read ~/app.log --tail=50
boxd local browser open               # Chrome on their screen
boxd local browser cdp-url            # drive it with Playwright/agent-browser
```

Only **personal and private org-billed machines** — the ones a single user has
a shell on — can reach a computer. Shared org machines and isolated machines
are refused, so a teammate on a shared box can never read someone's files or
drive their browser.

### Onboarding your users

The block below is written to be forwarded. **Substitute `<zone>` before you
send it** — none of these commands work with the placeholder still in place.
It tells people that a machine's URL has no sign-in in front of it: with
public ingress that URL is on the internet, with
[private ingress](INSTALL.md#private-ingress-on-aws) on your network.

> **boxd is live at `https://app.<zone>`.**
>
> Sign in there, then install the CLI. It arrives pre-configured for our
> cluster, so there is no endpoint, account or API key to set up:
>
> ```sh
> curl -fsSL https://app.<zone>/downloads/install.sh | sh
> boxd auth login
> ```
>
> Your first machine:
>
> ```sh
> boxd new myapp        # a microVM, booted in milliseconds
> boxd connect myapp    # shell into it
> ```
>
> Whatever it serves is reachable at `https://myapp.<zone>` with a valid
> certificate, no configuration. That URL has no sign-in in front of it,
> private machine or not: anyone who can reach the cluster can open it, so
> put authentication in the app itself. `boxd --help` covers the rest, and
> bare `boxd` opens an interactive REPL.
>
> **About the background agent.** Logging in installs a small boxd agent that
> runs in the background on this computer — a systemd user service on Linux, a
> LaunchAgent on macOS. By default it does exactly one thing: keeps your
> `~/.ssh/config` in sync, so `ssh myapp.boxd` works alongside
> `boxd connect`.
>
> Optionally it also lets your machines reach back to this computer — your
> clipboard (so pasting images into Claude Code inside a machine works),
> read-only access to files under your home directory, and a Chrome on your
> own screen with your own logins:
>
> ```sh
> boxd config set client-utils.enable true
> ```
>
> Both are yours to switch off at any time:
>
> ```sh
> boxd config set client-utils.enable false   # just the reach-back
> boxd config set daemon.enable false         # the agent itself
> ```

One caveat worth passing on: the public documentation at `docs.boxd.sh`
describes this same CLI, but every URL in it is written against `boxd.sh`. On
your cluster the equivalent is always `<zone>` — `myapp.boxd.sh` there is
`myapp.<zone>` here.

### Adding capacity

On GKE, raise `[gke.pools.worker] nodes` (or the edge pool's) and re-apply:
see [Changing a GKE cluster](#changing-a-gke-cluster). Elsewhere, add the
node to `cluster.toml` and re-apply — terraform creates the instance,
ansible converges it, and `apply` runs `boxd-ctl sync-config` on the raft
leader, which adds the new node to the running cluster (a running cluster
never adds a learner from its config by itself; installer releases before
this one left a new worker logging "still not registered: not the leader",
and `sudo boxd-ctl sync-config` on the leader joins it by hand):

```toml
[[nodes]]
name         = "mycluster-worker-1"
roles        = ["worker"]
internal_ip  = "10.60.0.21"      # a free address in the VPC CIDR (AWS: the private subnet)
data_disk_gb = 500
```

```sh
./boxd-install validate && ./boxd-install apply
boxd-ctl status                  # on a control node: the new worker should appear in the count
```

On bare metal, prepare the host first ([Install on bare metal](INSTALL.md#1-prepare-the-hosts))
and give the node `data_disk` instead of `data_disk_gb`. Until the host has
the deploy key, `apply` stops and prints the line to add, as on the first
install. On the clouds, check your vCPU quota first. The same applies to an extra proxy or
nameserver. Control nodes are the exception: they are raft voters, so keep
the count odd (1, 3, 5) and add them one at a time.

### VM backups

The installer provisions a bucket and S3 credentials for disaster-recovery
backups of VM disks (GCS + HMAC key on GCP, S3 + IAM access key on AWS;
`auto_backups` in the cloud table), but **nothing is scheduled until someone
asks for it**. Set a default for new VMs in an org:

```sh
boxd-ctl org set-backups-default <org-id> --period-secs 86400 --retention-secs 604800
```

Existing VMs are unchanged by that, and a fork inherits its source's schedule
rather than the org default. Users drive their own from the CLI, in durations
rather than seconds:

```sh
boxd machine config set myapp backups.period 24h
boxd machine config set myapp backups.retention 14d
boxd machine backup list myapp
boxd machine backup restore myapp <backup-id>
```

A restore is an in-place disk rewrite: the machine cold-boots into the backup
and keeps its name, URL and settings, but anything written since is gone.
Backups cover the disk only — not memory or running state.

### Snapshot archive

Operators can move a snapshot's bytes off the workers' disks into a bucket,
and back (`boxd-ctl snapshot archive`, `unarchive` and `list`). While a
snapshot is archived, machines can't be created from it. Using it day to
day is in [OPERATIONS.md](../OPERATIONS.md#snapshot-archive). It needs an
assets version that has it: if `boxd-ctl snapshot --help` on a control node
doesn't list `archive`, yours doesn't yet, and the bucket would sit unused.
Turn it on in `cluster.toml`:

```toml
[snapshot_archive]
# archive_dir = "snapshot-archive"   # optional; the folder in the bucket, not under org/
```

On AWS, `apply` then creates a bucket for it,
`<cluster>-archive-<account>-<region>`, with:
- its own IAM user and access key, which open only that bucket;
- default encryption (SSE-S3; the archive never sends an encryption header);
- a lifecycle rule that aborts incomplete multipart uploads after a day.

`apply` writes the block, with those credentials, into the control and
worker configs only: proxies and consoles never get the key. With
[`bucket_access = "vpc"`](INSTALL.md#buckets-only-inside-the-vpc) the archive bucket
is VPC-only too.

**Your own bucket** (on GCP, which has no automatic one yet, or on AWS with
`[aws] auto_snapshot_archive = false`): put `endpoint`, `bucket`, `region`,
`access_key` and `secret_key` in a `[snapshot_archive]` table with
`./boxd-install providers edit`. Give the bucket default encryption and the
same lifecycle rule. `apply` refuses a declared archive with no bucket.

Removing `[snapshot_archive]` turns the archive off. On AWS the next
`apply` also tries to delete the bucket, and stops if anything is still in
it, because archived snapshots exist only there. Unarchive them, or empty
the bucket, first.

### Bucket storage (`[storage_engine]`)

boxd's storage engine keeps machines' disks in an object store bucket instead
of on one worker's disk, so any worker can open them. A machine then moves
between workers (live migration with `[network] mode = "routed"`, relocated
wakes), and a hibernated one's memory waits in the bucket. It is a licensed
feature: the license key needs `bucket-storage`, and the cluster runs a release
with bucket storage (with such a key, `latest` is one).

```toml
[storage_engine]                       # every worker runs the engine
# default_vm_storage = "bucket"        # new machines: "bucket" (default) or "file"
# hibernation_blobs = "uploads"        # "uploads" (default), "auto" or "off"
# ram_cache_gib = 32                   # boxd's defaults otherwise
# hibernated_ram_keep_hours = 24
# copy_mib_per_sec = 256
# cache_gib = 100
# freeze_interval_secs = 600
```

`default_vm_storage = "bucket"` needs `[license] key_file`. `backends`, `dir`,
`config` and `prefix` are the installer's to set, and the store's fields
belong in the providers config (below).

**What `apply` does.**
1. Before terraform: refuses a bundle without the engine (its manifest lists
   no `bucket` feature), and checks the one-way rules below.
2. Terraform makes the cluster's bucket:
   - AWS: `<cluster>-storage-<account>-<region>`, with an IAM user and access
     key that open only that bucket, SSE-S3, a lifecycle rule that aborts
     incomplete multipart uploads after a day, and no public access. With
     [`bucket_access = "vpc"`](INSTALL.md#buckets-only-inside-the-vpc) it is
     VPC-only too.
   - GCP: `<project>-<cluster>-storage`, with public access prevention, the
     same lifecycle rule, and a service account `<cluster>-storage` that may
     use only that bucket. The account is attached to the worker instances,
     so no key exists anywhere. Attaching it stops each running worker once:
     turning the engine on for an existing GCP cluster restarts its workers.
3. Every worker gets `[storage_engine]` in `worker.toml` and the bucket's
   details in `/etc/boxd/bucket.toml` (mode 0600, owned by `boxd`); control,
   proxy and console nodes get neither. Each cluster's objects live under its
   own prefix, `engine/<random>`, made once and kept in installer state.
4. After the workers start, each must log that it opened its bucket (opening
   writes a test object), or `apply` stops with the worker's error.
5. Then the cluster switches, each read first and set only when it differs:
   the storage catalog on, hibernation blobs, and the default for new machines.
   A license without `bucket-storage` stops the first `apply` that sets the
   default to `bucket`.
6. Templates: with the `bucket` default, `apply` builds bucket templates
   (`computer-bucket-<size>-…`), each built once and shared by every worker
   (`[worker] templates = "both"` unless you set it), and removes the file ones.

**Your own bucket** (bare metal always, or `[aws]`/`[gcp]`
`auto_storage_bucket = false`): put an S3-compatible store in a
`[storage_engine]` table with `./boxd-install providers edit`: `endpoint`,
`region`, `bucket`, `access_key`, `secret_key`, and optionally
`virtual_hosted_style`. Give the bucket a lifecycle rule that aborts incomplete
multipart uploads. Leave `conditional_put` out unless your store needs it:
with `native` or `head-then-put`, opening the bucket writes nothing, so `apply`
can't tell a wrong key. A providers store is refused while `apply` makes the
bucket itself. Decide which bucket before the first `apply` with the engine.

#### S3 Express storage bucket

On AWS the installer can make the storage bucket an S3 Express One Zone
directory bucket in the cluster's availability zone, for lower request
latency than a regional bucket:

```toml
[aws]
storage_bucket_type = "express"   # "standard" (the default) or "express"
```

Terraform then makes `<cluster>-storage-<account>--<az-id>--x-s3` (the AZ id
of `availability_zone`, such as `euc1-az2`), an IAM user whose key may only
open sessions on it (`s3express:CreateSession`), the abort-incomplete-multipart
lifecycle rule, and an `s3express` gateway endpoint so workers reach it
through the VPC. Workers' `bucket.toml` gets `s3_express = true`, and boxd
talks to the zonal endpoint with session credentials. Express is offered in
some availability zones only: check that yours has it before the first apply.

**The trade-off is durability.** A directory bucket keeps its data in one
availability zone. The storage bucket holds the only durable copy of
machines' disks, snapshots and hibernated memory, so losing that zone loses
them. `validate` and `apply` warn. A directory bucket takes no VPC-endpoint
bucket policy, so `bucket_access = "vpc"` doesn't cover it. Storage also
costs more per GB than a standard bucket.

Like the bucket itself, the choice is one-way: once the catalog is on,
`apply` refuses a change of `storage_bucket_type`, because it gives the
workers another bucket. Your own directory bucket (`auto_storage_bucket =
false`) goes in `providers edit` with `s3_express = true`, a `region`, and no
`endpoint`.

**One way.** Once the storage catalog is on, machines can live in the
bucket. From then on `apply` refuses:
- removing `[storage_engine]`;
- a bundle without the engine (a standard bundle, or a pinned `latest`);
- changing `auto_storage_bucket`, or your own bucket's `bucket` or `endpoint`;
- anything that would give the workers another bucket or prefix than the one
  they run on (checked against each worker's deployed `bucket.toml` too, so a
  cluster turned on by hand keeps its bucket and prefix).

There is no override: moving a cluster to another bucket is a support
procedure. Before the catalog was ever on, removing `[storage_engine]` is
allowed and the workers' bucket file is deleted. `apply` never turns the
catalog off; if someone did (before rolling a node back), `apply` stops until
it is on again (`boxd-ctl cluster set-storage-catalog on`).

**When the license lapses**, `apply` warns: the default stays `bucket` but new
machines are born on file, and machines already in the bucket keep working.

Existing machines stay on file storage when the engine is turned on; moving
them is in [OPERATIONS.md](../OPERATIONS.md#bucket-storage).

### State and recovery

```sh
./boxd-install state pull                    # decrypt state into ./pulled-state
./boxd-install state push                    # encrypt it back, then delete ./pulled-state
./boxd-install providers get                 # print the current providers config
./boxd-install --out-dir rendered render     # render configs/inventory into ./rendered, no changes
```

Both directories hold decrypted secrets. While `./pulled-state` exists, other
commands refuse to run until you push it or delete it, so its edits can't be
lost or overwritten. If an `apply` can't push state back at the end, it keeps
its copy in `./pulled-state` and tells you to push it.

The encrypted state is `<cluster>/installer-state.enc` in the state bucket,
with a `.prev` sibling and full bucket versioning behind it. On bare metal
it's the file `metal-state/<cluster>/installer-state.enc` next to
`cluster.toml`, with only the `.prev` copy behind it: back it up. Don't run two
applies concurrently — the last state push wins.

**The egress CA.** Every worker signs the HTTPS its machines send through
boxd's egress proxy with one CA, which each machine installs in its trust
store when it boots. The state keeps the cluster's pair (`keys/egress-ca.pem`,
`keys/egress-ca.key`), made at the first apply, and every `worker.toml` (mode
0600) carries it as `egress_ca_cert` / `egress_ca_key`. So a machine that moves
to another worker (a live migration, a relocated wake, a restore) keeps
trusting it. It is never replaced. A cluster applied before this release has
none in its state, and each worker made its own: the next apply adopts the
first worker's pair it finds under `/mnt/boxd-data/egress-ca/` and says so.
Machines on the other workers get it at their next boot, restore or fork, and
their HTTPS through the egress proxy fails until then.

### Tearing down

```sh
./boxd-install destroy
```

On bare metal there is nothing to destroy, and `destroy` says so: reinstall
the hosts, then delete `metal-state/<cluster>/` and do steps 1, 2 and 4
below.

This destroys the cloud infrastructure. The encrypted state stays in the
bucket, so a later `apply` rebuilds the same cluster identity: the same
names, certificates, secrets and provider settings. The cluster's data does
not come back: users, orgs and machines live on the control nodes, so the
rebuilt cluster starts empty, with a **new cluster ID**. A license key bound
to the old ID is refused there (`apply` stops after the nodes are up); ask
boxd for a new key. An unbound key keeps working. Buckets with objects in
them are never deleted: the backups bucket (`<project>-backups` on GCP,
`<cluster>-backups-<account>-<region>` on AWS), the
[snapshot archive](#snapshot-archive) bucket, and the
[storage engine's](#bucket-storage-storage_engine) (`<cluster>-storage-…` on AWS,
`<project>-<cluster>-storage` on GCP). `destroy` removes everything else, names
each bucket it kept, and goes on. A later `apply` of the same cluster uses them
again; the storage engine starts under a new prefix there. Delete them by
hand only if the cluster is gone for good.

When the cluster is gone for good, also:

1. delete the NS and glue records from the parent zone;
2. delete the OAuth app or client you created for sign-in;
3. empty the state bucket, **including all object versions**, and delete it.
   This also deletes what you stored with `providers edit` and
   `providers set-tls-ca` (sign-in secrets, your CA): a new install needs
   them again. On GCP, objects stay restorable (and billed) for the bucket's
   soft-delete period;
4. delete `~/.boxd-<cluster>.env` and the password-manager entry.

---

## Troubleshooting

- **`apply` fails mid-converge**: re-run it, because every step is
  idempotent. A node that failed a play is skipped for the rest of that run,
  so a re-run is also how it catches up.
- **`apply` fails in terraform on quota** (`QUOTA_EXCEEDED`,
  `VcpuLimitExceeded`): raise the quota (step 2 of your walkthrough) and
  re-run.
- **`can't pull ghcr.io/…self-host-tools`** on the first run: no network
  to ghcr.io, or a proxy in the way. `./boxd-install` builds the image locally
  instead. That works, it's just slower.
- **Console unreachable right after the first apply**: the DNS delegation
  hasn't propagated, or certificates are still being issued (they need the
  delegation live first). `dig NS <zone> @1.1.1.1` should show your
  nameservers.
- **Sign-in button missing**: that provider isn't configured (see
  [Providers](#providers-secrets)). This is by design. With `[oidc]`, the
  single sign-on button is the only one.
- **Single sign-on fails.** The console's error names the provider:
  - *"could not get a usable answer from the identity provider"*: the
    console node can't reach it. Check its outbound HTTPS and DNS, and
    `ca_cert` for a provider behind a private CA (`sudo journalctl -u
    boxd-console` on the console node has the detail).
  - *"states a different issuer in its discovery document"*: set
    `[oidc].issuer` to the document's `issuer`, exactly. `validate` shows both.
  - *"would not complete the sign-in"*: the client ID or secret is wrong, or
    the secret expired (Entra ID's do, after at most 24 months).
  - *"sent no email address"*: the person has no email at the provider, or
    the application doesn't get the `email` scope.
  - *"did not pass verification"*: the console refused the ID token. Most
    often the console node's clock is off; the details name the check.
  - *"did not sign you in (…)"*: the provider refused, and the brackets
    give its reason, such as a person who isn't assigned to the application.
  - *"the answer came from another identity provider"*: the provider's
    answer names another issuer than `[oidc].issuer`. Start again from the
    sign-in page; if it keeps happening, check the issuer.
  - The provider itself refuses the redirect URI (Entra ID `AADSTS50011`;
    Okta "The 'redirect_uri' parameter must be a Login redirect URI in the
    client app settings"; Keycloak "Invalid parameter: redirect_uri"):
    register `https://app.<zone>/api/v1/auth/oidc/callback` exactly.
  - Entra ID stops at *"Need admin approval"*: grant admin consent
    ([Entra ID](#entra-id), step 5).
  - *"This cluster isn't set up yet"*: `bootstrap_admin` waits for its
    person's first sign-in. When it's their email but the provider marks it
    unverified, or the account is an Entra ID guest, the message says so.
- **`machine specs aren't turned on on this cluster yet`** when a machine is
  created, restored or forked with a spec: the cluster's `machine_specs`
  switch isn't on ([Upgrades](#upgrades-and-rollback)). Older installers
  never turned it on. Update the installer and apply, or turn it on yourself
  once every node runs the release: `sudo boxd-ctl cluster-config set
  machine_specs on` on a control node, then `sudo boxd-ctl cluster-config get
  machine_specs` prints `on`. While an `apply` upgrades boxd, the refusal is
  expected until the nodes are done. If `apply` printed `machine specs: off,
  as an operator set them`, someone turned them off on purpose.
- **`download failed (404)`** for a release: `assets_version` names a version
  boxd hasn't published (a typo in a pinned sha, or a channel name boxd
  doesn't publish). `download failed (403)` means the release bucket refused the
  download; if it persists, tell boxd.
- **`boxd has published no such release for this license key`**: either
  `license.key` doesn't hold the key boxd sent you (an old key after a
  renewal, or a copy from another cluster), or `assets_version` names a
  version boxd hasn't published for it. If both are right, tell boxd.
- **`assets download failed (403/404)`**, on a
  [cluster from before the release bucket](#clusters-installed-before-the-release-bucket):
  `BOXD_PRODUCT_LICENSE` is wrong or not set in this shell
  (`source ~/.boxd-<cluster>.env`), or `assets_version` doesn't match a
  release published for you.
- **No certificate after 15 minutes on GCP, and `apply` ended with
  "Google Trust Services says the EAB key was already used"** (in the
  boxd-control log: `Unknown external account binding (EAB) key`). The
  cluster's single-use ACME key was spent by a boxd-control process that
  stopped before it saved its account, so every later attempt is refused.
  Mint a new key and re-apply:

  ```sh
  ./boxd-install state reset-eab
  ./boxd-install apply
  ```

  Only do this when `apply` or the log says so: on a cluster whose
  certificates work, the stored key is what identifies its ACME account.
- **`state blob: HMAC mismatch — wrong BOXD_STATE_KEY`**: this shell has a
  different state key from the one the cluster was created with. Source the
  right env file.
- **`no remote or local state — fresh cluster`** on a cluster that exists:
  `state_bucket` or `[cluster].name` in `cluster.toml` doesn't match the
  cluster's. Stop before applying, or `apply` creates a second cluster.
