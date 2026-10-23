"""Installer state: stable node-id allocations + generated secrets.

state/ is the precious directory — installer.json (allocations), keys/
(secrets and keypairs), assets/ (extracted zips), composio.json (auth-config
cache). Everything under out/ is regenerable; nothing under state/ is.
"""
import json
import os
import secrets as pysecrets

from .util import InstallError, run

ROLE_ID_ORDER = ["control", "worker", "proxy", "dns", "console", "metrics"]
RAFT_PORT = {"control": 7000, "worker": 7000, "proxy": 7001, "dns": 7002}

class State:
    def __init__(self, state_dir: str):
        self.dir = state_dir
        self.keys_dir = os.path.join(state_dir, "keys")
        self.path = os.path.join(state_dir, "installer.json")
        os.makedirs(self.keys_dir, exist_ok=True)
        self.data: dict = {"node_ids": {}, "vnis": {}, "bridge_indexes": {}}
        if os.path.exists(self.path):
            with open(self.path) as fh:
                self.data = json.load(fh)

    def save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.data, fh, indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    def allocate(self, cfg) -> "Allocation":
        ids: dict = self.data.setdefault("node_ids", {})
        next_id = max([0] + [v for v in ids.values()]) + 1
        for role in ROLE_ID_ORDER:
            for node in cfg.nodes_with(role):
                key = f"{node.name}/{role}"
                if key not in ids:
                    ids[key] = next_id
                    next_id += 1

        vnis: dict = self.data.setdefault("vnis", {})
        bridges: dict = self.data.setdefault("bridge_indexes", {})
        next_vni = max([cfg.network["vni_range_start"] - 1] + list(vnis.values())) + 1
        next_bridge = max([-1] + list(bridges.values())) + 1
        for node in cfg.nodes_with("worker"):
            if node.name not in vnis:
                vnis[node.name] = next_vni
                next_vni += 1
            if node.name not in bridges:
                bridges[node.name] = next_bridge
                next_bridge += 1

        self.save()
        return Allocation(cfg, ids, vnis, bridges)

    def _secret_file(self, name: str, generate) -> str:
        path = os.path.join(self.keys_dir, name)
        if not os.path.exists(path):
            value = generate()
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(value)
        with open(path) as fh:
            return fh.read().strip()

    def secrets_kek(self) -> str:
        return self._secret_file("secrets_kek", lambda: pysecrets.token_hex(32))

    def jwt_secret(self) -> str:
        return self._secret_file("jwt_secret", lambda: pysecrets.token_urlsafe(32))

    def automations_webhook_secret(self) -> str:
        return self._secret_file("automations_webhook_secret", lambda: pysecrets.token_hex(32))

    def clickhouse_password(self) -> str:
        return self._secret_file("clickhouse_password", lambda: pysecrets.token_urlsafe(24))

    def providers_path(self) -> str:
        return os.path.join(self.keys_dir, "providers.toml")

    def load_providers(self) -> dict:
        """Secret service tables ([google], [github], …) kept in the encrypted
        state instead of plaintext cluster.toml. Merged over cluster.toml at
        render time (overlay wins)."""
        import tomllib
        path = self.providers_path()
        if not os.path.exists(path):
            return {}
        with open(path, "rb") as fh:
            return tomllib.load(fh)

    def drop_acme_eab(self) -> int:
        """Forget stored EAB keys; the next acme_eab() mints afresh. Only for
        a cluster whose ACME account never got registered (a live account is
        looked up by its kid: see acme_eab)."""
        n = 0
        for name in os.listdir(self.keys_dir) if os.path.isdir(self.keys_dir) else []:
            if name.startswith("acme_eab") and name.endswith(".json"):
                os.remove(os.path.join(self.keys_dir, name))
                n += 1
        return n

    def acme_eab(self, ca: str, *, project: str | None = None,
                 email: str | None = None) -> dict:
        """External Account Binding pair for the cluster's ACME CA, obtained
        ONCE and kept in state.

        gts: minted via gcloud (the google-beta
        google_public_ca_external_account_key resource returns unusable key
        material — verified 2026-09-02). SINGLE-USE: after the cluster
        registers its ACME account this pair is its identity (looked up by
        kid) — never re-mint for a live cluster.

        zerossl: issued by ZeroSSL's EAB endpoint for the contact email (no
        ZeroSSL account setup needed). Reusable — boxd registers a fresh
        account per process with it."""
        name = "acme_eab.json" if ca == "gts" else f"acme_eab_{ca}.json"
        path = os.path.join(self.keys_dir, name)
        if not os.path.exists(path):
            pair = _mint_gts_eab(project) if ca == "gts" else _mint_zerossl_eab(email)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(pair, fh, indent=1)
        with open(path) as fh:
            return json.load(fh)

    def ensure_keypairs(self) -> None:
        """ssh-keygen/openssl-backed material, needed for apply (not render)."""
        deploy = os.path.join(self.keys_dir, "boxd-deploy")
        if not os.path.exists(deploy):
            run(["ssh-keygen", "-t", "ed25519", "-f", deploy, "-N", "", "-C", "boxd-deploy"])
        host = os.path.join(self.keys_dir, "proxy_host_ed25519")
        if not os.path.exists(host):
            run(["ssh-keygen", "-t", "ed25519", "-f", host, "-N", "", "-C", "boxd-proxy-host"])
        pem = os.path.join(self.keys_dir, "wildcard.pem")
        if not os.path.exists(pem):
            run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt",
                 "ec_paramgen_curve:prime256v1", "-nodes",
                 "-keyout", os.path.join(self.keys_dir, "wildcard.key"),
                 "-out", pem, "-days", "365",
                 "-subj", "/CN=bootstrap.invalid"])

    def deploy_pubkey(self) -> str:
        path = os.path.join(self.keys_dir, "boxd-deploy.pub")
        if not os.path.exists(path):
            raise InstallError("state/keys/boxd-deploy.pub missing — run apply (or ensure_keypairs) first")
        with open(path) as fh:
            return fh.read().strip()

