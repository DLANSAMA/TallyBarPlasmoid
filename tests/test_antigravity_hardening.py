"""Hardening for credentialed Google calls (no-redirect opener) and symlink-preserving
credential save. Both are security/correctness measures:

- CPython's default HTTPRedirectHandler copies the Authorization: Bearer header / refresh-token
  body onto a cross-origin redirect target; _NO_REDIRECT_OPENER refuses 3xx (raises HTTPError).
- os.replace onto a symlinked creds path would swap in a regular file and fork the credential;
  the save resolves realpath first so rotation writes through the link.
"""

import http.server
import json
import stat
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

CODE_DIR = Path(__file__).parent.parent / "io.github.dlansama.tallybar" / "contents" / "code"
sys.path.insert(0, str(CODE_DIR))

import providers.antigravity as agmod  # noqa: E402


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
        else:
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *args):  # silence
        pass


def _serve():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_no_redirect_opener_raises_on_3xx_for_credentialed_host(monkeypatch):
    # Treat the loopback test host as a credentialed Google host so the redirect gate fires; the
    # 302 must surface as an HTTPError (credential not leaked to the Location) instead of being
    # followed.
    server = _serve()
    try:
        host, port = server.server_address
        monkeypatch.setattr(agmod, "_CREDENTIALED_GOOGLE_HOSTS", (host,))
        url = f"http://{host}:{port}/redirect"
        try:
            agmod._NO_REDIRECT_OPENER.open(urllib.request.Request(url), timeout=4.0)
            assert False, "expected HTTPError, redirect was followed"
        except urllib.error.HTTPError as err:
            assert err.code == 302
    finally:
        server.shutdown()


def test_redirect_gate_blocks_channel_prefixed_cloudcode_host():
    # The live Cloud Code host on this machine is channel-prefixed (daily-/autopush-/staging-):
    # the char before "cloudcode" is '-', not '.', so a dot-suffix gate would MISS it and follow
    # a 3xx (leaking the Bearer/refresh-token). Unit-test the handler's decision directly against
    # a constructed Request, with NO monkeypatch of the host list.
    handler = agmod._NoRedirectHandler()
    for host in (
        "daily-cloudcode-pa.googleapis.com",
        "autopush-cloudcode-pa.googleapis.com",
        "staging-cloudcode-pa.googleapis.com",
        "cloudcode-pa.googleapis.com",
        "oauth2.googleapis.com",
    ):
        req = urllib.request.Request(f"https://{host}/v1:generate")
        assert handler.redirect_request(
            req, None, 302, "Found", {}, "https://evil.example/steal") is None, host
    # A non-credentialed host still follows the redirect (returns a Request, not None).
    benign = urllib.request.Request("https://raw.githubusercontent.com/a/b")
    followed = handler.redirect_request(
        benign, None, 302, "Found", {}, "https://raw.githubusercontent.com/a/c")
    assert followed is not None


def test_is_credentialed_google_host_matrix():
    ok = agmod._is_credentialed_google_host
    assert ok("daily-cloudcode-pa.googleapis.com")
    assert ok("cloudcode-pa.googleapis.com")
    assert ok("oauth2.googleapis.com")
    assert ok("us-central1.oauth2.googleapis.com")  # dot-suffix
    assert not ok("cloudcode-pa.googleapis.com.evil.example")  # suffix must be at the end
    assert not ok("googleapis.com")
    assert not ok("raw.githubusercontent.com")
    assert not ok("")


def test_no_redirect_opener_allows_200():
    server = _serve()
    try:
        host, port = server.server_address
        url = f"http://{host}:{port}/ok"
        with agmod._NO_REDIRECT_OPENER.open(urllib.request.Request(url), timeout=4.0) as resp:
            assert resp.status == 200
            assert json.loads(resp.read().decode("utf-8")) == {"ok": True}
    finally:
        server.shutdown()


def test_no_redirect_opener_follows_redirect_for_other_hosts():
    # A non-credentialed host (e.g. the pricing catalog / local language server) must keep normal
    # redirect following through the same installed opener.
    server = _serve()
    try:
        host, port = server.server_address
        url = f"http://{host}:{port}/redirect"
        with agmod._NO_REDIRECT_OPENER.open(urllib.request.Request(url), timeout=4.0) as resp:
            assert resp.status == 200
            assert json.loads(resp.read().decode("utf-8")) == {"ok": True}
    finally:
        server.shutdown()


def test_save_writes_through_symlink(tmp_path):
    target = tmp_path / "real_creds.json"
    target.write_text(json.dumps({"access_token": "old"}))
    link = tmp_path / "oauth_creds.json"
    link.symlink_to(target)

    agmod.save_antigravity_credentials(link, {"not_a_token": True}, {"access_token": "new"})

    # The link is preserved (not replaced with a regular file), the real target got the update,
    # and the target file is exactly 0600.
    assert link.is_symlink()
    data = json.loads(target.read_text())
    assert data["access_token"] == "new"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
