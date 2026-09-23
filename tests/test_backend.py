import datetime as dt
from pathlib import Path
from unittest.mock import patch, mock_open, MagicMock

import pytest

# Adjust sys.path to find backend modules
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "io.github.dlansama.tallybar" / "contents" / "code"))

import backend
import http_helpers
import providers
import cookies

@pytest.mark.asyncio
async def test_run_threaded_provider_success():
    def dummy_func(x):
        return {"status": "ok", "value": x}
    
    res = await http_helpers.run_threaded_provider(
        dummy_func,
        42,
        timeout=1.0,
        fallback={"status": "fallback"}
    )
    assert res == {"status": "ok", "value": 42}

@pytest.mark.asyncio
async def test_run_threaded_provider_exception():
    def dummy_func():
        raise ValueError("Oops")
    
    res = await http_helpers.run_threaded_provider(
        dummy_func,
        timeout=1.0,
        fallback={"label": "Test", "status": "fallback"}
    )
    assert res["status"] == "api-error"
    assert "Oops" in res["message"]


@pytest.mark.asyncio
async def test_run_threaded_provider_timeout_orphans_a_daemon_thread():
    """A provider whose blocking work outlives the timeout returns status='timeout', AND the
    orphaned worker is a DAEMON thread, so it can never gate the one-shot backend's (or
    pytest's) exit — the teardown-hang an external audit flagged. Reverting run_threaded_provider
    to a non-daemon asyncio.to_thread would fail the daemon assertion below."""
    import threading

    started = threading.Event()
    release = threading.Event()

    def slow():
        started.set()
        release.wait(5)          # outlives the 0.05s timeout; released at the end of the test
        return {"status": "ok"}

    try:
        res = await http_helpers.run_threaded_provider(slow, timeout=0.05, fallback={"label": "X"})
        assert res["status"] == "timeout"
        assert started.wait(1.0)     # the worker really started
        workers = [t for t in threading.enumerate() if t.name == "tallybar-worker" and t.is_alive()]
        assert workers, "the orphaned worker should still be running (tallybar-worker)"
        assert all(t.daemon for t in workers), "orphaned worker MUST be a daemon so it can't gate exit"
    finally:
        release.set()            # let the orphan finish instead of sleeping the full 5s

def test_sqlite_copy(tmp_path):
    src_path = tmp_path / "dummy.sqlite"
    src_path.write_text("dummy db")

    try:
        # sqlite_copy is a context manager: it yields a secure 0600 copy in a
        # 0700 temp dir and tears the whole dir down on exit.
        with cookies.sqlite_copy(src_path) as tmp_path:
            assert tmp_path is not None
            assert tmp_path.exists()
            assert tmp_path.read_text() == "dummy db"

            # Verify permissions
            stat = tmp_path.stat()
            assert stat.st_mode & 0o777 == 0o600

            copied = tmp_path
            tmp_dir = tmp_path.parent

        # After the context exits the copy and its temp dir are gone.
        assert not copied.exists()
        assert not tmp_dir.exists()
    finally:
        if src_path.exists():
            src_path.unlink()

@pytest.mark.asyncio
async def test_find_antigravity_process_local():
    # Force the stdlib /proc fallback (the path that runs in the stdlib-only
    # Plasma runtime) by making the optional psutil import fail.
    cmdline = "language_server\0--app_data_dir=antigravity\0--csrf_token\0mytoken\0--https_server_port\0"
    real_open = open

    def scoped_open(path, *args, **kwargs):
        # Only mock the /proc/<pid>/cmdline reads — fall through to the real open
        # for everything else, so this patch can't silently break unrelated file
        # I/O during the test (the bug a bare patch("builtins.open") invites).
        if str(path).startswith("/proc/"):
            return mock_open(read_data=cmdline)()
        return real_open(path, *args, **kwargs)

    with patch.dict(sys.modules, {"psutil": None}), \
            patch("os.listdir", return_value=["1234"]), \
            patch("builtins.open", side_effect=scoped_open), \
            patch.object(providers.antigravity, "_owned_by_current_user", return_value=True):
        # find_antigravity_processes memoizes its scan for the life of the process; drop
        # that memo so this test observes a fresh /proc scan regardless of test ordering.
        providers.antigravity._reset_process_scan_cache()
        res = providers.antigravity.find_antigravity_process()
        assert res == (1234, "mytoken", "https")


def test_find_antigravity_processes_scans_once_per_process():
    # The backend is one-shot; find_antigravity_processes must scan /proc at most once
    # per process (it's called for both the account-status and RPC-harvest paths).
    agmod = providers.antigravity
    fake = [(42, "tok", "https")]
    calls = []

    def fake_scan():
        calls.append(1)
        return fake

    agmod._reset_process_scan_cache()
    with patch.object(agmod, "_scan_antigravity_processes", side_effect=fake_scan):
        first = agmod.find_antigravity_processes()
        second = agmod.find_antigravity_processes()
        assert first == fake and second == fake
        assert len(calls) == 1          # scanned once, second call served from memo
        agmod._reset_process_scan_cache()
        agmod.find_antigravity_processes()
        assert len(calls) == 2          # reset -> re-scan
    agmod._reset_process_scan_cache()   # don't leak the memo into other tests

@pytest.mark.asyncio
async def test_taskgroup_timeout():
    # build_snapshot must surface a provider that timed out as status="timeout" (and not crash
    # the TaskGroup). The real run_threaded_provider timeout path + daemon-orphan guarantee is
    # covered in isolation by test_run_threaded_provider_timeout_orphans_a_daemon_thread; here
    # we mock it to return the timeout fallback directly, so the test is deterministic and
    # leaves NO orphaned worker thread. Codex RPC + cost scan are mocked: no subprocess/disk I/O.
    args = MagicMock()
    args.no_network = False
    args.timeout = 0.05

    async def timed_out(func, *a, **k):
        if func.__name__.startswith("run_antigravity"):
            fb = dict(k.get("fallback", {"label": "Antigravity"}))
            fb.update(status="timeout", message="Antigravity telemetry timed out")
            return fb
        return {"status": "ok", "limits": []}

    async def benign_rpc(*a, **k):
        return {"label": "Codex", "status": "not-running", "limits": []}

    with patch("backend.load_config", return_value={}), \
         patch("backend.collect_browser_sessions", return_value=([], {})), \
         patch("backend.run_threaded_provider", side_effect=timed_out), \
         patch("backend.run_codex_rpc", side_effect=benign_rpc), \
         patch("backend.compute_local_cost_summaries", side_effect=lambda deadline=None: {}):
        res = await backend.build_snapshot(args)
        assert res["ok"] is True
        assert res["providers"]["antigravity"]["status"] == "timeout"

@pytest.mark.asyncio
async def test_build_snapshot_resilience_on_timeout():
    """A timed-out provider transitions to status='timeout' in the snapshot. Deterministic
    mock of the timeout fallback (the real timeout path + daemon-orphan guarantee is tested
    in isolation above), so no real sleeping thread is left behind."""
    args = MagicMock()
    args.no_network = False
    args.timeout = 0.05

    async def side_effect(func, *a, **k):
        if func.__name__ == "run_gemini_web":
            fb = dict(k.get("fallback", {"label": "Gemini"}))
            fb.update(status="timeout", message="Gemini telemetry timed out")
            return fb
        return {"status": "ok", "limits": []}

    async def benign_rpc(*a, **k):
        return {"label": "Codex", "status": "not-running", "limits": []}

    with patch("backend.load_config", return_value={}), \
         patch("backend.load_snapshot", return_value={}), \
         patch("backend.collect_browser_sessions", return_value=([], {})), \
         patch("backend.run_threaded_provider", side_effect=side_effect), \
         patch("backend.run_codex_rpc", side_effect=benign_rpc), \
         patch("backend.compute_local_cost_summaries", side_effect=lambda deadline=None: {}):
        res = await backend.build_snapshot(args)
        assert res["ok"] is True
        # The timed-out Gemini provider must have status "timeout" instead of "idle"
        # (no cached snapshot, so the transient carry-forward is a no-op here).
        assert res["providers"]["gemini"]["status"] == "timeout"
        assert "timed out" in res["providers"]["gemini"]["message"]


