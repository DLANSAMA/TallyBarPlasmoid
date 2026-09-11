"""Item 7: monthly cost-rollup archive.

Drives the REAL loader/saver/lock/change-gate (not mocks) so a regression that drops the
.bak recovery, the change-gate, or the past-month freeze fails here instead of silently.
The archive must survive the 35-day Antigravity ledger prune (past months freeze once
written) and follow the same atomic-0600 + .bak + never-load-into-empty discipline.
"""

import datetime as dt
import json
import sys
from pathlib import Path

CODE_DIR = Path(__file__).parent.parent / "io.github.dlansama.tallybar" / "contents" / "code"
sys.path.insert(0, str(CODE_DIR))

from providers import cost as costmod  # noqa: E402


def _point_at(monkeypatch, tmp_path):
    arc = tmp_path / "cost_archive.json"
    monkeypatch.setattr(costmod, "COST_ARCHIVE_PATH", arc)
    return arc


def _summary(*month_buckets):
    """A provider cost summary carrying monthlyTokenUsage buckets {inMonth, cost, tokens}."""
    return {"monthlyTokenUsage": list(month_buckets)}


def _now(year, month, day):
    return dt.datetime(year, month, day, 12, 0, 0, tzinfo=dt.timezone.utc)


def test_current_month_upsert_sums_inmonth_buckets(monkeypatch, tmp_path):
    arc = _point_at(monkeypatch, tmp_path)
    summaries = {
        "codex": _summary({"inMonth": True, "cost": 1.5, "tokens": 100},
                          {"inMonth": True, "cost": 0.5, "tokens": 50},
                          {"inMonth": False, "cost": 99.0, "tokens": 9}),  # out-of-month excluded
        "claude": _summary({"inMonth": True, "cost": 2.0, "tokens": 200}),
        "gemini": None,  # no summary -> not recorded
    }
    costmod.update_cost_archive(summaries, now=_now(2026, 6, 9))
    data = json.loads(arc.read_text())
    assert data["version"] == 1
    june = data["months"]["2026-06"]
    assert june["codex"] == {"cost": 2.0, "tokens": 150}
    assert june["claude"] == {"cost": 2.0, "tokens": 200}
    assert "gemini" not in june
    assert june.get("partial") is True  # first month ever written


def test_change_gate_skips_rewrite_when_unchanged(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path)
    summaries = {"codex": _summary({"inMonth": True, "cost": 1.0, "tokens": 10})}
    costmod.update_cost_archive(summaries, now=_now(2026, 6, 9))
    saves = []
    monkeypatch.setattr(costmod, "_save_cost_archive", lambda a: saves.append(a))
    # Identical data on a later run -> change-gated, no write.
    costmod.update_cost_archive(summaries, now=_now(2026, 6, 9))
    assert saves == []
    # Changed data -> a write happens.
    costmod.update_cost_archive({"codex": _summary({"inMonth": True, "cost": 5.0, "tokens": 50})},
                                now=_now(2026, 6, 9))
    assert len(saves) == 1


def test_past_month_freezes_when_current_advances(monkeypatch, tmp_path):
    arc = _point_at(monkeypatch, tmp_path)
    # June has real data.
    costmod.update_cost_archive({"codex": _summary({"inMonth": True, "cost": 3.0, "tokens": 30})},
                                now=_now(2026, 6, 28))
    # July run: even if the (pruned) Antigravity ledger now reports nothing for June, the
    # July upsert must NOT touch the frozen June entry.
    costmod.update_cost_archive({"codex": _summary({"inMonth": True, "cost": 1.0, "tokens": 10})},
                                now=_now(2026, 7, 2))
    months = json.loads(arc.read_text())["months"]
    assert months["2026-06"]["codex"] == {"cost": 3.0, "tokens": 30}  # frozen, untouched
    assert months["2026-07"]["codex"] == {"cost": 1.0, "tokens": 10}
    # June was the first month written -> partial; July is NOT (archive already had months).
    assert months["2026-06"].get("partial") is True
    assert "partial" not in months["2026-07"]


def test_last_inmonth_write_wins_then_freezes(monkeypatch, tmp_path):
    arc = _point_at(monkeypatch, tmp_path)
    # Several same-month refreshes: the latest in-month total wins (upsert, not append).
    for cost in (1.0, 2.5, 4.0):
        costmod.update_cost_archive({"codex": _summary({"inMonth": True, "cost": cost, "tokens": int(cost * 10)})},
                                    now=_now(2026, 6, 9))
    june = json.loads(arc.read_text())["months"]["2026-06"]
    assert june["codex"] == {"cost": 4.0, "tokens": 40}


def test_save_mirrors_main_to_bak(monkeypatch, tmp_path):
    arc = _point_at(monkeypatch, tmp_path)
    costmod.update_cost_archive({"codex": _summary({"inMonth": True, "cost": 1.0, "tokens": 10})},
                                now=_now(2026, 6, 9))
    bak = arc.with_name(arc.name + ".bak")
    assert arc.exists() and bak.exists()
    assert arc.read_text() == bak.read_text()


def test_load_recovers_from_bak_and_quarantines_corrupt_main(monkeypatch, tmp_path):
    arc = _point_at(monkeypatch, tmp_path)
    good = {"version": 1, "months": {"2026-05": {"codex": {"cost": 9.0, "tokens": 90}}}}
    bak = arc.with_name(arc.name + ".bak")
    bak.write_text(json.dumps(good))
    arc.write_text("{ this is not json")  # corrupt main
    loaded = costmod._load_cost_archive()
    assert loaded == good                                   # recovered from .bak
    assert arc.with_name(arc.name + ".corrupt").exists()    # corrupt main quarantined


def test_load_into_empty_only_when_truly_absent(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path)
    assert costmod._load_cost_archive() == {"version": 1, "months": {}}


def test_flock_contention_returns_without_write(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path)
    saves = []
    monkeypatch.setattr(costmod, "_save_cost_archive", lambda a: saves.append(a))
    monkeypatch.setattr(costmod, "flock_with_timeout", lambda fd, budget: False)  # lock held elsewhere
    costmod.update_cost_archive({"codex": _summary({"inMonth": True, "cost": 1.0, "tokens": 10})},
                                now=_now(2026, 6, 9))
    assert saves == []  # contention -> skip this refresh's write, no block


def test_deadline_bounds_lock_budget(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path)
    seen = {}
    monkeypatch.setattr(costmod, "flock_with_timeout",
                        lambda fd, budget: seen.setdefault("budget", budget) or True)
    import time as _time
    costmod.update_cost_archive({"codex": _summary({"inMonth": True, "cost": 1.0, "tokens": 10})},
                                now=_now(2026, 6, 9), deadline=_time.time() + 0.05)
    assert 0.0 <= seen["budget"] <= 0.1  # bounded by the (near-expired) deadline, not the 2s default
