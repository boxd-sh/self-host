"""Telemetry: [telemetry] enabled in cluster.toml.

Every boxd cluster sends one usage report an hour to boxd: counts only, no
personal data (docs/REFERENCE.md "Telemetry"). A cluster on the free tier always sends;
with a license key that includes telemetry opt-out, `enabled = false` turns
it off.

`apply` touches the cluster's setting only when it is set explicitly: in
cluster.toml, or by the customer defaults file below. Then it runs
`boxd-ctl telemetry on` or `off`, after it installs the key (`off` needs a
key with telemetry opt-out). When it is unset, `apply` runs neither and the
cluster keeps what it has: on for a new cluster, and off for a cluster that
was turned off before.

A copy of the installer that boxd made for one customer may hold a
customer_defaults.toml next to this file, with [telemetry] enabled = false;
it is used when cluster.toml sets none. Without that file there is no
default. An explicit cluster.toml value wins.
"""
import os
import tomllib

CUSTOMER_DEFAULTS = os.path.join(os.path.dirname(__file__), "customer_defaults.toml")

CLUSTER_TOML = "cluster.toml"
CUSTOMER_DEFAULT = "customer default"

VALIDATE_LINE = ("This cluster sends an hourly usage report to boxd (no personal data). "
                 "See it with `boxd-ctl telemetry show`.")


def customer_default(path: str | None = None) -> bool | None:
    """[telemetry] enabled from the customer defaults file (CUSTOMER_DEFAULTS,
    read at the call so a test can patch it), None without one."""
    path = path or CUSTOMER_DEFAULTS
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as fh:
        v = tomllib.load(fh).get("telemetry", {}).get("enabled")
    return v if isinstance(v, bool) else None


def setting(cfg, defaults: str | None = None) -> tuple[bool | None, str | None]:
    """The explicit setting and where it comes from: (value, CLUSTER_TOML),
    (value, CUSTOMER_DEFAULT), or (None, None) when nothing sets it."""
    t = cfg.doc.get("telemetry", {})
    if "enabled" in t:
        return t["enabled"], CLUSTER_TOML
    d = customer_default(defaults)
    if d is not None:
        return d, CUSTOMER_DEFAULT
    return None, None


def validate_line(cfg, defaults: str | None = None) -> str:
    value, source = setting(cfg, defaults)
    if value is False:
        where = "[telemetry] enabled = false" if source == CLUSTER_TOML else \
            "this installer's default, [telemetry] enabled = false"
        return (f"Telemetry: apply turns it off ({where}); that needs a license key that "
                "includes telemetry opt-out.")
    if value is None:
        return (VALIDATE_LINE + " apply leaves the cluster's setting as it is "
                "([telemetry] enabled is not set).")
    return VALIDATE_LINE


def warnings(cfg, defaults: str | None = None) -> list[str]:
    """`validate`'s warnings: the customer default turns telemetry off, but
    there is no license key to allow it (apply warns and goes on)."""
    value, source = setting(cfg, defaults)
    lic = cfg.doc.get("license")
    if value is False and source == CUSTOMER_DEFAULT and \
            not (isinstance(lic, dict) and lic.get("key_file")):
        return ["this installer's default turns telemetry off, but there is no [license] "
                "key_file: control refuses `off` without a key that includes telemetry "
                "opt-out, so the cluster keeps sending. Add your license key."]
    return []


def control_table(cfg, defaults: str | None = None) -> dict:
    """control's [telemetry]: cloud and region from the provider config, and
    `default_enabled = false` when the setting is off. Rendered before the
    converge, so control never sends before `apply` runs `telemetry off`
    (a current customer's first upgrade). Like the row, it turns telemetry
    off only with a key that includes telemetry opt-out."""
    table = {"cloud": cfg.cloud_name, "region": str(cfg.cloud.get("region", ""))}
    if setting(cfg, defaults)[0] is False:
        table["default_enabled"] = False
    return table