def test_read_firefox_cookies_quoting(tmp_path):
    import urllib.parse
    special_path = tmp_path / "path with spaces and # hashes" / "cookies.sqlite"
    
    with patch("cookies.sqlite_copy") as mock_sqlite_copy, \
         patch("sqlite3.connect") as mock_connect:
        
        mock_sqlite_copy.return_value.__enter__.return_value = special_path
        mock_sqlite_copy.return_value.__exit__.return_value = None
        
        mock_con = MagicMock()
        mock_con.execute.return_value = []
        mock_connect.return_value = mock_con
        
        cookies.read_firefox_cookies(Path("/dummy/firefox/profile/cookies.sqlite"), ())
        
        expected_uri = f"file:{urllib.parse.quote(str(special_path))}?mode=ro"
        mock_connect.assert_any_call(expected_uri, uri=True, timeout=1.0)


def test_post_local_json_ssl_relaxed():
    import ssl
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"status": "ok"}'
        mock_response.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_response

        status, data = providers.antigravity.post_local_json(
            "https://localhost:12345/api",
            "token123",
            timeout=1.0
        )
        assert status == 200
        assert data == {"status": "ok"}
        
        args, kwargs = mock_urlopen.call_args
        context = kwargs["context"]
        assert context.check_hostname is False
        assert context.verify_mode == ssl.CERT_NONE

        mock_urlopen.reset_mock()
        status, data = providers.antigravity.post_local_json(
            "https://example.com/api",
            "token123",
            timeout=1.0
        )
        args, kwargs = mock_urlopen.call_args
        context = kwargs["context"]
        assert context.check_hostname is True
        assert context.verify_mode == ssl.CERT_REQUIRED


# --- Orchestrator provider-selection logic (CLAUDE.md "provider quirks") ---

def test_choose_antigravity_result_prefers_ok_local():
    local = {"status": "ok", "label": "Antigravity", "limits": [{"percent": 10}]}
    remote = {"status": "ok", "limits": [{"percent": 99}]}
    assert providers.antigravity.choose_antigravity_result(local, remote) is local


def test_choose_antigravity_result_remote_none_returns_local():
    local = {"status": "api-error", "limits": []}
    assert providers.antigravity.choose_antigravity_result(local, None) is local


def test_choose_antigravity_result_falls_back_to_ok_remote():
    local = {"status": "not-running", "limits": []}
    remote = {"status": "ok", "limits": [{"percent": 42}]}
    assert providers.antigravity.choose_antigravity_result(local, remote) is remote


def test_choose_antigravity_result_merges_when_both_not_ok():
    local = {"status": "not-running", "message": "LS down", "limits": [{"percent": 1}]}
    remote = {"status": "api-error", "message": "cloud err", "limits": [{"percent": 2}]}
    out = providers.antigravity.choose_antigravity_result(local, remote)
    assert out["status"] == "api-error"          # remote preferred (not missing-oauth)
    assert out["limits"] == []                   # merged result clears limits
    assert out["source"] == "antigravity-only"
    assert "LS down" in out["message"] and "cloud err" in out["message"]


def test_choose_antigravity_result_missing_oauth_prefers_local():
    # remote missing-oauth + local not 'not-running' -> keep local's status/message
    local = {"status": "wallet-locked", "message": "wallet", "limits": []}
    remote = {"status": "missing-oauth", "message": "no oauth", "limits": []}
    out = providers.antigravity.choose_antigravity_result(local, remote)
    assert out["status"] == "wallet-locked"


def test_choose_antigravity_result_prefers_quota_summary_over_local():
    # The cloud retrieveUserQuotaSummary (5h+weekly, matches agy /usage) wins even when the local
    # LS is OK — local GetUserStatus only has a single-window view. Local tier/credits are merged in.
    local = {"status": "ok", "tier": "Google AI Ultra",
             "creditBalance": {"source": "google-one", "remaining": 5},
             "limits": [{"label": "Gemini", "percent": 20}]}
    remote = {"status": "ok", "limitsSource": "quota-summary", "tier": "Antigravity",
              "limits": [{"label": "Gemini", "sublabel": "5-hour", "percent": 18.7},
                         {"label": "Claude · GPT", "sublabel": "5-hour", "percent": 93.35}]}
    out = providers.antigravity.choose_antigravity_result(local, remote)
    assert out["limitsSource"] == "quota-summary"
    assert len(out["limits"]) == 2                       # the windowed cloud lanes, not local's
    assert out["tier"] == "Google AI Ultra"              # authoritative LS tier wins over "Antigravity"
    assert out["creditBalance"] == {"source": "google-one", "remaining": 5}  # LS credits merged


def test_choose_antigravity_result_quota_summary_when_local_down():
    # LS closed -> still use the cloud summary lanes (no local tier to merge).
    local = {"status": "not-running", "limits": []}
    remote = {"status": "ok", "limitsSource": "quota-summary", "tier": "Antigravity",
              "limits": [{"label": "Gemini", "sublabel": "Weekly", "percent": 9.07}]}
    out = providers.antigravity.choose_antigravity_result(local, remote)
    assert out["limitsSource"] == "quota-summary" and out["tier"] == "Antigravity"


def test_antigravity_cloudcode_host_detects_daily_from_log(tmp_path, monkeypatch):
    """The host is detected from agy's own request logs (prod vs daily return different quota
    numbers). A log mentioning the daily host -> daily; no log -> prod default."""
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    (log_dir / "cli-1.log").write_text(
        "blah\nURL: https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist\nmore\n")
    monkeypatch.setattr(providers.antigravity, "ANTIGRAVITY_CLOUDCODE_LOG_DIR", log_dir)
    assert providers.antigravity.antigravity_cloudcode_host() == "daily-cloudcode-pa.googleapis.com"

    # Empty dir -> prod default.
    monkeypatch.setattr(providers.antigravity, "ANTIGRAVITY_CLOUDCODE_LOG_DIR", tmp_path / "absent")
    assert providers.antigravity.antigravity_cloudcode_host() == "cloudcode-pa.googleapis.com"


@pytest.mark.asyncio
async def test_cost_summary_timeout_leaves_providers_intact():
    """On a cost-scan timeout the apply (merge) step must be skipped, so no half-written
    costSummary lands in the snapshot. The scan now runs CONCURRENTLY and returns its
    summaries separately (it never touches `providers`), so the rollback is simply 'don't
    apply on timeout' — a slow compute must therefore leave every provider costSummary-free."""
    import time as _time
    args = MagicMock()
    args.no_network = False
    args.timeout = 0.05

    def slow_compute(deadline=None):
        _time.sleep(0.3)  # exceed wait_for(timeout=args.timeout)
        return {p: {"_SENTINEL_": True} for p in ("codex", "claude", "gemini", "antigravity")}

    async def fast_ok(func, *a, **k):
        return {"status": "ok", "limits": []}

    async def noop_pricing(*a, **k):
        return

    with patch("backend.load_config", return_value={}), \
         patch("backend.collect_browser_sessions", return_value=([], {})), \
         patch("backend.run_threaded_provider", side_effect=fast_ok), \
         patch("pricing_data.refresh_pricing", noop_pricing), \
         patch("backend.compute_local_cost_summaries", side_effect=slow_compute):
        res = await backend.build_snapshot(args)

    assert res["ok"] is True
    assert res["diagnostics"].get("cost_summary_timeout") is True
    for prov in res["providers"].values():
        assert "_SENTINEL_" not in (prov.get("costSummary") or {})  # apply was skipped on timeout


# --- Codex RPC -> cookie fallback selection in build_snapshot ---

async def _build_snapshot_codex(codex_rpc_result, codex_cookie_result):
    """Drive build_snapshot with controllable codex RPC / cookie results.

    run_codex_rpc and run_openai_cookie_api are coroutines wrapped in
    bounded_provider, so we patch them on the backend module to return our
    canned dicts. Everything else (antigravity/gemini/google_one via
    run_threaded_provider, claude via run_claude_api, the cost scan, and
    pricing refresh) is stubbed to a fast no-op so only the codex selection
    branch is exercised.
    """
    args = MagicMock()
    args.no_network = False
    args.timeout = 0.5

    async def fast_threaded(func, *a, **k):
        return {"status": "ok", "limits": []}

    async def fake_codex_rpc(timeout):
        return codex_rpc_result

    async def fake_codex_cookie(cookies, timeout):
        return codex_cookie_result

    async def fake_claude(cookies, timeout, prev=None):
        return {"status": "ok", "limits": []}

    def noop_compute(deadline=None):
        return {}

    async def noop_pricing(*a, **k):
        return

    with patch("backend.load_config", return_value={}), \
         patch("backend.collect_browser_sessions", return_value=([], {})), \
         patch("backend.run_threaded_provider", side_effect=fast_threaded), \
         patch("backend.run_codex_rpc", side_effect=fake_codex_rpc), \
         patch("backend.run_openai_cookie_api", side_effect=fake_codex_cookie), \
         patch("backend.run_claude_api", side_effect=fake_claude), \
         patch("backend.compute_local_cost_summaries", side_effect=noop_compute), \
         patch("pricing_data.refresh_pricing", noop_pricing):
        return await backend.build_snapshot(args)


