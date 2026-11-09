"""The Claude Code sign-in (OAuth) fallback: reads the Claude limits with the token Claude
Code keeps when the cookie path has none. Pins the request, the never-renew / never-read-the-
refresh-token rules, the status mapping, the merge rule, the ordering in build_snapshot, and
that no secret reaches the output."""
import datetime as dt
import json
import time
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import backend
import http_helpers
import providers.claude as claude_mod

FIXTURE = Path(__file__).parent / "fixtures" / "claude_oauth_usage.json"
TOKEN = "test-access-token-0000"
REFRESH = "test-refresh-token-0000"
FAILED = {"label": "Claude", "status": "missing-cookies", "limits": [], "message": "No cookies",
          "accentColor": "#c07f63"}


def _payload():
    """The recorded response shape with live dates (the pace detail reads the clock)."""
    reset = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=3)).isoformat()
    return json.loads(FIXTURE.read_text().replace("RESET_WEEK", reset))


def _write_creds(expires_in=3600.0, tier="default_claude_max_20x", **extra):
    path = Path.home() / ".claude" / ".credentials.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    oauth = {"accessToken": TOKEN, "refreshToken": REFRESH,
             "expiresAt": int((time.time() + expires_in) * 1000),
             "subscriptionType": "max", "rateLimitTier": tier, **extra}
    path.write_text(json.dumps({"claudeAiOauth": oauth, "mcpOAuth": {}}))
    return path


class _Http:
    """Stands in for http_json_async and records every call."""

    def __init__(self, status=200, data=None, exc=None):
        self.status, self.data, self.exc, self.calls = status, data, exc, []

    async def __call__(self, url, jar, timeout, method="GET", body=None, headers=None):
        self.calls.append({"url": url, "method": method, "headers": dict(headers or {})})
        if self.exc is not None:
            raise self.exc
        return self.status, self.data if self.data is not None else _payload()


async def _run(http, prev=None):
    with patch.object(claude_mod, "http_json_async", http):
        return await claude_mod.run_claude_oauth(5.0, prev)


# --- the request and the reading ---------------------------------------------------

@pytest.mark.asyncio
async def test_reads_limits_with_the_bearer_and_beta_header():
    _write_creds()
    http = _Http()
    res = await _run(http)
    call = http.calls[0]
    assert call["url"] == "https://api.anthropic.com/api/oauth/usage" and call["method"] == "GET"
    assert call["headers"] == {"Authorization": f"Bearer {TOKEN}", "anthropic-beta": "oauth-2025-04-20"}
    assert res["status"] == "ok" and res["source"] == "claude-oauth"
    assert res["tier"] == "Ultra"                      # rateLimitTier, not the ambiguous "max"
    assert [(r["label"], r["percent"]) for r in res["limits"]] == [("Weekly", 64.0), ("Fable", 21.0)]
    assert res["limits"][0]["sublabel"] == "Claude Code 94% · Other 6%"
    assert not any(r["label"] == "Session" for r in res["limits"])   # Ultra has no 5-hour window
    assert dt.datetime.fromisoformat(res["fetchedAt"]).tzinfo is not None   # fetch time, not a capture time


@pytest.mark.asyncio
async def test_no_sign_in_on_this_machine_means_nothing_to_try():
    http = _Http()
    assert await _run(http) == {}
    assert http.calls == []


# --- the token is never renewed ----------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("expires_in", [-3600, 0, 30, 60])
async def test_expired_or_nearly_expired_token_is_not_sent(expires_in):
    _write_creds(expires_in=expires_in)
    http = _Http()
    res = await _run(http)
    assert http.calls == []
    assert res["status"] == "unauthorized" and "open Claude Code" in res["message"]


@pytest.mark.asyncio
async def test_token_just_outside_the_margin_is_sent():
    _write_creds(expires_in=120)
    http = _Http()
    assert (await _run(http))["status"] == "ok"


