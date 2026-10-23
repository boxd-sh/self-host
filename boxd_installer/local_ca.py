"""Local CA TLS mode: validate the operator's intermediate CA and keep it in
state's keys/ (`providers set-tls-ca`).

boxd-control signs every cert itself with this pair when [acme].ca = "local"
(see render.control_doc). Everything here shells out to openssl — the same
tool the operator used to produce the files — and checks up front what
boxd-control would otherwise only refuse at runtime (where it logs and keeps
retrying rather than failing the apply): certificates only in the cert file,
a CA first, a key that belongs to it, PKCS#8, no passphrase.
"""
import os
import re
import shutil
import subprocess

from .util import InstallError

CERT_FILE = "local-ca.pem"
KEY_FILE = "local-ca.key"
# Where ansible puts them on control nodes (root:boxd-ca 0640; only the
# boxd-control unit is in that group).
REMOTE_DIR = "/etc/boxd/ca"

_PEM_BLOCK = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----.*?-----END \1-----", re.S)

def stored(keys_dir: str) -> bool:
    return all(os.path.exists(os.path.join(keys_dir, f)) for f in (CERT_FILE, KEY_FILE))

def _openssl(*args: str, stdin: str | None = None, what: str = "") -> str:
    if not shutil.which("openssl"):
        raise InstallError("openssl not found on PATH (needed to check the CA files)")
    r = subprocess.run(["openssl", *args], input=stdin.encode() if stdin else None,
                       capture_output=True)
    if r.returncode != 0:
        err = r.stderr.decode(errors="replace").strip()
        raise InstallError(f"{what or 'openssl ' + args[0]} failed: {err}")
    return r.stdout.decode()

def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError as e:
        raise InstallError(f"{path}: {e.strerror}") from None

def _check_cert_file(cert_path: str) -> str:
    """The certificate chain, normalised to just its PEM blocks (no
    `subject=`/Bag Attributes text — boxd appends the file verbatim to every
    leaf it serves)."""
    blocks = [(m.group(1), m.group(0)) for m in _PEM_BLOCK.finditer(_read(cert_path))]
    if not blocks:
        raise InstallError(f"{cert_path}: no PEM certificate found (DER? convert with "
                           f"`openssl x509 -inform der -in {cert_path} -out ca.pem`)")
    others = sorted({label for label, _ in blocks if label != "CERTIFICATE"})
    if others:
        raise InstallError(f"{cert_path}: may only contain certificates (the intermediate "
                           f"first, then optionally its root), found {', '.join(others)}")
    chain = "\n".join(b for _, b in blocks) + "\n"
    # x509 reads the first certificate: the intermediate. -text rather than
    # -ext, which LibreSSL (macOS) lacks.
    info = _openssl("x509", "-noout", "-text", stdin=chain, what=f"reading {cert_path}")
    if not re.search(r"CA:TRUE", info):
        raise InstallError(f"{cert_path}: the first certificate is not a CA "
                           "(basicConstraints CA:TRUE missing) — it must be the intermediate")
    r = subprocess.run(["openssl", "x509", "-noout", "-checkend", "0"],
                       input=chain.encode(), capture_output=True)
    if r.returncode != 0:
        raise InstallError(f"{cert_path}: the CA certificate has expired")
    return chain

def _pkcs8(key_path: str) -> str:
    """The key as unencrypted PKCS#8 PEM, whatever form it came in (PKCS#1
    RSA, SEC1 EC, PKCS#8). An empty passphrase makes openssl fail on an
    encrypted key instead of prompting."""
    text = _read(key_path)
    if "ENCRYPTED" in text:
        raise InstallError(f"{key_path}: the key is encrypted — boxd-control needs it "
                           f"unencrypted (openssl pkey -in {key_path} -out key.pem)")
    if "PRIVATE KEY-----" not in text:
        raise InstallError(f"{key_path}: no PEM private key found")
    try:
        return _openssl("pkey", "-in", key_path, "-passin", "pass:", what=f"reading {key_path}")
    except InstallError as e:
        raise InstallError(f"{key_path}: not a usable private key ({e})") from None

def _check_algorithm(key: str, key_path: str) -> None:
    """What boxd-control can sign with: ECDSA P-256/P-384, RSA (ring wants
    >= 2048 bits) or Ed25519. Parsed leniently from `pkey -text`, whose
    layout differs between OpenSSL and LibreSSL; anything unrecognised is
    left for boxd-control to judge."""
    info = _openssl("pkey", "-noout", "-text", stdin=key)
    curve = re.search(r"ASN1 OID:\s*(\S+)", info)
    if curve and curve.group(1) not in ("prime256v1", "secp384r1"):
        raise InstallError(f"{key_path}: EC curve {curve.group(1)} is not supported by "
                           "boxd-control (use P-256 or P-384, RSA or Ed25519)")
    rsa = re.search(r"Private-Key: \((\d+) bit", info)
    if rsa and "modulus" in info.lower() and int(rsa.group(1)) < 2048:
        raise InstallError(f"{key_path}: RSA key of {rsa.group(1)} bits — boxd-control "
                           "needs at least 2048")

def validate(cert_path: str, key_path: str) -> tuple[str, str]:
    """(cert chain PEM, PKCS#8 key PEM), or InstallError naming the problem."""
    chain = _check_cert_file(cert_path)
    key = _pkcs8(key_path)
    _check_algorithm(key, key_path)
    key_pub = _openssl("pkey", "-pubout", stdin=key)
    cert_pub = _openssl("x509", "-noout", "-pubkey", stdin=chain)
    if key_pub.strip() != cert_pub.strip():
        raise InstallError(f"{key_path} is not the private key of the CA certificate "
                           f"(the first one) in {cert_path}")
    return chain, key

def store(cert_path: str, key_path: str, keys_dir: str) -> None:
    write(keys_dir, *validate(cert_path, key_path))

def write(keys_dir: str, chain: str, key: str) -> None:
    """Into state's keys/, 0600 (ansible ships them from there)."""
    for name, content in ((CERT_FILE, chain), (KEY_FILE, key)):
        dest = os.path.join(keys_dir, name)
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
        os.chmod(dest, 0o600)