@pytest.mark.asyncio
async def test_codex_falls_back_to_cookie_when_rpc_not_ok():
    """RPC non-ok + cookie ok => providers['codex'] becomes the cookie result."""
    rpc = {"status": "not-running", "label": "Codex", "limits": [], "source": "json-rpc"}
    cookie = {"status": "ok", "label": "Codex", "limits": [{"percent": 7}], "source": "browser-api"}
    res = await _build_snapshot_codex(rpc, cookie)
    assert res["providers"]["codex"]["status"] == "ok"
    assert res["providers"]["codex"]["source"] == "browser-api"
    # enrich_ui_formatting decorates limits, so compare on the load-bearing percent.
    assert res["providers"]["codex"]["limits"][0]["percent"] == 7


@pytest.mark.asyncio
async def test_codex_keeps_rpc_result_when_rpc_ok():
    """Mirror: RPC ok => the cookie result is ignored, codex stays the RPC result."""
    rpc = {"status": "ok", "label": "Codex", "limits": [{"percent": 3}], "source": "json-rpc"}
    cookie = {"status": "ok", "label": "Codex", "limits": [{"percent": 99}], "source": "browser-api"}
    res = await _build_snapshot_codex(rpc, cookie)
    assert res["providers"]["codex"]["status"] == "ok"
    assert res["providers"]["codex"]["source"] == "json-rpc"
    # RPC's percent (3) survives, not the ignored cookie's (99).
    assert res["providers"]["codex"]["limits"][0]["percent"] == 3


# --- Lazy OpenAI cookie fallback: only fired when codex RPC is not ok ---

async def _build_snapshot_codex_counting(codex_rpc_result, codex_cookie_result):
    """Like _build_snapshot_codex but records how many times the OpenAI cookie
    fallback was invoked (in ``calls['cookie']``), to assert laziness."""
    args = MagicMock()
    args.no_network = False
    args.timeout = 0.5
    calls = {"cookie": 0}

    async def fast_threaded(func, *a, **k):
        return {"status": "ok", "limits": []}

    async def fake_codex_rpc(timeout):
        return codex_rpc_result

    async def fake_codex_cookie(cookies, timeout):
        calls["cookie"] += 1
        return codex_cookie_result

    async def fake_claude(cookies, timeout, prev=None):
        return {"status": "ok", "limits": []}

    def noop_compute(deadline=None):
        return {}

    async def noop_pricing(*a, **k):
        return

    with patch("backend.load_config", return_value={}), \
         patch("backend.collect_browser_sessions", return_value=([], {})), \
         patch("backend.run_threaded_provider", side_effect=fast_threaded), \
         patch("backend.run_codex_rpc", side_effect=fake_codex_rpc), \
         patch("backend.run_openai_cookie_api", side_effect=fake_codex_cookie), \
         patch("backend.run_claude_api", side_effect=fake_claude), \
         patch("backend.compute_local_cost_summaries", side_effect=noop_compute), \
         patch("pricing_data.refresh_pricing", noop_pricing):
        res = await backend.build_snapshot(args)
    return res, calls


@pytest.mark.asyncio
async def test_codex_cookie_not_called_when_rpc_ok():
    """Laziness (a): a healthy codex RPC => the OpenAI cookie GET is NOT invoked at all."""
    rpc = {"status": "ok", "label": "Codex", "limits": [{"percent": 3}], "source": "json-rpc"}
    cookie = {"status": "ok", "label": "Codex", "limits": [{"percent": 99}], "source": "browser-api"}
    res, calls = await _build_snapshot_codex_counting(rpc, cookie)
    assert calls["cookie"] == 0
    assert res["providers"]["codex"]["source"] == "json-rpc"
    assert res["providers"]["codex"]["limits"][0]["percent"] == 3


@pytest.mark.asyncio
async def test_codex_cookie_called_and_adopted_when_rpc_not_ok():
    """Laziness (b): RPC not-ok => the cookie GET IS invoked (once) and adopted when ok."""
    rpc = {"status": "not-running", "label": "Codex", "limits": [], "source": "json-rpc"}
    cookie = {"status": "ok", "label": "Codex", "limits": [{"percent": 7}], "source": "browser-api"}
    res, calls = await _build_snapshot_codex_counting(rpc, cookie)
    assert calls["cookie"] == 1
    assert res["providers"]["codex"]["source"] == "browser-api"
    assert res["providers"]["codex"]["limits"][0]["percent"] == 7


# --- KWallet-locked propagation to gemini/claude in build_snapshot ---

async def _build_snapshot_walletlocked(gemini_status, claude_status, wallet_status="wallet-locked"):
    """Drive build_snapshot with a given kwallet status in browser_stats and
    controllable gemini/claude statuses (gemini via run_threaded_provider,
    claude via run_claude_api). Codex paths and the cost scan are stubbed."""
    args = MagicMock()
    args.no_network = False
    args.timeout = 0.5

    browser_stats = {"kwallet": {"status": wallet_status}}

    async def fast_threaded(func, *a, **k):
        if getattr(func, "__name__", "") == "run_gemini_web":
            return {"status": gemini_status, "label": "Gemini", "limits": []}
        return {"status": "ok", "limits": []}

    async def fake_codex_rpc(timeout):
        return {"status": "ok", "label": "Codex", "limits": []}

    async def fake_codex_cookie(cookies, timeout):
        return {"status": "missing-cookies", "label": "Codex", "limits": []}

    async def fake_claude(cookies, timeout, prev=None):
        return {"status": claude_status, "label": "Claude", "limits": []}

    def noop_compute(deadline=None):
        return {}

    async def noop_pricing(*a, **k):
        return

    with patch("backend.load_config", return_value={}), \
         patch("backend.collect_browser_sessions", return_value=([], browser_stats)), \
         patch("backend.run_threaded_provider", side_effect=fast_threaded), \
         patch("backend.run_codex_rpc", side_effect=fake_codex_rpc), \
         patch("backend.run_openai_cookie_api", side_effect=fake_codex_cookie), \
         patch("backend.run_claude_api", side_effect=fake_claude), \
         patch("backend.compute_local_cost_summaries", side_effect=noop_compute), \
         patch("pricing_data.refresh_pricing", noop_pricing):
        return await backend.build_snapshot(args)


@pytest.mark.asyncio
async def test_wallet_locked_flips_missing_cookies_to_wallet_locked():
    """kwallet wallet-locked => gemini & claude 'missing-cookies' both flip to 'wallet-locked'."""
    res = await _build_snapshot_walletlocked("missing-cookies", "missing-cookies")
    assert res["providers"]["gemini"]["status"] == "wallet-locked"
    assert res["providers"]["claude"]["status"] == "wallet-locked"
    assert "KWallet is locked" in res["providers"]["gemini"]["message"]
    assert "KWallet is locked" in res["providers"]["claude"]["message"]


@pytest.mark.asyncio
async def test_wallet_locked_does_not_overwrite_ok_provider():
    """Guard: a provider already 'ok' is NOT clobbered to 'wallet-locked'."""
    res = await _build_snapshot_walletlocked("ok", "missing-cookies")
    assert res["providers"]["gemini"]["status"] == "ok"        # ok untouched
    assert res["providers"]["claude"]["status"] == "wallet-locked"  # missing-cookies flipped