@pytest.mark.asyncio
async def test_refresh_token_is_never_read_and_the_file_is_never_written():
    path = _write_creds()
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    touched = []

    class Recording(dict):
        def __getitem__(self, key):
            touched.append(key)
            return super().__getitem__(key)

        def get(self, key, default=None):
            touched.append(key)
            return super().get(key, default)

        def __iter__(self):
            touched.append("<iter>")
            return super().__iter__()

        def items(self):
            touched.append("<items>")
            return super().items()

        def values(self):
            touched.append("<values>")
            return super().values()

    real_loads = json.loads
    http = _Http(data=_payload())   # built first: only the credentials read is recorded
    with patch.object(claude_mod.json, "loads", lambda s, **k: real_loads(s, object_hook=Recording)):
        res = await _run(http)
    assert res["status"] == "ok"
    assert "refreshToken" not in touched and not {"<iter>", "<items>", "<values>"} & set(touched)
    assert before == (path.read_bytes(), path.stat().st_mtime_ns)


# --- status mapping ----------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_token_is_unauthorized_with_the_renew_hint(status):
    _write_creds()
    res = await _run(_Http(status, {"type": "error", "error": {"message": f"leaked {TOKEN}"}}))
    assert res["status"] == "unauthorized" and "open Claude Code" in res["message"]
    assert TOKEN not in json.dumps(res)


@pytest.mark.asyncio
async def test_429_backs_off_and_the_next_refresh_does_not_call():
    _write_creds()
    res = await _run(_Http(429, {http_helpers.RETRY_AFTER_KEY: 120}))
    assert res["status"] == "api-error" and "rate-limiting" in res["message"]
    until = dt.datetime.fromisoformat(res[claude_mod.OAUTH_RATE_LIMIT_KEY])
    assert 60 <= (until - dt.datetime.now(dt.timezone.utc)).total_seconds() <= 130
    http = _Http()
    again = await _run(http, prev={claude_mod.OAUTH_RATE_LIMIT_KEY: res[claude_mod.OAUTH_RATE_LIMIT_KEY]})
    assert http.calls == [] and again["status"] == "api-error"
    assert again[claude_mod.OAUTH_RATE_LIMIT_KEY] == res[claude_mod.OAUTH_RATE_LIMIT_KEY]


@pytest.mark.asyncio
async def test_the_claude_ai_backoff_does_not_stop_the_oauth_route():
    _write_creds()
    stamp = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)).isoformat()
    res = await _run(_Http(), prev={"rateLimitedUntil": stamp})
    assert res["status"] == "ok"


@pytest.mark.asyncio
async def test_other_http_errors_and_empty_bodies():
    _write_creds()
    assert (await _run(_Http(500, {})))["message"] == "Claude usage API returned HTTP 500"
    assert (await _run(_Http(302, {})))["status"] == "api-error"
    assert (await _run(_Http(200, {"seven_day": None, "five_hour": None})))["status"] == "api-empty"
    assert (await _run(_Http(200, {"text": "<html>"})))["status"] == "api-empty"


@pytest.mark.asyncio
async def test_timeouts_and_failures_use_fixed_messages():
    _write_creds()
    res = await _run(_Http(exc=TimeoutError(f"timed out {TOKEN}")))
    assert res["status"] == "timeout" and TOKEN not in json.dumps(res)
    res = await _run(_Http(exc=urllib.error.URLError(TimeoutError())))
    assert res["status"] == "timeout"
    res = await _run(_Http(exc=RuntimeError(f"Authorization: Bearer {TOKEN} {REFRESH}")))
    assert res["status"] == "api-error" and res["message"] == "Claude usage API request failed"
    assert TOKEN not in json.dumps(res) and REFRESH not in json.dumps(res)


@pytest.mark.asyncio
async def test_secrets_never_appear_in_any_result():
    _write_creds()
    for http in (_Http(), _Http(401, {}), _Http(429, {}), _Http(500, {"error": TOKEN}),
                 _Http(exc=RuntimeError(TOKEN))):
        blob = json.dumps(await _run(http))
        assert TOKEN not in blob and REFRESH not in blob and "Bearer" not in blob


# --- the transport refuses redirects with a bearer ---------------------------------

