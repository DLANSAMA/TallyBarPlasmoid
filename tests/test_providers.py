"""Unit tests for the provider-orchestration functions.

These exercise the per-provider fetch paths in ``providers/`` — the missing-cookie
guards, the success paths (with all network I/O mocked), and the auth/error branches.
Everything external (HTTP, subprocess, /proc, OAuth creds) is mocked, so the tests are
fully deterministic and offline.
"""
import datetime as dt
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# Mirror the existing tests: prepend the backend code dir so `import providers` works
# the same way Plasma runs the script from that directory.
sys.path.insert(0, str(Path(__file__).parent.parent / "io.github.dlansama.tallybar" / "contents" / "code"))

import providers  # noqa: E402
from cookies import BrowserCookie  # noqa: E402


def _cookie(host: str, name: str = "sess", value: str = "abc") -> BrowserCookie:
    """Build a minimal BrowserCookie whose host will match a provider domain."""
    return BrowserCookie(
        browser="Chrome",
        profile="Default",
        host=host,
        name=name,
        path="/",
        value=value,
        secure=True,
        expires_utc=0,
    )


# ---------------------------------------------------------------------------
# run_openai_cookie_api (providers/codex.py)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_openai_cookie_api_missing_cookies():
    # No chatgpt.com/openai.com cookie -> missing_cookie_provider shape.
    res = await providers.codex.run_openai_cookie_api([_cookie("claude.ai")], timeout=1.0)
    assert res["status"] == "missing-cookies"
    assert res["label"] == "Codex"
    assert res["limits"] == []


@pytest.mark.asyncio
async def test_openai_cookie_api_success():
    # A usage payload extract_limits_from_json can read: usagePercent -> a limit row.
    usage = {"primary": {"usagePercent": 42, "reset": "in 3 hours"}}
    with patch("providers.codex.http_json_async", return_value=(200, usage)):
        res = await providers.codex.run_openai_cookie_api([_cookie("chatgpt.com")], timeout=1.0)
    assert res["status"] == "ok"
    assert res["source"] == "browser-api"
    assert any(abs(lim["percent"] - 42.0) < 0.01 for lim in res["limits"])


@pytest.mark.asyncio
async def test_openai_cookie_api_empty():
    # 200 but no recognizable limits -> api-empty.
    with patch("providers.codex.http_json_async", return_value=(200, {"unrelated": True})):
        res = await providers.codex.run_openai_cookie_api([_cookie("openai.com")], timeout=1.0)
    assert res["status"] == "api-empty"
    assert res["limits"] == []


@pytest.mark.asyncio
async def test_openai_cookie_api_unauthorized():
    with patch("providers.codex.http_json_async", return_value=(401, {})):
        res = await providers.codex.run_openai_cookie_api([_cookie("chatgpt.com")], timeout=1.0)
    assert res["status"] == "unauthorized"
    assert "401" in res["message"]


@pytest.mark.asyncio
async def test_openai_cookie_api_server_error():
    with patch("providers.codex.http_json_async", return_value=(500, {})):
        res = await providers.codex.run_openai_cookie_api([_cookie("chatgpt.com")], timeout=1.0)
    assert res["status"] == "api-error"
    assert "500" in res["message"]


# ---------------------------------------------------------------------------
# run_codex_rpc (providers/codex.py)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_codex_rpc_missing_cli():
    # Patch shutil.which (used inside providers.codex) to report no codex binary.
    with patch("providers.codex.shutil.which", return_value=None):
        res = await providers.codex.run_codex_rpc(timeout=1.0)
    assert res["status"] == "missing-cli"
    assert res["label"] == "Codex"
    assert res["limits"] == []


class _FakeRpcChild:
    """Stand-in for JsonRpcChild: the handshake is a no-op and account/rateLimits/read
    returns a canned result, so run_codex_rpc's success mapping can be driven without a
    real codex subprocess (previously only the missing-CLI path was tested)."""
    def __init__(self, rate_limits_result):
        self._result = rate_limits_result
        self.terminated = False

    async def request(self, method, params=None, timeout=0.0):
        if method == "account/rateLimits/read":
            return {"result": self._result}
        return {"result": {}}

    def notify(self, method, params=None):
        pass

    async def drain(self):
        pass

    async def terminate(self):
        self.terminated = True


@pytest.mark.asyncio
async def test_codex_rpc_success_maps_limits_and_tier():
    result = {
        "planType": "pro",
        "rateLimits": {
            "primary": {"usedPercent": 42.0, "resetDescription": "in 3h"},
            "secondary": {"usedPercent": 7.5, "resetDescription": "in 5d"},
        },
    }
    child = _FakeRpcChild(result)

    async def _fake_start(codex, argv):
        return child

    with patch("providers.codex.shutil.which", return_value="/usr/bin/codex"), \
         patch("providers.codex.JsonRpcChild.start", side_effect=_fake_start):
        res = await providers.codex.run_codex_rpc(timeout=2.0)

    assert res["status"] == "ok"
    assert res["source"] == "codex-json-rpc"
    assert res["tier"] == "Pro"                                   # normalize_tier("pro")
    labels = [row["label"] for row in res["limits"]]
    assert "Session" in labels and "Weekly" in labels
    session = next(r for r in res["limits"] if r["label"] == "Session")
    assert session["percent"] == 42.0
    assert child.terminated is True                              # child always cleaned up


@pytest.mark.asyncio
async def test_codex_rpc_empty_rate_limits_is_api_empty():
    child = _FakeRpcChild({"rateLimits": {}})

    async def _fake_start(codex, argv):
        return child

    with patch("providers.codex.shutil.which", return_value="/usr/bin/codex"), \
         patch("providers.codex.JsonRpcChild.start", side_effect=_fake_start):
        res = await providers.codex.run_codex_rpc(timeout=2.0)
    assert res["status"] == "api-empty" and res["limits"] == []


class _FakeRpcChildRpcError:
    """Stand-in for JsonRpcChild whose account/rateLimits/read reply is a JSON-RPC
    error shape ({"error": {...}}) rather than a result — regression test for the
    'error' field never being inspected (a real app-server error was silently
    reported as status "api-empty")."""
    def __init__(self, error):
        self._error = error
        self.terminated = False

    async def request(self, method, params=None, timeout=0.0):
        if method == "account/rateLimits/read":
            return {"error": self._error}
        return {"result": {}}

    def notify(self, method, params=None):
        pass

    async def drain(self):
        pass

    async def terminate(self):
        self.terminated = True


@pytest.mark.asyncio
async def test_codex_rpc_error_shape_maps_to_api_error():
    child = _FakeRpcChildRpcError({"code": -32000, "message": "internal server error"})

    async def _fake_start(codex, argv):
        return child

    with patch("providers.codex.shutil.which", return_value="/usr/bin/codex"), \
         patch("providers.codex.JsonRpcChild.start", side_effect=_fake_start):
        res = await providers.codex.run_codex_rpc(timeout=2.0)

    assert res["status"] == "api-error"
    assert "internal server error" in res["message"]
    assert res["limits"] == []
    assert child.terminated is True


@pytest.mark.asyncio
async def test_codex_rpc_error_shape_unauthenticated_maps_to_unauthorized():
    child = _FakeRpcChildRpcError({"code": -32001, "message": "Not authenticated"})

    async def _fake_start(codex, argv):
        return child

    with patch("providers.codex.shutil.which", return_value="/usr/bin/codex"), \
         patch("providers.codex.JsonRpcChild.start", side_effect=_fake_start):
        res = await providers.codex.run_codex_rpc(timeout=2.0)

    assert res["status"] == "unauthorized"
    assert child.terminated is True


# ---------------------------------------------------------------------------
# run_claude_api (providers/claude.py)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_claude_api_missing_cookies():
    res = await providers.claude.run_claude_api([_cookie("chatgpt.com")], timeout=1.0)
    assert res["status"] == "missing-cookies"
    assert res["label"] == "Claude"