@pytest.mark.asyncio
async def test_wallet_state_unknown_relabels_like_wallet_locked():
    """Error-UX: an unreadable wallet (wallet-state-unknown, e.g. isOpen couldn't be checked
    during a background refresh) must relabel gemini/claude 'missing-cookies' too — so the UI
    offers the KWallet remedy, not a misleading 'sign in again' — while keeping the distinct
    status (and an honest 'could not be checked' message) rather than claiming it's locked."""
    res = await _build_snapshot_walletlocked("missing-cookies", "missing-cookies",
                                             wallet_status="wallet-state-unknown")
    assert res["providers"]["gemini"]["status"] == "wallet-state-unknown"
    assert res["providers"]["claude"]["status"] == "wallet-state-unknown"
    assert "could not be checked" in res["providers"]["claude"]["message"]
    # Guard: an 'ok' provider is still not clobbered.
    res2 = await _build_snapshot_walletlocked("ok", "missing-cookies",
                                              wallet_status="wallet-state-unknown")
    assert res2["providers"]["gemini"]["status"] == "ok"
    assert res2["providers"]["claude"]["status"] == "wallet-state-unknown"


# --- D1/D2: carry-forward wired end-to-end through build_snapshot + persistence ---

async def _build_snapshot_carry_forward(claude_status, cached_snapshot):
    """Drive build_snapshot with claude returning `claude_status` (no limits) and
    backend.load_snapshot returning `cached_snapshot`. Gemini/Codex return ok, the
    cost scan is stubbed (patch compute_local_cost_summaries per CLAUDE.md)."""
    args = MagicMock()
    args.no_network = False
    args.timeout = 0.5

    browser_stats = {"kwallet": {"status": "ok"}}

    async def fast_threaded(func, *a, **k):
        if getattr(func, "__name__", "") == "run_gemini_web":
            return {"status": "ok", "label": "Gemini", "limits": []}
        return {"status": "ok", "limits": []}

    async def fake_codex_rpc(timeout):
        return {"status": "ok", "label": "Codex", "limits": []}

    async def fake_codex_cookie(cookies, timeout):
        return {"status": "missing-cookies", "label": "Codex", "limits": []}

    async def fake_claude(cookies, timeout, prev=None):
        return {"status": claude_status, "label": "Claude", "limits": [], "message": "boom"}

    def noop_compute(deadline=None):
        return {}

    async def noop_pricing(*a, **k):
        return

    with patch("backend.load_config", return_value={}), \
         patch("backend.load_snapshot", return_value=cached_snapshot), \
         patch("backend.collect_browser_sessions", return_value=([], browser_stats)), \
         patch("backend.run_threaded_provider", side_effect=fast_threaded), \
         patch("backend.run_codex_rpc", side_effect=fake_codex_rpc), \
         patch("backend.run_openai_cookie_api", side_effect=fake_codex_cookie), \
         patch("backend.run_claude_api", side_effect=fake_claude), \
         patch("backend.compute_local_cost_summaries", side_effect=noop_compute), \
         patch("pricing_data.refresh_pricing", noop_pricing):
        return await backend.build_snapshot(args)


@pytest.mark.asyncio
async def test_build_snapshot_carries_forward_transient_claude_failure():
    """D1: pins the carry-forward LOOP in build_snapshot (backend.py:1007), not just the
    unit function. A transient claude 'api-error' with a fresh good cache => the resulting
    snapshot's claude entry is 'ok', carries the cached limits, and is marked stale."""
    good_limits = [{"label": "Session", "percent": 40}]
    cached = _cached_provider("Claude", good_limits, ts_offset=60.0)
    res = await _build_snapshot_carry_forward("api-error", cached)
    claude = res["providers"]["claude"]
    assert claude["status"] == "ok"
    # The cached limit is carried forward (build_snapshot's later enrich_ui_formatting
    # adds display fields, so match on identity, not exact dict equality).
    assert len(claude["limits"]) == 1
    assert claude["limits"][0]["label"] == "Session"
    assert claude["limits"][0]["percent"] == 40
    assert claude["stale"] is True
    assert claude["staleAsOf"] == cached["timestamp"]


@pytest.mark.asyncio
async def test_build_snapshot_carries_forward_transient_error_status():
    """Defensive backstop: a transient provider 'error' status is also carried forward."""
    good_limits = [{"label": "Session", "percent": 40}]
    cached = _cached_provider("Claude", good_limits, ts_offset=60.0)
    res = await _build_snapshot_carry_forward("error", cached)
    claude = res["providers"]["claude"]
    assert claude["status"] == "ok"
    assert len(claude["limits"]) == 1
    assert claude["limits"][0]["label"] == "Session"
    assert claude["stale"] is True


# --- Google One AI credit-pool throttle (mirrors the Claude CREDIT_REFRESH_SECONDS carry) ---

def _g1_cb(*, source="google-one-ai", ts_offset=0.0, fetched=None):
    """A Google One creditBalance dict for the previous-snapshot antigravity provider."""
    if fetched is None:
        fetched = _iso(ts_offset)
    return {"label": "Credits", "amount": 12000, "currency": "credits",
            "source": source, "fetchedAt": fetched}


def test_google_one_credit_fresh_returns_dict_when_fresh():
    prev = {"creditBalance": _g1_cb(ts_offset=60.0)}
    out = backend.google_one_credit_fresh(prev)
    assert isinstance(out, dict) and out["source"] == "google-one-ai" and out["amount"] == 12000


def test_google_one_credit_fresh_none_when_stale():
    prev = {"creditBalance": _g1_cb(ts_offset=400.0)}   # > 300s
    assert backend.google_one_credit_fresh(prev) is None


def test_google_one_credit_fresh_none_on_wrong_source():
    prev = {"creditBalance": _g1_cb(source="antigravity-plan-status", ts_offset=10.0)}
    assert backend.google_one_credit_fresh(prev) is None


def test_google_one_credit_fresh_none_on_bad_fetchedat():
    assert backend.google_one_credit_fresh({"creditBalance": _g1_cb(fetched="not-a-date")}) is None
    assert backend.google_one_credit_fresh({"creditBalance": _g1_cb(fetched="")}) is None
    # missing fetchedAt key
    cb = _g1_cb(ts_offset=10.0)
    cb.pop("fetchedAt")
    assert backend.google_one_credit_fresh({"creditBalance": cb}) is None


def test_google_one_credit_fresh_none_on_negative_age():
    # A future fetchedAt (clock jump) yields a negative age and must be rejected.
    prev = {"creditBalance": _g1_cb(ts_offset=-120.0)}
    assert backend.google_one_credit_fresh(prev) is None


def test_google_one_credit_fresh_none_on_missing_prev():
    assert backend.google_one_credit_fresh(None) is None
    assert backend.google_one_credit_fresh({}) is None
    assert backend.google_one_credit_fresh({"creditBalance": "garbage"}) is None


async def _build_snapshot_google_one(cached_snapshot):
    """Drive build_snapshot with backend.load_snapshot returning `cached_snapshot`,
    recording how many times run_google_one_credits was scheduled via
    run_threaded_provider (calls['g1']). All providers return ok; the RPC path is
    patched (run_google_one_credits itself never executes)."""
    args = MagicMock()
    args.no_network = False
    args.timeout = 0.5
    calls = {"g1": 0}

    async def fast_threaded(func, *a, **k):
        name = getattr(func, "__name__", "")
        if name == "run_google_one_credits":
            calls["g1"] += 1
            return {"status": "ok", "creditBalance": _g1_cb(ts_offset=0.0)}
        if name == "run_gemini_web":
            return {"status": "ok", "label": "Gemini", "limits": []}
        return {"status": "ok", "limits": []}

    async def fake_codex_rpc(timeout):
        return {"status": "ok", "label": "Codex", "limits": []}

    async def fake_codex_cookie(cookies, timeout):
        return {"status": "missing-cookies", "label": "Codex", "limits": []}

    async def fake_claude(cookies, timeout, prev=None):
        return {"status": "ok", "label": "Claude", "limits": []}

    def noop_compute(deadline=None):
        return {}

    async def noop_pricing(*a, **k):
        return

    with patch("backend.load_config", return_value={}), \
         patch("backend.load_snapshot", return_value=cached_snapshot), \
         patch("backend.collect_browser_sessions", return_value=([], {"kwallet": {"status": "ok"}})), \
         patch("backend.run_threaded_provider", side_effect=fast_threaded), \
         patch("backend.run_codex_rpc", side_effect=fake_codex_rpc), \
         patch("backend.run_openai_cookie_api", side_effect=fake_codex_cookie), \
         patch("backend.run_claude_api", side_effect=fake_claude), \
         patch("backend.compute_local_cost_summaries", side_effect=noop_compute), \
         patch("pricing_data.refresh_pricing", noop_pricing):
        res = await backend.build_snapshot(args)
    return res, calls


