"""[oidc] single sign-on: the cluster.toml and providers rules, what render
emits for the console, the TOML string escapes a PEM needs, and the discovery
check `validate` runs. Run: python3 -m unittest discover -s tests"""
import _isolation  # noqa: F401  (first: the tests never read the real customer defaults)
import contextlib
import http.server
import io
import json
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import tomllib
import unittest
from unittest import mock

from boxd_installer import config, oidc, render, toml_emit
from boxd_installer.util import InstallError

from test_local_ca import HAVE_OPENSSL
from test_machine_sizes import FakeAlloc, FakeState, aws_doc, validate

ISSUER = "https://login.microsoftonline.com/a1b2c3d4-0000-4000-8000-00000000abcd/v2.0"
CONSOLE = "boxd-aws-demo-control-0"
JSON = {"Content-Type": "application/json"}

def oidc_doc(**oidc_keys) -> dict:
    doc = aws_doc()
    doc["oidc"] = {"issuer": ISSUER, "client_id": "11111111-2222-3333-4444-555555555555",
                   "display_name": "Contoso", **oidc_keys}
    return doc

def providers_errors(doc: dict) -> str:
    try:
        config.validate_providers(doc)
    except InstallError as e:
        return str(e)
    return ""

def make_ca(d: str, *, san: bool = True, intermediate: bool = False,
            name: str = "127.0.0.1") -> None:
    """ca.pem, and server.pem/server.key for `name` signed by it. With
    intermediate, int.pem (signed by ca.pem) signs the server instead. With
    san=False, the server's certificate names `name` in its CN alone."""
    def ssl_(*args):
        subprocess.run(["openssl", *args], cwd=d, check=True, capture_output=True)
    def ext(name: str, text: str) -> str:
        with open(os.path.join(d, name), "w") as fh:
            fh.write(text)
        return name
    ssl_("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
         "-keyout", "ca.key", "-out", "ca.pem", "-days", "2", "-subj", "/CN=Test IdP Root",
         "-addext", "basicConstraints=critical,CA:TRUE")
    signer = "ca"
    if intermediate:
        ssl_("req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
             "-keyout", "int.key", "-out", "int.csr", "-subj", "/CN=Test IdP Issuing")
        ssl_("x509", "-req", "-in", "int.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
             "-CAcreateserial", "-out", "int.pem", "-days", "2", "-extfile",
             ext("int.ext", "basicConstraints=critical,CA:TRUE\n"
                            "keyUsage=critical,keyCertSign,cRLSign\n"))
        signer = "int"
    ssl_("req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
         "-keyout", "server.key", "-out", "server.csr", "-subj", f"/CN={name}")
    kind = "IP" if name[0].isdigit() else "DNS"
    leaf = "basicConstraints=critical,CA:FALSE\n" + (f"subjectAltName={kind}:{name}\n" if san else "")
    ssl_("x509", "-req", "-in", "server.csr", "-CA", f"{signer}.pem", "-CAkey", f"{signer}.key",
         "-CAcreateserial", "-out", "server.pem", "-days", "2", "-extfile", ext("leaf.ext", leaf))

def read(d: str, name: str) -> str:
    with open(os.path.join(d, name)) as fh:
        return fh.read()

