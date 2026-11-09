"""Cost carry day bound, --cost staleness, legacy notification keys, config reset."""
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "io.github.dlansama.tallybar" / "contents" / "code"))

import backend  # noqa: E402


def test_carried_cost_summary_never_crosses_local_midnight(monkeypatch):
    yesterday_noon = (dt.datetime.now().astimezone() - dt.timedelta(days=1)).replace(hour=12)
    iso = yesterday_noon.astimezone(dt.timezone.utc).isoformat()
    # A summary computed yesterday, re-carried from a snapshot taken today.
    monkeypatch.setattr(backend, "_stale_origin", lambda cached, entry: (iso, 3600.0))
    cached = {"timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
              "providers": {"claude": {"label": "Claude", "costSummary": {"cost30d": 1.0}}}}
    providers: dict = {}
    backend.carry_forward_cost_summaries(providers, cached)
    assert providers == {}


def test_carried_cost_summary_from_today_is_kept():
    now = dt.datetime.now(dt.timezone.utc)
    cached = {"timestamp": now.isoformat(),
              "providers": {"claude": {"label": "Claude", "costSummary": {"cost30d": 1.0}}}}
    providers: dict = {}
    backend.carry_forward_cost_summaries(providers, cached)
    if now.astimezone().date() == dt.datetime.now().astimezone().date():
        assert providers["claude"]["costSummary"]["stale"] is True


def test_cost_export_flags_error_and_carries_staleness():
    snap = {"timestamp": "t", "providers": {"claude": {"costSummary": {
        "cost30d": 2.0, "stale": True, "staleAsOf": "2026-01-01T00:00:00Z"}}},
        "diagnostics": {"cost_summary_error": "claude: boom"}}
    out = backend.cost_export(snap)
    assert out["degraded"] is True and out["degradedReason"] == "cost_summary_error"
    assert out["providers"]["claude"]["stale"] is True
    assert out["providers"]["claude"]["staleAsOf"] == "2026-01-01T00:00:00Z"


def test_legacy_key_in_hold_band_stays_armed(tmp_path, monkeypatch):
    state = tmp_path / "notify_state.json"
    monkeypatch.setattr(backend, "NOTIFY_STATE_PATH", state)
    state.write_text(json.dumps({"fired": ["antigravity|Gemini|90"]}))
    cfg = {"notificationsEnabled": True, "notificationThresholds": [90]}

    def provs(pct):
        return {"antigravity": {"label": "Antigravity", "limits": [
            {"label": "Gemini", "window": "5h", "percent": pct}]}}

    backend.compute_notifications(provs(85), cfg)  # hold band: 80 <= 85 < 90
    fired = set(json.loads(state.read_text())["fired"])
    assert fired == {"antigravity|Gemini|5h|90"}
    assert backend.compute_notifications(provs(92), cfg) == []


def test_missing_config_is_defaults_not_backup(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(backend, "CONFIG_PATH", cfg)
    (tmp_path / "config.json.bak").write_text(json.dumps({"monthlyBudget": 50}))
    assert backend.load_config() == {}
    cfg.write_text("{broken")
    assert backend.load_config() == {"monthlyBudget": 50}
    assert (tmp_path / "config.json.corrupt").exists()


def test_last_snapshot_refreshes_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(backend, "SNAPSHOT_PATH", tmp_path / "snap.json")
    monkeypatch.setattr(backend, "CONFIG_PATH", tmp_path / "config.json")
    (tmp_path / "snap.json").write_text(json.dumps({"providers": {}, "config": {"monthlyBudget": 1}}))
    (tmp_path / "config.json").write_text(json.dumps({"monthlyBudget": 77}))
    monkeypatch.setattr(sys, "argv", ["backend.py", "--last-snapshot"])
    assert backend.main() == 0
    assert json.loads(capsys.readouterr().out)["config"]["monthlyBudget"] == 77