@pytest.mark.asyncio
async def test_build_snapshot_google_one_carried_when_fresh():
    """A fresh cached google-one-ai balance => the RPC is NOT scheduled and the carried
    balance is what renders on the antigravity provider."""
    carried = _g1_cb(ts_offset=60.0)
    cached = {"timestamp": _iso(60.0), "providers": {"antigravity": {"creditBalance": carried}}}
    res, calls = await _build_snapshot_google_one(cached)
    assert calls["g1"] == 0
    cb = res["providers"]["antigravity"].get("creditBalance")
    assert isinstance(cb, dict)
    assert cb["source"] == "google-one-ai"
    assert cb["fetchedAt"] == carried["fetchedAt"]   # NOT re-stamped


@pytest.mark.asyncio
async def test_build_snapshot_google_one_refetched_when_stale():
    """A stale cached balance => the RPC IS scheduled (once)."""
    cached = {"timestamp": _iso(400.0),
              "providers": {"antigravity": {"creditBalance": _g1_cb(ts_offset=400.0)}}}
    res, calls = await _build_snapshot_google_one(cached)
    assert calls["g1"] == 1


@pytest.mark.asyncio
async def test_build_snapshot_no_carry_forward_when_cache_too_old():
    """D1 negative: a cache older than the 900s grace window must let the real error
    surface (the loop's max_stale_seconds bound is live in build_snapshot)."""
    cached = _cached_provider("Claude", [{"label": "Session", "percent": 40}], ts_offset=1000.0)
    res = await _build_snapshot_carry_forward("api-error", cached)
    claude = res["providers"]["claude"]
    assert claude["status"] == "api-error"
    assert claude.get("stale") is not True


def test_carry_forward_staleAsOf_survives_save_load_roundtrip(tmp_path, monkeypatch):
    """D2: a carried-forward entry's stale/staleAsOf flags must survive the JSON
    serialization seam (save_snapshot -> load_snapshot), so the widget can read them
    on a cold-start cache paint."""
    monkeypatch.setattr(backend, "SNAPSHOT_PATH", tmp_path / "last_snapshot.json")
    cached = _cached_provider("Claude", [{"label": "Session", "percent": 40}], ts_offset=60.0)
    failing = {"label": "Claude", "status": "api-error", "limits": []}
    carried = backend.carry_forward_provider_last_good(failing, cached)
    assert carried["stale"] is True

    snapshot = {"timestamp": _iso(0.0), "ok": True, "providers": {"claude": carried}}
    backend.save_snapshot(snapshot)
    reloaded = backend.load_snapshot()

    rl_claude = reloaded["providers"]["claude"]
    assert rl_claude["stale"] is True
    assert rl_claude["staleAsOf"] == cached["timestamp"]
    assert rl_claude["status"] == "ok"
    assert rl_claude["limits"] == [{"label": "Session", "percent": 40}]


# --- _snapshot_has_live_data helper ---

def test_snapshot_has_live_data_true_for_ok_status():
    """Any provider with status 'ok' marks the snapshot as live."""
    snap = {"providers": {"codex": {"status": "timeout"}, "gemini": {"status": "ok"}}}
    assert backend._snapshot_has_live_data(snap) is True


def test_snapshot_has_live_data_true_for_cookies_ready_status():
    """'cookies-ready' (the --no-network healthy status) also counts as live."""
    snap = {"providers": {"claude": {"status": "cookies-ready"}}}
    assert backend._snapshot_has_live_data(snap) is True


def test_snapshot_has_live_data_false_for_all_degraded():
    """An all-timeout/error snapshot is NOT live (don't clobber the good cache)."""
    snap = {"providers": {
        "codex": {"status": "timeout"},
        "gemini": {"status": "api-error"},
        "claude": {"status": "missing-cookies"},
    }}
    assert backend._snapshot_has_live_data(snap) is False


def test_snapshot_has_live_data_false_for_non_dict_and_missing_providers():
    """Non-dict input, missing 'providers', or non-dict providers => False (no crash)."""
    assert backend._snapshot_has_live_data(None) is False
    assert backend._snapshot_has_live_data("not a dict") is False
    assert backend._snapshot_has_live_data({}) is False              # missing providers
    assert backend._snapshot_has_live_data({"providers": []}) is False  # providers not a dict
    assert backend._snapshot_has_live_data({"providers": {"x": "notdict"}}) is False



# --- Screen-lock refresh gate ---------------------------------------

def test_screen_locked_parses_busctl_boolean(monkeypatch):
    class _P:
        def __init__(self, rc, out):
            self.returncode, self.stdout = rc, out
    monkeypatch.setattr(backend.subprocess, "run", lambda *a, **k: _P(0, b"b true\n"))
    assert backend._screen_locked() is True
    monkeypatch.setattr(backend.subprocess, "run", lambda *a, **k: _P(0, b"b false\n"))
    assert backend._screen_locked() is False
    # Non-zero return code => fail-open (treat as unlocked).
    monkeypatch.setattr(backend.subprocess, "run", lambda *a, **k: _P(1, b""))
    assert backend._screen_locked() is False


def test_screen_locked_fail_open_when_busctl_missing(monkeypatch):
    def _boom(*a, **k):
        raise FileNotFoundError("no busctl on this box")
    monkeypatch.setattr(backend.subprocess, "run", _boom)
    assert backend._screen_locked() is False


def test_screenlock_skips_background_refresh(monkeypatch, capsys):
    import json as _json
    monkeypatch.setattr(backend, "_screen_locked", lambda: True)
    monkeypatch.setattr(backend, "load_snapshot",
                        lambda: {"ok": True, "providers": {"codex": {"status": "ok"}}})
    saved, built = [], []
    monkeypatch.setattr(backend, "save_snapshot", lambda s: saved.append(s))

    async def _fake_build(args):
        built.append(1)
        return {}
    monkeypatch.setattr(backend, "build_snapshot", _fake_build)
    monkeypatch.setattr(sys, "argv", ["backend.py", "--once", "--background"])
    assert backend.main() == 0
    out = _json.loads(capsys.readouterr().out)
    assert out["diagnostics"]["refresh_skipped"] == "screen-locked"
    assert out["notifications"] == []
    assert out["providers"]["codex"]["status"] == "ok"  # cached snapshot re-painted
    assert saved == []   # last-known-good cache never clobbered
    assert built == []   # build_snapshot skipped entirely


def test_screenlock_ignored_for_foreground_refresh(monkeypatch, capsys):
    # A foreground run (no --background, e.g. the KWallet-unlock path) ALWAYS refreshes,
    # even while locked.
    monkeypatch.setattr(backend, "_screen_locked", lambda: True)
    built = []

    async def _fake_build(args):
        built.append(1)
        return {"providers": {}, "config": {}}
    monkeypatch.setattr(backend, "build_snapshot", _fake_build)
    monkeypatch.setattr(backend, "load_snapshot", lambda: {})
    monkeypatch.setattr(backend, "save_snapshot", lambda s: None)
    monkeypatch.setattr(sys, "argv", ["backend.py", "--once"])
    assert backend.main() == 0
    assert built == [1]

# ---------------------------------------------------------------------------
# main() ALWAYS-prints-JSON contract (the headline CLAUDE.md invariant)
# ---------------------------------------------------------------------------

def test_fatal_build_snapshot_still_prints_cached_json(monkeypatch, capsys):
    import json as _json
    monkeypatch.setattr(backend, "_screen_locked", lambda: False)

    async def _boom(args):
        raise RuntimeError("orchestrator died")
    monkeypatch.setattr(backend, "build_snapshot", _boom)
    monkeypatch.setattr(backend, "load_snapshot",
                        lambda: {"ok": True, "providers": {"codex": {"status": "ok"}}})
    saved = []
    monkeypatch.setattr(backend, "save_snapshot", lambda s: saved.append(s))
    monkeypatch.setattr(sys, "argv", ["backend.py", "--once"])
    assert backend.main() == 0
    out = _json.loads(capsys.readouterr().out)
    assert "RuntimeError" in out["diagnostics"]["fatal"]
    assert out["notifications"] == []
    assert out["providers"]["codex"]["status"] == "ok"  # last-known-good re-painted
    assert saved == []  # a fatal must never clobber the cold-start cache