class ClusterToml(unittest.TestCase):
    def test_absent_or_complete_is_valid(self):
        self.assertEqual(validate(aws_doc()), "")
        self.assertEqual(validate(oidc_doc(bootstrap_admin="alice@contoso.com",
                                           link_email_domains=["contoso.com", "@Fabrikam.com"])), "")

    def test_required_keys(self):
        for key in config.OIDC_REQUIRED:
            doc = oidc_doc()
            del doc["oidc"][key]
            self.assertIn(f"[oidc].{key} required", validate(doc))
            for blank in (" ", "　"):  # both are whitespace to Rust's trim
                doc["oidc"][key] = blank
                self.assertIn(f"[oidc].{key} required", validate(doc))

    def test_secret_and_unknown_keys_are_refused(self):
        self.assertIn("[oidc].client_secret is a secret: set it with `providers edit`",
                      validate(oidc_doc(client_secret="s3cret")))
        self.assertIn("[oidc].scopes: unknown key", validate(oidc_doc(scopes=["openid"])))

    def test_issuer_as_the_console_takes_it(self):
        for ok in ("https://corp.okta.com", "https://accounts.google.com",
                   "https://sso.corp.example/realms/boxd", "https://sso.corp.example:8443/realms",
                   "http://127.0.0.1:8180/realms/boxd", "http://localhost:8180/realms/boxd",
                   "http://127.1:8180/realms/boxd", "http://[::1]:8180/realms/boxd"):
            self.assertEqual(validate(oidc_doc(issuer=ok)), "", ok)
        for bad, why in (("http://sso.corp.example/realms/boxd", "must be an https:// URL"),
                         ("http://10.0.0.1/realms/boxd", "must be an https:// URL"),
                         ("sso.corp.example", "must be an https:// URL"),
                         ("ftp://sso.corp.example", "must be an https:// URL"),
                         ("https://sso.corp.example/?tenant=1", "no query or fragment"),
                         ("https://sso.corp.example/?", "no query or fragment"),
                         ("https://sso.corp.example/#boxd", "no query or fragment"),
                         (42, "quoted string")):
            self.assertIn(why, validate(oidc_doc(issuer=bad)), bad)

    def test_issuers_the_console_refuses_or_can_never_match(self):
        # The url crate refuses the URL, so the console doesn't start; or it
        # keeps the issuer as written, so no sign-in can match it.
        for bad, why in (("https://127.0.0.1:84430/realms/boxd", "is not a URL"),
                         ("https://sso.corp.example:https/realms/boxd", "is not a URL"),
                         ("https://[::1/realms/boxd", "is not a URL"),
                         ("https://[nothost]/realms/boxd", "is not a URL"),
                         ("https://1.2.3.256/realms/boxd", "isn't an IPv4 address"),
                         ("https://idp<x>.corp.example/realms", "a character a host can't"),
                         ("https://idp%zz.corp.example/realms", "a character a host can't"),
                         ("https://sso.corp.example/realms/boxd ", "whitespace"),
                         ("https://sso corp.example/realms/boxd", "whitespace"),
                         ("https://sso.corp.example\\realms\\boxd", "a backslash"),
                         ("https://sso.corp.example/realms/\x01boxd", "a control character")):
            self.assertIn(why, validate(oidc_doc(issuer=bad)), bad)

    def test_bootstrap_admin_is_an_email(self):
        self.assertEqual(validate(oidc_doc(bootstrap_admin="  ")), "")
        self.assertEqual(validate(oidc_doc(bootstrap_admin="　alice@contoso.com ")), "")
        # U+001C is no whitespace to Rust, so the console sees a value.
        for bad in ("alice", "alice@", "@contoso.com", "a lice@contoso.com",
                    "a　lice@contoso.com", "\x1c"):
            self.assertIn("is not an email address", validate(oidc_doc(bootstrap_admin=bad)), bad)
        self.assertIn("must be a quoted string", validate(oidc_doc(bootstrap_admin=1)))

    def test_link_email_domains_are_domains(self):
        self.assertEqual(validate(oidc_doc(link_email_domains=[])), "")
        self.assertIn("must be a list", validate(oidc_doc(link_email_domains="contoso.com")))
        for bad in ("", "@", "alice@contoso.com", "contoso com", 7):
            self.assertIn("is not a domain", validate(oidc_doc(link_email_domains=[bad])), bad)

    def test_ca_cert_is_pem(self):
        self.assertEqual(validate(oidc_doc(ca_cert="")), "")
        self.assertIn("holds no PEM certificate", validate(oidc_doc(ca_cert="not a cert")))
        self.assertIn("holds no PEM certificate", validate(oidc_doc(ca_cert="\x1c")))
        # The console's PEM reader takes CERTIFICATE blocks only.
        x509 = "-----BEGIN X509 CERTIFICATE-----\nMIIB\n-----END X509 CERTIFICATE-----\n"
        self.assertIn("holds no PEM certificate", validate(oidc_doc(ca_cert=x509)))
        self.assertIn("must be PEM text", validate(oidc_doc(ca_cert=["pem"])))

    def test_ca_cert_with_a_private_key_is_refused(self):
        cert = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
        key = "-----BEGIN PRIVATE KEY-----\nMIGH\n-----END PRIVATE KEY-----\n"
        self.assertIn("holds a private key", validate(oidc_doc(ca_cert=cert + key)))

    @unittest.skipUnless(HAVE_OPENSSL, "openssl not on PATH")
    def test_ca_cert_pem_is_valid(self):
        d = tempfile.mkdtemp(prefix="oidc-ca-")
        self.addCleanup(shutil.rmtree, d)
        make_ca(d)
        pem = read(d, "ca.pem")
        self.assertEqual(validate(oidc_doc(ca_cert=pem)), "")
        # Text around the PEM, as `openssl x509 -text` prints it, is ignored,
        # as by the console, ASCII or not.
        self.assertEqual(validate(oidc_doc(ca_cert="Subject: O=Société Générale\n" + pem)), "")