@pytest.mark.asyncio
async def test_claude_api_success():
    # http_json_async is called repeatedly: first the /organizations list, then the
    # per-org credits/overage/usage endpoints. side_effect feeds each call in order.
    orgs = [{"uuid": "org-1", "rate_limit_tier": "claude_pro"}]
    five_hour_usage = {"five_hour": {"utilization": 37, "resets_at": "2099-01-01T00:00:00Z"}}

    def side_effect(url, jar, timeout):
        if url.endswith("/organizations"):
            return (200, orgs)
        if url.endswith("/prepaid/credits"):
            return (404, {})
        if url.endswith("/overage_spend_limit"):
            return (404, {})
        if url.endswith("/usage_summary"):
            return (200, five_hour_usage)
        return (404, {})

    with patch("providers.claude.http_json_async", side_effect=side_effect):
        res = await providers.claude.run_claude_api([_cookie("claude.ai")], timeout=1.0)
    assert res["status"] == "ok"
    assert res["tier"] == "Pro"
    assert any(lim["label"] == "Session" for lim in res["limits"])


@pytest.mark.asyncio
async def test_claude_api_empty_when_no_usage():
    # Org list returns, but every usage endpoint is empty -> api-empty.
    orgs = [{"uuid": "org-1"}]

    def side_effect(url, jar, timeout):
        if url.endswith("/organizations"):
            return (200, orgs)
        return (404, {})

    with patch("providers.claude.http_json_async", side_effect=side_effect):
        res = await providers.claude.run_claude_api([_cookie("claude.ai")], timeout=1.0)
    assert res["status"] == "api-empty"
    assert res["limits"] == []


@pytest.mark.asyncio
async def test_claude_api_unauthorized():
    # The first /organizations call is rejected -> unauthorized, no further calls.
    with patch("providers.claude.http_json_async", return_value=(403, {})):
        res = await providers.claude.run_claude_api([_cookie("claude.ai")], timeout=1.0)
    assert res["status"] == "unauthorized"
    assert "403" in res["message"]


def test_claude_tier_from_credentials_valid(tmp_path, monkeypatch):
    # subscriptionType "max" maps through normalize_tier -> "Max" (same as the API path).
    creds = tmp_path / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True)
    creds.write_text(json.dumps({"claudeAiOauth": {"subscriptionType": "max",
                                                   "accessToken": "secret"}}))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert providers.claude.claude_tier_from_credentials() == "Max"


def test_claude_tier_from_credentials_rate_limit_fallback(tmp_path, monkeypatch):
    # subscriptionType missing -> fall back to rateLimitTier ("default_claude_max_20x" -> "Max").
    creds = tmp_path / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True)
    creds.write_text(json.dumps({"claudeAiOauth": {"rateLimitTier": "default_claude_max_20x"}}))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert providers.claude.claude_tier_from_credentials() == "Max"


def test_claude_tier_from_credentials_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert providers.claude.claude_tier_from_credentials() is None


def test_claude_tier_from_credentials_malformed(tmp_path, monkeypatch):
    creds = tmp_path / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True)
    creds.write_text("{not valid json")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert providers.claude.claude_tier_from_credentials() is None


@pytest.mark.asyncio
async def test_claude_api_unauthorized_still_carries_disk_tier(tmp_path, monkeypatch):
    # A failed (unauthorized) scrape still shows a tier from the local creds file.
    creds = tmp_path / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True)
    creds.write_text(json.dumps({"claudeAiOauth": {"subscriptionType": "max"}}))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    with patch("providers.claude.http_json_async", return_value=(403, {})):
        res = await providers.claude.run_claude_api([_cookie("claude.ai")], timeout=1.0)
    assert res["status"] == "unauthorized"   # status unchanged
    assert res["tier"] == "Max"              # disk fallback stamped


@pytest.mark.asyncio
async def test_claude_api_success_tier_overrides_disk(tmp_path, monkeypatch):
    # API tier wins over the disk fallback when the scrape succeeds.
    creds = tmp_path / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True)
    creds.write_text(json.dumps({"claudeAiOauth": {"subscriptionType": "max"}}))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    orgs = [{"uuid": "org-1", "rate_limit_tier": "claude_pro"}]
    summary = {"five_hour": {"utilization": 5, "resets_at": "2099-01-01T00:00:00Z"}}

    def side_effect(url, jar, timeout):
        if url.endswith("/organizations"):
            return (200, orgs)
        if url.endswith("/usage_summary"):
            return (200, summary)
        return (404, {})

    with patch("providers.claude.http_json_async", side_effect=side_effect):
        res = await providers.claude.run_claude_api([_cookie("claude.ai")], timeout=1.0)
    assert res["status"] == "ok"
    assert res["tier"] == "Pro"   # API "Pro" beats disk "Max"


@pytest.mark.asyncio
async def test_claude_api_prefers_usage_summary_sequential_skips_usage():
    # usage_summary is PREFERRED and the /usage endpoint is now only fetched when
    # usage_summary produced NO limits. When usage_summary succeeds, /usage must NOT
    # be fetched at all (one fewer GET per refresh — rate-limit avoidance).
    orgs = [{"uuid": "org-1", "rate_limit_tier": "claude_pro"}]
    summary = {"five_hour": {"utilization": 50, "resets_at": "2099-01-01T00:00:00Z"}}
    seen: set[str] = set()

    def side_effect(url, jar, timeout):
        seen.add(url.rsplit("/", 1)[-1])
        if url.endswith("/organizations"):
            return (200, orgs)
        if url.endswith("/usage_summary"):
            return (200, summary)
        return (404, {})

    with patch("providers.claude.http_json_async", side_effect=side_effect):
        res = await providers.claude.run_claude_api([_cookie("claude.ai")], timeout=1.0)

    assert res["status"] == "ok"
    session = next(lim for lim in res["limits"] if lim["label"] == "Session")
    assert session["percent"] == 50
    # usage_summary + the two throttled endpoints were fetched; /usage was SKIPPED.
    assert {"credits", "overage_spend_limit", "usage_summary"} <= seen
    assert "usage" not in seen


@pytest.mark.asyncio
async def test_claude_api_falls_back_to_usage_when_summary_empty():
    # usage_summary returns no limits -> /usage IS fetched and used.
    orgs = [{"uuid": "org-1", "rate_limit_tier": "claude_pro"}]
    usage = {"five_hour": {"utilization": 77, "resets_at": "2099-01-01T00:00:00Z"}}
    seen: set[str] = set()

    def side_effect(url, jar, timeout):
        seen.add(url.rsplit("/", 1)[-1])
        if url.endswith("/organizations"):
            return (200, orgs)
        if url.endswith("/usage_summary"):
            return (200, {})  # no recognizable limits
        if url.endswith("/usage"):
            return (200, usage)
        return (404, {})

    with patch("providers.claude.http_json_async", side_effect=side_effect):
        res = await providers.claude.run_claude_api([_cookie("claude.ai")], timeout=1.0)

    assert res["status"] == "ok"
    session = next(lim for lim in res["limits"] if lim["label"] == "Session")
    assert session["percent"] == 77
    assert "usage" in seen  # the fallback fired


@pytest.mark.asyncio
async def test_claude_api_throttles_credit_calls_when_prev_fresh():
    # prev's creditBalance.fetchedAt < CREDIT_REFRESH_SECONDS -> credits + overage GETs
    # are SKIPPED and the prior creditBalance + Monthly row are carried forward.
    orgs = [{"uuid": "org-1", "rate_limit_tier": "claude_pro"}]
    summary = {"five_hour": {"utilization": 42, "resets_at": "2099-01-01T00:00:00Z"}}
    seen: set[str] = set()

    def side_effect(url, jar, timeout):
        seen.add(url.rsplit("/", 1)[-1])
        if url.endswith("/organizations"):
            return (200, orgs)
        if url.endswith("/usage_summary"):
            return (200, summary)
        return (404, {})

    fresh = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    prev = {
        "creditBalance": {"amount": 12.5, "fetchedAt": fresh},
        "limits": [{"label": "Monthly", "percent": 33, "usedText": "$3.30 / $10"}],
    }

    with patch("providers.claude.http_json_async", side_effect=side_effect):
        res = await providers.claude.run_claude_api([_cookie("claude.ai")], timeout=1.0, prev=prev)

    assert res["status"] == "ok"
    # D3: the warm path hits EXACTLY the org list + usage_summary — no credits/overage,
    # and no /usage fallback (usage_summary produced a limit). An EXACT-set assertion
    # (not just "credits not in seen") catches any newly-added call regressing the throttle.
    assert seen == {"organizations", "usage_summary"}
    # Prior values carried forward.
    assert res["creditBalance"]["amount"] == 12.5
    assert any(str(lim.get("label", "")).lower() == "monthly" for lim in res["limits"])