def test_notifications_failure_degrades_not_empty_stdout(monkeypatch, capsys):
    # The post-snapshot tail (cache write + compute_notifications) is enrichment: a crash
    # there must still print the freshly built snapshot, minus notifications.
    import json as _json
    monkeypatch.setattr(backend, "_screen_locked", lambda: False)

    async def _ok(args):
        return {"ok": True, "providers": {"codex": {"status": "ok"}}, "config": {}}
    monkeypatch.setattr(backend, "build_snapshot", _ok)
    monkeypatch.setattr(backend, "save_snapshot", lambda s: None)
    monkeypatch.setattr(backend, "load_snapshot", lambda: {})

    def _boom(providers, config):
        raise TypeError("'int' object is not iterable")
    monkeypatch.setattr(backend, "compute_notifications", _boom)
    monkeypatch.setattr(sys, "argv", ["backend.py", "--once"])
    assert backend.main() == 0
    out = _json.loads(capsys.readouterr().out)
    assert out["notifications"] == []
    assert "TypeError" in out["diagnostics"]["post_snapshot_error"]
    assert out["providers"]["codex"]["status"] == "ok"  # fresh snapshot still shipped


def test_unserializable_snapshot_falls_back_to_cached_json(monkeypatch, capsys):
    import json as _json
    monkeypatch.setattr(backend, "_screen_locked", lambda: False)

    async def _bad(args):
        # A set is not JSON-serializable — simulates a bug leaking a raw object in.
        return {"ok": True, "providers": {"codex": {"status": "ok", "blob": {1, 2}}}, "config": {}}
    monkeypatch.setattr(backend, "build_snapshot", _bad)
    monkeypatch.setattr(backend, "save_snapshot", lambda s: None)
    monkeypatch.setattr(backend, "load_snapshot",
                        lambda: {"ok": True, "providers": {"claude": {"status": "ok"}}})
    monkeypatch.setattr(sys, "argv", ["backend.py", "--once"])
    assert backend.main() == 0
    out = _json.loads(capsys.readouterr().out)
    assert out["diagnostics"]["fatal"].startswith("snapshot-serialize:")
    assert out["providers"]["claude"]["status"] == "ok"  # disk cache, JSON by construction


def test_notification_thresholds_scalar_config_tolerated(monkeypatch, tmp_path):
    # A hand-edited config.json holding a scalar instead of a list must fall back to the
    # default thresholds instead of raising out of compute_notifications.
    monkeypatch.setattr(backend, "NOTIFY_STATE_PATH", tmp_path / "notify_state.json")
    providers = {"codex": {"label": "Codex", "limits": [{"label": "Session", "percent": 99.0}]}}
    out = backend.compute_notifications(providers, {"notificationThresholds": 90})
    assert [n["threshold"] for n in out] == [90]  # defaults (80/90/100 set) applied instead


# ---------------------------------------------------------------------------
# --cost CLI wiring: exit code 3 on a degraded (timed-out) cost scan
# ---------------------------------------------------------------------------

def test_cost_flag_exit_codes(monkeypatch, capsys):
    import json as _json

    async def _degraded(args):
        return {"ok": True, "timestamp": "t", "providers": {},
                "diagnostics": {"cost_summary_timeout": True}}
    monkeypatch.setattr(backend, "build_snapshot", _degraded)
    monkeypatch.setattr(sys, "argv", ["backend.py", "--cost"])
    assert backend.main() == 3
    out = _json.loads(capsys.readouterr().out)
    assert out["degraded"] is True and out["degradedReason"] == "cost_summary_timeout"

    async def _healthy(args):
        return {"ok": True, "timestamp": "t",
                "providers": {"codex": {"costSummary": {"cost30d": 1.0}}}, "diagnostics": {}}
    monkeypatch.setattr(backend, "build_snapshot", _healthy)
    monkeypatch.setattr(sys, "argv", ["backend.py", "--cost"])
    assert backend.main() == 0
    assert "degraded" not in _json.loads(capsys.readouterr().out)


def test_cost_flag_survives_fatal_build_snapshot(monkeypatch, capsys):
    # Mirrors test_fatal_build_snapshot_still_prints_cached_json for the --cost branch:
    # a BaseException escaping build_snapshot (e.g. a re-raised BaseExceptionGroup leaf)
    # must degrade to the cached snapshot's cost export, not crash with a bare traceback
    # and empty stdout.
    import json as _json

    async def _boom(args):
        raise RuntimeError("orchestrator died")
    monkeypatch.setattr(backend, "build_snapshot", _boom)
    monkeypatch.setattr(backend, "load_snapshot",
                        lambda: {"ok": True, "timestamp": "t",
                                 "providers": {"codex": {"costSummary": {"cost30d": 1.0}}}})
    monkeypatch.setattr(sys, "argv", ["backend.py", "--cost"])
    assert backend.main() == 3  # fatal fallback -> non-zero, like a degraded scan
    out = _json.loads(capsys.readouterr().out)
    assert out["providers"]["codex"]["cost30d"] == 1.0  # cached snapshot still projected


def test_cost_flag_reraises_keyboard_interrupt(monkeypatch):
    async def _interrupt(args):
        raise KeyboardInterrupt()
    monkeypatch.setattr(backend, "build_snapshot", _interrupt)
    monkeypatch.setattr(sys, "argv", ["backend.py", "--cost"])
    with pytest.raises(KeyboardInterrupt):
        backend.main()


# ---------------------------------------------------------------------------
# update_refresh_interval: success + config-lock contention (TimeoutError)
# ---------------------------------------------------------------------------

def test_update_refresh_interval_success_and_lock_timeout(tmp_path, monkeypatch):
    cfg_path = tmp_path / ".tallybar" / "config.json"
    monkeypatch.setattr(backend, "CONFIG_PATH", cfg_path)

    out = backend.update_refresh_interval(5)
    assert out["refreshIntervalMinutes"] == 5

    with pytest.raises(ValueError):
        backend.update_refresh_interval(7)  # not in the allowed set (1/2/5/15/30)

    # Lock contention (another --set-refresh-interval/--set-config writer holds
    # .config.lock) must raise TimeoutError, not hang or silently corrupt config.json.
    monkeypatch.setattr(backend, "flock_with_timeout", lambda fd, budget: False)
    with pytest.raises(TimeoutError):
        backend.update_refresh_interval(2)
    # The failed write must not have touched the config (still the earlier successful value).
    assert backend.load_config()["refreshIntervalMinutes"] == 5


def test_set_refresh_interval_cli_maps_lock_timeout_to_exit_2(monkeypatch, capsys):
    def _boom(minutes):
        raise TimeoutError("could not acquire config lock (another write in progress)")
    monkeypatch.setattr(backend, "update_refresh_interval", _boom)
    monkeypatch.setattr(sys, "argv", ["backend.py", "--set-refresh-interval", "5"])
    assert backend.main() == 2
    assert "config lock" in capsys.readouterr().err


def test_set_config_cli_maps_lock_timeout_to_exit_2(monkeypatch, capsys):
    def _boom(updates):
        raise TimeoutError("could not acquire config lock (another write in progress)")
    monkeypatch.setattr(backend, "update_config_values", _boom)
    monkeypatch.setattr(sys, "argv", ["backend.py", "--set-config", '{"notificationsEnabled": true}'])
    assert backend.main() == 2
    assert "config lock" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# compute_notifications: flock-guarded read-modify-write of notify_state.json
# ---------------------------------------------------------------------------

def test_compute_notifications_lock_contention_raises_timeout(tmp_path, monkeypatch):
    # Two concurrent --once invocations must not lost-update the armed-set: on contention
    # for the (new) .notify.lock, compute_notifications raises rather than silently racing
    # the other writer. main()'s post-snapshot tail guard (test_notifications_failure_
    # degrades_not_empty_stdout) already covers the graceful-degrade side of this.
    monkeypatch.setattr(backend, "NOTIFY_STATE_PATH", tmp_path / "notify_state.json")
    monkeypatch.setattr(backend, "flock_with_timeout", lambda fd, budget: False)
    providers = {"codex": {"label": "Codex", "limits": [{"label": "Session", "percent": 99.0}]}}
    with pytest.raises(TimeoutError):
        backend.compute_notifications(providers, {"notificationThresholds": [90]})