class Providers(unittest.TestCase):
    def test_secret_only(self):
        self.assertEqual(providers_errors({"oidc": {"client_secret": "s3cret"}}), "")
        err = providers_errors({"oidc": {"client_secret": "s3cret", "issuer": ISSUER}})
        self.assertIn("[oidc].issuer belongs in cluster.toml's [oidc]", err)
        self.assertIn("[oidc].client_secret must be a quoted string",
                      providers_errors({"oidc": {"client_secret": 1234}}))

    def test_google_and_github_sign_in_are_named_inert_with_oidc(self):
        google = {"client_id": "g", "client_secret": "s"}
        self.assertIsNone(config.inert_sign_in({"google": google}))
        self.assertIsNone(config.inert_sign_in({"oidc": {"issuer": ISSUER}}))
        msg = config.inert_sign_in({"oidc": {"issuer": ISSUER}, "google": google,
                                    "github": {"client_id": "h", "client_secret": "t"}})
        self.assertIn("[google] and [github] sign-in is off while [oidc] is set", msg)
        # A GitHub App alone (repository access) keeps working: not named.
        self.assertIsNone(config.inert_sign_in({"oidc": {"issuer": ISSUER},
                                                "github": {"app_id": "1"}}))

class Render(unittest.TestCase):
    def rendered(self, doc: dict, overlay: dict) -> dict:
        cfg = config.Config(doc)
        cfg.apply_overlay(overlay)
        tf_out = {"nodes": {n.name: {"external_ip": "203.0.113.1"} for n in cfg.nodes}}
        return render.Renderer(cfg, FakeAlloc(), tf_out, FakeState()).deploy_configs()

    def files(self, doc: dict, overlay: dict) -> dict:
        return {path: tomllib.loads(body) for path, body in self.rendered(doc, overlay).items()}

    def test_console_gets_the_merged_table_and_nothing_else_does(self):
        pem = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
        doc = oidc_doc(bootstrap_admin="alice@contoso.com", link_email_domains=["contoso.com"],
                       ca_cert=pem)
        files = self.files(doc, {"oidc": {"client_secret": "s3cret"}})
        self.assertEqual(files[f"deploy-config/{CONSOLE}/console.toml"]["oidc"], {
            "issuer": ISSUER,
            "client_id": "11111111-2222-3333-4444-555555555555",
            "display_name": "Contoso",
            "bootstrap_admin": "alice@contoso.com",
            "link_email_domains": ["contoso.com"],
            "ca_cert": pem,
            "client_secret": "s3cret",
        })
        others = [p for p, t in files.items() if "oidc" in t and not p.endswith("/console.toml")]
        self.assertEqual(others, [])

    def test_no_oidc_no_table(self):
        files = self.files(aws_doc(), {})
        self.assertNotIn("oidc", files[f"deploy-config/{CONSOLE}/console.toml"])

    def test_missing_secret_or_cluster_keys_fail_the_render(self):
        for overlay in ({}, {"oidc": {"client_secret": "　"}}):
            with self.assertRaises(InstallError) as cm:
                self.files(oidc_doc(), overlay)
            self.assertIn("[oidc] has no client_secret", str(cm.exception))
        with self.assertRaises(InstallError) as cm:
            self.files(aws_doc(), {"oidc": {"client_secret": "s3cret"}})
        self.assertIn("To turn single sign-on off, remove [oidc] from the providers config too",
                      str(cm.exception))

    def test_apply_asks_the_same_before_terraform(self):
        self.assertIsNone(render.oidc_render_error(
            {**oidc_doc()["oidc"], "client_secret": "s3cret"}))
        self.assertIn("[oidc] has no client_secret", render.oidc_render_error(oidc_doc()["oidc"]))

    def test_braces_reach_the_console_but_no_placeholder_does(self):
        out = self.rendered(oidc_doc(display_name="Contoso {{EU}}"),
                            {"oidc": {"client_secret": "a{{b"}})
        body = out[f"deploy-config/{CONSOLE}/console.toml"]
        # boxd refuses a config file with "{{" in it (BoxdConfig::load).
        self.assertNotIn("{{", body)
        t = tomllib.loads(body)["oidc"]
        self.assertEqual((t["display_name"], t["client_secret"]), ("Contoso {{EU}}", "a{{b"))

