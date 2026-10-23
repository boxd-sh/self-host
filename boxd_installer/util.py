import subprocess
import sys

class InstallError(Exception):
    pass

def log(msg: str) -> None:
    print(f"==> {msg}", file=sys.stderr)

def run(cmd: list[str], *, cwd: str | None = None, capture: bool = False,
        check: bool = True, env: dict | None = None,
        quiet: bool = False, input: str | None = None) -> subprocess.CompletedProcess:
    """`input` goes to the command's stdin and is never logged."""
    if not quiet:
        log(" ".join(cmd) if len(" ".join(cmd)) < 120 else cmd[0] + " …")
    try:
        return subprocess.run(
            cmd, cwd=cwd, check=check, env=env,
            capture_output=capture, text=True, input=input,
        )
    except FileNotFoundError:
        raise InstallError(f"required tool not found: {cmd[0]}")
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or "").strip()[-2000:] if capture else ""
        raise InstallError(f"command failed ({e.returncode}): {' '.join(cmd)}\n{detail}")