# ---------------------------------------------------------------------------
# carry_forward_partial_antigravity_lanes (main-tree unique)
# ---------------------------------------------------------------------------

def _cached_with_lanes(lanes):
    return {"providers": {"antigravity": {"status": "ok", "limits": lanes}}}


def test_partial_carry_forward_grafts_last_known_lanes():
    """A "partial" Antigravity result (authenticated, no lanes) gets the last-known lanes from the
    cached snapshot grafted in and tagged stale."""
    lanes = [{"label": "Session (Gemini)", "percent": 42}, {"label": "Session (Claude)", "percent": 10}]
    prov = {"status": "partial", "limits": []}
    out = backend.carry_forward_partial_antigravity_lanes(prov, _cached_with_lanes(lanes))
    assert out["limits"] == lanes
    assert out["stale"] is True
    assert out["status"] == "partial"  # status is NOT upgraded to ok


def test_partial_carry_forward_noop_without_cached_lanes():
    """No cached lanes (cold start / cache absent) => nothing grafted, no stale tag, no crash."""
    prov = {"status": "partial", "limits": []}
    assert backend.carry_forward_partial_antigravity_lanes(prov, None) == {"status": "partial", "limits": []}
    assert backend.carry_forward_partial_antigravity_lanes(dict(prov), {"providers": {}}) == prov
    assert backend.carry_forward_partial_antigravity_lanes(dict(prov), _cached_with_lanes([])) == prov


def test_partial_carry_forward_leaves_other_statuses_untouched():
    """Only "partial" with empty limits triggers the graft; an "ok" read or one that already
    has lanes is returned unchanged."""
    fresh = {"status": "ok", "limits": [{"label": "Session", "percent": 5}]}
    assert backend.carry_forward_partial_antigravity_lanes(dict(fresh), _cached_with_lanes([{"percent": 99}])) == fresh
    has_lanes = {"status": "partial", "limits": [{"percent": 7}]}
    assert backend.carry_forward_partial_antigravity_lanes(dict(has_lanes), _cached_with_lanes([{"percent": 99}])) == has_lanes


# ---------------------------------------------------------------------------
# carry_forward_provider_last_good (transient failure grace window)
# ---------------------------------------------------------------------------

def _iso(offset_seconds: float = 0.0) -> str:
    ts = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=offset_seconds)
    return ts.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _cached_provider(label, limits, *, status="ok", ts_offset=0.0, extra=None):
    entry = {"label": label, "status": status, "limits": limits}
    if extra:
        entry.update(extra)
    return {"timestamp": _iso(ts_offset), "providers": {label.lower(): entry}}


def test_carry_forward_transient_within_window_freezes_last_good():
    """A transient Claude failure (unauthorized, no limits) with a fresh healthy cache =>
    the cached limits are frozen, status forced to ok, stale flag + staleAsOf set."""
    good_limits = [{"label": "Session", "percent": 40}]
    cached = _cached_provider("Claude", good_limits, ts_offset=60.0)
    failing = {"label": "Claude", "status": "unauthorized", "limits": [], "message": "403"}
    out = backend.carry_forward_provider_last_good(failing, cached)
    assert out["status"] == "ok"
    assert out["limits"] == good_limits
    assert out["stale"] is True
    assert out["staleAsOf"] == cached["timestamp"]


def test_carry_forward_preserves_creditBalance_and_tier():
    cached = _cached_provider(
        "Claude", [{"label": "Session", "percent": 40}], ts_offset=30.0,
        extra={"tier": "Pro", "creditBalance": {"amount": 5.0}})
    failing = {"label": "Claude", "status": "api-error", "limits": []}
    out = backend.carry_forward_provider_last_good(failing, cached)
    assert out["tier"] == "Pro"
    assert out["creditBalance"] == {"amount": 5.0}


def test_carry_forward_beyond_window_surfaces_original_error():
    """Cache older than max_stale_seconds => the real error is preserved (expired cookie
    must surface after the grace window)."""
    cached = _cached_provider("Claude", [{"label": "Session", "percent": 40}], ts_offset=1000.0)
    failing = {"label": "Claude", "status": "unauthorized", "limits": []}
    out = backend.carry_forward_provider_last_good(failing, cached, max_stale_seconds=900)
    assert out is failing
    assert out["status"] == "unauthorized"


def test_carry_forward_noop_for_non_transient_status():
    cached = _cached_provider("Claude", [{"label": "Session", "percent": 40}], ts_offset=10.0)
    for status in ("ok", "missing-cookies", "cookies-ready", "api-empty"):
        prov = {"label": "Claude", "status": status, "limits": []}
        assert backend.carry_forward_provider_last_good(prov, cached) is prov


def test_carry_forward_noop_when_provider_already_has_limits():
    cached = _cached_provider("Claude", [{"label": "Session", "percent": 40}], ts_offset=10.0)
    prov = {"label": "Claude", "status": "timeout", "limits": [{"label": "Session", "percent": 1}]}
    assert backend.carry_forward_provider_last_good(prov, cached) is prov


def test_carry_forward_noop_when_cache_missing_or_degraded():
    failing = {"label": "Claude", "status": "unauthorized", "limits": []}
    # No cache at all.
    assert backend.carry_forward_provider_last_good(dict(failing), None)["status"] == "unauthorized"
    # Cache present but the matching entry is itself degraded (no live status).
    degraded = _cached_provider("Claude", [], status="api-error", ts_offset=10.0)
    assert backend.carry_forward_provider_last_good(dict(failing), degraded)["status"] == "unauthorized"
    # Cache present but has no entry for this provider.
    other = _cached_provider("Gemini", [{"label": "Session", "percent": 5}], ts_offset=10.0)
    assert backend.carry_forward_provider_last_good(dict(failing), other)["status"] == "unauthorized"


def test_carry_forward_staleAsOf_does_not_ratchet():
    """Repeated carry-forwards must keep staleAsOf pinned to the ORIGINAL good fetch time so
    the window truly expires. Simulate: run 1 carries forward (stamps staleAsOf); feed that
    carried-forward entry back as the cache for run 2 with a NEWER top-level timestamp — the
    staleAsOf must stay the original, and age is measured from it (so a stale-enough original
    stops being carried forward even though the cache timestamp is fresh)."""
    original_ts = _iso(120.0)
    # Run 1: cache is healthy, fetched 120s ago.
    cached1 = {
        "timestamp": original_ts,
        "providers": {"claude": {"label": "Claude", "status": "ok",
                                 "limits": [{"label": "Session", "percent": 40}]}},
    }
    failing = {"label": "Claude", "status": "unauthorized", "limits": []}
    carried1 = backend.carry_forward_provider_last_good(dict(failing), cached1)
    assert carried1["staleAsOf"] == original_ts

    # Run 2: the carried-forward entry becomes the cache, but with a FRESH top-level timestamp
    # (as save_snapshot would stamp). staleAsOf must ride through unchanged.
    cached2 = {"timestamp": _iso(0.0), "providers": {"claude": carried1}}
    carried2 = backend.carry_forward_provider_last_good(dict(failing), cached2)
    assert carried2["staleAsOf"] == original_ts  # NOT reset to the fresh cache timestamp

    # Now push the original past the window: even with a brand-new cache timestamp, age is
    # measured from the original staleAsOf, so it must NO LONGER carry forward.
    expired_origin = _iso(1000.0)
    carried_expired = dict(carried1)
    carried_expired["staleAsOf"] = expired_origin
    cached3 = {"timestamp": _iso(0.0), "providers": {"claude": carried_expired}}
    out = backend.carry_forward_provider_last_good(dict(failing), cached3, max_stale_seconds=900)
    assert out["status"] == "unauthorized"  # window expired off the original fetch time


# --- diagnostics.timings -----------------------------------------------------------
# The refresh is a one-shot on a 5-minute timer; a phase that quietly grows costs battery
# and disk on every tick with nothing in the snapshot to show it. That is how the
# 2026-06-07 cost-scan regression stayed invisible until it started tripping the deadline.

