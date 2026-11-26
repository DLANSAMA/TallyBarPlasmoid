"""Hardening for credentialed Google calls (no-redirect opener) and symlink-preserving
credential save. Both are security/correctness measures:

- CPython's default HTTPRedirectHandler copies the Authorization: Bearer header / refresh-token
  body onto a cross-origin redirect target; _NO_REDIRECT_OPENER refuses 3xx (raises HTTPError).
- os.replace onto a symlinked creds path would swap in a regular file and fork the credential;
  the save resolves realpath first so rotation writes through the link.
"""

import http.server
import json
import os
import stat
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import mock_open, patch

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


def test_loopback_tls_skip_needs_an_exact_loopback_host():
    assert agmod._is_loopback_https("https://127.0.0.1:4321/rpc")
    assert agmod._is_loopback_https("https://localhost:4321/rpc")
    assert not agmod._is_loopback_https("https://127.0.0.1.evil.com/rpc")
    assert not agmod._is_loopback_https("https://localhost.evil.com/rpc")
    assert not agmod._is_loopback_https("https://evil.com@127.0.0.1x.com/rpc")
    assert not agmod._is_loopback_https("http://127.0.0.1:4321/rpc")


def test_parse_language_server_reads_daemon_discovery_file():
    daemon_dir = Path.home() / ".gemini" / "antigravity" / "daemon"
    daemon_dir.mkdir(parents=True)
    (daemon_dir / "hub_45183.json").write_text(json.dumps({
        "pid": 1131564,
        "httpsPort": 34663,
        "httpPort": 45183,
        "lsVersion": "2.35.2",
        "csrfToken": "daemon-tok-1",
    }))
    args = [
        "/opt/Antigravity/resources/bin/language_server",
        "--standalone",
        "--https_server_port", "0",
        "--app_data_dir", "antigravity",
    ]
    cmd = " ".join(args)
    assert agmod._parse_language_server_cmdline(cmd, args, pid=1131564) == ("daemon-tok-1", "https")
    assert agmod._parse_language_server_cmdline(cmd, args, pid=999999) is None
    assert agmod._parse_language_server_cmdline(cmd, args) is None

    eq_args = ["/opt/Antigravity/resources/bin/language_server", "--app_data_dir=antigravity"]
    assert agmod._parse_language_server_cmdline(" ".join(eq_args), eq_args, pid=1131564) == ("daemon-tok-1", "https")


def test_daemon_discovery_prefers_newest_file_and_respects_http_only():
    daemon_dir = Path.home() / ".gemini" / "antigravity" / "daemon"
    daemon_dir.mkdir(parents=True)
    older = daemon_dir / "hub_40000.json"
    newer = daemon_dir / "hub_45183.json"
    older.write_text(json.dumps({"pid": 4242, "httpsPort": 40001, "csrfToken": "stale-tok"}))
    newer.write_text(json.dumps({"pid": 4242, "httpsPort": 0, "httpPort": 45183, "csrfToken": "fresh-tok"}))
    os.utime(older, (1000, 1000))
    os.utime(newer, (2000, 2000))

    args = ["language_server", "--app_data_dir", "antigravity"]
    assert agmod._parse_language_server_cmdline(" ".join(args), args, pid=4242) == ("fresh-tok", "http")


def test_cmdline_csrf_token_takes_precedence_over_daemon_file():
    daemon_dir = Path.home() / ".gemini" / "antigravity" / "daemon"
    daemon_dir.mkdir(parents=True)
    (daemon_dir / "hub_45183.json").write_text(json.dumps({
        "pid": 4242,
        "httpsPort": 34663,
        "csrfToken": "daemon-tok",
    }))
    args = ["language_server", "--app_data_dir", "antigravity", "--csrf_token", "cmdline-tok", "--https_server_port", "0"]
    assert agmod._parse_language_server_cmdline(" ".join(args), args, pid=4242) == ("cmdline-tok", "https")


