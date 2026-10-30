# Install boxd on GCP, AWS or bare metal

Read [Before you start](#before-you-start), then follow **one** walkthrough
from top to bottom: [Install on GCP](#install-on-gcp),
[Install on AWS](#install-on-aws) or
[Install on bare metal](#install-on-bare-metal). All three end in
[After apply: delegate DNS and check](#after-apply-delegate-dns-and-check).
Run every command from the root of this repository. Configuration, day-2
operations and troubleshooting are in [REFERENCE.md](REFERENCE.md).

## Before you start

You need:

- **Docker**, on the machine you install from (Linux, or macOS with Docker
  Desktop). Nothing else: see [How `./boxd-install` works](#how-boxd-install-works).
- **A cloud account** you can create networks, instances, IAM and buckets in:
  a GCP project with billing, or an AWS account. The walkthroughs check
  permissions and quota first. Or **hosts you already run**: see
  [Install on bare metal](#install-on-bare-metal).
- **A DNS zone you can delegate**, e.g. `boxd.example.com` under
  `example.com`: you must be able to add NS and A records in the parent zone
  at your DNS provider. The cluster runs the authoritative DNS for its zone.
  With [private ingress](#private-ingress-on-aws) nothing is delegated in
  public DNS: you add a conditional forwarder on your internal DNS instead.
- Optional: a **license key** from boxd, for more than 16 worker vCPUs
  ([License key](REFERENCE.md#license-key)). You can add it later.
- About an hour. Most of it is `apply`, which runs unattended for 20–40 minutes.

### How `./boxd-install` works

`./boxd-install` runs `./install.py` inside a Docker image that carries every
tool the installer uses (terraform, ansible, gcloud, the aws CLI with the
Session Manager plugin, ssh, openssl) at tested versions. The first run pulls
it from `ghcr.io/boxd-sh/self-host-tools` (about 1.8 GB). If it can't
be pulled, it's built locally from `docker/`, once, in a few minutes.

```sh
./boxd-install validate                 # = ./install.py validate, and so on
./boxd-install gcloud …                 # gcloud, aws, terraform, ansible, ssh: that tool
./boxd-install shell                    # a shell inside the image
```

- **Every command in this guide runs through it.** Your machine needs no
  `gcloud`, `aws` or `terraform`.
- **What it sees:** this directory (as `/work`), `~/.config/gcloud` and
  `~/.aws`, so cloud logins persist between runs and are shared with any
  native install. A file you pass to it (e.g. `providers set FILE`) must be
  inside this directory.
- **Environment:** `BOXD_STATE_KEY`, `AWS_PROFILE`, `AWS_REGION`,
  `AWS_DEFAULT_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
  `AWS_SESSION_TOKEN`, `CLOUDSDK_CORE_PROJECT`, `BOXD_RELEASES_URL` (a copy
  of boxd's release bucket, only for tests; a `file://` directory is mounted
  read-only) and `BOXD_PRODUCT_LICENSE` (only for
  [clusters from before the release bucket](REFERENCE.md#clusters-installed-before-the-release-bucket))
  pass through from your shell, by name. Nothing else does.
- **Native instead:** install `terraform` (>= 1.10), `ansible-playbook` with
  the `ansible.posix` collection, `python3` (>= 3.11), OpenSSL 3 (`openssl`:
  it checks the signature of every boxd release; macOS ships LibreSSL, which
  cannot, so `brew install openssl@3` and put it first in `PATH`) and your
  cloud's CLI (on AWS also the Session Manager plugin), then use
  `./install.py` wherever this guide says `./boxd-install`. VS Code users can
  open the repo in its dev container, which uses the same image.

### Your state key

The installer needs one secret on every run: **`BOXD_STATE_KEY`**. It
encrypts all cluster state (TLS keys, node identities, provider secrets) in
your state bucket. **You generate it once.** Losing it means losing the
cluster's identity. boxd never sees it and can't recover it.

Keep it in an env file readable only by you, and in your password manager.
Create the file **once** (the `set -C` refuses to overwrite an existing file,
so running this twice can't replace your key):

```sh
( umask 077; set -C
  printf 'export BOXD_STATE_KEY=%s\n' "$(openssl rand -hex 32)" > ~/.boxd-<cluster>.env )
```

Copy the state key into your password manager now
(`grep STATE ~/.boxd-<cluster>.env` shows it). A [license key](REFERENCE.md#license-key),
if you have one, is a file next to `cluster.toml`, not an environment
variable.

**In every new shell**, before any installer command:

```sh
source ~/.boxd-<cluster>.env
```

---

## Install on GCP

Example names used below: project `my-project`, region `europe-west4`,
cluster `mycluster`, zone `boxd.example.com`. Use your own.

### 1. Sign in

```sh
./boxd-install gcloud auth login --no-launch-browser
./boxd-install gcloud auth application-default login --no-launch-browser
./boxd-install gcloud config set project my-project
./boxd-install gcloud auth application-default set-quota-project my-project
```

Each login prints a URL: open it, sign in, and paste the code back. The first
login is for gcloud commands, the second is the credential terraform uses.
The last line stops gcloud warning that the project and the credential's quota
project don't match.

### 2. Check the project

- **Billing** must be enabled.
- **Permissions:** boxd tests with an account that has **Owner** on the
  project. `apply` enables the APIs it needs itself (Compute, IAM, IAP,
  Public CA for certificates, Storage), which needs permission to enable
  services.
- **Quota:** the example needs 24 N2 vCPUs in your region (an `n2-standard-8`
  control node and an `n2-standard-16` worker). Check:

  ```sh
  ./boxd-install gcloud compute regions describe europe-west4 \
      --flatten=quotas --filter='quotas.metric=N2_CPUS' \
      --format='value(quotas.limit,quotas.usage)'
  ```

  If the limit minus usage is less than you need, request an increase
  (console → IAM & Admin → Quotas) before you apply.

### 3. Secrets

Create `~/.boxd-mycluster.env` as in [Your state key](#your-state-key),
and `source` it.

### 4. State bucket

One bucket per cluster holds the terraform state and the encrypted installer
state (`<cluster>/installer-state.enc`). Versioning is the recovery path for a
bad write:

```sh
./boxd-install gcloud storage buckets create gs://mycluster-state \
    --location europe-west4 --uniform-bucket-level-access --public-access-prevention
./boxd-install gcloud storage buckets update gs://mycluster-state --versioning
```

### 5. `cluster.toml`

```sh
cp cluster.example.toml cluster.toml     # the same file as examples/gcp/cluster.toml
```

Edit it and change every value marked `CHANGE`: the cluster name, zone,
project, region, state bucket, the certificate contact email, and the node
names (start them with your cluster name). Leave `assets_version = "latest"`:
see [Upgrades](REFERENCE.md#upgrades-and-rollback). Then:

```sh
./boxd-install validate
```

`validate` checks the file only. The license, bucket and quota are checked
when you apply.

**Worker machine types must support nested virtualization**, because boxd's
microVMs run under KVM inside the instance: **N2 (recommended)** or N1. The
default is `n2-standard-16` without a [license key](REFERENCE.md#license-key) and
`n2-standard-32` with one; the example uses `n2-standard-16`. AMD
(N2D/C2D/T2D), Arm (T2A/C4A) and E2 machines don't support it on GCP and
won't boot VMs. Other roles have no such constraint. The rest of the file is
explained in [Nodes and roles](REFERENCE.md#nodes-and-roles) and
[Machine sizes](REFERENCE.md#machine-sizes-templates).

### 6. Console sign-in (Google)

To have people sign in with your company's identity provider instead (Entra
ID, Okta, Google Workspace, Keycloak), follow
[Single sign-on](REFERENCE.md#single-sign-on-oidc) in place of this step.

In the Google Cloud console for your project:

1. **APIs & Services → OAuth consent screen:** audience **Internal**
   (recommended: only accounts in your Google Workspace can sign in, and
   Google skips app verification). External works too, but lets any Google
   account try to sign in and may need verification.
2. **APIs & Services → Credentials → Create credentials → OAuth client ID:**
   type **Web application**, authorized redirect URI
   `https://app.boxd.example.com/api/v1/auth/google/callback`.

Then store the client id and secret:

```sh
./boxd-install providers edit
```

It opens an editor on a commented template. Uncomment the `[google]` table and
fill it in. Values are **quoted** strings:

```toml
[google]
client_id = "1234567890-abc123.apps.googleusercontent.com"
client_secret = "GOCSPX-…"
```

Save and quit. It prints `providers config stored (google)`. If the file
doesn't parse, it says why and reopens the editor. Other providers (GitHub
sign-in, email, automations) are optional: see [Providers](REFERENCE.md#providers-secrets).

### 7. Apply

```sh
./boxd-install apply 2>&1 | tee apply.log
```

20–40 minutes: download and verify the assets → terraform (VPC, firewalls,
Cloud NAT, instances, a backups bucket) → per-node configs → ansible (installs
the boxd host kernel on workers, one reboot) → wait for the control plane →
check workers registered → seed VM templates. It ends by printing the `Assets:`
version it installed and the **glue records**.

**TLS on GCP** comes from **Google Trust Services** (`[acme] ca = "gts"`, the
GCP default). On the first apply the installer creates the ACME account key
for `contact_email` through your project's Public CA API and keeps it in the
encrypted state. There's nothing to set up. Inside a corporate network
certificates can come from your own CA instead: see
[TLS from your own CA](REFERENCE.md#tls-from-your-own-ca).

Continue with [After apply: delegate DNS and check](#after-apply-delegate-dns-and-check).

---

## Install on AWS

An AWS cluster has **no GCP dependency**. Example names used below: region
`eu-central-1`, AZ `eu-central-1a`, cluster `mycluster`, zone
`boxd.example.com`. Use your own.

### 1. Sign in

Any standard AWS credential source works. The aws CLI runs in the image, so
configure it through the wrapper; your config lands in `~/.aws`:

```sh
./boxd-install aws configure sso                # IAM Identity Center (recommended); once
./boxd-install aws sso login --use-device-code --profile <profile>
# or an IAM user's access keys:
./boxd-install aws configure --profile <profile>
```

Then, in each shell (or in your env file):

```sh
export AWS_PROFILE=<profile> AWS_REGION=eu-central-1
./boxd-install aws sts get-caller-identity      # the account you expect?
```

A session from `aws sts get-session-token` **without MFA** can't call IAM,
and `apply` creates IAM roles. Use SSO, an assumed role, or access keys.

### 2. Check the account

- **Permissions:** boxd tests with **AdministratorAccess**. `apply` creates
  VPC, EC2, IAM, S3 and SSM resources.
- **vCPU quota:** the example needs 24 vCPUs of "Running On-Demand Standard
  instances" (an `m7i.2xlarge` control node and an `m8i.4xlarge` worker; with
  a license key the default worker is `m8i.8xlarge`, and the two need 40). New accounts often have less, and
  `apply` would fail halfway through terraform:

  ```sh
  ./boxd-install aws service-quotas get-service-quota \
      --service-code ec2 --quota-code L-1216C47A --query Quota.Value
  # if it's too low:
  ./boxd-install aws service-quotas request-service-quota-increase \
      --service-code ec2 --quota-code L-1216C47A --desired-value 64
  ```

- **Worker instance type** offered in your AZ (not every type is in every AZ).
  An empty table means it isn't:

  ```sh
  ./boxd-install aws ec2 describe-instance-type-offerings \
      --location-type availability-zone \
      --filters Name=instance-type,Values=m8i.4xlarge \
                Name=location,Values=eu-central-1a --output table
  ```

### 3. Secrets

Create `~/.boxd-mycluster.env` as in [Your state key](#your-state-key),
and `source` it. Adding the `AWS_PROFILE`/`AWS_REGION` export there too saves
retyping it.

### 4. State bucket

One S3 bucket per cluster holds the terraform state and the encrypted
installer state (`<cluster>/installer-state.enc`). Terraform's state object is
encrypted with a key derived from `BOXD_STATE_KEY` (SSE-C). S3 blocks that by
default on buckets created since April 2026, and the last command allows it.
S3 bucket names are global, so add something unique such as your account id:

```sh
B=mycluster-state-<account-id>
./boxd-install aws s3api create-bucket --bucket $B \
    --create-bucket-configuration LocationConstraint=eu-central-1   # omit this line in us-east-1
./boxd-install aws s3api put-bucket-versioning --bucket $B \
    --versioning-configuration Status=Enabled
./boxd-install aws s3api put-public-access-block --bucket $B \
    --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
./boxd-install aws s3api put-bucket-encryption --bucket $B \
    --server-side-encryption-configuration '{"Rules":[{
      "ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},
      "BlockedEncryptionTypes":{"EncryptionType":["NONE"]}}]}'
```

### 5. `cluster.toml`

```sh
cp examples/aws/cluster.toml cluster.toml
```

Edit it and change every value marked `CHANGE`: the cluster name, zone,
region and availability zone, state bucket, the certificate contact email,
and the node names (start them with your cluster name). Leave
`assets_version = "latest"`: see [Upgrades](REFERENCE.md#upgrades-and-rollback). Then:

```sh
./boxd-install validate
```

`validate` checks the file only. The license, bucket and quota are checked
when you apply.

**Subnets.** The cluster lives in one availability zone with two subnets.
Nodes with a `proxy` or `dns` role go in the **public** subnet and get Elastic
IPs; everything else goes in the **private** one, with egress through a NAT
gateway. With [private ingress](#private-ingress-on-aws) every node goes in
the private one. `validate` checks that each `internal_ip` is in the right
subnet and isn't one of the five addresses AWS reserves per subnet.

**Worker instance types** need hardware virtualization. Two options:

- **Nested virtualization** on C7i/M7i/R7i or C8i/M8i/R8i (including `-flex`
  and `d` variants). The default is `m8i.4xlarge` without a
  [license key](REFERENCE.md#license-key) and `m8i.8xlarge` with one; the installer turns
  nested virtualization on for the instance.
- Any **`*.metal`** type (e.g. `c7i.metal-24xl`): no nested-virt overhead,
  which AWS recommends for latency-sensitive workloads, at a higher entry
  price and with slower instance boots.

For memory-heavy machine sizes (e.g. 1 vCPU · 32 GiB), use the
memory-optimised **R7i/R8i** family (`r8i.8xlarge` = 32 vCPU / 256 GiB, or
`r7i.metal-24xl` / `r8i.metal-48xl`): an M-family worker has 4 GiB per vCPU,
so a few 32 GiB machines fill it. Keep all workers of a cluster on one
instance family, because VM snapshots move only between hosts with the same
CPU model. The rest of the file is explained in [Nodes and roles](REFERENCE.md#nodes-and-roles)
and [Machine sizes](REFERENCE.md#machine-sizes-templates).

### 6. Console sign-in (GitHub or Google)

To have people sign in with your company's identity provider instead (Entra
ID, Okta, Google Workspace, Keycloak), follow
[Single sign-on](REFERENCE.md#single-sign-on-oidc) in place of this step.

On AWS, the simplest sign-in is a **GitHub OAuth App** (below). Google sign-in
works on AWS too, if you have a Google Cloud project to hold its OAuth client:
follow [GCP step 6](#6-console-sign-in-google) instead, with your `<zone>`.
You can set both.

Create the GitHub app under your GitHub organization (Settings → Developer
settings → OAuth Apps → New OAuth App) or your account:

- **Homepage URL:** `https://app.boxd.example.com`
- **Authorization callback URL:** `https://app.boxd.example.com/api/v1/auth/github/callback`

Generate a client secret, then store both:

```sh
./boxd-install providers edit
```

It opens an editor on a commented template. Uncomment the `[github]` table and
fill in `client_id` and `client_secret` only. Values are **quoted** strings:

```toml
[github]
client_id = "Ov23li…"
client_secret = "…"
```

Save and quit. It prints `providers config stored (github)`. If the file
doesn't parse, it says why and reopens the editor. The `app_*` fields are for
a separate, optional GitHub App that gives orgs repository access: see
[Providers](REFERENCE.md#providers-secrets).

### 7. Apply

```sh
./boxd-install apply 2>&1 | tee apply.log
```

20–40 minutes: download and verify the assets → terraform → per-node configs →
ansible (each node downloads the assets bundle itself; installs the boxd host
kernel on workers, one reboot) → wait for the
control plane → check workers registered → seed VM templates. It ends by
printing the `Assets:` version it installed and the **glue records**.

On a fresh cluster `apply` may print `restarting services that gave up
registering`: a service started before the control plane had elected a
leader. That's expected; the restart fixes it, and the next line says every
node registered (or `apply` fails, naming the ones that didn't).

**What `apply` creates on AWS:**
- the VPC, both subnets, an internet gateway and a NAT gateway;
- security groups: internal allow-all; proxy ports 80/443/9443/10000-30000/40000-60000
  from `ingress_cidrs` (default anywhere); dns 53 from anywhere; no inbound SSH anywhere;
- an SSM instance profile;
- Elastic IPs for proxy and dns nodes (none with private ingress);
- gp3 data volumes for workers (12k IOPS / 500 MiB/s by default);
- the backups bucket, with a dedicated IAM user and access key;
- with [`[snapshot_archive]`](REFERENCE.md#snapshot-archive), the archive bucket and its
  own IAM user and access key;
- with [`bucket_access = "vpc"`](#buckets-only-inside-the-vpc), an S3
  gateway endpoint and a policy on each of those buckets.

Instances run IMDSv2-only with a hop limit of 1, and workers also firewall
guests off the metadata endpoint. Outbound traffic from VMs goes through the
NAT gateway, which bills per GB processed.

**TLS on AWS** comes from **ZeroSSL** (`[acme] ca = "zerossl"`, the AWS
default). On the first apply the installer gets ZeroSSL's account key for
`contact_email` and keeps it in the encrypted state. There's nothing to set
up. Inside a corporate network certificates can come from your own CA
instead: see [TLS from your own CA](REFERENCE.md#tls-from-your-own-ca).

Continue with [After apply: delegate DNS and check](#after-apply-delegate-dns-and-check).

### Private ingress on AWS

For a cluster that only people on your network should reach, set
`ingress = "private"`. No node gets a public IP, and laptops come in over
[Twingate](https://www.twingate.com/) (or any VPN that lands in the VPC).

```toml
[aws]
ingress = "private"
# ingress_cidrs = ["10.70.0.0/16"]   # default in private mode: vpc_cidr

[acme]
ca = "local"
dns_verify_resolver = "10.70.0.2"    # the VPC's resolver: vpc_cidr's base address + 2
```

With the example's addresses, move the control node into the private subnet
too: `internal_ip = "10.70.1.10"` for the control node and `"10.70.1.20"` for
the worker (two nodes can't share an address).

- **No Elastic IPs.** Proxy and dns nodes go in the private subnet
  (`validate` checks it), and the cluster advertises their private IPs
  wherever it would advertise the Elastic IPs: the DNS records, and the
  proxy IP the CLI and console show. Outbound traffic still leaves through
  the NAT gateway, so the public subnet, internet gateway and NAT gateway
  stay. VMs reach `*.<zone>` on the private proxy IP too.
- **Needs the local CA** ([TLS from your own CA](REFERENCE.md#tls-from-your-own-ca)).
  Public CAs validate by querying the cluster's nameservers from the
  internet, which a private cluster can't answer, so `validate` refuses
  `ingress = "private"` with ZeroSSL.
- **`ingress_cidrs`** is who may reach the proxy ports (80, 443, 9443,
  10000-30000, 40000-60000). It defaults to `vpc_cidr` in private mode,
  where the Twingate connector lives, and to `0.0.0.0/0` in public mode. Add
  the CIDR of anything that dials the proxy from outside the VPC (a peered
  VPC, a Direct Connect range). IPv4 CIDRs only. dns (53) is not restricted
  by it: a nameserver is queried by resolvers, not by your users, so
  narrowing it would break resolution. With private ingress the dns nodes
  have no public IP, so only networks routed into the VPC reach them.
  Keep the default in public mode: narrowing it also drops the cluster's
  own traffic to `*.<zone>`, which reaches the proxy's Elastic IP from the
  NAT gateway's address.
- **DNS instead of glue.** `apply` ends with the DNS setup in place of
  registrar glue records: the dns nodes' private IPs, and a conditional
  forwarder for `<zone>` to them. Put the forwarder on your corporate DNS, on
  a Route 53 Resolver outbound endpoint + forwarding rule associated with the
  VPC, or on the Twingate connector host's resolver.

**Twingate.**

1. Deploy a connector in the VPC (any subnet; its address is then inside
   `vpc_cidr`, which the default `ingress_cidrs` admits).
2. Make the connector resolve `<zone>` through boxd-dns. Twingate resolves
   resource names **on the connector**, not on the laptop, so this is what
   makes `app.<zone>` work for users. Either point the connector host's
   resolver at the dns nodes for `<zone>`, or add the Route 53 Resolver
   forwarding rule above (then the VPC's own resolver answers for the zone).
3. Add two resources: `<zone>` and `*.<zone>`, all ports (or 80, 443, 9443,
   10000-30000, 40000-60000).
4. Laptops need your corporate root in their trust store, as for any local
   CA cluster.

Then check from a laptop with the Twingate client on:

```sh
dig +short app.<zone>                     # an address Twingate assigns (100.x), not empty
curl -sI https://app.<zone> | head -1
```

**Switching an existing cluster** between public and private is
disruptive. It releases the Elastic IPs (or allocates new ones going back),
moves proxy and dns nodes to the other subnet, which replaces those
instances and changes every advertised IP, and (coming from ZeroSSL)
switches the CA. Plan it for a maintenance window: change `internal_ip`,
`ingress` and `[acme]`, `apply`, swap the glue records for the forwarder
(or back), and expect every client to reconnect.

Users, orgs, machines and everything else in the control state survive it.
When the plan replaces a control node, `apply` stops `boxd-control` there
and keeps a copy of its state (`/var/lib/boxd/control`) in the encrypted
state before terraform runs, and restores it on the new instance before
`boxd-control` starts. Workers aren't replaced, so machines keep running
and keep their disks. Not carried over: the ClickHouse metrics on that
node (usage history starts again). Custom domains whose DNS points at the
old Elastic IP need their records updated by their owners.

### Buckets only inside the VPC

The buckets `apply` creates (backups, and the
[snapshot archive](REFERENCE.md#snapshot-archive) when you turn it on) are private
either way: only the IAM users `apply` creates can use them. To also refuse
any request that doesn't come from inside the cluster's VPC, even one with
valid keys, set:

```toml
[aws]
bucket_access = "vpc"      # default "anywhere"
```

`apply` then adds an S3 gateway endpoint to the VPC, on both route tables,
so the nodes' S3 traffic also stops going through the NAT gateway. It also
adds a policy to each bucket that denies reading, writing and deleting
objects unless the request comes through that endpoint. Only nodes in the
VPC use the buckets, so nothing in the cluster changes. Bucket settings stay
manageable with IAM from anywhere, so `apply` and `destroy` keep working.
Listing object names isn't restricted, because terraform's check that a
bucket exists needs that permission. Their contents are.
It works with public and private ingress, and switching it either way
doesn't disrupt anything.

Objects can't be read or deleted from your laptop any more. To empty a
bucket before [Tearing down](REFERENCE.md#tearing-down), do it from a node
([SSH to nodes](REFERENCE.md#ssh-to-nodes), then `aws s3 rm --recursive`), or set
`bucket_access = "anywhere"` and `apply` first.

---

## Install on bare metal

Use this when you already run the hosts: your own servers, VMs on your own
hypervisor, or instances in another cloud. The installer creates nothing
here. You prepare the hosts and the network, list them in `cluster.toml`,
and `apply` installs boxd on them over ssh. There is no terraform, no cloud
account and no state bucket.

Example names used below: cluster `mycluster`, zone `boxd.example.com`, a
private network `10.60.0.0/24` on NIC `eno2`. Use your own.

### 1. Prepare the hosts

Every node needs:

- **Ubuntu 24.04, x86_64.**
- **A user `ubuntu` with passwordless sudo**, reachable over ssh from the
  machine you install from. You add the installer's key in
  [step 5](#5-apply).
- **An address on one private network** shared by all nodes, inside
  `10.0.0.0/8`, with all TCP and UDP open between the nodes and an MTU of at
  least 1550 (VM traffic between hosts runs over VXLAN). The NIC on that
  network must have the same name on every host.
- **Outbound internet.** Each node downloads the boxd release from
  `storage.googleapis.com` and packages from the Ubuntu, Docker and
  ClickHouse repositories.

Workers also need:

- **Hardware virtualization**: `/dev/kvm` must exist. On a server, turn on
  VT-x or AMD-V in the firmware. A worker that is itself a VM needs nested
  virtualization.
- **GRUB boot.** `apply` installs boxd's host kernel, makes it the GRUB
  default and **reboots the worker** into it.
- **A dedicated data disk** for VM data. `apply` **formats it as XFS**
  unless it is XFS already, so everything on it is lost. It also puts a 64 GB
  swap file on it.

Proxy and dns nodes also need a **public IPv4** (1:1 NAT is fine), open from
the internet on:

| Node | Ports |
|---|---|
| proxy | TCP 80, 443, 9443, 10000–30000, 40000–60000 |
| dns | TCP and UDP 53 |

Port 22 stays your host's own sshd. The proxy takes per-VM SSH on
10000–30000, HTTPS on 443, the API the CLI uses on 9443, and raw TCP ports
on 40000–60000.

**Use dedicated hosts.** Besides the kernel and the disk, `apply` installs
Docker on workers, sets sysctls and transparent hugepages, adds iptables
NAT and forwarding rules, a `boxd-br0` bridge and VXLAN interfaces, and runs
ClickHouse on the metrics node. VMs get addresses from `bridge_subnet_base`
(default `10.100.0.0/16`), which must not overlap a network your hosts
route to.

### 2. Secrets

Create `~/.boxd-mycluster.env` as in [Your state key](#your-state-key),
and `source` it.

### 3. `cluster.toml`

```sh
cp examples/metal/cluster.toml cluster.toml
```

Change every value marked `CHANGE`: the cluster name, zone, `vpc_cidr` (your
private network), `interface` (the NIC on it), the certificate contact
email, and the nodes. Each node takes:

| Key | |
|---|---|
| `internal_ip` | its address on the private network |
| `public_ip` | proxy and dns nodes: the address clients and the glue records point at |
| `ssh_host` | optional: where the installer connects. Default: `public_ip`, else `internal_ip` |
| `data_disk` | workers: the data disk. Prefer a stable name from `/dev/disk/by-id/` over `/dev/sdb` |

```toml
[metal]
vpc_cidr  = "10.60.0.0/24"
interface = "eno2"

[[nodes]]
name        = "mycluster-control-0"
roles       = ["control", "proxy", "console", "metrics", "dns:ns1"]
internal_ip = "10.60.0.10"
public_ip   = "203.0.113.10"

[[nodes]]
name        = "mycluster-worker-0"
roles       = ["worker"]
internal_ip = "10.60.0.20"
ssh_host    = "203.0.113.20"
data_disk   = "/dev/disk/by-id/nvme-SAMSUNG_MZQL23T8HCLS-00A07_S64HNE0T123456"
```

Then:

```sh
./boxd-install validate
```

Certificates come from ZeroSSL by default. Inside a corporate network they
can come from [your own CA](REFERENCE.md#tls-from-your-own-ca) instead;
`providers set-tls-ca` works the same here.

### 4. Console sign-in

Set up Google sign-in ([GCP step 6](#6-console-sign-in-google)), GitHub
sign-in ([AWS step 6](#6-console-sign-in-github-or-google)) or
[single sign-on](REFERENCE.md#single-sign-on-oidc), the same as on a
cloud. Google sign-in needs a Google Cloud project for its OAuth client, not
for the cluster.

### 5. Apply

```sh
./boxd-install apply
```

The first time, `apply` makes the cluster's deploy key and stops before it
changes anything, because no host accepts the key yet:

```
error: these hosts are not ready:
  mycluster-control-0 (ubuntu@203.0.113.10): ssh failed: ubuntu@203.0.113.10: Permission denied (publickey).
  mycluster-worker-0 (ubuntu@203.0.113.20): ssh failed: ubuntu@203.0.113.20: Permission denied (publickey).
On every node, the user ubuntu needs passwordless sudo and this line in ~ubuntu/.ssh/authorized_keys (the cluster's deploy key):

  ssh-ed25519 AAAA… boxd-deploy

Then run apply again.
```

Add that line on every node, then run it again:

```sh
./boxd-install apply 2>&1 | tee apply.log
```

It checks ssh, sudo and `/dev/kvm` first, then: download and verify the
assets → per-node configs → ansible (installs the boxd host kernel on
workers, one reboot) → wait for the control plane → check workers
registered → seed VM templates. It ends by printing the `Assets:` version it
installed and the **glue records**. Without a license key it also counts the
workers' CPUs and warns when they're over the free tier.

**The state is a file.** Instead of a bucket, the encrypted state lives in
`metal-state/<cluster>/installer-state.enc` next to `cluster.toml`, with a
`.prev` copy beside it (`[metal] state_dir` moves it). It holds every key
the cluster has, encrypted with `BOXD_STATE_KEY`. **Back it up**, and keep
it with the state key: losing either loses the cluster's identity. Git
ignores `metal-state/`. `state pull`, `state push` and `providers` work on
it as they do on a bucket.

`destroy` doesn't apply: the installer created no hosts. To remove boxd,
reinstall the hosts.

Continue with [After apply: delegate DNS and check](#after-apply-delegate-dns-and-check).

---

## After apply: delegate DNS and check

**1. Delegate the zone.** `apply` ends with something like:

```
DNS delegation: add these records in the parent zone example.com, at whoever hosts it:
  ns1.boxd.example.com  A  203.0.113.10    (host "ns1.boxd")
  NS records for boxd.example.com: ns1.boxd.example.com    (host "boxd", one NS record each)
```

At your DNS provider, in the **parent** zone (`example.com`), add:

- an **NS** record for `boxd` (i.e. `boxd.example.com`) → `ns1.boxd.example.com.`,
  one per nameserver listed;
- an **A** record for `ns1.boxd` (i.e. `ns1.boxd.example.com`) → the IP shown,
  one per nameserver. These "glue" records let resolvers find the
  nameservers inside the zone they serve.

With private ingress, `apply` prints a conditional-forwarder setup instead:
see [Private ingress on AWS](#private-ingress-on-aws).

**2. Wait for it, then for certificates.**

```sh
dig NS boxd.example.com +short @1.1.1.1         # your ns1 (and ns2)?
curl -sI https://app.boxd.example.com | head -1  # HTTP/2 200 once certificates are issued
```

The cluster requests its certificates itself, and only *after* the
delegation resolves publicly. Until they're issued, HTTPS fails: browsers show
`ERR_SSL_PROTOCOL_ERROR`, curl shows `tlsv1 alert access denied`. That's
normal for a few minutes after adding the records and clears by itself. If it
lasts more than about 15 minutes, see [Troubleshooting](REFERENCE.md#troubleshooting).

**3. Sign in first yourself** at `https://app.boxd.example.com`. Whoever
signs in first creates and owns the cluster's org (see
[Users, orgs, sign-in](REFERENCE.md#users-orgs-sign-in)). Then follow the console's
onboarding: it walks you through installing the CLI and starting your first
VM, which checks the whole path (sign-in, control plane, a worker, VM boot,
per-VM HTTPS through the proxy).

**4. Tell your users:** [Onboarding your users](REFERENCE.md#onboarding-your-users) has a
message to forward.

Re-running `apply` is always safe, and it's how you roll out any change:
topology, service config, providers, or a new `assets_version`. Services
restart only where something actually changed.
