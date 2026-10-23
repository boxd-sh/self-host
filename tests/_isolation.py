"""Imported first by every test module: the tests never read this tree's
boxd_installer/customer_defaults.toml.

A customer tree (made by `release/customer.py sync`) has that file, and the
installer reads it as the default of [telemetry] enabled (telemetry.py). A
test that read it would pass in this repo and fail in a customer's. So this
points telemetry.CUSTOMER_DEFAULTS at a path that does not exist; an audit
hook fails any open of the real path, and os.stat/os.lstat (behind
os.path.isfile and exists) fail for it too, which `test_telemetry.Isolation`
checks. A test that wants a defaults file patches the path to its own.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from boxd_installer import telemetry  # noqa: E402

REAL = os.path.abspath(telemetry.CUSTOMER_DEFAULTS)
telemetry.CUSTOMER_DEFAULTS = os.path.join(tempfile.gettempdir(),
                                           f"boxd-test-no-customer-defaults-{os.getpid()}.toml")


def _no_real_defaults(event: str, args: tuple) -> None:
    if event == "open" and args and isinstance(args[0], str) and \
            os.path.abspath(args[0]) == REAL:
        raise AssertionError(f"a test read the real {REAL}; patch "
                             "telemetry.CUSTOMER_DEFAULTS instead")


sys.addaudithook(_no_real_defaults)

# The audit hook sees open() only. telemetry.customer_default first asks
# os.path.isfile, which here (no real file in this repo) answers False with
# no open, so a test that bypassed the repoint would pass here and fail in a
# customer tree. os.path.isfile/exists/getsize and friends all go through
# os.stat, so a stat of the real path fails too.
_stat, _lstat = os.stat, os.lstat


def _guard(real_fn):
    def wrapped(path, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)) and \
                os.path.abspath(os.fspath(path)) == REAL:
            raise AssertionError(f"a test looked at the real {REAL}; patch "
                                 "telemetry.CUSTOMER_DEFAULTS instead")
        return real_fn(path, *args, **kwargs)
    return wrapped


os.stat = _guard(_stat)
os.lstat = _guard(_lstat)
# shutil and pathlib ask `os.stat in os.supports_follow_symlinks` (and the
# dir_fd / fd sets) before they pass those arguments: the wrappers take them
# exactly where the originals do.
for _supports in (os.supports_follow_symlinks, os.supports_dir_fd, os.supports_fd,
                  os.supports_effective_ids):
    for _orig, _new in ((_stat, os.stat), (_lstat, os.lstat)):
        if _orig in _supports:
            _supports.add(_new)