@pytest.mark.asyncio
async def test_claude_api_refetches_credit_calls_when_prev_stale():
    # prev's fetchedAt >= CREDIT_REFRESH_SECONDS -> the two GETs fire again.
    orgs = [{"uuid": "org-1", "rate_limit_tier": "claude_pro"}]
    summary = {"five_hour": {"utilization": 42, "resets_at": "2099-01-01T00:00:00Z"}}
    seen: set[str] = set()

    def side_effect(url, jar, timeout):
        seen.add(url.rsplit("/", 1)[-1])
        if url.endswith("/organizations"):
            return (200, orgs)
        if url.endswith("/usage_summary"):
            return (200, summary)
        return (404, {})

    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1000)).replace(
        microsecond=0).isoformat().replace("+00:00", "Z")
    prev = {"creditBalance": {"amount": 12.5, "fetchedAt": old}, "limits": []}

    with patch("providers.claude.http_json_async", side_effect=side_effect):
        res = await providers.claude.run_claude_api([_cookie("claude.ai")], timeout=1.0, prev=prev)

    assert res["status"] == "ok"
    assert "credits" in seen
    assert "overage_spend_limit" in seen


@pytest.mark.asyncio
async def test_claude_api_fetches_credit_calls_when_prev_none():
    # No prev -> both GETs fire (baseline behaviour, back-compatible default).
    orgs = [{"uuid": "org-1", "rate_limit_tier": "claude_pro"}]
    summary = {"five_hour": {"utilization": 42, "resets_at": "2099-01-01T00:00:00Z"}}
    seen: set[str] = set()

    def side_effect(url, jar, timeout):
        seen.add(url.rsplit("/", 1)[-1])
        if url.endswith("/organizations"):
            return (200, orgs)
        if url.endswith("/usage_summary"):
            return (200, summary)
        return (404, {})

    with patch("providers.claude.http_json_async", side_effect=side_effect):
        res = await providers.claude.run_claude_api([_cookie("claude.ai")], timeout=1.0)

    assert res["status"] == "ok"
    assert {"credits", "overage_spend_limit"} <= seen


@pytest.mark.asyncio
async def test_claude_api_throttles_when_credit_balance_unparseable():
    # A3: /prepaid/credits answers 200 but has NO parseable prepaid balance (the common
    # case for accounts without prepaid credits). The first refresh must still stamp an
    # amount-less sentinel creditBalance carrying fetchedAt, so the SECOND refresh within
    # CREDIT_REFRESH_SECONDS skips the two slow GETs entirely — and after the window they fire.
    orgs = [{"uuid": "org-1", "rate_limit_tier": "claude_pro"}]
    summary = {"five_hour": {"utilization": 42, "resets_at": "2099-01-01T00:00:00Z"}}

    def make_side_effect(seen):
        def side_effect(url, jar, timeout):
            seen.add(url.rsplit("/", 1)[-1])
            if url.endswith("/organizations"):
                return (200, orgs)
            if url.endswith("/usage_summary"):
                return (200, summary)
            # /prepaid/credits + /overage_spend_limit answer 200 with nothing parseable.
            return (200, {})
        return side_effect

    # First refresh (no prev): credits + overage ARE fetched, come back empty, and a
    # sentinel creditBalance is stamped.
    seen1: set[str] = set()
    with patch("providers.claude.parse_claude_credit_balance", return_value=None), \
         patch("providers.claude.http_json_async", side_effect=make_side_effect(seen1)):
        res1 = await providers.claude.run_claude_api([_cookie("claude.ai")], timeout=1.0)

    assert res1["status"] == "ok"
    assert {"credits", "overage_spend_limit"} <= seen1
    # The sentinel: an amount-less dict with a fresh fetchedAt (throttle gate).
    assert isinstance(res1["creditBalance"], dict)
    assert "amount" not in res1["creditBalance"]
    assert isinstance(res1["creditBalance"].get("fetchedAt"), str)

    # Second refresh within the window, feeding res1 as prev: the two slow GETs are SKIPPED.
    seen2: set[str] = set()
    with patch("providers.claude.parse_claude_credit_balance", return_value=None), \
         patch("providers.claude.http_json_async", side_effect=make_side_effect(seen2)):
        res2 = await providers.claude.run_claude_api([_cookie("claude.ai")], timeout=1.0, prev=res1)

    assert res2["status"] == "ok"
    assert "credits" not in seen2
    assert "overage_spend_limit" not in seen2

    # A prev whose sentinel is older than the window: the two GETs fire again.
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1000)).replace(
        microsecond=0).isoformat().replace("+00:00", "Z")
    stale_prev = {"creditBalance": {"fetchedAt": old}, "limits": []}
    seen3: set[str] = set()
    with patch("providers.claude.parse_claude_credit_balance", return_value=None), \
         patch("providers.claude.http_json_async", side_effect=make_side_effect(seen3)):
        res3 = await providers.claude.run_claude_api([_cookie("claude.ai")], timeout=1.0, prev=stale_prev)

    assert res3["status"] == "ok"
    assert "credits" in seen3
    assert "overage_spend_limit" in seen3


# ---------------------------------------------------------------------------
# run_gemini_web (providers/gemini.py)
# ---------------------------------------------------------------------------

def test_gemini_web_missing_cookies():
    res = providers.gemini.run_gemini_web([_cookie("claude.ai")], timeout=1.0)
    assert res["status"] == "missing-cookies"
    assert res["label"] == "Gemini"


def test_gemini_web_unauthorized():
    # http_text returns (status, body); a 401 on the usage page -> unauthorized. Force the
    # token cache empty so this hits the full-scrape path regardless of machine cache state.
    with patch("providers.gemini._load_gemini_tokens", return_value=None), \
         patch("providers.gemini.http_text", return_value=(401, "")):
        res = providers.gemini.run_gemini_web([_cookie("gemini.google.com")], timeout=1.0)
    assert res["status"] == "unauthorized"
    assert "401" in res["message"]


def test_gemini_web_missing_rpc_tokens():
    # 200 but the HTML lacks the SNlM0e/cfb2h/FdrFJe tokens -> api-empty.
    with patch("providers.gemini._load_gemini_tokens", return_value=None), \
         patch("providers.gemini.http_text", return_value=(200, "<html>no tokens here</html>")):
        res = providers.gemini.run_gemini_web([_cookie("gemini.google.com")], timeout=1.0)
    assert res["status"] == "api-empty"
    assert "RPC tokens" in res["message"]


# The logged-OUT shell of the WIZ app (observed live 2026-07-02): the page still embeds
# cfb2h/FdrFJe but NOT the auth token SNlM0e, and carries a sign-in URL. That means the
# session cookies no longer authenticate — actionable, so it must classify as
# `unauthorized` (not `api-empty`, which reads as a page-shape regression).
_SIGNED_OUT_HTML = (
    '<html><script>window.WIZ_global_data = {"cfb2h":"boq_assistantbardwebserver_1",'
    '"FdrFJe":"-813:20"};</script>'
    '<a href="https://accounts.google.com/ServiceLogin?continue=x">Sign in</a></html>'
)


def test_gemini_web_signed_out_shell_is_unauthorized():
    with patch("providers.gemini._load_gemini_tokens", return_value=None), \
         patch("providers.gemini.http_text", return_value=(200, _SIGNED_OUT_HTML)):
        res = providers.gemini.run_gemini_web([_cookie("gemini.google.com")], timeout=1.0)
    assert res["status"] == "unauthorized"
    assert "sign in to gemini.google.com" in res["message"]


def test_google_one_signed_out_shell_is_unauthorized():
    with patch("providers.gemini._load_google_one_tokens", return_value=None), \
         patch("providers.gemini.http_text", return_value=(200, _SIGNED_OUT_HTML)):
        res = providers.gemini.run_google_one_credits([_cookie("one.google.com")], timeout=1.0)
    assert res["status"] == "unauthorized"
    assert "sign in to one.google.com" in res["message"]


