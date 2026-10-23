"""[oidc]: the provider's discovery document and keys, checked by `validate`
from the operator's machine before boxd-console fetches them at the first
sign-in. The checks are the console's (openidconnect's discovery), so that
"discovery OK" means the console can use them."""
import http.client
import json
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request

from .config import pem_certificates
from .util import InstallError, log

CONFIG_URL_SUFFIX = ".well-known/openid-configuration"
# boxd-console's client: 5 s to connect, 15 s for a whole request.
CONNECT_SECS = 5
REQUEST_SECS = 15
MAX_BYTES = 1 << 20
JSON_TYPES = ("application/json",)
JWKS_TYPES = ("application/json", "application/jwk-set+json")
# What openidconnect needs in a discovery document, and token_endpoint,
# without which the console can't exchange the code.
REQUIRED_URLS = ("authorization_endpoint", "token_endpoint", "jwks_uri")
REQUIRED_LISTS = ("response_types_supported", "subject_types_supported",
                  "id_token_signing_alg_values_supported")
# OpenSSL's verify codes: the chain doesn't end at a trusted certificate
# (7: a trusted one has the issuer's name but another key, such as a CA
# re-keyed under its old name), and the certificate is for another name.
_UNTRUSTED = {2, 7, 18, 19, 20, 21}
_WRONG_NAME = {62, 64}

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """boxd-console follows no redirect, so neither does the check: a 3xx
    comes back as an HTTPError."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def discovery_url(issuer: str) -> str:
    """As openidconnect builds it: one slash between the issuer and the
    suffix, unless the issuer ends in one."""
    return issuer + ("" if issuer.endswith("/") else "/") + CONFIG_URL_SUFFIX

def _opener(table: dict):
    """TLS as the console checks it: the names in the certificate's subject
    alternative names only (webpki has no fallback to the CN), and ca_cert's
    certificates on top of the public roots, each trusted as it stands, an
    intermediate CA included."""
    ctx = ssl.create_default_context()
    ctx.hostname_checks_common_name = False
    ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    # Python 3.13 turns on X509_STRICT, which wants key identifiers webpki
    # doesn't ask for: off, so that a private CA the console takes passes.
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    if certs := pem_certificates(table.get("ca_cert") or ""):
        ctx.load_verify_locations(cadata="\n".join(certs))
    return urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ctx))

def _fetch(opener, url: str) -> tuple[int, object, bytes]:
    """GET url with the console's limits: REQUEST_SECS for the whole
    answer, not for each read, so a provider that trickles bytes can't hold
    `validate`. The thread left behind on a timeout ends with the process."""
    got: dict = {}
    def run():
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with opener.open(req, timeout=CONNECT_SECS) as resp:
                got["answer"] = resp.status, resp.headers, resp.read(MAX_BYTES + 1)
        except BaseException as e:  # handed to the caller below
            got["error"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(REQUEST_SECS)
    if t.is_alive():
        raise TimeoutError(f"no complete answer within {REQUEST_SECS} s")
    if "error" in got:
        raise got["error"]
    return got["answer"]

ISSUER_HINT = "is [oidc].issuer the provider's issuer? (see README, Single sign-on)"
KEYS_HINT = "it's the discovery document's jwks_uri, where boxd-console fetches the keys"

def _get(opener, url: str, types: tuple[str, ...], ca_set: bool, hint: str) -> bytes | None:
    """The body of url as boxd-console would take it: no redirect, status
    200 only, and a Content-Type of `types` when there is one. None, with a
    warning, when this machine can't reach it. Raises InstallError where the
    console would fail every sign-in."""
    try:
        status, headers, body = _fetch(opener, url)
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            raise InstallError(
                f"[oidc] {url} answers {e.code}, a redirect to {e.headers.get('Location')!r}. "
                "boxd-console follows no redirects: set [oidc].issuer to the provider's issuer "
                "exactly") from None
        raise InstallError(f"[oidc] {url} answers HTTP {e.code}: {hint}") from None
    except urllib.error.URLError as e:
        if isinstance(e.reason, ssl.SSLCertVerificationError):
            raise InstallError(_untrusted(url, e.reason, ca_set)) from None
        return _unreachable(url, e.reason)
    except http.client.RemoteDisconnected as e:
        return _unreachable(url, e)
    except http.client.HTTPException as e:
        raise InstallError(f"[oidc] {url} doesn't answer in HTTP ({e!r})") from None
    except OSError as e:  # a timeout, or a reset while reading
        return _unreachable(url, e)
    except ValueError as e:  # e.g. a path urllib can't send as it stands
        raise InstallError(f"[oidc] {url} isn't a URL this machine can fetch ({e})") from None
    if status != 200:
        raise InstallError(f"[oidc] {url} answers HTTP {status}; boxd-console takes 200 only")
    if len(body) > MAX_BYTES:
        raise InstallError(f"[oidc] {url} answers with over {MAX_BYTES >> 20} MiB")
    ctype = headers.get("Content-Type")
    if ctype is not None and ctype.split(";", 1)[0].lower() not in types:
        raise InstallError(f"[oidc] {url} answers with Content-Type {ctype!r}; boxd-console "
                           f"takes {' or '.join(types)}")
    return body

def _untrusted(url: str, err: ssl.SSLCertVerificationError, ca_set: bool) -> str:
    if err.verify_code in _WRONG_NAME:
        return (f"[oidc] {url}: the provider's TLS certificate isn't for this host "
                f"({err.verify_message}). boxd-console reads the names in its subject "
                "alternative names only: is [oidc].issuer's host one of them?")
    if err.verify_code in _UNTRUSTED and ca_set:
        return (f"[oidc] {url}: the provider's TLS certificate isn't trusted "
                f"({err.verify_message}), and no certificate in [oidc].ca_cert issued its "
                "chain: put the CA that did in ca_cert")
    if err.verify_code in _UNTRUSTED:
        return (f"[oidc] {url}: the provider's TLS certificate isn't trusted "
                f"({err.verify_message}). A provider behind a private CA needs that CA in "
                "[oidc].ca_cert. (A proxy that intercepts TLS on this machine's network has "
                "the same effect; then run validate from elsewhere.)")
    return f"[oidc] {url}: the provider's TLS certificate isn't valid ({err.verify_message})"

def _json_object(body: bytes, url: str, what: str, hint: str) -> dict:
    try:
        doc = json.loads(body)
    except (ValueError, RecursionError):  # nesting too deep for the parser
        doc = None
    if not isinstance(doc, dict):
        raise InstallError(f"[oidc] {url} isn't {what} (not a JSON object): {hint}")
    return doc

def _http_url(v) -> bool:
    """An http(s) URL, as the console's client can fetch: no file:// or
    ftp://, which urllib would otherwise open."""
    if not isinstance(v, str) or any(c <= " " or c == "\x7f" for c in v):
        return False
    try:
        u = urllib.parse.urlsplit(v)
        u.port
    except ValueError:
        return False
    return u.scheme in ("https", "http") and bool(u.hostname)

