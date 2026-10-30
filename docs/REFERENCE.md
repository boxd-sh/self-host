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
  live there.
- **Nameservers:** the examples have one (`dns:ns1`). For redundancy, give a
  second node `dns:ns2`; `apply` then prints glue records for both. Today the
  zone's own NS answer always lists `ns1` and `ns2`, even with one
  nameserver. Resolvers that try the missing `ns2` fall back to `ns1`, so
  lookups work, just slower now and then. A boxd release will fix this. For
  production, run two.

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

### Telemetry

Every boxd cluster sends boxd one usage report an hour, over HTTPS, from the
control leader to `https://telemetry.boxd.sh/v1/report`. It has **no
personal data**: counts only. A cluster that cannot reach boxd keeps working,
and nothing waits for the report.

What it sends (the full list; the report has no other field):

| Field | What |
|---|---|
| `format` | The report's version, `1` |
| `cluster_id` | The cluster's random ID (`boxd-ctl license status` shows it) |
| `boxd_sha` | The boxd version (its commit) |
| `sent_at` | When the report was built |
| `license` | `none`, or the license ID, licensee, state and features of the key |
| `workers` | For each worker: its CPU count, its host kernel version and its average CPU use over the hour. No names, no IDs, no IPs |
| `worker_vcpus` | The worker vCPUs the free tier counts |
| `nodes` | How many control nodes (Raft voters) and proxies; `dns` counts only DNS nodes registered with `boxd-ctl node add` (0 on an installer cluster, whose DNS nodes do not register) |
| `usage` | How many users, orgs, machines (all, running), and the vCPUs of the running machines |
| `location` | The cloud and the region from `cluster.toml` |

It never sends names, emails, user or org IDs, machine names, the zone or IP
addresses.

To see the exact report the cluster sends next, on a control node:

```bash
sudo boxd-ctl telemetry show          # the report, whether it is on, the last send
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

## Day-2 operations

### Updating the installer

This repo is released by boxd as tagged versions. Each one is listed in
[CHANGELOG.md](../CHANGELOG.md) and published as a GitHub Release (watch the
repo → Custom → Releases to be notified). To take one:

```sh
git fetch --tags
git log --oneline HEAD..origin/main     # what's new
git pull --ff-only
source ~/.boxd-<cluster>.env
./boxd-install validate && ./boxd-install apply
```

- Your `cluster.toml` and env file aren't tracked, so a pull never touches them.
- **Received the installer as a zip?** Unzip the new release next to the old
  one, copy your `cluster.toml` into it, and run from the new folder. The
  release notes are in its `CHANGELOG.md`.
- If the release moved to a new tools image, `./boxd-install` pulls it on its
  next run.
- An installer release changes how the cluster is installed (terraform,
  ansible, these docs). The boxd version it runs comes from `assets_version`:
  see the next section. Read the CHANGELOG entry before you apply: it says
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

Every `apply` prints the version it installed (`Assets: <sha> (channel latest)`)
and records it in the installer state. Note it down: it's the exact value to
pin to stay on, or come back to, that release.

An upgrade covers everything in the bundle: binaries, the boxd host kernel,
the console, the CLI your users download, and VM templates. Services restart
only where something actually changed, and `site.yml` converges control nodes
last with `serial: 1`, so replicas upgrade before the leader.

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

Add the node to `cluster.toml` and re-apply — terraform creates the instance,
ansible converges it, and the control plane picks it up:

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
boxd for a new key. An unbound key keeps working. If VM backups
were ever taken, `destroy` stops at the backups bucket, because it won't
delete one that isn't empty. Empty it first if you don't need the backups:
`<project>-backups` on GCP, `<cluster>-backups-<account>-<region>` on AWS.
The same goes for the [snapshot archive](#snapshot-archive) bucket,
`<cluster>-archive-<account>-<region>`.

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
- **`can't pull ghcr.io/…boxd-installer-tools`** on the first run: no network
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
- **`download failed (404)`** for a release: `assets_version` names a version
  boxd hasn't published (a typo in a pinned sha, or a channel name boxd
  doesn't publish). `download failed (403)` means the release bucket refused the
  download; if it persists, tell boxd.
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