def test_gemini_web_fast_path_skips_html_get(monkeypatch):
    # Cached tokens + a POST that yields limits => the usage-HTML GET is skipped; limits still
    # come from the live POST.
    monkeypatch.setattr(providers.gemini, "_load_gemini_tokens", lambda: ("AT", "BL", "SID"))
    limits = [{"label": "Session", "percent": 12.0, "unit": "%"}]

    def post(jar, timeout, rpc, at, bl, sid):
        assert (at, bl, sid) == ("AT", "BL", "SID")  # cached tokens used
        return (200, "payload")

    with patch("providers.gemini.http_text",
               side_effect=AssertionError("usage-HTML GET must be skipped on a cache hit")), \
         patch("providers.gemini.post_gemini_batchexecute", side_effect=post), \
         patch("providers.gemini.parse_batchexecute_payload", return_value={"_": 1}), \
         patch("providers.gemini.parse_gemini_usage_info", return_value=limits):
        res = providers.gemini.run_gemini_web([_cookie("gemini.google.com")], timeout=1.0)

    assert res["status"] == "ok"
    assert res["limits"] == limits


def test_gemini_web_falls_back_when_cached_tokens_empty(monkeypatch):
    # Cached tokens present but the POST yields EMPTY limits (stale) => the full HTML scrape
    # runs and succeeds. An empty cached result never surfaces as a wrong/empty answer.
    monkeypatch.setattr(providers.gemini, "_load_gemini_tokens", lambda: ("OLD", "OLD", "OLD"))
    monkeypatch.setattr(providers.gemini, "_save_gemini_tokens", lambda *a: None)
    fresh = [{"label": "Weekly", "percent": 8.0, "unit": "%"}]
    gets = {"n": 0}

    def http_text(url, *a, **k):
        gets["n"] += 1
        return (200, "<html>fresh</html>")

    # First parse (cached fast-path POST) -> empty; second (scrape POST) -> fresh limits.
    parsed = iter([[], fresh])
    with patch("providers.gemini.http_text", side_effect=http_text), \
         patch("providers.gemini.extract_embedded_config_value",
               side_effect=lambda h, key: {"SNlM0e": "A", "cfb2h": "B", "FdrFJe": "S"}[key]), \
         patch("providers.gemini.post_gemini_batchexecute", return_value=(200, "payload")), \
         patch("providers.gemini.parse_batchexecute_payload", return_value={"_": 1}), \
         patch("providers.gemini.parse_gemini_usage_info", side_effect=lambda p: next(parsed)):
        res = providers.gemini.run_gemini_web([_cookie("gemini.google.com")], timeout=1.0)

    assert res["status"] == "ok"
    assert res["limits"] == fresh
    assert gets["n"] == 1  # the HTML GET ran (fallback engaged)


def test_gemini_web_full_flow_caches_then_fast_path(tmp_path, monkeypatch):
    # End-to-end: cold run scrapes HTML + persists tokens (0600 atomic); next run reads that
    # cache and skips the GET.
    monkeypatch.setattr(providers.gemini, "_GEMINI_TOKEN_CACHE", tmp_path / "g.json")
    limits = [{"label": "Session", "percent": 5.0, "unit": "%"}]

    with patch("providers.gemini.http_text", return_value=(200, "<html>cold</html>")), \
         patch("providers.gemini.extract_embedded_config_value",
               side_effect=lambda h, key: {"SNlM0e": "A", "cfb2h": "B", "FdrFJe": "S"}[key]), \
         patch("providers.gemini.post_gemini_batchexecute", return_value=(200, "payload")), \
         patch("providers.gemini.parse_batchexecute_payload", return_value={"_": 1}), \
         patch("providers.gemini.parse_gemini_usage_info", return_value=limits):
        cold = providers.gemini.run_gemini_web([_cookie("gemini.google.com")], timeout=1.0)

    assert cold["status"] == "ok"
    import json
    persisted = json.loads((tmp_path / "g.json").read_text(encoding="utf-8"))
    assert (persisted["at"], persisted["bl"], persisted["sid"]) == ("A", "B", "S")
    assert (tmp_path / "g.json").stat().st_mode & 0o777 == 0o600  # session secrets -> 0600

    with patch("providers.gemini.http_text",
               side_effect=AssertionError("GET must be skipped once tokens are cached")), \
         patch("providers.gemini.post_gemini_batchexecute", return_value=(200, "payload")), \
         patch("providers.gemini.parse_batchexecute_payload", return_value={"_": 1}), \
         patch("providers.gemini.parse_gemini_usage_info", return_value=limits):
        warm = providers.gemini.run_gemini_web([_cookie("gemini.google.com")], timeout=1.0)

    assert warm["status"] == "ok"
    assert warm["limits"] == limits


# ---------------------------------------------------------------------------
# run_google_one_credits (providers/gemini.py)
# ---------------------------------------------------------------------------

def test_google_one_credits_missing_cookies():
    # offline / no one.google.com cookies -> missing-cookies, no credit balance.
    res = providers.gemini.run_google_one_credits([_cookie("claude.ai")], timeout=1.0)
    assert res["status"] == "missing-cookies"
    assert res["creditBalance"] is None


def test_google_one_credits_empty_without_tokens():
    # Cookies match, page loads 200 but without RPC tokens -> api-empty. Force the token
    # cache empty so this deterministically exercises the full-scrape path regardless of any
    # real ~/.tallybar cache on the dev machine.
    with patch("providers.gemini._load_google_one_tokens", return_value=None), \
         patch("providers.gemini.http_text", return_value=(200, "<html></html>")):
        res = providers.gemini.run_google_one_credits([_cookie("one.google.com")], timeout=1.0)
    assert res["status"] == "api-empty"
    assert res["creditBalance"] is None


def test_google_one_credits_fast_path_skips_html_get(monkeypatch):
    # Cached tokens + a POST that yields a balance => the ~0.6s activity-HTML GET is skipped
    # entirely; the balance still comes from the live POST.
    monkeypatch.setattr(providers.gemini, "_load_google_one_tokens", lambda: ("AT", "BL", "SID"))

    def post_with_cached(jar, timeout, rpc, at, bl, sid):
        assert (at, bl, sid) == ("AT", "BL", "SID")  # the cached tokens were used
        return (200, "payload")

    with patch("providers.gemini.http_text",
               side_effect=AssertionError("activity-HTML GET must be skipped on a cache hit")), \
         patch("providers.gemini.post_google_one_batchexecute", side_effect=post_with_cached), \
         patch("providers.gemini.parse_batchexecute_payload", return_value={"_": 1}), \
         patch("providers.gemini.parse_google_one_credits", return_value={"credits": 1234, "expiration": ""}):
        res = providers.gemini.run_google_one_credits([_cookie("one.google.com")], timeout=1.0)

    assert res["status"] == "ok"
    assert res["creditBalance"]["amount"] == 1234


def test_google_one_credits_falls_back_when_cached_tokens_stale(monkeypatch):
    # Cached tokens present but the POST yields no balance (stale/expired) => the full HTML
    # scrape runs, succeeds, and refreshes the cache. A stale token never surfaces as an error.
    monkeypatch.setattr(providers.gemini, "_load_google_one_tokens", lambda: ("OLD", "OLD", "OLD"))
    saved: dict[str, str] = {}
    monkeypatch.setattr(providers.gemini, "_save_google_one_tokens",
                        lambda at, bl, sid: saved.update(at=at, bl=bl, sid=sid))

    def post(jar, timeout, rpc, at, bl, sid):
        return (200, "payload") if at == "FRESH_AT" else (401, "")  # cached OLD tokens rejected

    with patch("providers.gemini.http_text", return_value=(200, "<html>fresh</html>")), \
         patch("providers.gemini.extract_embedded_config_value",
               side_effect=lambda h, key: {"SNlM0e": "FRESH_AT", "cfb2h": "FRESH_BL", "FdrFJe": "FRESH_SID"}[key]), \
         patch("providers.gemini.post_google_one_batchexecute", side_effect=post), \
         patch("providers.gemini.parse_batchexecute_payload", return_value={"_": 1}), \
         patch("providers.gemini.parse_google_one_credits", return_value={"credits": 999, "expiration": ""}):
        res = providers.gemini.run_google_one_credits([_cookie("one.google.com")], timeout=1.0)

    assert res["status"] == "ok"
    assert res["creditBalance"]["amount"] == 999
    assert saved == {"at": "FRESH_AT", "bl": "FRESH_BL", "sid": "FRESH_SID"}  # fresh tokens cached