class TomlStrings(unittest.TestCase):
    def test_control_characters_round_trip(self):
        for s in ("-----BEGIN CERTIFICATE-----\r\nMIIB\n-----END CERTIFICATE-----\n",
                  'quote " and back\\slash', "tab\tbell\x07del\x7f", "plain"):
            self.assertEqual(tomllib.loads(toml_emit.dumps({"v": s}))["v"], s)

    def test_no_value_writes_a_template_placeholder(self):
        for s in ("{{ NAME }}", "a{{{b", "{x}{y}", "{"):
            out = toml_emit.dumps({"v": s})
            self.assertNotIn("{{", out, s)
            self.assertEqual(tomllib.loads(out)["v"], s)

class _Idp(http.server.BaseHTTPRequestHandler):
    """Answers as `self.server.answer` says: (status, headers, body). A list
    body is written a chunk at a time, slowly."""
    def do_GET(self):
        status, headers, body = self.server.answer(self)
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        for chunk in body if isinstance(body, list) else [body]:
            with contextlib.suppress(OSError):
                self.wfile.write(chunk)
                self.wfile.flush()
            if isinstance(body, list):
                time.sleep(0.25)

    def log_message(self, *args):
        pass

def discovery_doc(base: str, **changes) -> dict:
    """What a provider at base publishes; a change to None drops the key."""
    doc = {"issuer": base, "authorization_endpoint": base + "/auth",
           "token_endpoint": base + "/token", "jwks_uri": base + "/certs",
           "response_types_supported": ["code"], "subject_types_supported": ["public"],
           "id_token_signing_alg_values_supported": ["RS256"], **changes}
    return {k: v for k, v in doc.items() if v is not None}