def check_discovery(table: dict, opener=None) -> str:
    """Fetch the issuer's discovery document and its keys as boxd-console
    does, and check that the document names the issuer exactly. Returns the
    line to print; raises InstallError where the console would fail every
    sign-in. A provider this machine can't reach is a warning only: it may
    answer the cluster's network alone."""
    issuer = table["issuer"]
    ca_set = bool(pem_certificates(table.get("ca_cert") or ""))
    opener = opener or _opener(table)
    url = discovery_url(issuer)
    body = _get(opener, url, JSON_TYPES, ca_set, ISSUER_HINT)
    if body is None:
        return "[oidc] discovery not checked from here"
    doc = _json_object(body, url, "a discovery document", ISSUER_HINT)
    found = doc.get("issuer")
    if found != issuer:
        raise InstallError(
            f"[oidc] the provider's discovery document names the issuer {found!r}, but "
            f"[oidc].issuer is {issuer!r}. They must match exactly, a trailing slash "
            "included: set [oidc].issuer to the document's value")
    missing = [k for k in REQUIRED_URLS if not _http_url(doc.get(k))]
    missing += [k for k in REQUIRED_LISTS
                if not (isinstance(doc.get(k), list) and all(isinstance(x, str) for x in doc[k]))]
    if missing:
        raise InstallError(f"[oidc] the discovery document at {url} has no usable "
                           f"{', '.join(missing)}: boxd-console can't sign anyone in with it")
    jwks_uri = doc["jwks_uri"]
    body = _get(opener, jwks_uri, JWKS_TYPES, ca_set, KEYS_HINT)
    if body is None:
        return f"[oidc] discovery OK: {issuer} (its keys at {jwks_uri} not checked from here)"
    keys = _json_object(body, jwks_uri, "a key set", KEYS_HINT).get("keys")
    if not isinstance(keys, list):
        raise InstallError(f"[oidc] {jwks_uri} isn't a key set (no \"keys\" list): {KEYS_HINT}")
    if not keys:
        log(f"warning: [oidc] {jwks_uri} lists no signing keys: boxd-console can't verify "
            "an ID token until the provider publishes one")
    # boxd-console sends PKCE (S256) whatever the document says; a provider
    # that doesn't list it may still take it, so this is information only.
    pkce = ("PKCE S256 listed" if "S256" in (doc.get("code_challenge_methods_supported") or [])
            else "PKCE S256 not listed")
    return (f"[oidc] discovery OK: {issuer}\n"
            f"  issuer matches; authorization and token endpoints present; "
            f"{len(keys)} signing key(s) at {jwks_uri}; {pkce}")

def _unreachable(url: str, reason) -> None:
    log(f"warning: [oidc] couldn't fetch {url} from this machine ({reason}). Expected if "
        "the provider answers the cluster's network only; boxd-console fetches it at the "
        "first sign-in. Its nodes need outbound HTTPS to the provider.")
    return None
