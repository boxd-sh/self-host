# How the installer works

`cluster.toml` goes in, a running cluster comes out. The installer is a Python
program (`boxd_installer/`) that drives three tools. Each tool has one job, and
the Python decides the order and writes every tool's input.

## Who owns what

| Layer | Where | It owns | It never touches |
|---|---|---|---|
| **terraform** | `terraform/gcp`, `terraform/aws`, `terraform/gke` | Cloud resources: the network, firewall rules, instances (on GKE, the cluster and its node pools), buckets | Anything on a node |
| **ansible** | `ansible/` (`site.yml` and one role per node role) | What is on each host: packages, the boxd host kernel, the release bundle, the config files, the systemd units | Cloud resources. Not used on GKE |
| **Kubernetes objects** | `boxd_installer/kube_render.py`, `kube.py` | On GKE only, in place of ansible: the pods, their config Secret, the services | Cloud resources |
| **boxd-ctl** | run by `boxd_installer/cluster.py` | Settings inside the running cluster: the license, VM templates, orgs, telemetry | Files on a node |
| **the installer** | `boxd_installer/` | The checks, the order, each tool's input (tfvars, inventory, configs), and the encrypted state | |

Bare metal has no terraform: the hosts exist, and everything else is the same.

## The five phases of `apply`

`apply` always runs the same five phases, and logs each one as it starts
(`phase 2/5, infrastructure: terraform`). In `boxd_installer/apply.py` they
are one function each, called from `_apply` (GCP, AWS, bare metal) and
`_apply_gke`, and the file reads top to bottom in that order.

| # | Phase | What happens | Tool | Function |
|---|---|---|---|---|
| 1 | **preflight** | Checks `cluster.toml`, the license key and the state; asks before removing a node; downloads the release bundle and checks its signature. Nothing in the cloud or on a node has changed yet | none | `_preflight` |
| 2 | **infrastructure** | Plans and applies the cloud resources. Skipped on bare metal | terraform | `_infrastructure` |
| 3 | **render** | Writes every node's config files and the ansible inventory, from `cluster.toml`, your providers config, the state and terraform's outputs | none | `_render_configs` |
| 4 | **converge** | Installs the bundle and the configs on every node, then waits for the control nodes to elect a leader and for every worker to register | ansible (on GKE, Kubernetes objects) | `_converge` |
| 5 | **finish** | Sets up the running cluster: cluster id, license, storage, telemetry, VM templates, orgs. Prints the DNS records to delegate | boxd-ctl | `_finish` |

Running `apply` again is always safe: every phase can be repeated, and that
is how a failed `apply` is resumed.

## Which command runs what

| Command | What it runs |
|---|---|
| `validate` | The checks on `cluster.toml` only. Changes nothing |
| `plan` | Preflight's checks, then the plan half of phase 2: it prints `terraform plan` and stops. Changes nothing |
| `render` | Phase 3 only, from the outputs the last `apply` saved |
| `apply` | All five phases |
| `destroy` | `terraform destroy` on what phase 2 created |
| `ctl -- <args>` | One `boxd-ctl` command on a control node |
| `state`, `providers` | Read or edit the encrypted state. No phase runs |

## The terraform directories don't run on their own

`terraform plan` inside `terraform/aws` fails, on purpose:

- **The state is encrypted.** Terraform's state object in your bucket is
  encrypted with a key derived from `BOXD_STATE_KEY`. Terraform can't read it
  without that key in its environment.
- **The variables are generated.** `terraform.tfvars.json` is written by the
  installer from `cluster.toml`, the state and your license. It isn't kept
  between runs.
- **The checks live outside terraform.** Before it plans, the installer
  refuses changes that would destroy data, such as moving a cluster to another
  VPC, or a worker between an EBS data disk and instance store.

To see what terraform would change, run `./boxd-install plan`. To keep the
tfvars and the plan file, add `--out-dir DIR`.

## State

Two objects live in your state bucket, both encrypted under `BOXD_STATE_KEY`:

- **Terraform's state**: what phase 2 created.
- **The installer's state** (`<cluster>/installer-state.enc`): TLS and SSH
  keys, each node's id, your providers config, and what the last `apply`
  installed.

Every command that needs the installer's state downloads it at the start and
uploads it at the end (`boxd_installer/remote_state.py`). `plan` only
downloads it. On bare metal the state is a file next to `cluster.toml`.

## Where to look in `boxd_installer/`

| For | Read |
|---|---|
| The commands and their arguments | `main.py` |
| `apply`'s five phases, their checks, and `plan` | `apply.py` |
| What `cluster.toml` may contain | `config.py` |
| What differs per cloud | `clouds/gcp.py`, `clouds/aws.py`, `clouds/gke.py`, `clouds/metal.py` |
| How terraform is run | `tf.py` |
| The config files a node gets | `render.py` (on GKE, `kube_render.py`) |
| The one place ansible is run | `ansible.py` |
| ssh to a node, and every `boxd-ctl` step | `cluster.py` (on GKE, through `kube.py`) |
| The encrypted state | `state.py`, `remote_state.py` |
| The release bundle and its signature | `assets.py` |
