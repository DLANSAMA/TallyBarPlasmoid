"""The backend consumes the Claude Code statusLine capture as a fallback.

integrations/claude_code/statusline_capture.py writes ~/.tallybar/claude_statusline.json;
until 2026-09 nothing read it, although the integration README sold it as the
Cloudflare-proof complement to the cookie path. These pin the reader, the fallback rules,
the build_snapshot wiring, and the writer<->reader schema contract.
"""
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import backend
import providers.claude as claude_mod

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "integrations" / "claude_code" / "statusline_capture.py"
NOW = dt.datetime(2026, 9, 22, 12, 0, tzinfo=dt.timezone.utc)


def _capture(age_s=60.0, five=(30.0, 2 * 3600), seven=(57.0, 3 * 86400)):
    """A capture taken ``age_s`` before NOW; windows as (usedPercent, seconds-until-reset)."""
    limits = {}
    if five is not None:
        limits["five_hour"] = {"usedPercent": five[0], "resetsAt": NOW.timestamp() + five[1]}
    if seven is not None:
        limits["seven_day"] = {"usedPercent": seven[0], "resetsAt": NOW.timestamp() + seven[1]}
    return {"capturedAt": (NOW - dt.timedelta(seconds=age_s)).isoformat(), "rateLimits": limits}


FAILED = {"label": "Claude", "status": "unauthorized", "limits": [], "message": "API rejected session cookies (403)",
          "accentColor": "#c07f63"}


def test_fresh_capture_replaces_failed_cookie_provider():
    out = claude_mod.apply_claude_statusline_fallback(dict(FAILED), _capture(), NOW)
    assert out["status"] == "ok"
    assert out["source"] == "claude-statusline"
    assert [(r["label"], r["percent"]) for r in out["limits"]] == [("Session", 30.0), ("Weekly", 57.0)]
    assert "403" in out["message"]            # the cookie failure stays visible in the message
    assert out["accentColor"] == "#c07f63"
    assert "stale" not in out
    assert out["limits"][1]["windowMinutes"] == 10080  # pace detail attached like parse_claude_usage


def test_live_cookie_reading_is_never_replaced():
    live = {"label": "Claude", "status": "ok", "limits": [{"label": "Session", "percent": 5}]}
    assert claude_mod.apply_claude_statusline_fallback(live, _capture(), NOW) is live


def test_expired_windows_are_dropped_and_all_expired_is_a_noop():
    out = claude_mod.apply_claude_statusline_fallback(dict(FAILED), _capture(five=(90.0, -60)), NOW)
    assert [r["label"] for r in out["limits"]] == ["Weekly"]
    both = _capture(five=(90.0, -60), seven=(80.0, -1))
    assert claude_mod.apply_claude_statusline_fallback(dict(FAILED), both, NOW)["status"] == "unauthorized"


def test_capture_older_than_max_age_is_ignored():
    old = _capture(age_s=claude_mod.STATUSLINE_MAX_AGE_SECONDS + 1)
    assert claude_mod.apply_claude_statusline_fallback(dict(FAILED), old, NOW)["status"] == "unauthorized"


def test_aged_capture_is_flagged_stale():
    out = claude_mod.apply_claude_statusline_fallback(dict(FAILED), _capture(age_s=1200), NOW)
    assert out["status"] == "ok"
    assert out["stale"] is True
    assert out["staleAsOf"] == out["fetchedAt"]


def test_carried_forward_reading_replaced_only_by_a_newer_capture():
    carried = {"label": "Claude", "status": "ok", "stale": True, "limits": [{"label": "Session", "percent": 12}],
               "staleAsOf": (NOW - dt.timedelta(seconds=300)).isoformat()}
    older = _capture(age_s=600)
    assert claude_mod.apply_claude_statusline_fallback(dict(carried), older, NOW)["limits"][0]["percent"] == 12
    newer = _capture(age_s=30)
    out = claude_mod.apply_claude_statusline_fallback(dict(carried), newer, NOW)
    assert out["source"] == "claude-statusline" and out["limits"][0]["percent"] == 30.0