def test_google_one_credits_full_flow_caches_then_fast_path(tmp_path, monkeypatch):
    # End-to-end: a cold run (no cache) scrapes the HTML, persists the tokens atomically, and
    # the NEXT run reads that cache and skips the GET — the real save+load round-trip.
    monkeypatch.setattr(providers.gemini, "_GOOGLE_ONE_TOKEN_CACHE", tmp_path / "tok.json")

    with patch("providers.gemini.http_text", return_value=(200, "<html>cold</html>")), \
         patch("providers.gemini.extract_embedded_config_value",
               side_effect=lambda h, key: {"SNlM0e": "A", "cfb2h": "B", "FdrFJe": "S"}[key]), \
         patch("providers.gemini.post_google_one_batchexecute", return_value=(200, "payload")), \
         patch("providers.gemini.parse_batchexecute_payload", return_value={"_": 1}), \
         patch("providers.gemini.parse_google_one_credits", return_value={"credits": 42, "expiration": ""}):
        cold = providers.gemini.run_google_one_credits([_cookie("one.google.com")], timeout=1.0)

    assert cold["status"] == "ok"
    import json
    persisted = json.loads((tmp_path / "tok.json").read_text(encoding="utf-8"))
    assert (persisted["at"], persisted["bl"], persisted["sid"]) == ("A", "B", "S")
    assert isinstance(persisted["ts"], (int, float))
    assert (tmp_path / "tok.json").stat().st_mode & 0o777 == 0o600  # session secrets -> 0600

    # Second run: cache present and fresh -> fast path, no HTML GET.
    with patch("providers.gemini.http_text",
               side_effect=AssertionError("GET must be skipped once tokens are cached")), \
         patch("providers.gemini.post_google_one_batchexecute", return_value=(200, "payload")), \
         patch("providers.gemini.parse_batchexecute_payload", return_value={"_": 1}), \
         patch("providers.gemini.parse_google_one_credits", return_value={"credits": 42, "expiration": ""}):
        warm = providers.gemini.run_google_one_credits([_cookie("one.google.com")], timeout=1.0)

    assert warm["status"] == "ok"
    assert warm["creditBalance"]["amount"] == 42


# ---------------------------------------------------------------------------
# run_antigravity_local / run_antigravity_remote (providers/antigravity.py)
# ---------------------------------------------------------------------------

def test_antigravity_local_not_running():
    # No language-server process found -> not-running fallback (no /proc touched).
    with patch("providers.antigravity.find_antigravity_process", return_value=None):
        res = providers.antigravity.run_antigravity_local(timeout=1.0)
    assert res["status"] == "not-running"
    assert res["label"] == "Antigravity"
    assert res["limits"] == []


def test_antigravity_remote_no_credentials():
    # No OAuth credentials on disk -> the early "missing-oauth" fallback (no
    # ~/.gemini read happens because load_antigravity_oauth_credentials is mocked
    # to return an empty list).
    with patch("providers.antigravity.load_antigravity_oauth_credentials", return_value=[]):
        res = providers.antigravity.run_antigravity_remote(timeout=1.0)
    assert res["status"] == "missing-oauth"
    assert res["label"] == "Antigravity"
    assert res["limits"] == []


def test_antigravity_remote_oauth_unavailable():
    # A credential source exists but the per-credential fetch fails for every
    # source -> the loop collects failures and returns "oauth-unavailable".
    creds = ({"access_token": "tok"}, Path("/dev/null"), {}, "test-source")
    with patch("providers.antigravity.load_antigravity_oauth_credentials", return_value=[creds]), \
         patch(
             "providers.antigravity.run_antigravity_remote_with_credentials",
             side_effect=RuntimeError("boom"),
         ):
        res = providers.antigravity.run_antigravity_remote(timeout=1.0)
    assert res["status"] == "oauth-unavailable"
    assert "boom" in res["message"]
    assert res["limits"] == []


# ---------------------------------------------------------------------------
# run_antigravity_remote_with_credentials + the OAuth-refresh/Cloud-Code-API
# chain it drives (antigravity_token_expiry_seconds, refresh_antigravity_
# credentials, antigravity_bearer_json, antigravity_project_id). Only
# urllib.request.urlopen is mocked (matching test_backend.py's
# test_post_local_json_ssl_relaxed style) so the real request-construction,
# expiry, refresh and persistence logic all actually run.
# ---------------------------------------------------------------------------

_ANTIGRAVITY_QUOTA_SAMPLE = {
    "groups": [
        {
            "displayName": "Gemini Models",
            "buckets": [
                {"bucketId": "gemini-weekly", "window": "weekly",
                 "resetTime": "2099-06-28T03:18:59Z", "remainingFraction": 0.9},
                {"bucketId": "gemini-5h", "window": "5h",
                 "resetTime": "2099-06-24T01:03:34Z", "remainingFraction": 0.8},
            ],
        },
    ],
}


class _FakeHTTPResponse:
    """Minimal stand-in for the object `with urllib.request.urlopen(...) as r:` yields."""

    def __init__(self, status, payload):
        self.status = status
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self, _n=-1):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_urlopen(oauth_payload=None, oauth_raises=None,
                   load_code_assist=(200, {"currentTier": {"name": "Google AI Ultra"}}),
                   quota_summary=(200, _ANTIGRAVITY_QUOTA_SAMPLE)):
    """Build a `urllib.request.urlopen` stand-in that routes on request URL, mirroring the
    per-URL side_effect style used for run_claude_api / run_codex_rpc above."""
    def fake(request, timeout=None, context=None):
        url = request.full_url
        if "oauth2.googleapis.com/token" in url:
            if oauth_raises is not None:
                raise oauth_raises
            return _FakeHTTPResponse(200, oauth_payload or {})
        if "loadCodeAssist" in url:
            return _FakeHTTPResponse(*load_code_assist)
        if "retrieveUserQuotaSummary" in url:
            return _FakeHTTPResponse(*quota_summary)
        return _FakeHTTPResponse(404, {})
    return fake


def test_antigravity_remote_with_credentials_success(tmp_path):
    # Non-expired token -> no refresh call; loadCodeAssist + retrieveUserQuotaSummary both
    # succeed -> the quota-summary primary path wins ("ok" + limitsSource=quota-summary).
    credentials = {
        "access_token": "tok-1",
        "expiry_date": int((time.time() + 3600) * 1000),  # 1h from now, epoch-ms
    }
    with patch("urllib.request.urlopen", side_effect=_fake_urlopen()):
        res = providers.antigravity.run_antigravity_remote_with_credentials(
            credentials, tmp_path / "creds.json", {}, "test-source", timeout=1.0,
        )
    assert res["status"] == "ok"
    assert res["source"] == "test-source"
    assert res["limitsSource"] == "quota-summary"
    assert res["tier"] == "Google AI Ultra"
    assert len(res["limits"]) == 2  # one group x (5h, weekly)


def test_antigravity_remote_with_credentials_expired_token_refreshes(tmp_path):
    # Expired token -> refresh_antigravity_credentials exchanges the refresh_token for a new
    # access_token via oauth2.googleapis.com/token, persists it (save_antigravity_credentials),
    # and the Cloud Code calls that follow use the REFRESHED token.
    path = tmp_path / "creds.json"
    credentials = {
        "access_token": "old-tok",
        "refresh_token": "refresh-abc",
        "expiry_date": int((time.time() - 100) * 1000),  # already expired
        "client_id": "111111111111-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.apps.googleusercontent.com",
        "client_secret": "GOCSPX-" + "a" * 28,
    }
    seen_auth_headers = []
    fake = _fake_urlopen(oauth_payload={"access_token": "new-tok", "expires_in": 3600, "token_type": "Bearer"})

    def wrapped(request, timeout=None, context=None):
        auth = request.get_header("Authorization")
        if auth:
            seen_auth_headers.append(auth)
        return fake(request, timeout=timeout, context=context)

    with patch("urllib.request.urlopen", side_effect=wrapped):
        res = providers.antigravity.run_antigravity_remote_with_credentials(
            credentials, path, {}, "test-source", timeout=1.0,
        )
    assert res["status"] == "ok"
    # The Cloud Code calls made AFTER the refresh must carry the new token, not the old one.
    assert "Bearer new-tok" in seen_auth_headers
    assert "Bearer old-tok" not in seen_auth_headers
    # refresh_antigravity_credentials persisted the refreshed credentials via
    # save_antigravity_credentials -> atomic_write_text (real disk write to tmp_path).
    assert "new-tok" in path.read_text(encoding="utf-8")