ZEROSSL_EAB_URL = "https://api.zerossl.com/acme/eab-credentials-email"

def _mint_gts_eab(project: str | None) -> dict:
    if not project:
        raise InstallError("GTS EAB minting needs a GCP project")
    res = run(["gcloud", "publicca", "external-account-keys", "create",
               f"--project={project}", "--format=json"], capture=True)
    d = json.loads(res.stdout)
    return {"kid": d["keyId"], "hmac": d["b64MacKey"]}

def _mint_zerossl_eab(email: str | None) -> dict:
    import urllib.error
    import urllib.parse
    import urllib.request
    if not email:
        raise InstallError("ZeroSSL EAB needs [acme].contact_email")
    req = urllib.request.Request(ZEROSSL_EAB_URL, method="POST",
                                 data=urllib.parse.urlencode({"email": email}).encode())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            d = json.load(resp)
    except (urllib.error.URLError, ValueError) as e:
        raise InstallError(f"ZeroSSL EAB request failed: {e}")
    if not (d.get("success") and d.get("eab_kid") and d.get("eab_hmac_key")):
        raise InstallError(f"ZeroSSL EAB request refused: {d.get('error', d)}")
    return {"kid": d["eab_kid"], "hmac": d["eab_hmac_key"]}

class Allocation:
    def __init__(self, cfg, ids: dict, vnis: dict, bridges: dict):
        self.cfg = cfg
        self.ids = ids
        self.vnis = vnis
        self.bridge_indexes = bridges

    def node_id(self, node_name: str, role: str) -> int:
        return self.ids[f"{node_name}/{role}"]

    def bridge_subnet(self, node_name: str) -> str:
        base = self.cfg.network["bridge_subnet_base"]
        o = base.split(".")
        second = int(o[1]) + self.bridge_indexes[node_name]
        return f"{o[0]}.{second}.0.0/16"

    def bridge_gateway(self, node_name: str) -> str:
        return self.bridge_subnet(node_name).split("/")[0].rsplit(".", 3)[0] + \
            "." + self.bridge_subnet(node_name).split(".")[1] + ".0.1"

    def peers(self) -> list[dict]:
        return [{"id": self.node_id(n.name, "control"), "addr": f"{n.internal_ip}:7000"}
                for n in self.cfg.nodes_with("control")]

    def learners(self) -> list[dict]:
        out = []
        for role in ("worker", "proxy", "dns"):
            for n in self.cfg.nodes_with(role):
                out.append({"id": self.node_id(n.name, role),
                            "addr": f"{n.internal_ip}:{RAFT_PORT[role]}"})
        out.sort(key=lambda e: e["id"])
        return out