def test_daemon_discovery_rejects_traversal_symlinks_and_other_uid(tmp_path, monkeypatch):
    # Path traversal in --app_data_dir must never escape ~/.gemini.
    evil_dir = Path.home() / "antigravity-evil" / "daemon"
    evil_dir.mkdir(parents=True)
    (evil_dir / "hub_1.json").write_text(json.dumps({"pid": 77, "httpsPort": 1234, "csrfToken": "evil"}))
    traversal = ["language_server", "--app_data_dir", "../antigravity-evil"]
    assert agmod._parse_language_server_cmdline(" ".join(traversal), traversal, pid=77) is None

    # Symlinks inside daemon/ are skipped (O_NOFOLLOW / follow_symlinks=False).
    daemon_dir = Path.home() / ".gemini" / "antigravity" / "daemon"
    daemon_dir.mkdir(parents=True)
    outside = tmp_path / "secret.json"
    outside.write_text(json.dumps({"pid": 77, "httpsPort": 1234, "csrfToken": "via-link"}))
    (daemon_dir / "hub_link.json").symlink_to(outside)
    args = ["language_server", "--app_data_dir", "antigravity"]
    assert agmod._parse_language_server_cmdline(" ".join(args), args, pid=77) is None

    # A regular file owned by another UID is rejected.
    (daemon_dir / "hub_other.json").write_text(json.dumps({"pid": 77, "httpsPort": 1234, "csrfToken": "other-uid"}))
    real_getuid = os.getuid
    monkeypatch.setattr(os, "getuid", lambda: real_getuid() + 1)
    assert agmod._parse_language_server_cmdline(" ".join(args), args, pid=77) is None


def test_scan_antigravity_processes_finds_daemon_only_language_server():
    daemon_dir = Path.home() / ".gemini" / "antigravity" / "daemon"
    daemon_dir.mkdir(parents=True)
    (daemon_dir / "hub_45183.json").write_text(json.dumps({
        "pid": 5555,
        "httpsPort": 34663,
        "httpPort": 45183,
        "csrfToken": "hub-tok",
    }))
    cmdline = "/opt/Antigravity/resources/bin/language_server\0--standalone\0--app_data_dir\0antigravity\0--https_server_port\00\0"
    real_open = open

    def scoped_open(path, *a, **kw):
        if str(path) == "/proc/5555/cmdline":
            return mock_open(read_data=cmdline)()
        return real_open(path, *a, **kw)

    with patch.dict(sys.modules, {"psutil": None}), \
            patch("os.listdir", return_value=["5555"]), \
            patch("builtins.open", side_effect=scoped_open), \
            patch.object(agmod, "_owned_by_current_user", return_value=True):
        agmod._reset_process_scan_cache()
        found = agmod.find_antigravity_processes()
    agmod._reset_process_scan_cache()
    assert found == [(5555, "hub-tok", "https")]


def test_daemon_discovery_falls_back_when_app_data_dir_flag_omitted():
    # If a future build drops --app_data_dir from cmdline, scan ~/.gemini/antigravity*/daemon/
    # while ignoring non-antigravity directories.
    ag_daemon = Path.home() / ".gemini" / "antigravity-hub" / "daemon"
    ag_daemon.mkdir(parents=True)
    (ag_daemon / "ls_9000.json").write_text(json.dumps({
        "pid": 8888,
        "httpsPort": 34663,
        "csrfToken": "omitted-flag-tok",
    }))
    other_daemon = Path.home() / ".gemini" / "other-tool" / "daemon"
    other_daemon.mkdir(parents=True)
    (other_daemon / "ls_9001.json").write_text(json.dumps({
        "pid": 8889,
        "httpsPort": 34664,
        "csrfToken": "wrong-tool-tok",
    }))
    args = ["/opt/Antigravity/resources/bin/language_server", "--standalone"]
    assert agmod._parse_language_server_cmdline(" ".join(args), args, pid=8888) == ("omitted-flag-tok", "https")
    assert agmod._parse_language_server_cmdline(" ".join(args), args, pid=8889) is None
    agmod._reset_process_scan_cache()