class Discovery(unittest.TestCase):
    def serve(self, answer, tls_dir: str | None = None) -> str:
        srv = http.server.HTTPServer(("127.0.0.1", 0), _Idp)
        srv.answer = answer
        scheme = "http"
        if tls_dir:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(os.path.join(tls_dir, "server.pem"),
                                os.path.join(tls_dir, "server.key"))
            srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
            scheme = "https"
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f"{scheme}://127.0.0.1:{srv.server_address[1]}/realms/boxd"

    def provider(self, doc_of=discovery_doc, keys=(200, JSON, b'{"keys": []}'),
                 tls_dir: str | None = None) -> str:
        """A provider at the returned issuer: the document doc_of(issuer) at
        its discovery URL, and `keys` at <issuer>/certs."""
        def answer(handler):
            scheme = "https" if isinstance(handler.connection, ssl.SSLSocket) else "http"
            base = f"{scheme}://127.0.0.1:{handler.server.server_address[1]}/realms/boxd"
            if handler.path.endswith("/" + oidc.CONFIG_URL_SUFFIX):
                return 200, JSON, json.dumps(doc_of(base)).encode()
            if handler.path == "/realms/boxd/certs":
                return keys
            return 404, {}, b"not found"
        return self.serve(answer, tls_dir)

    def tls_dir(self, **kw) -> str:
        d = tempfile.mkdtemp(prefix="oidc-tls-")
        self.addCleanup(shutil.rmtree, d)
        make_ca(d, **kw)
        return d

    def test_discovery_url_as_openidconnect_builds_it(self):
        for issuer, suffix in (("https://idp.example/realms/boxd", "/realms/boxd/"),
                               ("https://idp.example/", "/"),
                               ("https://idp.example//", "//")):
            self.assertEqual(oidc.discovery_url(issuer),
                             f"https://idp.example{suffix}.well-known/openid-configuration")

    def test_matching_issuer(self):
        issuer = self.provider(keys=(200, JSON, b'{"keys": [{"kid": "a"}, {"kid": "b"}]}'))
        self.assertEqual(oidc.check_discovery({"issuer": issuer}),
                         f"[oidc] discovery OK: {issuer}\n  issuer matches; authorization "
                         f"and token endpoints present; 2 signing key(s) at {issuer}/certs; "
                         "PKCE S256 not listed")
        issuer = self.provider(lambda base: discovery_doc(
            base, issuer=base + "/", code_challenge_methods_supported=["S256"]))
        out = oidc.check_discovery({"issuer": issuer + "/"})
        self.assertIn("discovery OK", out)
        self.assertIn("PKCE S256 listed", out)
        # The key set's own media type is fine too; an empty set only warns.
        issuer = self.provider(keys=(200, {"Content-Type": "application/jwk-set+json"},
                                     b'{"keys": []}'))
        with mock.patch.object(oidc, "log") as warn:
            out = oidc.check_discovery({"issuer": issuer})
        self.assertTrue(out.startswith(f"[oidc] discovery OK: {issuer}\n"))
        self.assertIn("0 signing key(s)", out)
        self.assertIn("lists no signing keys", warn.call_args.args[0])

    def test_an_issuer_differing_in_a_slash_fails(self):
        issuer = self.provider(lambda base: discovery_doc(base, issuer=base + "/"))
        with self.assertRaises(InstallError) as cm:
            oidc.check_discovery({"issuer": issuer})
        self.assertIn(f"names the issuer '{issuer}/', but [oidc].issuer is '{issuer}'",
                      str(cm.exception))

    def test_answers_the_console_refuses_fail(self):
        nested = b"[" * 100_000 + b"]" * 100_000
        for answer, why in (
                (lambda h: (404, {}, b"no"), "answers HTTP 404"),
                (lambda h: (302, {"Location": "https://elsewhere.example/"}, b""),
                 "answers 302, a redirect to 'https://elsewhere.example/'"),
                (lambda h: (203, JSON, b"{}"), "answers HTTP 203; boxd-console takes 200 only"),
                (lambda h: (200, {"Content-Type": "text/html"}, b"<html>sign in</html>"),
                 "Content-Type 'text/html'"),
                (lambda h: (200, {"Content-Type": "application/json ; charset=utf-8"}, b"{}"),
                 "Content-Type 'application/json ; charset=utf-8'"),
                (lambda h: (200, {}, b'["issuer"]'), "isn't a discovery document"),
                (lambda h: (200, JSON, nested), "isn't a discovery document")):
            issuer = self.serve(answer)
            with self.assertRaises(InstallError) as cm:
                oidc.check_discovery({"issuer": issuer})
            self.assertIn(why, str(cm.exception))

    def test_a_document_the_console_cant_use_fails(self):
        for change, key in (({"jwks_uri": None}, "jwks_uri"),
                            ({"token_endpoint": None}, "token_endpoint"),
                            ({"jwks_uri": "file:///etc/passwd"}, "jwks_uri"),
                            ({"authorization_endpoint": "/auth"}, "authorization_endpoint"),
                            ({"subject_types_supported": "public"}, "subject_types_supported")):
            issuer = self.provider(lambda base, c=change: discovery_doc(base, **c))
            with self.assertRaises(InstallError) as cm:
                oidc.check_discovery({"issuer": issuer})
            self.assertIn(f"has no usable {key}", str(cm.exception))

    def test_the_keys_are_checked_too(self):
        for keys, why in (((404, {}, b"no"), "/certs answers HTTP 404"),
                          ((200, {"Content-Type": "text/plain"}, b'{"keys": []}'),
                           "Content-Type 'text/plain'"),
                          ((200, JSON, b'{"no": "keys"}'), "isn't a key set (no \"keys\" list)")):
            issuer = self.provider(keys=keys)
            with self.assertRaises(InstallError) as cm:
                oidc.check_discovery({"issuer": issuer})
            self.assertIn(why, str(cm.exception))

    def test_unreachable_is_a_warning(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        with contextlib.redirect_stderr(io.StringIO()) as log:
            out = oidc.check_discovery({"issuer": f"http://127.0.0.1:{port}/realms/boxd"})
        self.assertEqual(out, "[oidc] discovery not checked from here")
        self.assertIn("warning: [oidc] couldn't fetch", log.getvalue())

    def test_keys_this_machine_cant_reach_are_a_warning(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            far = f"http://127.0.0.1:{s.getsockname()[1]}/certs"
        issuer = self.provider(lambda base: discovery_doc(base, jwks_uri=far))
        with contextlib.redirect_stderr(io.StringIO()) as log:
            out = oidc.check_discovery({"issuer": issuer})
        self.assertEqual(out, f"[oidc] discovery OK: {issuer} (its keys at {far} not checked "
                              "from here)")
        self.assertIn(f"couldn't fetch {far}", log.getvalue())

    def test_a_provider_that_trickles_runs_out_of_time(self):
        self.addCleanup(setattr, oidc, "REQUEST_SECS", oidc.REQUEST_SECS)
        oidc.REQUEST_SECS = 1
        issuer = self.serve(lambda h: (200, JSON, [b"{"] + [b" "] * 10 + [b"}"]))
        start = time.monotonic()
        with contextlib.redirect_stderr(io.StringIO()) as log:
            out = oidc.check_discovery({"issuer": issuer})
        self.assertLess(time.monotonic() - start, 2.5)
        self.assertEqual(out, "[oidc] discovery not checked from here")
        self.assertIn("no complete answer within 1 s", log.getvalue())

    def test_an_answer_that_isnt_http_fails(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        self.addCleanup(srv.close)
        def ssh():
            with contextlib.suppress(OSError):
                conn, _ = srv.accept()
                with conn:
                    conn.recv(4096)
                    conn.sendall(b"SSH-2.0-OpenSSH_9.6\r\n")
        threading.Thread(target=ssh, daemon=True).start()
        with self.assertRaises(InstallError) as cm:
            oidc.check_discovery({"issuer": f"http://127.0.0.1:{srv.getsockname()[1]}/realms"})
        self.assertIn("doesn't answer in HTTP", str(cm.exception))

    @unittest.skipUnless(HAVE_OPENSSL, "openssl not on PATH")
    def test_a_private_ca_needs_ca_cert(self):
        d = self.tls_dir()
        issuer = self.provider(tls_dir=d)
        with self.assertRaises(InstallError) as cm:
            oidc.check_discovery({"issuer": issuer})
        self.assertIn("isn't trusted", str(cm.exception))
        self.assertIn("needs that CA in [oidc].ca_cert", str(cm.exception))
        self.assertIn("discovery OK", oidc.check_discovery({"issuer": issuer,
                                                            "ca_cert": read(d, "ca.pem")}))
        # Non-ASCII text around the PEM is ignored, as by the console.
        self.assertIn("discovery OK", oidc.check_discovery(
            {"issuer": issuer, "ca_cert": "Subject: O=Société\n" + read(d, "ca.pem")}))

    @unittest.skipUnless(HAVE_OPENSSL, "openssl not on PATH")
    def test_an_intermediate_ca_is_enough(self):
        # webpki trusts a certificate in ca_cert as it stands; so does the check.
        d = self.tls_dir(intermediate=True)
        issuer = self.provider(tls_dir=d)
        self.assertIn("discovery OK", oidc.check_discovery({"issuer": issuer,
                                                            "ca_cert": read(d, "int.pem")}))

    @unittest.skipUnless(HAVE_OPENSSL, "openssl not on PATH")
    def test_another_ca_in_ca_cert_says_so(self):
        d, other = self.tls_dir(), self.tls_dir()
        issuer = self.provider(tls_dir=d)
        with self.assertRaises(InstallError) as cm:
            oidc.check_discovery({"issuer": issuer, "ca_cert": read(other, "ca.pem")})
        self.assertIn("no certificate in [oidc].ca_cert issued its chain", str(cm.exception))

    @unittest.skipUnless(HAVE_OPENSSL, "openssl not on PATH")
    def test_a_name_in_the_cn_alone_fails(self):
        # webpki reads the subject alternative names only; OpenSSL would fall
        # back to the CN for a host name (never for an IP address).
        d = self.tls_dir(san=False, name="localhost")
        issuer = self.provider(tls_dir=d).replace("127.0.0.1", "localhost")
        with self.assertRaises(InstallError) as cm:
            oidc.check_discovery({"issuer": issuer, "ca_cert": read(d, "ca.pem")})
        self.assertIn("isn't for this host", str(cm.exception))

if __name__ == "__main__":
    unittest.main()
