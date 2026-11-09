"""Claude failure statuses, statusLine carry-over, projection gate, percent/credit text, ms resets."""
from __future__ import annotations

import datetime as dt
from unittest.mock import patch

import pytest

import providers.claude as claude_mod
from accounting import codex_limit_from_window, codex_rate_limit_rows
from accounting.limits import _credit_amount_text, enrich_ui_formatting
from parsers import usage_pace_detail
from test_providers import _cookie


def _backend(usage_result, summary_result=(404, {})):
    def side(url, jar, timeout):
        if url.endswith("/organizations"):
            return (200, [{"uuid": "org-1"}])
        r = usage_result if url.endswith("/usage") else summary_result if url.endswith("/usage_summary") else (404, {})
        if isinstance(r, Exception):
            raise r
        return r
    return side


@pytest.mark.asyncio
@pytest.mark.parametrize("usage,status", [
    ((429, {"retryAfterSeconds": 600}), "api-error"),
    ((403, {}), "unauthorized"),
    ((503, {}), "api-error"),
    (TimeoutError(), "timeout"),
    (OSError("boom"), "api-error"),
])
async def test_usage_failures_are_transient_statuses(usage, status):
    with patch("providers.claude.http_json_async", side_effect=_backend(usage)):
        res = await claude_mod.run_claude_api([_cookie("claude.ai")], timeout=1.0)
    assert res["status"] == status
    assert ("rateLimitedUntil" in res) == (isinstance(usage, tuple) and usage[0] == 429)


@pytest.mark.asyncio
async def test_usage_definite_miss_stays_api_empty():
    with patch("providers.claude.http_json_async", side_effect=_backend((404, {}))):
        res = await claude_mod.run_claude_api([_cookie("claude.ai")], timeout=1.0)
    assert res["status"] == "api-empty"


def test_statusline_fallback_carries_backoff_credit_and_probe_stamp():
    now = dt.datetime.now(dt.timezone.utc)
    until = (now + dt.timedelta(minutes=10)).isoformat()
    past = (now - dt.timedelta(minutes=10)).isoformat()
    cap = {"capturedAt": now.isoformat(), "rateLimits": {"five_hour": {
        "usedPercent": 30.0, "resetsAt": (now + dt.timedelta(hours=2)).timestamp()}}}
    prov = {"label": "Claude", "status": "api-error", "limits": [], "rateLimitedUntil": until,
            "creditBalance": {"amount": 5}, "usageSummaryUnavailableSince": past}
    out = claude_mod.apply_claude_statusline_fallback(prov, cap, now)
    assert out["source"] == "claude-statusline"
    assert out["rateLimitedUntil"] == until and out["creditBalance"] == {"amount": 5}
    assert out["usageSummaryUnavailableSince"] == past
    prov["rateLimitedUntil"] = past
    assert "rateLimitedUntil" not in claude_mod.apply_claude_statusline_fallback(prov, cap, now)


def _iso(seconds):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)).isoformat()


def test_no_run_out_projection_before_five_percent_of_window():
    week = 7 * 86400
    assert "Runs out" not in str(usage_pace_detail(5, _iso(week - 600), 7 * 24 * 60))
    assert "Runs out" not in str(usage_pace_detail(2, _iso(5 * 3600 - 1), 300))
    assert "Runs out" in str(usage_pace_detail(90, _iso(week * 0.5), 7 * 24 * 60))


def _used_text(pct):
    data = {"codex": {"limits": [{"label": "Session", "percent": pct, "unit": "%"}]}}
    enrich_ui_formatting(data)
    return data["codex"]["limits"][0]["formattedUsedText"]


@pytest.mark.parametrize("pct,text", [(99.6, "99% used"), (99.99, "99% used"), (100, "100% used"),
                                      (12.5, "13% used"), (14.5, "15% used"), (37.4, "37% used"),
                                      (0.5, "0.5% used"), (9.96, "10% used"), (9.94, "9.9% used"),
                                      (5.04, "5% used"), (0.04, "<0.1% used"), (0.05, "0.1% used"),
                                      (0, "0% used"), (10, "10% used")])
def test_percent_text_rounds_half_up_and_never_shows_100_early(pct, text):
    assert _used_text(pct) == text


def test_credit_amount_text():
    assert _credit_amount_text(12.5) == "12.5"
    assert _credit_amount_text(0.4) == "0.4"
    assert _credit_amount_text(12.345) == "12.35"
    assert _credit_amount_text(1500.0) != "1500.00"


def test_millisecond_reset_is_accepted_and_garbage_is_not_fatal():
    secs = dt.datetime.now(dt.timezone.utc).timestamp() + 3600
    a = codex_limit_from_window("primary", "Session", {"usedPercent": 5, "resetsAt": secs})
    b = codex_limit_from_window("primary", "Session", {"usedPercent": 5, "resetsAt": secs * 1000})
    assert a["reset"] == b["reset"]
    c = codex_limit_from_window("primary", "Session", {"usedPercent": 5, "resetsAt": 1e30})
    assert c is not None and c["percent"] == 5
    assert codex_rate_limit_rows({"primary": {"usedPercent": 5, "resetsAt": 1e30}})