def test_http_json_refuses_redirects_only_for_bearer_requests():
    seen = []

    class Opener:
        def open(self, request, timeout):
            raise urllib.error.URLError("stop")

    def fake_build_opener(*handlers):
        seen.append(handlers)
        return Opener()

    with patch.object(http_helpers.urllib.request, "build_opener", side_effect=fake_build_opener):
        for headers in ({"Authorization": "Bearer t"}, None):
            with pytest.raises(urllib.error.URLError):
                http_helpers.http_json("https://x", http_helpers.CookieJar(), 1.0, headers=headers)
    assert any(isinstance(h, http_helpers._RefuseRedirects) for h in seen[0])
    assert not any(isinstance(h, http_helpers._RefuseRedirects) for h in seen[1])


# --- the merge rule ----------------------------------------------------------------

def _ok_reading():
    return {"label": "Claude", "status": "ok", "source": "claude-oauth", "message": "Read Claude Code sign-in quota",
            "limits": [{"label": "Weekly", "percent": 64.0}], "fetchedAt": "2026-11-08T12:00:00+00:00",
            "tier": "Ultra"}


def test_good_reading_replaces_a_failed_cookie_provider():
    out = claude_mod.apply_claude_oauth_fallback(
        {**FAILED, "rateLimitedUntil": "2099-01-01T00:00:00+00:00", "creditBalance": {"amount": 5}}, _ok_reading())
    assert out["status"] == "ok" and out["source"] == "claude-oauth" and out["tier"] == "Ultra"
    assert out["accentColor"] == "#c07f63" and out["limits"][0]["percent"] == 64.0
    assert "claude.ai: No cookies" in out["message"]
    # the claude.ai backoff must outlive the replacement, or the next refresh polls through it
    assert out["rateLimitedUntil"] == "2099-01-01T00:00:00+00:00" and out["creditBalance"] == {"amount": 5}
    assert "stale" not in out


def test_good_reading_replaces_a_carried_last_good_one_and_never_a_live_one():
    carried = {"label": "Claude", "status": "ok", "stale": True, "staleAsOf": "x",
               "limits": [{"label": "Weekly", "percent": 1.0}]}
    out = claude_mod.apply_claude_oauth_fallback(carried, _ok_reading())
    assert out["source"] == "claude-oauth" and "stale" not in out and "claude.ai" not in out["message"]
    live = {"label": "Claude", "status": "ok", "limits": [{"label": "Weekly", "percent": 1.0}]}
    assert claude_mod.apply_claude_oauth_fallback(live, _ok_reading()) is live
    assert not claude_mod.claude_needs_oauth(live) and claude_mod.claude_needs_oauth(carried)


def test_no_cookies_and_an_expired_sign_in_points_at_claude_code():
    oauth = {"status": "unauthorized", "message": "Claude Code sign-in expired; open Claude Code to renew it"}
    out = claude_mod.apply_claude_oauth_fallback(dict(FAILED), oauth)
    assert out["status"] == "unauthorized" and out["message"] == oauth["message"]


@pytest.mark.parametrize("status", ["wallet-locked", "wallet-state-unknown", "unauthorized", "api-error", "timeout"])
def test_a_failed_reading_never_hides_the_cookie_paths_own_state(status):
    failed = {**FAILED, "status": status}
    oauth = {"status": "unauthorized", "message": "Claude Code sign-in expired; open Claude Code to renew it"}
    assert claude_mod.apply_claude_oauth_fallback(dict(failed), oauth) == failed
    assert claude_mod.apply_claude_oauth_fallback(dict(failed), {"status": "api-error", "message": "x"}) == failed


def test_a_rate_limited_reading_only_keeps_its_stamp():
    oauth = {"status": "api-error", claude_mod.OAUTH_RATE_LIMIT_KEY: "2099-01-01T00:00:00+00:00"}
    out = claude_mod.apply_claude_oauth_fallback(dict(FAILED), oauth)
    assert out == {**FAILED, claude_mod.OAUTH_RATE_LIMIT_KEY: "2099-01-01T00:00:00+00:00"}


def test_nothing_to_try_leaves_the_provider_alone():
    assert claude_mod.apply_claude_oauth_fallback(FAILED, {}) is FAILED


# --- build_snapshot wiring ---------------------------------------------------------