def test_antigravity_remote_with_credentials_refresh_failure(tmp_path):
    # Expired token, and the refresh POST itself fails for every OAuth client pair
    # (network error) -> refresh_antigravity_credentials returns None -> "oauth-expired",
    # without ever reaching the Cloud Code API calls.
    credentials = {
        "access_token": "old-tok",
        "refresh_token": "refresh-abc",
        "expiry_date": int((time.time() - 100) * 1000),
        "client_id": "111111111111-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.apps.googleusercontent.com",
        "client_secret": "GOCSPX-" + "a" * 28,
    }
    fake = _fake_urlopen(oauth_raises=OSError("network down"))
    with patch("urllib.request.urlopen", side_effect=fake):
        res = providers.antigravity.run_antigravity_remote_with_credentials(
            credentials, tmp_path / "creds.json", {}, "test-source", timeout=1.0,
        )
    assert res["status"] == "oauth-expired"
    assert "test-source" in res["message"]
    assert not (tmp_path / "creds.json").exists()  # never reached the persist step


def test_antigravity_token_expiry_seconds_variants():
    # Direct unit coverage of the pure helper: epoch seconds, epoch milliseconds, and
    # ISO-8601 (with trailing Z and over-precise fractional seconds) all resolve to the
    # same instant; missing/unparseable input is None.
    now = time.time()
    assert abs(providers.antigravity.antigravity_token_expiry_seconds({"expiry": now}) - now) < 1
    ms = now * 1000
    assert abs(providers.antigravity.antigravity_token_expiry_seconds({"expiry_date": ms}) - now) < 1
    iso = dt.datetime.fromtimestamp(now, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")
    assert abs(providers.antigravity.antigravity_token_expiry_seconds({"expiryDate": iso}) - now) < 2
    assert providers.antigravity.antigravity_token_expiry_seconds({}) is None
    assert providers.antigravity.antigravity_token_expiry_seconds({"expiry": "not-a-date"}) is None


def test_antigravity_project_id_variants():
    assert providers.antigravity.antigravity_project_id("nope") is None
    assert providers.antigravity.antigravity_project_id({}) is None
    assert providers.antigravity.antigravity_project_id({"cloudaicompanionProject": "proj-1"}) == "proj-1"
    assert providers.antigravity.antigravity_project_id(
        {"cloudaicompanionProject": {"id": "proj-2"}}) == "proj-2"
    assert providers.antigravity.antigravity_project_id(
        {"cloudaicompanionProject": {"projectId": "proj-3"}}) == "proj-3"


def test_antigravity_oauth_pairs_from_data():
    # Single client id + secret -> exactly that pair.
    data = b"111111111111-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.apps.googleusercontent.com " + \
        b"GOCSPX-" + b"a" * 28
    pairs = providers.antigravity.antigravity_oauth_pairs_from_data(data)
    assert pairs == [
        ("111111111111-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.apps.googleusercontent.com", "GOCSPX-" + "a" * 28)
    ]
    # No secret at all -> no pairs (both lists must be non-empty).
    assert providers.antigravity.antigravity_oauth_pairs_from_data(
        b"111111111111-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.apps.googleusercontent.com") == []


# ---------------------------------------------------------------------------
# apply_google_one_credits (providers/antigravity.py)
# Replaces Antigravity's misleading Code Assist prompt/flow "credits" with the
# real Google One AI credit pool, and DROPS the "Monthly credit pool" lane.
# ---------------------------------------------------------------------------

def test_apply_google_one_credits_replaces_pool_and_sets_balance():
    # [AUDIT-4a] With a Google One result carrying a creditBalance dict, the
    # provider's creditBalance is overwritten from Google One and the
    # misleading "Monthly credit pool" lane is stripped from limits.
    antigravity = {
        "label": "Antigravity",
        "status": "ok",
        "creditBalance": {"source": "antigravity-plan-status", "remaining": 500},
        "limits": [
            {"label": "Gemini", "percent": 10, "reset": "Daily"},
            {"label": "Credits", "percent": 99, "reset": "Monthly credit pool"},
        ],
    }
    google_one = {
        "creditBalance": {"source": "google-one", "remaining": 12345, "total": 50000},
    }
    providers.antigravity.apply_google_one_credits(antigravity, google_one)

    # Balance comes from Google One, not the stale plan-status figure.
    assert antigravity["creditBalance"] == {
        "source": "google-one",
        "remaining": 12345,
        "total": 50000,
    }
    # The "Monthly credit pool" lane is gone; the unrelated Gemini lane survives.
    resets = [lane.get("reset") for lane in antigravity["limits"]]
    assert "Monthly credit pool" not in resets
    assert antigravity["limits"] == [
        {"label": "Gemini", "percent": 10, "reset": "Daily"},
    ]


def test_apply_google_one_credits_none_clears_plan_status_balance():
    # [AUDIT-4b] Documented offline behaviour: when Google One could not be
    # fetched (google_one is None), an existing plan-status creditBalance is
    # dropped rather than left showing the wrong prompt/flow figure — and the
    # "Monthly credit pool" lane is still removed.
    antigravity = {
        "label": "Antigravity",
        "status": "ok",
        "creditBalance": {"source": "antigravity-plan-status", "remaining": 500},
        "limits": [
            {"label": "Gemini", "percent": 10, "reset": "Daily"},
            {"label": "Credits", "percent": 99, "reset": "Monthly credit pool"},
        ],
    }
    providers.antigravity.apply_google_one_credits(antigravity, None)

    # The stale plan-status balance is cleared entirely.
    assert "creditBalance" not in antigravity
    # The pool lane is dropped even with no Google One replacement.
    assert antigravity["limits"] == [
        {"label": "Gemini", "percent": 10, "reset": "Daily"},
    ]


def test_apply_google_one_credits_none_keeps_non_plan_status_balance():
    # Guards the source-gated clear: a creditBalance that is NOT from
    # "antigravity-plan-status" must survive a None Google One result (only the
    # misleading plan-status figure is the one we drop when offline).
    antigravity = {
        "label": "Antigravity",
        "status": "ok",
        "creditBalance": {"source": "google-one", "remaining": 777},
        "limits": [{"label": "Credits", "percent": 99, "reset": "Monthly credit pool"}],
    }
    providers.antigravity.apply_google_one_credits(antigravity, None)

    assert antigravity["creditBalance"] == {"source": "google-one", "remaining": 777}
    # The pool lane is still removed regardless of the balance source.
    assert antigravity["limits"] == []


# ---------------------------------------------------------------------------
# run_grok_local (providers/grok.py)
# ---------------------------------------------------------------------------

def test_run_grok_local_not_running(tmp_path):
    from providers import grok as grok_mod
    res = grok_mod.run_grok_local(timeout=1.0, grok_home=tmp_path / "absent", log_path=tmp_path / "nope")
    assert res["status"] == "not-running"
    assert res["limits"] == []


def test_run_grok_local_ok_from_log(tmp_path):
    from providers import grok as grok_mod
    home = tmp_path / ".grok"
    logs = home / "logs"
    logs.mkdir(parents=True)
    (home / "auth.json").write_text("{}", encoding="utf-8")
    line = json.dumps({
        "ts": "2026-07-10T12:00:00Z",
        "msg": "billing: fetched credits config",
        "ctx": {
            "config": {
                "creditUsagePercent": 42.5,
                "currentPeriod": {
                    "type": "USAGE_PERIOD_TYPE_WEEKLY",
                    "end": "2030-07-11T00:00:00+00:00",
                },
            },
            "subscriptionTier": "SuperGrok",
        },
    })
    (logs / "unified.jsonl").write_text(line + "\n", encoding="utf-8")
    res = grok_mod.run_grok_local(timeout=1.0, grok_home=home, log_path=logs / "unified.jsonl")
    assert res["status"] == "ok"
    assert res["tier"] == "SuperGrok"
    assert res["limits"][0]["percent"] == 42.5
    assert res["limits"][0]["label"] == "Weekly"


def test_run_grok_local_finds_billing_buried_under_large_tail(tmp_path):
    """Billing is logged at session start; multi-MB of later tool noise must not hide it.
    The reverse chunked scan has to walk past a large non-billing tail."""
    from providers import grok as grok_mod
    home = tmp_path / ".grok"
    logs = home / "logs"
    logs.mkdir(parents=True)
    billing = json.dumps({
        "ts": "2026-07-10T12:00:00Z",
        "msg": "billing: fetched credits config",
        "ctx": {
            "config": {
                "creditUsagePercent": 33.0,
                "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY",
                                  "end": "2030-07-11T00:00:00+00:00"},
            },
            "subscriptionTier": "SuperGrok",
        },
    })
    # ~600KB of filler after the billing line (larger than one reverse chunk).
    filler = json.dumps({
        "ts": "2026-07-10T12:01:00Z",
        "msg": "shell.tool.exec_done",
        "ctx": {"tool_name": "read_file", "pad": "x" * 200},
    }) + "\n"
    log_path = logs / "unified.jsonl"
    with log_path.open("w", encoding="utf-8") as handle:
        handle.write(billing + "\n")
        # Write until the file is clearly larger than one 256KB reverse chunk.
        written = 0
        while written < 600_000:
            handle.write(filler)
            written += len(filler)
    res = grok_mod.run_grok_local(timeout=1.0, grok_home=home, log_path=log_path)
    assert res["status"] == "ok"
    assert res["limits"][0]["percent"] == 33.0


