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
| yours | this directory | the software every machine starts with |

The build runs boxd's setup first, then your `install.ps1`, then generalizes the image
with sysprep, so every machine made from it gets its own identity.

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

Software that must differ per machine (domain joins, per-machine licences) doesn't
belong here. It belongs in an on-clone hook, which runs on each new machine's first
boot.

## Building and installing the image

```sh
./win-img/fetch.sh
./boxd-install windows-image build \
    --iso ~/iso/windows-server-2025.iso \
    --registry registry.example.com/boxd/windows-server-2025:2026-11
```

The command:
1. builds the image on a host with KVM, with boxd's setup from your installed release, then
   this directory's `install.ps1`, then sysprep;
2. boots it once to check it (the agent answers, the machine reports ready);
3. pushes it to your registry.

Then name it in `cluster.toml` and apply (docs/REFERENCE.md "Windows machines"). Your
workers pull it at their first Windows machine. After a boxd upgrade, build the image
again: boxd's part of the image comes from the release, and new Windows machines get what
the release adds to them. Machines made from an older image keep working.
