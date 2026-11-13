# Windows images

A boxd cluster runs Windows Server machines next to its Linux ones, when its license key
has Windows guests. boxd can't hand you a Windows image: Windows is licensed by Microsoft,
and every Windows machine you run is licensed through you. So you build the image yourself,
from your own Windows installation media, and push it to a registry your workers can pull
from. This directory is your part of that image.

## What you need

- **A license key with Windows guests** (`[license] key_file`, docs/REFERENCE.md
  "License key"). Ask boxd to add the feature to your key.
- **Windows Server 2025 installation media** (an ISO), below.
- **A private container registry** your workers can pull from, for the image you build.
  The image is about 40 GB.
- **A KMS host** for activation, if your media is from volume licensing or SPLA (below).
- **A build host:** Linux with `/dev/kvm`, docker, `curl` and about 100 GB of free
  disk. One of your workers will do. A build takes about 15 minutes.

## The Windows ISO

### For production: your volume-licensing or SPLA media

Use Windows Server 2025 Standard or Datacenter media from your Microsoft agreement:
the Volume Licensing Service Center, or your SPLA reseller for hosting. A volume
edition is a KMS client: its machines activate against your KMS host, which you name
once for the cluster. Microsoft licenses each Windows machine (per virtual machine or
per host, by core) through your agreement, and you report its use there. boxd counts
nothing for Microsoft, but its machine listings show which machines run Windows and their
vCPUs, which a per-VM count needs.

### For trying it: the evaluation ISO

Microsoft's evaluation of Windows Server 2025 is free for 180 days:

1. Open the Microsoft Evaluation Center:
   <https://www.microsoft.com/en-us/evalcenter/download-windows-server-2025>.
2. Fill in the form (name, company, email, country), then choose **ISO downloads**
   and your language.
3. Download the 64-bit ISO, about 5 GB.

The English (United States) ISO also has a direct link, which needs no form:
<https://go.microsoft.com/fwlink/?linkid=2293312&clcid=0x409&culture=en-us&country=us>.

An evaluation image runs every boxd feature. But its machines can't activate against a
KMS host, and each one counts its 180 days from its own first boot. It's for trying
boxd's Windows machines, not for production: build your production image from
licensed media.

## The image's two parts

| Part | Where it comes from | What it holds |
|---|---|---|
| boxd's | the release bundle you install, so it always matches your cluster's version | the boxd agent (exec, files, RDP passwords, the coding-agent logins, `boxd` and `run` in the machine), the boot script that gives each machine its own name, address and SID, the virtio drivers, the terminal support (ConPTY), sysprep, and the packaging as an image |
| yours | this directory | the software every machine starts with (`fetch.sh`, `install.ps1`), and the hooks a copied machine runs (`on-clone.d/`) |

The build runs boxd's setup first, then your `install.ps1`, puts your `on-clone.d/` hooks in
place, then generalizes the image with sysprep, so every machine made from it gets its own
identity.

## Your software layer

- `fetch.sh` runs on the build host. It downloads each file, checks it against a
  pinned SHA-256, and puts it in `files/` (git-ignored).
- `install.ps1` runs inside the image build and installs from `files/`. The build has no
  network: everything it needs comes from `files/`.

boxd's default installs the tools coding agents use on a Windows machine: Git for
Windows, Claude Code, Codex, Bun and the GitHub CLI. As on Linux machines, the boxd agent
signs Claude Code and Codex in with the user's connected accounts, and gives `git` and
`gh` the machine's GitHub credential, keeping both fresh.

### The rules for `install.ps1`

- Install for all users: under `C:\Program Files`, on the machine `Path`. Never into the
  Administrator's profile: sysprep removes it, and every machine starts without it.
