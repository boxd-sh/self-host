"""Composio auth-config sync, ported from boxd's
scripts/composio-ensure-auth-configs.py.

Ensures a Composio auth config exists for every curated toolkit
(assets/automations/curated.json):
  tier1_managed      -> use_composio_managed_auth (zero-setup OAuth)
  tier2_api_key      -> use_custom_auth + API_KEY, empty credentials (the end
                        user supplies their key on Composio's connect page)
  tier3_custom_oauth -> never created here: these need a real OAuth app
                        registered with the provider first. Create them by
                        hand in Composio and they're picked up like any
                        other existing config.

Idempotent — existing configs (one per toolkit, oldest wins, so hand-made
customs stay) are left alone. The slug->id map is cached in state as
composio_auth_configs, which render feeds into the console's
[automations].auth_configs.
"""
import json
import os
import time
import urllib.request

from .util import InstallError, log

API = "https://backend.composio.dev/api/v3"


def _call(key: str, method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{API}{path}",
        method=method,
        headers={"x-api-key": key, "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body else None,
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def sync_auth_configs(cfg, state, assets_root: str) -> None:
    auto = cfg.table("automations")
    key = auto.get("composio_api_key") if auto else None
    if not key:
        return
    curated_path = os.path.join(assets_root, "automations", "curated.json")
    if not os.path.exists(curated_path):
        log("composio: no curated.json in assets — skipping auth-config sync")
        return
    with open(curated_path) as fh:
        curated = json.load(fh)

    existing: dict[str, str] = {}
    cursor = None
    try:
        while True:
            page = _call(key, "GET", "/auth_configs?limit=100"
                         + (f"&cursor={cursor}" if cursor else ""))
            for c in page.get("items", []):
                slug = (c.get("toolkit") or {}).get("slug", "")
                existing.setdefault(slug, c["id"])
            cursor = page.get("next_cursor")
            if not cursor:
                break
    except Exception as e:
        raise InstallError(f"composio: listing auth configs failed "
                           f"(bad composio_api_key?): {e}") from e

    plans = [
        ("tier1_managed", {"type": "use_composio_managed_auth"}),
        ("tier2_api_key", {"type": "use_custom_auth", "authScheme": "API_KEY",
                           "credentials": {}}),
    ]
    created = 0
    for tier, ac in plans:
        for slug in curated.get(tier, []):
            if slug in existing:
                continue
            body = {"toolkit": {"slug": slug},
                    "auth_config": {**ac, "name": f"boxd-{slug}"}}
            try:
                resp = _call(key, "POST", "/auth_configs", body)
                existing[slug] = resp["auth_config"]["id"]
                created += 1
            except Exception as e:
                log(f"composio: could not create auth config for {slug}: {e}")
            time.sleep(0.2)

    wanted: set[str] = set()
    for t in ("tier1_managed", "tier2_api_key", "tier3_custom_oauth"):
        wanted.update(curated.get(t, []))
    cache = {slug: cid for slug, cid in sorted(existing.items())
             if slug in wanted}
    state.data["composio_auth_configs"] = cache
    state.save()
    log(f"composio: {len(cache)} auth configs synced ({created} created)")
