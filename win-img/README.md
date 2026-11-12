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

1. **The sensor and your CID.** With a `[crowdstrike]` provider (a Falcon API client with
   **Sensor download: Read**, docs/REFERENCE.md "Providers"), `windows-image build` downloads
   the newest Windows sensor through Falcon's API, checks its SHA-256 against the one Falcon
   lists, and reads your CID; the build installs it as below and you write nothing. Without
   the provider, download `FalconSensor_Windows.exe` from your Falcon console (Host setup and
   management → Sensor downloads) into `files/`, or add a `fetch` line that takes it from
   your own artifact store, with its SHA-256; CrowdStrike doesn't publish it.
2. **Install it in the image** with your CID and three options:
   - `NO_START=1`: the image's sensor never registers; each machine registers at its first
     boot, as its own host;
   - `VDI=1`: a fork or a snapshot's copy notices it was cloned and registers as a new host;
   - `ProvNoWait=1`: the build has no network, and without it the installer waits for
     CrowdStrike's cloud.

   Retry it: right after the image build's first logon the installer can fail (exit 28, or
   1618 while Windows Installer is busy), and the same command a few minutes later succeeds.
   By hand, in `install.ps1`:
   ```powershell
   for ($try = 1; $try -le 10; $try++) {
       $p = Start-Process -FilePath (Need 'FalconSensor_Windows.exe') -Wait -PassThru -ArgumentList @(
           '/install', '/quiet', '/norestart', 'CID=<your CID>', 'NO_START=1', 'VDI=1', 'ProvNoWait=1')
       if ($p.ExitCode -eq 0 -and (Get-Service CSAgent -ErrorAction SilentlyContinue)) { break }
       Start-Sleep -Seconds 60
   }
   if ($p.ExitCode -ne 0) { throw "the Falcon sensor's installer exited $($p.ExitCode)" }
   ```
   The CID is in the image, readable by every machine's administrators, as on any host you
   deploy the sensor to.
3. **Copies.** A fork or a snapshot's copy starts with its source's agent ID (AID). With
   `VDI=1` the sensor sorts it out by itself: on boxd, about two minutes after a fork booted,
   the copy registered as a new host, and its source re-registered once under its own
   hostname, with no hook and no detection. Without `VDI=1`, copies come up with their source's
   AID. `on-clone.d/crowdstrike-falcon.ps1.example` is the other way, and a worse one: it clears
   the copied AID before the copy reaches the network, but only when the hosts' sensor update
   policy has uninstall and maintenance protection off. With protection on, the sensor refuses
   and the copy stays off the network. Either way Falcon raises a **High "Defense Evasion via
   Disable or Modify Tools" detection** for every copy, unless an IOA exclusion covers the hook.
   With `copies = "fast"` a copy resumes its source's running sensor instead of booting, and
   keeps its source's AID until the sensor notices otherwise: check the copies in your Falcon
   console before relying on it.
4. **Network.** The sensor talks to CrowdStrike's cloud over HTTPS. Machines reach the
   internet by default; on a machine with an egress allowlist, allow CrowdStrike's hosts for
   your cloud.

## Building and installing the image

```sh
./win-img/fetch.sh
./boxd-install windows-image build \
    --iso ~/iso/windows-server-2025.iso \
    --registry registry.example.com/boxd/windows-server-2025:2026-11
```

The command:
1. builds the image on a host with KVM, with boxd's setup from your installed release, then
   this directory's `install.ps1` and `on-clone.d/` hooks, then sysprep;
2. boots it once to check it (the agent answers, the machine reports ready);
3. pushes it to your registry.

Then name it in `cluster.toml` and apply (docs/REFERENCE.md "Windows machines"). Your
workers pull it at their first Windows machine. After a boxd upgrade, build the image
again: boxd's part of the image comes from the release, and new Windows machines get what
the release adds to them. Machines made from an older image keep working.