- No network. Everything comes from `files\`.
- Don't run sysprep, shut down or reboot. boxd's setup does that after your script.
- Stop on any error (`throw`). A failed build is better than an image with half its
  software.
- For boxd's GitHub wiring, keep Git for Windows in `C:\Program Files\Git` and the GitHub
  CLI in `C:\Program Files\GitHub CLI`. The agent becomes git's credential helper for
  `github.com` and puts its `gh` wrapper ahead of `gh`, each only when it finds them
  there.

### Changing it

- **Add software:** a `fetch NAME URL SHA256` line in `fetch.sh`, and its silent install in
  `install.ps1`. For an MSI:
  `Start-Process msiexec.exe -Wait -ArgumentList '/i', (Need 'tool.msi'), '/qn', '/norestart'`,
  and check its exit code.
- **Drop the coding tools:** remove their lines from both files. Machines work without
  them; the agent's GitHub wiring then does nothing.
- **Move a tool forward:** its URL and SHA-256 in `fetch.sh`. The next image has it.

What a copy of a machine must not inherit (an EDR agent's host ID, a monitoring agent's ID)
belongs in an on-clone hook, below. A domain join happens once the machine is up: hooks run
before it has a network.

## Hooks on copies (`on-clone.d/`)

A fork, or a machine made from a snapshot, starts from a copy of another machine's disk:
the software on it has already run as that machine. Every `*.ps1` in `on-clone.d/` goes
into the image (`C:\ProgramData\boxd\on-clone.d\`), and a copy runs them at its first
boot, as SYSTEM, before it gets an address. A hook that fails keeps the copy off the network.
A fresh machine from the image doesn't run them. [on-clone.d/README.md](on-clone.d/README.md)
has the details, the variables a hook sees and the rules.

With `[windows] copies = "fast"` in cluster.toml (docs/REFERENCE.md "Windows machines"), a
copy is not booted: it resumes its source's memory with a new network address. No hook runs
then, and the software on it carries on as its source, its host IDs included.

## Example: CrowdStrike Falcon

`examples/crowdstrike/` installs the Falcon sensor in the image:

1. **A Falcon API client** with the scope **Sensor download: Read** (Falcon console: Support and
   resources → API clients and keys). Only the image build uses it: put it in the build host's
   environment when you run the build, not in `cluster.toml` or the providers config.
   ```sh
   export FALCON_CLIENT_ID=… FALCON_CLIENT_SECRET=… FALCON_CLOUD=eu-1   # us-1, us-2, eu-1, us-gov-1
   ```
2. **The sensor and your CID.** Add this line to the end of `fetch.sh`:
   ```sh
   OUT="$PWD" sh "$HERE/examples/crowdstrike/fetch-falcon.sh"
   ```
   It downloads the Windows sensor through Falcon's API, checks its SHA-256 against the one Falcon
   lists, and reads your customer ID (CID), into `files/`. The newest sensor by default;
   `FALCON_SENSOR_VERSION=7.20.19011` for another. Your hosts' sensor update policy moves every
   machine's sensor to its own version anyway, so match it.
3. **Install it in the image.** Copy `examples/crowdstrike/install-falcon.ps1` next to
   `install.ps1`, and add this line to the end of `install.ps1`:
   ```powershell
   & (Join-Path $PSScriptRoot 'install-falcon.ps1')
   ```
   It installs the sensor with your CID and three options: `NO_START=1` (the image's sensor never
   registers; each machine registers at its first boot, as its own host), `VDI=1` (a fork or a
   snapshot's copy notices it was cloned and registers as a new host) and `ProvNoWait=1` (the
   build has no network). It tries up to 10 times: right after the build's first logon the
   installer can fail (exit 28, or 1618 while Windows Installer is busy). The CID is in the image,
   readable by every machine's administrators, as on any host you deploy the sensor to.
4. **Copies.** With `VDI=1` the sensor sorts copies out by itself: on boxd, about two minutes
   after a fork booted, the copy registered as a new host, and its source re-registered once under
   its own hostname, with no hook and no detection. `on-clone.d/crowdstrike-falcon.ps1.example` is
   the other way, and a worse one: it clears the copied agent ID before the copy reaches the
   network, but only when the hosts' sensor update policy has uninstall and maintenance protection
   off (with it on, the sensor refuses and the copy stays off the network), and Falcon raises a
   **High "Defense Evasion via Disable or Modify Tools" detection** for every copy unless an IOA
   exclusion covers the hook. With `copies = "fast"` a copy resumes its source's running sensor
   instead of booting, and keeps its source's agent ID until the sensor notices otherwise: check
   the copies in your Falcon console before relying on it.
5. **Network.** The sensor talks to CrowdStrike's cloud over HTTPS. Machines reach the internet
   by default; on a machine with an egress allowlist, allow CrowdStrike's hosts for your cloud.

## Building and installing the image

Name the image in `cluster.toml` first (`[windows] image`, with a tag you haven't pushed before)
and put the registry's credentials in the providers config (`[windows_registry]`, docs/REFERENCE.md
"Windows machines"). Then, on the build host, in the installer's directory:

```sh
./boxd-install windows-image build --iso ~/iso/windows-server-2025.iso
```

It takes about 20 minutes:

1. **boxd's half** of the image comes from the release your cluster runs (`[cluster]
   assets_version`): the installer fetches and checks the release bundle, as `apply` does, and
   copies its Windows image kit into `cache/windows-image/<release>/`;
2. **the inputs**: boxd's (the virtio drivers, the terminal support), each checked against a pinned
   SHA-256, and yours (`fetch.sh` into `files/`). Downloads stay in the cache for the next build;
3. **the install** of Windows from your ISO under QEMU, with no network: boxd's setup (the agent, the
   boot script), then your `install.ps1` and `on-clone.d/` hooks, then sysprep, so every machine
   made from the image gets its own identity. A failed or hung `install.ps1` stops the build after
   an hour at most, with its log in the message;
4. **the push** of the image to `[windows] image`, with `[windows_registry]`'s credentials (else
   the build host's own `docker login`). The built disk is deleted afterwards (`--keep` keeps it).

Then `./boxd-install apply` makes it the cluster's Windows image. Options:

| Option | |
|---|---|
| `--iso FILE` | your Windows Server 2025 ISO (above) |
| `--product-key-file FILE` | a file with the product key Windows Setup needs for volume-licensing media: Microsoft's KMS client setup key for the edition (Windows Server 2025 Datacenter: `D764K-2NDRG-47T6Q-P8T8W-YP6DF`, Standard: `TVRH6-WHNXV-R9WG3-9XRFY-MY832`). The evaluation needs none |
| `--index N` | the ISO's edition, its image index (default 4, Datacenter with Desktop Experience, on the evaluation and on volume media; 3 Datacenter Core, 2 Standard with Desktop Experience, 1 Standard Core) |
| `--size SIZE` | the image's disk, default `32G`: every machine's disk grows past it |
| `--name NAME` | the build's name on the build host |
| `--keep` | keep the built disk in `cache/windows-image/` |

The build host is Linux with `/dev/kvm` and Docker (usable as your user), with about
100 GB free in the installer's directory, and the installer's access as for `apply` (its state
key and cloud login: it reads the release and `[windows_registry]` from there). The host you run
the installer on, if it has KVM; else any Linux host with KVM where you set the installer up, a
worker for instance. On a cloud VM, nested virtualization must be on.