@pytest.mark.asyncio
async def test_build_snapshot_reports_phase_timings():
    args = MagicMock()
    args.no_network = False
    args.timeout = 5.0

    async def ok(func, *a, **k):
        return {"status": "ok", "limits": []}

    async def benign_rpc(*a, **k):
        return {"label": "Codex", "status": "not-running", "limits": []}

    with patch("backend.load_config", return_value={}), \
         patch("backend.load_snapshot", return_value={}), \
         patch("backend.collect_browser_sessions", return_value=([], {})), \
         patch("backend.run_threaded_provider", side_effect=ok), \
         patch("backend.run_codex_rpc", side_effect=benign_rpc), \
         patch("backend.compute_local_cost_summaries", side_effect=lambda deadline=None: {}):
        res = await backend.build_snapshot(args)

    timings = res["diagnostics"]["timings"]
    assert set(timings) == {"cookies", "providers", "cost_scan", "total"}
    for phase, value in timings.items():
        # Monotonic clock: never negative, even across a wall-clock step.
        assert isinstance(value, int) and value >= 0, f"{phase}={value!r}"
    # The phases are bounded by the whole run. They deliberately do NOT sum to total —
    # the cost scan overlaps the provider fetches — so assert containment, not a sum.
    for phase in ("cookies", "providers", "cost_scan"):
        assert timings[phase] <= timings["total"] + 50, (
            f"{phase} ({timings[phase]}ms) exceeds total ({timings['total']}ms)"
        )


# --- Carried-forward providers get THIS run's local cost summary ---

def test_carry_forward_drops_cached_cost_summary():
    """The cost summary is local data, not part of the frozen network reading."""
    cached = _cached_provider("Claude", [{"label": "Session", "percent": 40}], ts_offset=60.0,
                              extra={"costSummary": {"cost30d": 1.0}, "creditBalance": {"amount": 5}})
    failing = {"label": "Claude", "status": "unauthorized", "limits": [], "message": "403"}
    out = backend.carry_forward_provider_last_good(failing, cached)
    assert out["stale"] is True
    assert "costSummary" not in out
    assert out["creditBalance"] == {"amount": 5}   # network data IS carried


@pytest.mark.asyncio
async def test_build_snapshot_carried_provider_gets_fresh_cost_summary():
    """End to end: a transient Claude failure carries the cached limits forward, but the
    costSummary must be this run's fresh scan, not the cached one (which used to win
    through apply_cost_summaries' setdefault for the whole 15-minute grace window)."""
    cached = _cached_provider("Claude", [{"label": "Session", "percent": 40}], ts_offset=60.0,
                              extra={"costSummary": {"cost30d": 1.0, "today": "stale"}})
    fresh = {"cost30d": 99.0, "today": "Today: $1.00 · 1K tok"}
    with patch("backend.compute_local_cost_summaries",
               side_effect=lambda deadline=None: {"claude": fresh}):
        res = await _build_snapshot_carry_forward_nocost("api-error", cached)
    claude = res["providers"]["claude"]
    assert claude["stale"] is True
    assert claude["costSummary"]["cost30d"] == 99.0


async def _build_snapshot_carry_forward_nocost(claude_status, cached_snapshot):
    """_build_snapshot_carry_forward without its compute_local_cost_summaries patch, so the
    caller can supply the scan result."""
    args = MagicMock()
    args.no_network = False
    args.timeout = 0.5

    async def fast_threaded(func, *a, **k):
        return {"status": "ok", "label": "Gemini", "limits": []}

    async def fake_codex_rpc(timeout):
        return {"status": "ok", "label": "Codex", "limits": []}

    async def fake_claude(cookies, timeout, prev=None):
        return {"status": claude_status, "label": "Claude", "limits": [], "message": "boom"}

    async def noop_pricing(*a, **k):
        return

    with patch("backend.load_config", return_value={}), \
         patch("backend.load_snapshot", return_value=cached_snapshot), \
         patch("backend.collect_browser_sessions", return_value=([], {"kwallet": {"status": "ok"}})), \
         patch("backend.run_threaded_provider", side_effect=fast_threaded), \
         patch("backend.run_codex_rpc", side_effect=fake_codex_rpc), \
         patch("backend.run_claude_api", side_effect=fake_claude), \
         patch("pricing_data.refresh_pricing", noop_pricing):
        return await backend.build_snapshot(args)


# --- The widget's refresh watchdog must outlast the backend's worst case ---

def test_refresh_watchdog_covers_backend_worst_case():
    """main.qml abandons a refresh after refreshWatchdogMs and DROPS its result, so the
    watchdog must exceed the backend's bounded worst case plus interpreter startup."""
    import re as _re
    qml = (Path(__file__).parent.parent / "io.github.dlansama.tallybar" / "contents" / "ui" / "main.qml").read_text()
    timeout = int(_re.search(r"readonly property int backendTimeoutSeconds: (\d+)", qml).group(1))
    watchdog_ms = int(_re.search(r"readonly property int refreshWatchdogMs: (\d+)", qml).group(1))
    assert '--once --timeout " + root.backendTimeoutSeconds' in qml   # one source for the flag
    assert "interval: root.refreshWatchdogMs" in qml
    startup_margin_s = 5
    assert watchdog_ms >= (backend.refresh_worst_case_seconds(timeout) + startup_margin_s) * 1000


@pytest.mark.asyncio
async def test_build_snapshot_respects_worst_case_bound():
    """Every phase stalls far past its timeout: the refresh still returns within
    refresh_worst_case_seconds — the guarantee the widget's watchdog is sized against."""
    import asyncio as _asyncio
    import time as _time
    args = MagicMock()
    args.no_network = False
    args.timeout = 0.3

    async def slow_cookies(timeout, background=False):
        await _asyncio.sleep(30)

    async def slow_threaded(func, *a, **k):
        await _asyncio.sleep(30)

    async def failed_codex(timeout):
        return {"status": "api-error", "label": "Codex", "limits": []}

    async def slow_openai(cookies, timeout):
        await _asyncio.sleep(30)

    async def slow_claude(cookies, timeout, prev=None):
        await _asyncio.sleep(30)

    async def noop_pricing(*a, **k):
        return

    started = _time.monotonic()
    with patch("backend.load_config", return_value={}), \
         patch("backend.load_snapshot", return_value={}), \
         patch("backend.collect_browser_sessions", side_effect=slow_cookies), \
         patch("backend.run_threaded_provider", side_effect=slow_threaded), \
         patch("backend.run_codex_rpc", side_effect=failed_codex), \
         patch("backend.run_openai_cookie_api", side_effect=slow_openai), \
         patch("backend.run_claude_api", side_effect=slow_claude), \
         patch("backend.compute_local_cost_summaries", side_effect=lambda deadline=None: {}), \
         patch("pricing_data.refresh_pricing", noop_pricing):
        snap = await backend.build_snapshot(args)
    elapsed = _time.monotonic() - started
    assert snap["ok"] is True
    assert elapsed <= backend.refresh_worst_case_seconds(args.timeout) + 0.5, elapsed



# --- The process scan only considers the current user's processes ---

def test_process_scan_skips_other_users_language_servers():
    """/proc/<pid>/cmdline is world-readable: another user's language server (and its CSRF
    token) must not be picked up on a shared machine."""
    mine = "language_server\0--app_data_dir=antigravity\0--csrf_token\0MINE\0"
    theirs = "language_server\0--app_data_dir=antigravity\0--csrf_token\0THEIRS\0"
    cmdlines = {"/proc/100/cmdline": mine, "/proc/200/cmdline": theirs}
    real_open = open

    def scoped_open(path, *args, **kwargs):
        if str(path) in cmdlines:
            return mock_open(read_data=cmdlines[str(path)])()
        return real_open(path, *args, **kwargs)

    with patch.dict(sys.modules, {"psutil": None}), \
            patch("os.listdir", return_value=["100", "200"]), \
            patch("builtins.open", side_effect=scoped_open), \
            patch.object(providers.antigravity, "_owned_by_current_user", side_effect=lambda pid: pid == 100):
        providers.antigravity._reset_process_scan_cache()
        found = providers.antigravity.find_antigravity_processes()
    providers.antigravity._reset_process_scan_cache()
    assert found == [(100, "MINE", "http")]


def test_owned_by_current_user_real_proc():
    import os as _os
    assert providers.antigravity._owned_by_current_user(_os.getpid()) is True
    assert providers.antigravity._owned_by_current_user(1) is (_os.getuid() == 0)   # init: root's
    assert providers.antigravity._owned_by_current_user(2**22 + 12345) is False     # no such pid