async def _snapshot(claude_result, oauth_result, no_network=False, config=None):
    config = {"claudeSignInFallback": True} if config is None else config
    args = MagicMock()
    args.no_network = no_network
    args.timeout = 0.5
    oauth_calls = []

    async def fast_threaded(func, *a, **k):
        return {"status": "ok", "label": "Gemini", "limits": []}

    async def fake_codex_rpc(timeout):
        return {"status": "ok", "label": "Codex", "limits": []}

    async def fake_claude(cookies, timeout, prev=None):
        return dict(claude_result)

    async def fake_oauth(timeout, prev=None):
        oauth_calls.append(prev)
        return dict(oauth_result)

    async def noop_pricing(*a, **k):
        return

    with patch("backend.load_config", return_value=config), \
         patch("backend.load_snapshot", return_value={}), \
         patch("backend.collect_browser_sessions", return_value=([], {"kwallet": {"status": "ok"}})), \
         patch("backend.run_threaded_provider", side_effect=fast_threaded), \
         patch("backend.run_codex_rpc", side_effect=fake_codex_rpc), \
         patch("backend.run_claude_api", side_effect=fake_claude), \
         patch("backend.run_claude_oauth", side_effect=fake_oauth), \
         patch("backend.compute_local_cost_summaries", side_effect=lambda deadline=None: {}), \
         patch("pricing_data.refresh_pricing", noop_pricing):
        snap = await backend.build_snapshot(args)
    return snap, oauth_calls


def _statusline_capture():
    resets = dt.datetime.now(dt.timezone.utc).timestamp() + 7200
    claude_mod.CLAUDE_STATUSLINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    claude_mod.CLAUDE_STATUSLINE_PATH.write_text(json.dumps({
        "capturedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "rateLimits": {"five_hour": {"usedPercent": 33.0, "resetsAt": resets}}}))


@pytest.mark.asyncio
async def test_oauth_comes_before_the_statusline_capture():
    _statusline_capture()
    snap, calls = await _snapshot(FAILED, _ok_reading())
    claude = snap["providers"]["claude"]
    assert claude["source"] == "claude-oauth" and claude["limits"][0]["percent"] == 64.0
    assert "formattedUsedText" in claude["limits"][0] and len(calls) == 1


@pytest.mark.asyncio
async def test_statusline_still_covers_a_failed_oauth_route():
    _statusline_capture()
    snap, _ = await _snapshot(FAILED, {"status": "unauthorized", "message": "Claude Code sign-in expired; open Claude Code to renew it"})
    assert snap["providers"]["claude"]["source"] == "claude-statusline"


@pytest.mark.asyncio
async def test_a_live_cookie_reading_never_asks_the_oauth_route():
    live = {"label": "Claude", "status": "ok", "source": "browser-api", "limits": [{"label": "Weekly", "percent": 5.0}]}
    snap, calls = await _snapshot(live, _ok_reading())
    assert snap["providers"]["claude"]["source"] == "browser-api" and calls == []


@pytest.mark.asyncio
async def test_no_network_never_asks_the_oauth_route():
    snap, calls = await _snapshot(FAILED, _ok_reading(), no_network=True)
    assert calls == [] and snap["providers"]["claude"]["source"] != "claude-oauth"


@pytest.mark.asyncio
async def test_expired_sign_in_without_cookies_surfaces_in_the_snapshot():
    snap, _ = await _snapshot(FAILED, {"status": "unauthorized", "message": "Claude Code sign-in expired; open Claude Code to renew it"})
    claude = snap["providers"]["claude"]
    assert claude["status"] == "unauthorized" and "open Claude Code" in claude["message"]


@pytest.mark.asyncio
async def test_sign_in_fallback_is_off_until_enabled():
    for config in ({}, {"claudeSignInFallback": False}, {"claudeSignInFallback": "yes"}):
        snap, calls = await _snapshot(FAILED, _ok_reading(), config=config)
        assert calls == [] and snap["providers"]["claude"].get("source") != "claude-oauth"


def test_sign_in_fallback_setting_is_a_validated_boolean():
    assert backend.public_config({})["claudeSignInFallback"] is False
    assert backend.update_config_values({"claudeSignInFallback": True})["claudeSignInFallback"] is True
    assert backend.public_config(backend.load_config())["claudeSignInFallback"] is True
    assert backend.update_config_values({"claudeSignInFallback": "yes"})["claudeSignInFallback"] is False
