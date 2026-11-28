"""Orchestrator resilience: cost carry-forward, notification keys, browser failure,
config durability, scrubber anchoring and strict JSON output."""

import datetime as dt
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

CODE_DIR = Path(__file__).parent.parent / "io.github.dlansama.tallybar" / "contents" / "code"
sys.path.insert(0, str(CODE_DIR))

import backend  # noqa: E402
import http_helpers  # noqa: E402
import io_helpers  # noqa: E402


def _iso(offset_seconds: float = 5.0) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=offset_seconds)).isoformat()


def _pin_backend_clock(monkeypatch) -> dt.datetime:
    """Freeze ``backend``'s clock at a local midday and return that instant."""
    noon = dt.datetime(2026, 11, 15, 12, 0).astimezone()

    class Fixed(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(noon.timestamp(), tz)

    monkeypatch.setattr(backend, "dt", types.SimpleNamespace(**{**vars(dt), "datetime": Fixed}))
    return noon


def _cached_with_cost(ts: str):
    return {
        "timestamp": ts,
        "providers": {
            "claude": {"label": "Claude", "status": "ok", "limits": [], "costSummary": {"cost30d": 12.5}},
            "grok": {"label": "Grok", "status": "ok", "source": "local-grok-logs", "limits": [],
                     "costSummary": {"cost30d": 3.0}},
        },
    }


async def _build(cached, summaries=None, scan_exc=None, collect=None):
    args = MagicMock()
    args.no_network = False
    args.timeout = 0.05

    async def side_effect(func, *a, **k):
        status = "missing-cookies" if func.__name__ == "run_gemini_web" else "ok"
        return dict(k.get("fallback", {"label": "x"}), status=status, limits=[])

    async def benign_rpc(*a, **k):
        return {"label": "Codex", "status": "not-running", "limits": []}

    def scan(deadline=None):
        if scan_exc:
            raise scan_exc
        return summaries or {}

    async def default_collect(*a, **k):
        return [], {}

    with patch("backend.load_config", return_value={}), \
         patch("backend.load_snapshot", return_value=cached), \
         patch("backend.collect_browser_sessions", side_effect=collect or default_collect), \
         patch("backend.run_threaded_provider", side_effect=side_effect), \
         patch("backend.run_claude_api", side_effect=benign_rpc), \
         patch("backend.run_codex_rpc", side_effect=benign_rpc), \
         patch("backend.compute_local_cost_summaries", side_effect=scan):
        return await backend.build_snapshot(args)


@pytest.mark.asyncio
async def test_cost_scan_timeout_carries_previous_cost_summaries():
    ts = _iso()
    res = await _build(_cached_with_cost(ts), scan_exc=TimeoutError())
    assert res["diagnostics"]["cost_summary_timeout"] is True
    for name, cost in (("claude", 12.5), ("grok", 3.0)):
        summary = res["providers"][name]["costSummary"]
        assert summary["cost30d"] == cost
        assert summary["stale"] is True
        assert summary["staleAsOf"] == ts


@pytest.mark.asyncio
async def test_cost_carry_keeps_original_stale_as_of_and_expires(monkeypatch):
    # The carry keeps only readings from the same local day, so a 3-hour-old one needs a clock
    # more than 3 hours past midnight.
    noon = _pin_backend_clock(monkeypatch)

    def ago(seconds: float) -> str:
        return (noon - dt.timedelta(seconds=seconds)).astimezone(dt.timezone.utc).isoformat()

    old = ago(3 * 3600)
    cached = _cached_with_cost(ago(5))
    cached["providers"]["claude"]["costSummary"].update(stale=True, staleAsOf=old)
    res = await _build(cached, scan_exc=TimeoutError())
    assert res["providers"]["claude"]["costSummary"]["staleAsOf"] == old
    expired = _cached_with_cost(ago(5))
    expired["providers"]["claude"]["costSummary"].update(stale=True, staleAsOf=ago(7 * 3600))
    res = await _build(expired, scan_exc=TimeoutError())
    assert "costSummary" not in res["providers"]["claude"]


@pytest.mark.asyncio
async def test_cost_scan_partial_errors_carry_only_failed_provider():
    ts = _iso()
    summaries = {"claude": {"cost30d": 99.0}, "_errors": {"grok": "ValueError: bad log token=abc"}}
    res = await _build(_cached_with_cost(ts), summaries=summaries)
    assert res["providers"]["claude"]["costSummary"] == {"cost30d": 99.0}
    assert res["providers"]["grok"]["costSummary"]["stale"] is True
    err = res["diagnostics"]["cost_summary_error"]
    assert err.startswith("grok: ValueError") and len(err) <= 160


@pytest.mark.asyncio
async def test_browser_cookie_timeout_is_transient_not_signed_out():
    cached = {"timestamp": _iso(), "providers": {
        "gemini": {"label": "Gemini", "status": "ok", "limits": [{"label": "Pro", "percent": 10}]}}}

    async def slow(*a, **k):
        raise TimeoutError

    res = await _build(cached, collect=slow)
    assert res["diagnostics"]["browser"]["status"] == "timeout"
    assert res["providers"]["gemini"]["status"] == "ok"
    assert res["providers"]["gemini"]["stale"] is True


def _fired(tmp_path, monkeypatch, keys=()):
    state = tmp_path / "notify_state.json"
    monkeypatch.setattr(backend, "NOTIFY_STATE_PATH", state)
    if keys:
        state.write_text(json.dumps({"fired": list(keys)}))
    return state


def _ag(five, weekly):
    return {"antigravity": {"label": "Antigravity", "status": "ok", "limits": [
        {"label": "Gemini", "window": "5h", "sublabel": "5-hour", "percent": five},
        {"label": "Gemini", "window": "weekly", "sublabel": "Weekly", "percent": weekly},
    ]}}


def test_shared_label_lanes_do_not_refire(tmp_path, monkeypatch):
    _fired(tmp_path, monkeypatch)
    cfg = {"notificationThresholds": [90]}
    assert backend.compute_notifications(_ag(95, 10), cfg)
    assert backend.compute_notifications(_ag(95, 10), cfg) == []


def test_legacy_armed_key_is_not_refired_after_upgrade(tmp_path, monkeypatch):
    _fired(tmp_path, monkeypatch, ["antigravity|Gemini|90"])
    out = backend.compute_notifications(_ag(95, 10), {"notificationThresholds": [90]})
    assert out == []


def test_window_weekly_without_window_minutes_is_weekly():
    assert backend._is_weekly_limit({"label": "Gemini", "window": "weekly"})
    assert not backend._is_weekly_limit({"label": "Gemini", "window": "5h"})


def test_weekly_label_with_capital_t_stays_armed(tmp_path, monkeypatch):
    state = _fired(tmp_path, monkeypatch, ["weekly80|claude|Weekly Tokens"])
    providers = {"claude": {"label": "Claude", "status": "ok",
                            "limits": [{"label": "Weekly Tokens", "percent": 78}]}}
    backend.compute_notifications(providers, {"notificationThresholds": [95]})
    assert "weekly80|claude|Weekly Tokens" in json.loads(state.read_text())["fired"]


def test_hidden_provider_is_silent(tmp_path, monkeypatch):
    _fired(tmp_path, monkeypatch)
    providers = {"claude": {"label": "Claude", "status": "ok", "limits": [{"label": "S", "percent": 99}]},
                 "codex": {"label": "Codex", "status": "ok", "limits": [{"label": "S", "percent": 99}]}}
    out = backend.compute_notifications(providers, {"providers": ["codex"], "notificationThresholds": [90]})
    assert {n["provider"] for n in out} == {"codex"}


def test_carry_partial_lanes_stamps_and_expires():
    ts = _iso(60)
    cached = {"timestamp": ts, "providers": {"antigravity": {"limits": [{"label": "G"}]}}}
    out = backend.carry_forward_partial_antigravity_lanes({"status": "partial", "limits": []}, cached)
    assert out["stale"] is True and out["staleAsOf"] == ts
    old = {"timestamp": _iso(10), "providers": {"antigravity": {
        "limits": [{"label": "G"}], "stale": True, "staleAsOf": _iso(3600)}}}
    out = backend.carry_forward_partial_antigravity_lanes({"status": "partial", "limits": []}, old)
    assert not out["limits"] and "stale" not in out


def test_load_json_with_backup_survives_invalid_utf8(tmp_path):
    path = tmp_path / "c.json"
    path.write_bytes(b"\xff\xfe\x00bad")
    path.with_name("c.json.bak").write_text('{"a": 1}')
    got = io_helpers.load_json_with_backup(path, lambda d: isinstance(d, dict), {})
    assert got == {"a": 1}
    assert path.with_name("c.json.corrupt").exists()


def test_corrupt_config_keeps_settings_through_next_save(tmp_path, monkeypatch):
    cfg = tmp_path / ".tallybar" / "config.json"
    monkeypatch.setattr(backend, "CONFIG_PATH", cfg)
    backend.update_config_values({"monthlyBudget": 50})
    cfg.write_text("{not json")
    out = backend.update_config_values({"panelDisplayMode": "cost"})
    assert out["monthlyBudget"] == 50.0
    assert cfg.with_name("config.json.corrupt").exists()


def test_scrubber_leaves_ordinary_words_and_covers_base64_bearer():
    assert http_helpers.scrub_credentials("disk-full on task-queue") == "disk-full on task-queue"
    assert "abc" not in http_helpers.scrub_credentials("bearer abc+def/ghi==")
    assert http_helpers.scrub_credentials("key sk-proj-abcdefghijklmnop1234").endswith("sk-[REDACTED]")


def test_dump_json_is_strict():
    out = backend._dump_json({"a": float("nan"), "b": [float("inf")], "c": 1.5}, False)
    assert json.loads(out, parse_constant=lambda c: pytest.fail(c)) == {"a": None, "b": [None], "c": 1.5}


def test_degraded_snapshot_uses_live_config(tmp_path, monkeypatch):
    cfg = tmp_path / ".tallybar" / "config.json"
    monkeypatch.setattr(backend, "CONFIG_PATH", cfg)
    backend.update_config_values({"monthlyBudget": 77})
    monkeypatch.setattr(backend, "load_snapshot", lambda: {"ok": True, "providers": {},
                                                          "config": {"monthlyBudget": 1}})
    assert backend._degraded_snapshot("boom")["config"]["monthlyBudget"] == 77.0
