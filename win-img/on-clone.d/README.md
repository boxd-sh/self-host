# On-clone hooks

Every `*.ps1` file in this directory goes into the image as
`C:\ProgramData\boxd\on-clone.d\`. A Windows machine runs them when its disk was copied
from another machine: a fork, or a machine made from a snapshot. Each copy gets a new
machine id. A fresh machine from the image doesn't run them; it has nothing of another
machine to undo.

They run at the copy's first boot:
- in name order, each in its own PowerShell, as SYSTEM;
- **before the machine gets its address**, so the copy can't reach the network with its
  source's identity until every hook has passed;
- a hook that exits non-zero keeps the machine off the network, and boxd never shows it
  running.

After the hooks, the computer is renamed to the machine's name and reboots once.

Each hook sees, in its environment:

| Variable | |
|---|---|
| `BOXD_PREVIOUS_VM_ID` | the machine the disk was copied from |
| `BOXD_VM_ID` | this machine |
| `BOXD_NAME` | this machine's name |

## What they're for

Whatever must change on a copy before it shows up anywhere: an EDR agent's host ID, a
monitoring agent's ID, a licence bound to the machine. `crowdstrike-falcon.ps1.example` is
one; rename it to `.ps1` to use it.

## Rules

- No network: a hook runs before the machine has an address. It can't join a domain (an
  offline join with `djoin /requestodj` can).
- A copy of a domain member has already left its domain when the hooks run (its source
  keeps the computer account): join it again under its own name once it's up. A copy of a
  domain controller never gets this far; it stays off the network.
- Exit non-zero only when the copy must not come up. Retrying is up to the hook.
- Never put a secret in a hook: every administrator of every machine made from the image
  can read it.
