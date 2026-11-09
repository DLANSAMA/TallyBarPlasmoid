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