def _billing_line(
    *,
    ts: str,
    percent: float | None,
    start: str,
    end: str,
    tier: str = "SuperGrok",
) -> str:
    """Real-shape billing line; omit creditUsagePercent when percent is None."""
    config: dict = {
        "currentPeriod": {
            "type": "USAGE_PERIOD_TYPE_WEEKLY",
            "start": start,
            "end": end,
        },
        "historyLen": 0 if percent is None else 1,
    }
    if percent is not None:
        config["creditUsagePercent"] = percent
    return json.dumps({
        "ts": ts,
        "msg": "billing: fetched credits config",
        "ctx": {"config": config, "subscriptionTier": tier},
    })


def test_run_grok_local_skips_midperiod_null_percent(tmp_path):
    """Incomplete newer snapshot (no percent, same period) must not zero the bar.

    Live logs (2026-07-15) show bursts of historyLen==0 configs mid-week after
    a real metered percent was already reported for the same period.
    """
    from providers import grok as grok_mod
    home = tmp_path / ".grok"
    logs = home / "logs"
    logs.mkdir(parents=True)
    start = "2026-07-11T13:20:44.991585+00:00"
    end = "2026-07-18T13:20:44.991585+00:00"
    lines = [
        _billing_line(ts="2026-07-15T12:00:00Z", percent=46.0, start=start, end=end),
        _billing_line(ts="2026-07-15T16:41:16Z", percent=None, start=start, end=end),
        _billing_line(ts="2026-07-15T16:41:22Z", percent=None, start=start, end=end),
    ]
    log_path = logs / "unified.jsonl"
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    res = grok_mod.run_grok_local(timeout=1.0, grok_home=home, log_path=log_path,
                                  now=dt.datetime(2026, 7, 15, 17, 0, tzinfo=dt.timezone.utc))
    assert res["status"] == "ok"
    assert res["limits"][0]["percent"] == 46.0
    assert res["period"]["start"] == start


def test_run_grok_local_new_period_without_percent_is_zero(tmp_path):
    """After weekly reset the newest events omit percent for the new period.

    Must show 0% on the *new* window — not stick on last week's metered %.
    """
    from providers import grok as grok_mod
    home = tmp_path / ".grok"
    logs = home / "logs"
    logs.mkdir(parents=True)
    old_start = "2026-07-11T13:20:44.991585+00:00"
    old_end = "2026-07-18T13:20:44.991585+00:00"
    new_start = "2026-07-18T13:20:44.991585+00:00"
    new_end = "2026-07-25T13:20:44.991585+00:00"
    lines = [
        _billing_line(ts="2026-07-18T13:07:10Z", percent=46.0, start=old_start, end=old_end),
        _billing_line(ts="2026-07-18T14:06:51Z", percent=None, start=new_start, end=new_end),
        _billing_line(ts="2026-07-18T14:06:54Z", percent=None, start=new_start, end=new_end),
    ]
    log_path = logs / "unified.jsonl"
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    res = grok_mod.run_grok_local(timeout=1.0, grok_home=home, log_path=log_path,
                                  now=dt.datetime(2026, 7, 18, 15, 0, tzinfo=dt.timezone.utc))
    assert res["status"] == "ok"
    assert res["limits"][0]["percent"] == 0.0
    assert res["period"]["start"] == new_start
    assert res["period"]["end"] == new_end
    assert res["limits"][0]["resetAt"] == new_end


def test_grok_billing_period_follows_new_period_without_percent(tmp_path):
    """Billing-week token window must advance with the new period, not stay on last week."""
    from providers.grok import grok_billing_period
    old_start = "2026-07-11T13:20:44.991585+00:00"
    old_end = "2026-07-18T13:20:44.991585+00:00"
    new_start = "2026-07-18T13:20:44.991585+00:00"
    new_end = "2026-07-25T13:20:44.991585+00:00"
    log = tmp_path / "unified.jsonl"
    log.write_text(
        "\n".join([
            _billing_line(ts="2026-07-18T13:07:10Z", percent=46.0, start=old_start, end=old_end),
            _billing_line(ts="2026-07-18T14:06:54Z", percent=None, start=new_start, end=new_end),
        ]) + "\n",
        encoding="utf-8",
    )
    result = grok_billing_period(log_path=log, now=dt.datetime(2026, 7, 18, 15, 0, tzinfo=dt.timezone.utc))
    assert result is not None
    start, end = result
    assert start == dt.datetime.fromisoformat(new_start)
    assert end == dt.datetime.fromisoformat(new_end)


# ---------------------------------------------------------------------------
# grok_billing_period (providers/grok.py)
# ---------------------------------------------------------------------------

def _billing_event_line(start: str, end: str, percent: float = 91.0) -> str:
    """Build a single-line billing event in the real log format."""
    return json.dumps({
        "ts": "2026-07-10T19:59:33.398Z",
        "src": "shell",
        "pid": 2012917,
        "lvl": "info",
        "msg": "billing: fetched credits config",
        "ctx": {
            "config": {
                "creditUsagePercent": percent,
                "currentPeriod": {
                    "type": "USAGE_PERIOD_TYPE_WEEKLY",
                    "start": start,
                    "end": end,
                },
            },
            "subscriptionTier": "SuperGrok",
        },
    })


def test_grok_billing_period_returns_aware_datetimes(tmp_path):
    """A log with the real-shape billing event returns (start, end) aware datetimes."""
    from providers.grok import grok_billing_period
    start_iso = "2026-07-04T13:20:44.991585+00:00"
    end_iso   = "2026-07-11T13:20:44.991585+00:00"
    log = tmp_path / "unified.jsonl"
    log.write_text(_billing_event_line(start_iso, end_iso) + "\n", encoding="utf-8")

    result = grok_billing_period(log_path=log, now=dt.datetime(2026, 7, 10, 20, 0, tzinfo=dt.timezone.utc))
    assert result is not None
    start, end = result
    assert start.tzinfo is not None
    assert end.tzinfo is not None
    # Times must match the ISO strings in the event.
    assert start == dt.datetime.fromisoformat(start_iso)
    assert end   == dt.datetime.fromisoformat(end_iso)
    assert start < end


def test_grok_billing_period_missing_file(tmp_path):
    """Missing log file → None (no crash)."""
    from providers.grok import grok_billing_period
    result = grok_billing_period(log_path=tmp_path / "nonexistent.jsonl")
    assert result is None


def test_grok_billing_period_no_billing_event(tmp_path):
    """Log with only non-billing lines → None."""
    from providers.grok import grok_billing_period
    log = tmp_path / "unified.jsonl"
    log.write_text(json.dumps({"ts": "2026-07-10T12:00:00Z", "msg": "some other event"}) + "\n",
                   encoding="utf-8")
    result = grok_billing_period(log_path=log)
    assert result is None


def test_grok_billing_period_event_without_period_start(tmp_path):
    """Billing event missing the period start key → None."""
    from providers.grok import grok_billing_period
    event = json.dumps({
        "msg": "billing: fetched credits config",
        "ctx": {
            "config": {"creditUsagePercent": 50.0},
            "subscriptionTier": "SuperGrok",
        },
    })
    log = tmp_path / "unified.jsonl"
    log.write_text(event + "\n", encoding="utf-8")
    result = grok_billing_period(log_path=log)
    assert result is None


# ---------------------------------------------------------------------------
# compute_local_cost_summaries billing-week wiring (providers/cost.py)
# ---------------------------------------------------------------------------