def test_daemon_endpoints_probed_directly_before_antigravity_ports(monkeypatch):
    # Daemon JSON ports are probed directly without invoking antigravity_ports(pid); if the
    # daemon port fails, fallback still calls antigravity_ports(pid) and skips already-tried pairs.
    daemon_dir = Path.home() / ".gemini" / "antigravity" / "daemon"
    daemon_dir.mkdir(parents=True)
    (daemon_dir / "hub_45183.json").write_text(json.dumps({
        "pid": 6060,
        "httpsPort": 34663,
        "httpPort": 45183,
        "csrfToken": "direct-tok",
    }))
    agmod._reset_process_scan_cache()
    args = ["/opt/Antigravity/resources/bin/language_server", "--app_data_dir", "antigravity"]
    assert agmod._parse_language_server_cmdline(" ".join(args), args, pid=6060) == ("direct-tok", "https")

    called_ports = []
    probed_urls = []

    def fail_if_called(pid):
        called_ports.append(pid)
        return ([35333, 34663], "")

    def fake_post(url, token, timeout):
        probed_urls.append(url)
        if url.startswith("https://127.0.0.1:34663/") and url.endswith("/GetUserStatus"):
            return 200, {"userStatus": {"cascadeModelConfigData": {"clientModelConfigs": [
                {"label": "Gemini 3.1 Pro", "quotaInfo": {"remainingFraction": 0.9}},
            ]}}}
        return 404, {}

    monkeypatch.setattr(agmod, "find_antigravity_processes", lambda: [(6060, "direct-tok", "https")])
    monkeypatch.setattr(agmod, "antigravity_ports", fail_if_called)
    monkeypatch.setattr(agmod, "post_local_json", fake_post)

    res = agmod.run_antigravity_local(2.0)
    assert res["status"] == "ok"
    assert res["source"] == "local-antigravity:34663"
    assert called_ports == []
    assert probed_urls[0].startswith("https://127.0.0.1:34663/")

    # Same direct-probe behaviour in collect_antigravity_rpc_usage.
    rpc_urls = []

    def fake_rpc(url, token, body, timeout):
        rpc_urls.append(url)
        if url.startswith("https://127.0.0.1:34663/") and url.endswith("/GetAllCascadeTrajectories"):
            return 200, {"trajectorySummaries": {}}
        return 200, {}

    monkeypatch.setattr(agmod, "_post_local_rpc", fake_rpc)
    harvest = agmod.collect_antigravity_rpc_usage(2.0)
    assert harvest.listing_complete is True
    assert called_ports == []
    assert rpc_urls[0].startswith("https://127.0.0.1:34663/")

    # When both daemon ports fail, fallback queries antigravity_ports and skips already-tried pairs.
    probed_urls.clear()

    def fallback_post(url, token, timeout):
        probed_urls.append(url)
        if url.startswith("https://127.0.0.1:35333/") and url.endswith("/GetUserStatus"):
            return 200, {"userStatus": {"cascadeModelConfigData": {"clientModelConfigs": [
                {"label": "Gemini 3.1 Pro", "quotaInfo": {"remainingFraction": 0.5}},
            ]}}}
        return 500, {}

    monkeypatch.setattr(agmod, "post_local_json", fallback_post)
    res2 = agmod.run_antigravity_local(2.0)
    assert res2["status"] == "ok"
    assert res2["source"] == "local-antigravity:35333"
    assert called_ports == [6060]
    assert probed_urls.count("https://127.0.0.1:34663/exa.language_server_pb.LanguageServerService/GetUserStatus") == 1
    agmod._reset_process_scan_cache()


def test_oauth_clients_scans_modern_binary_and_dedupes_symlinks(monkeypatch):
    # Modern Antigravity 2.23+/2.35+ installs place the binary at resources/bin/language_server,
    # and ~/.local/opt/Antigravity is often a symlink to the newest versioned directory.
    opt = Path.home() / ".local" / "opt"
    old_bin = opt / "Antigravity-2.23.0" / "resources" / "bin" / "language_server"
    new_bin = opt / "Antigravity-2.35.2" / "resources" / "bin" / "language_server"
    old_bin.parent.mkdir(parents=True)
    new_bin.parent.mkdir(parents=True)
    old_id = "111111111111-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.apps.googleusercontent.com"
    old_sec = "GOCSPX-" + "a" * 28
    new_id = "222222222222-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.apps.googleusercontent.com"
    new_sec = "GOCSPX-" + "b" * 28
    old_bin.write_bytes(f"{old_id} {old_sec}".encode("ascii"))
    new_bin.write_bytes(f"{new_id} {new_sec}".encode("ascii"))
    os.utime(old_bin, (1000, 1000))
    os.utime(new_bin, (2000, 2000))
    (opt / "Antigravity").symlink_to(opt / "Antigravity-2.35.2")

    opened = []
    real_open = open

    def tracking_open(path, *a, **kw):
        if "language_server" in str(path):
            opened.append(str(path))
        return real_open(path, *a, **kw)

    real_glob = agmod.glob.glob
    monkeypatch.setattr(agmod.glob, "glob", lambda p: [] if p.startswith("/opt/") else real_glob(p))
    monkeypatch.setattr("builtins.open", tracking_open)
    pairs = agmod.antigravity_oauth_clients({})
    assert pairs == [(new_id, new_sec)]
    # Symlink deduplicated and older archive binary skipped once newest binary yielded pairs.
    assert len(opened) == 1
    assert opened[0] == str(new_bin)

    # Lazy generator: when credentials already carry a working pair, zero binaries are opened.
    opened.clear()
    gen = agmod._iter_antigravity_oauth_clients({"client_id": old_id, "client_secret": old_sec})
    assert next(gen) == (old_id, old_sec)
    assert opened == []