def test_loader_tolerates_missing_and_corrupt_files(tmp_path):
    assert claude_mod.load_claude_statusline(tmp_path / "absent.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert claude_mod.load_claude_statusline(bad) is None
    bad.write_text(json.dumps({"capturedAt": "x"}))
    assert claude_mod.load_claude_statusline(bad) is None


def test_writer_reader_contract(tmp_path):
    """Pin the schema end to end: the real hook script's output feeds the backend reader."""
    resets = int(dt.datetime.now(dt.timezone.utc).timestamp()) + 3600
    payload = {"rate_limits": {"five_hour": {"used_percentage": 42.0, "resets_at": resets},
                               "seven_day": {"used_percentage": 64.5, "resets_at": resets + 86400}}}
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)}
    subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(payload), text=True,
                   env=env, capture_output=True, timeout=30, check=True)
    capture = claude_mod.load_claude_statusline(tmp_path / ".tallybar" / "claude_statusline.json")
    rows = claude_mod.claude_statusline_limits(capture)
    assert [(r["label"], r["percent"]) for r in rows] == [("Session", 42.0), ("Weekly", 64.5)]


async def _snapshot_with_claude(claude_result, no_network=False):
    args = MagicMock()
    args.no_network = no_network
    args.timeout = 0.5

    async def fast_threaded(func, *a, **k):
        return {"status": "ok", "label": "Gemini", "limits": []}

    async def fake_codex_rpc(timeout):
        return {"status": "ok", "label": "Codex", "limits": []}

    async def fake_claude(cookies, timeout, prev=None):
        return dict(claude_result)

    async def noop_pricing(*a, **k):
        return

    with patch("backend.load_config", return_value={}), \
         patch("backend.load_snapshot", return_value={}), \
         patch("backend.collect_browser_sessions", return_value=([], {"kwallet": {"status": "ok"}})), \
         patch("backend.run_threaded_provider", side_effect=fast_threaded), \
         patch("backend.run_codex_rpc", side_effect=fake_codex_rpc), \
         patch("backend.run_claude_api", side_effect=fake_claude), \
         patch("backend.compute_local_cost_summaries", side_effect=lambda deadline=None: {}), \
         patch("pricing_data.refresh_pricing", noop_pricing):
        return await backend.build_snapshot(args)


def _write_live_capture(path, pct=33.0):
    resets = dt.datetime.now(dt.timezone.utc).timestamp() + 7200
    path.write_text(json.dumps({"capturedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
                                "rateLimits": {"five_hour": {"usedPercent": pct, "resetsAt": resets}}}))


@pytest.mark.asyncio
async def test_build_snapshot_uses_capture_when_cookie_path_fails():
    _write_live_capture(claude_mod.CLAUDE_STATUSLINE_PATH)
    res = await _snapshot_with_claude(FAILED)
    claude = res["providers"]["claude"]
    assert claude["status"] == "ok" and claude["source"] == "claude-statusline"
    assert claude["limits"][0]["percent"] == 33.0
    assert "formattedUsedText" in claude["limits"][0]   # enrich_ui_formatting ran on it


@pytest.mark.asyncio
async def test_build_snapshot_uses_capture_offline():
    """--no-network: the cookie path can't fetch at all, but the capture is local data."""
    _write_live_capture(claude_mod.CLAUDE_STATUSLINE_PATH, pct=12.0)
    res = await _snapshot_with_claude(FAILED, no_network=True)
    assert res["providers"]["claude"]["source"] == "claude-statusline"
    assert res["providers"]["claude"]["limits"][0]["percent"] == 12.0


@pytest.mark.asyncio
async def test_build_snapshot_without_capture_keeps_the_real_error():
    res = await _snapshot_with_claude(FAILED)
    assert res["providers"]["claude"]["status"] == "unauthorized"