def test_compute_local_cost_summaries_passes_billing_week_to_grok(tmp_path):
    """compute_local_cost_summaries passes the grok_billing_period start/end to
    local_grok_token_summary as keyword args, and propagates the return value."""
    import providers.cost as cost_mod

    week_start = dt.datetime(2026, 7, 4, 13, 20, 44, tzinfo=dt.timezone.utc)
    week_end   = dt.datetime(2026, 7, 11, 13, 20, 44, tzinfo=dt.timezone.utc)
    sentinel = {"source": "local-grok-logs", "billingWeekTokens": 42}

    captured_kwargs: dict = {}

    def _fake_grok_summary(**kwargs):
        captured_kwargs.update(kwargs)
        return sentinel

    with patch("providers.grok.grok_billing_period", return_value=(week_start, week_end)), \
         patch("accounting.local_grok_token_summary", side_effect=_fake_grok_summary), \
         patch("accounting.local_claude_token_summary", return_value=None), \
         patch("accounting.local_codex_token_summary", return_value=None), \
         patch("accounting.local_gemini_token_summary", return_value=None), \
         patch("providers.cost.antigravity_ledger_cost_summary", return_value=None):
        summaries = cost_mod.compute_local_cost_summaries()

    assert captured_kwargs.get("week_start") == week_start
    assert captured_kwargs.get("week_end") == week_end
    assert summaries["grok"] is sentinel


def test_compute_local_cost_summaries_grok_billing_period_exception_is_swallowed(tmp_path):
    """An exception from grok_billing_period must not propagate — summaries still computed."""
    import providers.cost as cost_mod

    def _raises():
        raise RuntimeError("disk error")

    with patch("providers.grok.grok_billing_period", side_effect=_raises), \
         patch("accounting.local_grok_token_summary", return_value=None), \
         patch("accounting.local_claude_token_summary", return_value=None), \
         patch("accounting.local_codex_token_summary", return_value=None), \
         patch("accounting.local_gemini_token_summary", return_value=None), \
         patch("providers.cost.antigravity_ledger_cost_summary", return_value=None):
        # Must not raise
        summaries = cost_mod.compute_local_cost_summaries()

    # All summaries computed (all None here, but no exception).
    assert "grok" in summaries


def _grok_billing_log(tmp_path, ts_iso):
    """A ~/.grok tree whose single billing event carries `ts_iso`."""
    home = tmp_path / ".grok"
    logs = home / "logs"
    logs.mkdir(parents=True)
    (home / "auth.json").write_text("{}", encoding="utf-8")
    line = json.dumps({
        "ts": ts_iso,
        "msg": "billing: fetched credits config",
        "ctx": {
            "config": {
                "creditUsagePercent": 57.0,
                "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY",
                                  "end": "2030-07-11T00:00:00+00:00"},
            },
            "subscriptionTier": "SuperGrok",
        },
    })
    (logs / "unified.jsonl").write_text(line + "\n", encoding="utf-8")
    return home, logs / "unified.jsonl"


def test_grok_fetched_at_is_the_capture_time_not_the_read_time(tmp_path):
    """The weekly bar is xAI's own number, but it only reaches the log when the CLI
    runs — so its age is the last session's age, not our refresh's. Stamping
    now_iso() claimed a freshness we never had (observed: 15.3h old while the UI
    read "Updated just now")."""
    import datetime as dt
    from providers import grok as grok_mod

    ts = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)).isoformat()
    home, log = _grok_billing_log(tmp_path, ts)
    res = grok_mod.run_grok_local(timeout=1.0, grok_home=home, log_path=log)

    assert res["status"] == "ok"
    assert res["fetchedAt"] == ts          # the event's own stamp, not "now"
    assert res.get("stale") is not True     # 5 minutes old is fresh


def test_grok_marks_itself_stale_when_the_vendor_number_is_old(tmp_path):
    """Past the threshold the provider sets `stale`, which the UI already renders as
    a "(cached)" suffix — no QML change needed, same contract the carry-forward uses."""
    import datetime as dt
    from providers import grok as grok_mod

    ts = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=15)).isoformat()
    home, log = _grok_billing_log(tmp_path, ts)
    res = grok_mod.run_grok_local(timeout=1.0, grok_home=home, log_path=log)

    assert res["status"] == "ok"
    assert res["stale"] is True
    assert res["limits"][0]["percent"] == 57.0   # the number itself is unchanged


def test_grok_without_a_timestamp_falls_back_and_is_not_marked_stale(tmp_path):
    """A billing line with no `ts` must not be guessed at in either direction."""
    from providers import grok as grok_mod
    home, log = _grok_billing_log(tmp_path, "")
    res = grok_mod.run_grok_local(timeout=1.0, grok_home=home, log_path=log)
    assert res["status"] == "ok"
    assert res["fetchedAt"]                 # falls back to now
    assert res.get("stale") is not True     # unknown age is not evidence of staleness


# --- An ENDED billing period no longer shows last period's percentage ---

def test_run_grok_local_zeroes_bar_after_period_ends(tmp_path):
    """The billing event is only logged when the grok CLI runs. After a quiet week the newest
    event describes a period that is over — the pool has reset, so the bar must read 0% with
    the reset projected to the current period's end, not last week's 85% and "Reset due"."""
    from providers import grok as grok_mod
    home = tmp_path / ".grok"
    logs = home / "logs"
    logs.mkdir(parents=True)
    start = "2026-07-11T13:20:44+00:00"
    end = "2026-07-18T13:20:44+00:00"
    log_path = logs / "unified.jsonl"
    log_path.write_text(_billing_line(ts="2026-07-17T12:00:00Z", percent=85.0, start=start, end=end) + "\n",
                        encoding="utf-8")
    now = dt.datetime(2026, 7, 29, 9, 0, tzinfo=dt.timezone.utc)  # 1.8 periods after it ended
    res = grok_mod.run_grok_local(timeout=1.0, grok_home=home, log_path=log_path, now=now)
    limit = res["limits"][0]
    assert limit["percent"] == 0.0
    assert limit["resetAt"] == "2026-08-01T13:20:44+00:00"   # 07-18 + 2 whole weeks
    assert res["period"]["start"] == "2026-07-25T13:20:44+00:00" and res["period"]["projected"] is True
    assert "ended" in res["message"]
    assert res["stale"] is True   # the vendor reading itself is still old


def test_run_grok_local_keeps_percent_within_the_period(tmp_path):
    from providers import grok as grok_mod
    home = tmp_path / ".grok"
    logs = home / "logs"
    logs.mkdir(parents=True)
    log_path = logs / "unified.jsonl"
    log_path.write_text(_billing_line(ts="2026-07-17T12:00:00Z", percent=85.0,
                                      start="2026-07-11T13:20:44+00:00", end="2026-07-18T13:20:44+00:00") + "\n",
                        encoding="utf-8")
    res = grok_mod.run_grok_local(timeout=1.0, grok_home=home, log_path=log_path,
                                  now=dt.datetime(2026, 7, 17, 13, 0, tzinfo=dt.timezone.utc))
    assert res["limits"][0]["percent"] == 85.0
    assert "projected" not in res["period"]


def test_grok_billing_period_rolls_forward_after_period_ends(tmp_path):
    """The "This week" token window must follow the current period, not the ended one."""
    from providers.grok import grok_billing_period
    log = tmp_path / "unified.jsonl"
    log.write_text(_billing_line(ts="2026-07-17T12:00:00Z", percent=85.0,
                                 start="2026-07-11T13:20:44+00:00", end="2026-07-18T13:20:44+00:00") + "\n",
                   encoding="utf-8")
    start, end = grok_billing_period(log_path=log, now=dt.datetime(2026, 7, 20, tzinfo=dt.timezone.utc))
    assert start == dt.datetime(2026, 7, 18, 13, 20, 44, tzinfo=dt.timezone.utc)
    assert end == dt.datetime(2026, 7, 25, 13, 20, 44, tzinfo=dt.timezone.utc)


def test_roll_period_forward_non_weekly_ended_is_unknown():
    from providers.grok import roll_period_forward
    s0 = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
    e0 = dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc)
    assert roll_period_forward(s0, e0, "USAGE_PERIOD_TYPE_MONTHLY", dt.datetime(2026, 7, 5, tzinfo=dt.timezone.utc)) \
        == (None, None, True)
    assert roll_period_forward(s0, e0, "USAGE_PERIOD_TYPE_MONTHLY", dt.datetime(2026, 6, 5, tzinfo=dt.timezone.utc)) \
        == (s0, e0, False)
