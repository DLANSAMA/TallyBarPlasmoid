"""Tests for the TallyBar-parity features: per-model breakdown + burn-rate
(accounting.token_summary), threshold notifications + config setter + cost export
(backend). These guard the new productivity surfaces against regression."""

import json
import sys
from pathlib import Path
import pytest

CODE_DIR = Path(__file__).parent.parent / "io.github.dlansama.tallybar" / "contents" / "code"
sys.path.insert(0, str(CODE_DIR))

import accounting  # noqa: E402
import backend  # noqa: E402


# --- F3: per-model breakdown + burn-rate in token_summary -------------------

def test_model_breakdown_rows_sorted_and_capped():
    mc = {
        "a": {"cost": 1.0, "tokens": 10},
        "b": {"cost": 9.0, "tokens": 20},
        "c": {"cost": 0.0, "tokens": 0},  # dropped (no cost/tokens)
        "d": {"cost": 3.0, "tokens": 5},
    }
    rows = accounting.model_breakdown_rows(mc)
    assert [r["model"] for r in rows] == ["b", "d", "a"]  # cost desc, zero-row dropped
    assert accounting.model_breakdown_rows(None) == []
    assert len(accounting.model_breakdown_rows(mc, limit=2)) == 2


def test_token_summary_emits_raw_fields_and_burn_rate():
    import datetime as _dt
    daily = [{"date": f"2026-05-{d:02d}", "tokens": 1000, "cost": 7.0} for d in range(24, 31)]  # 7 days × $7
    s = accounting.token_summary(
        today_tokens=1000, month_tokens=30000, source="t",
        today_cost=7.0, month_cost=210.0, daily=daily,
        model_costs={"opus": {"cost": 200.0, "tokens": 28000}, "haiku": {"cost": 10.0, "tokens": 2000}},
    )
    assert s["costToday"] == 7.0
    assert s["cost7d"] == 49.0          # 7 × $7
    assert s["cost30d"] == 210.0
    # burn_rate = 49 / (6 + elapsed_fraction_of_today); denominator is in [6, 7].
    _now = _dt.datetime.now()
    _elapsed = (_now.hour * 3600 + _now.minute * 60 + _now.second) / 86400.0
    _expected_rate = 49.0 / (6.0 + _elapsed)
    assert s["burnRatePerDay"] == pytest.approx(_expected_rate, rel=1e-3)
    assert s["projectedMonthlyCost"] == pytest.approx(_expected_rate * 30.0, rel=1e-3)
    assert [m["model"] for m in s["modelBreakdown"]] == ["opus", "haiku"]


# --- F4: cost export projection ---------------------------------------------

def test_cost_export_projects_and_totals():
    snapshot = {
        "timestamp": "2026-05-31T00:00:00Z",
        "providers": {
            # Every provider carries its cost under "costSummary" — the only key the backend
            # ever attaches. (There is no top-level "cost" provider key; the old export
            # fallback that read one was dead code and was removed.)
            "claude": {"costSummary": {
                "cost30d": 100.0, "cost7d": 21.0, "costToday": 3.0,
                "burnRatePerDay": 3.0, "today": "Today: $3", "modelBreakdown": [{"model": "opus", "cost": 90.0, "tokens": 1}],
            }},
            "codex": {"costSummary": {"cost30d": 50.0, "burnRatePerDay": 2.0}},
            "gemini": {"status": "ok"},  # no cost -> skipped
        },
    }
    out = backend.cost_export(snapshot)
    assert out["generatedAt"] == "2026-05-31T00:00:00Z"
    assert set(out["providers"].keys()) == {"claude", "codex"}
    assert "today" not in out["providers"]["claude"]  # display strings excluded
    assert out["providers"]["claude"]["modelBreakdown"][0]["model"] == "opus"
    assert out["totals"]["cost30d"] == 150.0
    assert out["totals"]["burnRatePerDay"] == 5.0
    assert out["totals"]["projectedMonthlyCost"] == 150.0  # 5 × 30
    assert "degraded" not in out  # healthy scan -> no degraded flag


def test_cost_export_flags_degraded_cost_scan():
    # When the trajectory cost scan times out, NO provider carries a costSummary, so the
    # export would otherwise be byte-identical to genuine $0 spend. The diagnostics flag
    # must surface as out["degraded"] so a --cost consumer can tell the two apart.
    snapshot = {
        "timestamp": "2026-05-31T00:00:00Z",
        "providers": {"claude": {"status": "ok"}},  # cost scan never populated costSummary
        "diagnostics": {"cost_summary_timeout": True},
    }
    out = backend.cost_export(snapshot)
    assert out["providers"] == {}
    assert out["totals"]["cost30d"] == 0.0
    assert out["degraded"] is True
    assert out["degradedReason"] == "cost_summary_timeout"


# --- F1: threshold notifications --------------------------------------------

def _providers(claude_session=94, codex_session=100):
    return {
        "claude": {"label": "Claude", "limits": [
            {"label": "Session", "percent": claude_session},
            {"label": "Weekly", "percent": 50},
            {"label": "Credits", "percent": 0, "isExtraUsage": True},  # must be skipped
        ]},
        "codex": {"label": "Codex", "limits": [{"label": "Session", "percent": codex_session}]},
    }


def test_notifications_fire_dedupe_and_rearm(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "NOTIFY_STATE_PATH", tmp_path / "notify_state.json")
    cfg = {"notificationsEnabled": True, "notificationThresholds": [90, 100]}

    first = backend.compute_notifications(_providers(), cfg)
    keys = {(n["provider"], n["label"], n["threshold"]) for n in first}
    assert ("claude", "Session", 90) in keys      # highest crossed for 94%
    assert ("codex", "Session", 100) in keys       # limit reached
    assert ("claude", "Weekly", 90) not in keys    # 50% — below threshold
    assert all(n["label"] != "Credits" for n in first)  # isExtraUsage skipped
    assert any(n["urgency"] == "critical" for n in first)  # codex at 100

    # Same state -> nothing new (de-duped).
    assert backend.compute_notifications(_providers(), cfg) == []

    # Drop below 90 (re-arm), then cross again -> re-fires.
    backend.compute_notifications(_providers(claude_session=40), cfg)
    again = backend.compute_notifications(_providers(claude_session=92), cfg)
    assert ("claude", "Session", 90) in {(n["provider"], n["label"], n["threshold"]) for n in again}


def test_notifications_disabled_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "NOTIFY_STATE_PATH", tmp_path / "notify_state.json")
    assert backend.compute_notifications(_providers(), {"notificationsEnabled": False}) == []


def test_notifications_only_top_threshold(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "NOTIFY_STATE_PATH", tmp_path / "notify_state.json")
    cfg = {"notificationsEnabled": True, "notificationThresholds": [80, 90, 100]}
    out = backend.compute_notifications(
        {"x": {"label": "X", "limits": [{"label": "Session", "percent": 99}]}}, cfg)
    # 99% crosses 80 and 90 but should alert ONCE at the highest (90), not three times.
    assert len(out) == 1 and out[0]["threshold"] == 90


def _limit_provider(pct):
    # Single limit at a given percent — models Codex's rolling 5h session cap.
    return {"codex": {"label": "Codex", "limits": [{"label": "Session", "percent": pct}]}}


def test_notifications_rolling_window_dip_does_not_refire(tmp_path, monkeypatch):
    # Codex's 5h cap flaps 100→95→100 at the ceiling. Without hysteresis the 100-key
    # re-arms on the 95 dip and the "limit reached" critical popup re-fires. The gap
    # (_THRESHOLD_REARM_GAP=10) means 95 is inside the hold band [90, 100) -> no re-arm.
    monkeypatch.setattr(backend, "NOTIFY_STATE_PATH", tmp_path / "n.json")
    cfg = {"notificationsEnabled": True, "notificationThresholds": [90, 100]}

    first = backend.compute_notifications(_limit_provider(100), cfg)
    assert [n["threshold"] for n in first] == [100]  # fires once at 100

    assert backend.compute_notifications(_limit_provider(95), cfg) == []   # dip: held, no re-arm
    assert backend.compute_notifications(_limit_provider(100), cfg) == []  # back to 100: no re-fire


def test_notifications_gap_rearms_100_not_90(tmp_path, monkeypatch):
    # Dropping to 85 clears the 100-key's gap (85 < 100-10) but NOT the 90-key's
    # (85 >= 90-10). Returning to 100 re-fires the 100 alert only.
    monkeypatch.setattr(backend, "NOTIFY_STATE_PATH", tmp_path / "n.json")
    cfg = {"notificationsEnabled": True, "notificationThresholds": [90, 100]}

    assert [n["threshold"] for n in backend.compute_notifications(_limit_provider(100), cfg)] == [100]
    assert backend.compute_notifications(_limit_provider(85), cfg) == []  # re-arms 100, holds 90

    again = backend.compute_notifications(_limit_provider(100), cfg)
    assert [n["threshold"] for n in again] == [100]  # 100 re-fires; 90 stayed armed (no 90 alert)


def test_notifications_genuine_reset_rearms_both(tmp_path, monkeypatch):
    # A genuine window reset drops usage to ~0, clearing both keys' gaps. A later climb
    # re-fires (at the highest crossed threshold, 100).
    monkeypatch.setattr(backend, "NOTIFY_STATE_PATH", tmp_path / "n.json")
    cfg = {"notificationsEnabled": True, "notificationThresholds": [90, 100]}

    assert [n["threshold"] for n in backend.compute_notifications(_limit_provider(100), cfg)] == [100]
    assert backend.compute_notifications(_limit_provider(2), cfg) == []  # reset: re-arms 90 AND 100

    again = backend.compute_notifications(_limit_provider(100), cfg)
    assert [n["threshold"] for n in again] == [100]  # re-fires after the reset


# --- F2 + config setter: update_config_values -------------------------------

def test_update_config_values_whitelist_and_validate(tmp_path, monkeypatch):
    cfg_path = tmp_path / ".tallybar" / "config.json"
    monkeypatch.setattr(backend, "CONFIG_PATH", cfg_path)

    out = backend.update_config_values({
        "notificationsEnabled": False,
        "panelDisplayMode": "cost",
        "notificationThresholds": [95, 80, 80],  # dedup + sort
        "bogusKey": "ignored",
    })
    assert out["notificationsEnabled"] is False
    assert out["panelDisplayMode"] == "cost"
    assert out["notificationThresholds"] == [80, 95]
    assert "bogusKey" not in out

    # Invalid panel mode is ignored (prior value retained).
    out2 = backend.update_config_values({"panelDisplayMode": "nonsense"})
    assert out2["panelDisplayMode"] == "cost"

    # providers: canonical order, unknowns dropped, empty list ignored (never 0 tabs).
    out3 = backend.update_config_values({"providers": ["claude", "codex", "bogus"]})
    assert out3["providers"] == ["codex", "claude"]
    out4 = backend.update_config_values({"providers": []})
    assert out4["providers"] == ["codex", "claude"]  # empty kept prior

    # public_config surfaces the new keys with defaults when absent.
    pub = backend.public_config({})
    assert pub["notificationsEnabled"] is True
    assert pub["panelDisplayMode"] == "percent"
    assert pub["notificationThresholds"] == list(backend.DEFAULT_NOTIFY_THRESHOLDS)


def test_main_saves_snapshot_before_attaching_notifications(tmp_path, monkeypatch, capsys):
    # The cached snapshot must NEVER carry 'notifications' (else the cold-start cacheLoader
    # would replay stale alerts). This exercises the REAL main() ordering by driving main()
    # end-to-end: save_snapshot must be called with a snapshot that has no 'notifications'
    # key, which is only attached afterward for the live stdout. (The old test asserted on a
    # dict it built inline and never called main(), so it couldn't catch an ordering swap.)
    monkeypatch.setattr(backend, "NOTIFY_STATE_PATH", tmp_path / "n.json")

    live = {
        "timestamp": "2026-05-31T00:00:00Z",
        "providers": _providers(),  # claude Session 94 + codex Session 100 -> cross 90
        "config": {"notificationsEnabled": True, "notificationThresholds": [90]},
    }

    async def fake_build_snapshot(args):
        return live

    saved = {}

    def fake_save_snapshot(snap):
        saved["had_notifications_at_save"] = "notifications" in snap

    monkeypatch.setattr(backend, "build_snapshot", fake_build_snapshot)
    monkeypatch.setattr(backend, "save_snapshot", fake_save_snapshot)
    monkeypatch.setattr(backend, "load_snapshot", lambda: {})  # degraded cache -> force a save
    monkeypatch.setattr(sys, "argv", ["backend.py", "--once"])

    rc = backend.main()
    assert rc == 0

    # save_snapshot ran, and the snapshot it received had NO notifications key.
    assert saved.get("had_notifications_at_save") is False
    # ...but the live snapshot printed to stdout DOES carry them (claude/codex crossed 90%).
    printed = json.loads(capsys.readouterr().out)
    assert isinstance(printed.get("notifications"), list)
    assert any(n["provider"] in ("claude", "codex") for n in printed["notifications"])


def test_compute_notifications_no_rearm_on_transient_provider_failure(tmp_path, monkeypatch):
    # A crossing that already fired must NOT re-fire just because the provider transiently
    # fails (its fallback dict carries limits:[]). The armed-set is seeded from the persisted
    # state, so an unobserved limit keeps its armed key and the alert isn't replayed on
    # recovery. (Regression guard for the re-arm-on-timeout bug.)
    monkeypatch.setattr(backend, "NOTIFY_STATE_PATH", tmp_path / "n.json")
    cfg = {"notificationsEnabled": True, "notificationThresholds": [90]}

    # Run 1: Claude Session at 95% -> fires once and arms claude|Session|90.
    first = backend.compute_notifications(_providers(claude_session=95), cfg)
    assert ("claude", "Session", 90) in {(n["provider"], n["label"], n["threshold"]) for n in first}

    # Run 2: Claude transiently fails -> default_provider with empty limits. The USAGE
    # crossing must NOT re-arm/replay. (A status-transition alert for the new timeout IS
    # expected here — filter it out; the guard is that the usage row doesn't come back.)
    transient = {"claude": {"label": "Claude", "status": "timeout", "limits": []}}
    usage = [n for n in backend.compute_notifications(transient, cfg) if n.get("label") == "Session"]
    assert usage == []

    # Run 3: Claude recovers, still at 95%. The crossing is still armed -> NO duplicate alert.
    assert backend.compute_notifications(_providers(claude_session=95), cfg) == []


def test_compute_notifications_skips_redundant_state_write(tmp_path, monkeypatch):
    # The hot --once path must not rewrite+fsync notify_state.json on every refresh when the
    # armed-set is unchanged. _save_notify_state is only called when now_fired != prior.
    monkeypatch.setattr(backend, "NOTIFY_STATE_PATH", tmp_path / "n.json")
    cfg = {"notificationsEnabled": True, "notificationThresholds": [90]}

    backend.compute_notifications(_providers(claude_session=95), cfg)  # first write (state changes)
    calls = {"n": 0}
    real_save = backend._save_notify_state

    def counting_save(fired):
        calls["n"] += 1
        return real_save(fired)

    monkeypatch.setattr(backend, "_save_notify_state", counting_save)
    # Same state twice -> no further writes.
    backend.compute_notifications(_providers(claude_session=95), cfg)
    backend.compute_notifications(_providers(claude_session=95), cfg)
    assert calls["n"] == 0


# --- Status-transition notifications (error-UX cluster) ---------------------

def _status_providers(claude_status="ok", codex_status="ok"):
    """Providers carrying only a status (no limit crossings) so the returned notifications
    are purely status-transition alerts."""
    return {
        "claude": {"label": "Claude", "status": claude_status, "limits": []},
        "codex": {"label": "Codex", "status": codex_status, "limits": []},
    }


def test_status_notification_fires_once_on_ok_to_bad(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "NOTIFY_STATE_PATH", tmp_path / "n.json")
    cfg = {"notificationsEnabled": True}

    # Healthy baseline arms nothing.
    assert backend.compute_notifications(_status_providers(claude_status="ok"), cfg) == []

    # ok -> missing-cookies: exactly one status alert for Claude, with a usable payload.
    out = backend.compute_notifications(_status_providers(claude_status="missing-cookies"), cfg)
    assert len(out) == 1
    n = out[0]
    assert n["provider"] == "claude" and n["status"] == "missing-cookies"
    assert n["urgency"] == "normal" and n["title"] and n["body"]


def test_status_notification_no_refire_while_bad(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "NOTIFY_STATE_PATH", tmp_path / "n.json")
    cfg = {"notificationsEnabled": True}

    assert len(backend.compute_notifications(_status_providers(claude_status="unauthorized"), cfg)) == 1
    # Still bad next refresh -> no repeat (de-duped per outage episode).
    assert backend.compute_notifications(_status_providers(claude_status="unauthorized"), cfg) == []
    # A DIFFERENT bad status with no intervening recovery is still the same episode -> no refire.
    assert backend.compute_notifications(_status_providers(claude_status="api-error"), cfg) == []


def test_status_notification_refires_after_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "NOTIFY_STATE_PATH", tmp_path / "n.json")
    cfg = {"notificationsEnabled": True}

    assert len(backend.compute_notifications(_status_providers(claude_status="wallet-locked"), cfg)) == 1
    # Recovers to ok (re-arms), then breaks again -> fires a second time.
    assert backend.compute_notifications(_status_providers(claude_status="ok"), cfg) == []
    again = backend.compute_notifications(_status_providers(claude_status="wallet-locked"), cfg)
    assert len(again) == 1 and again[0]["provider"] == "claude"


def test_status_notification_ambiguous_status_holds_armed_state(tmp_path, monkeypatch):
    # A status that is neither actionable-bad nor healthy (e.g. not-running) HOLDS the armed
    # state: neither fires nor re-arms, so a later return to a bad status does NOT refire.
    monkeypatch.setattr(backend, "NOTIFY_STATE_PATH", tmp_path / "n.json")
    cfg = {"notificationsEnabled": True}

    assert len(backend.compute_notifications(_status_providers(claude_status="api-error"), cfg)) == 1
    assert backend.compute_notifications(_status_providers(claude_status="not-running"), cfg) == []
    assert backend.compute_notifications(_status_providers(claude_status="api-error"), cfg) == []


def test_status_notification_fires_on_generic_error(tmp_path, monkeypatch):
    """Defensive backstop: status='error' also triggers status notifications."""
    monkeypatch.setattr(backend, "NOTIFY_STATE_PATH", tmp_path / "n.json")
    cfg = {"notificationsEnabled": True}
    notifs = backend.compute_notifications(_status_providers(claude_status="error"), cfg)
    assert len(notifs) == 1
    assert notifs[0]["provider"] == "claude"
    assert notifs[0]["status"] == "error"


def test_status_notification_disabled_tracks_but_emits_nothing(tmp_path, monkeypatch):
    # Notifications off: no emit, but the armed-set still tracks the transition so a later
    # enable doesn't replay it (mirrors the usage/budget behaviour).
    monkeypatch.setattr(backend, "NOTIFY_STATE_PATH", tmp_path / "n.json")
    assert backend.compute_notifications(_status_providers(claude_status="timeout"),
                                         {"notificationsEnabled": False}) == []
    assert backend.compute_notifications(_status_providers(claude_status="timeout"),
                                         {"notificationsEnabled": True}) == []


def test_status_keys_disjoint_from_usage_keys(tmp_path, monkeypatch):
    # A usage crossing AND a status transition in the same run must both fire without
    # colliding in the shared armed-set (namespaces "status|<prov>" vs "<prov>|<label>|t").
    monkeypatch.setattr(backend, "NOTIFY_STATE_PATH", tmp_path / "n.json")
    cfg = {"notificationsEnabled": True, "notificationThresholds": [90]}
    providers = {
        "claude": {"label": "Claude", "status": "ok",
                   "limits": [{"label": "Session", "percent": 95}]},        # usage crossing
        "codex": {"label": "Codex", "status": "unauthorized", "limits": []},  # status transition
    }
    out = backend.compute_notifications(providers, cfg)
    usage = [n for n in out if n.get("label") == "Session"]
    status = [n for n in out if n.get("status") == "unauthorized"]
    assert len(usage) == 1 and usage[0]["provider"] == "claude"
    assert len(status) == 1 and status[0]["provider"] == "codex"


# --- Monthly cost-budget alerts ---------------------------------------------

def _cost_providers(*mtd_costs_per_provider):
    """Providers carrying a costSummary with calendar-month buckets. Each arg is a list
    of bucket dicts {inMonth, cost} for one provider."""
    out = {}
    for i, buckets in enumerate(mtd_costs_per_provider):
        out[f"p{i}"] = {"label": f"P{i}", "limits": [], "costSummary": {"monthlyTokenUsage": buckets}}
    return out


def test_all_ai_mtd_sums_inmonth_buckets_only_not_cost30d():
    # MTD = sum of inMonth bucket costs across providers; leading/trailing out-of-month
    # blanks are excluded, and the trailing-30-day cost30d is deliberately NOT used.
    providers = {
        "claude": {"costSummary": {
            "monthlyTokenUsage": [
                {"inMonth": True, "cost": 5.0}, {"inMonth": True, "cost": 3.0},
                {"inMonth": False, "cost": 99.0},  # prior-month leading blank -> excluded
            ],
            "cost30d": 1000.0,  # must NOT leak into the month-to-date figure
        }},
        "codex": {"costSummary": {"monthlyTokenUsage": [{"inMonth": True, "cost": 2.0}]}},
        "gemini": {"status": "ok"},  # no costSummary -> contributes 0, no raise
    }
    assert backend.all_ai_month_to_date_cost(providers) == 10.0
    assert backend.all_ai_month_to_date_cost({}) == 0.0


def test_budget_alert_fires_dedupes_rearms(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "NOTIFY_STATE_PATH", tmp_path / "n.json")
    # Neutralize the predictive crossing so this test stays about real-spend
    # crossings only — otherwise the calendar-pace projection of $85/$120 early in a month
    # would emit a second "all" notification and depend on today's date.
    monkeypatch.setattr(backend, "all_ai_projected_month_cost", lambda *a, **k: 0.0)
    cfg = {"notificationsEnabled": True, "notificationThresholds": [90, 100], "monthlyBudget": 100.0}

    # $85 of $100 = 85% -> crosses 80 (warn), not 100.
    p85 = _cost_providers([{"inMonth": True, "cost": 85.0}])
    first = [n for n in backend.compute_notifications(p85, cfg) if n["provider"] == "all"]
    assert len(first) == 1 and first[0]["threshold"] == 80 and first[0]["urgency"] == "normal"
    assert first[0]["label"] == "Monthly budget"

    # Same state -> de-duped (no replay).
    assert [n for n in backend.compute_notifications(p85, cfg) if n["provider"] == "all"] == []

    # Over budget -> 100 fires critical (highest crossed only, not 80 again).
    p120 = _cost_providers([{"inMonth": True, "cost": 120.0}])
    over = [n for n in backend.compute_notifications(p120, cfg) if n["provider"] == "all"]
    assert len(over) == 1 and over[0]["threshold"] == 100 and over[0]["urgency"] == "critical"

    # Drop below 80 (re-arm), then cross 80 again -> re-fires.
    backend.compute_notifications(_cost_providers([{"inMonth": True, "cost": 10.0}]), cfg)
    again = [n for n in backend.compute_notifications(p85, cfg) if n["provider"] == "all"]
    assert len(again) == 1 and again[0]["threshold"] == 80


def test_budget_alert_off_when_no_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "NOTIFY_STATE_PATH", tmp_path / "n.json")
    huge = _cost_providers([{"inMonth": True, "cost": 999.0}])
    assert [n for n in backend.compute_notifications(huge, {"notificationsEnabled": True}) if n["provider"] == "all"] == []
    assert [n for n in backend.compute_notifications(huge, {"notificationsEnabled": True, "monthlyBudget": 0}) if n["provider"] == "all"] == []


def test_budget_keys_disjoint_from_usage_keys(tmp_path, monkeypatch):
    # A usage crossing AND a budget crossing in the same run must both fire and not
    # collide in the shared armed-set (namespaces "<prov>|<label>|t" vs "budget|all|t").
    monkeypatch.setattr(backend, "NOTIFY_STATE_PATH", tmp_path / "n.json")
    monkeypatch.setattr(backend, "all_ai_projected_month_cost", lambda *a, **k: 0.0)
    providers = {"claude": {"label": "Claude",
                            "limits": [{"label": "Session", "percent": 95}],
                            "costSummary": {"monthlyTokenUsage": [{"inMonth": True, "cost": 100.0}]}}}
    cfg = {"notificationsEnabled": True, "notificationThresholds": [90], "monthlyBudget": 100.0}
    out = backend.compute_notifications(providers, cfg)
    provs = {n["provider"] for n in out}
    assert "claude" in provs and "all" in provs  # both surfaced from one run


def test_projected_month_cost_calendar_pace():
    import time as _time
    # Day 9 of a 30-day month: $45 mtd projects to 45/9*30 = $150.
    lt = _time.struct_time((2026, 6, 9, 12, 0, 0, 0, 0, 0))
    p = _cost_providers([{"inMonth": True, "cost": 45.0}])
    assert backend.all_ai_projected_month_cost(p, lt) == 150.0
    # Before day 3 the projection is left at mtd (day-1/2 extrapolation is wild).
    lt2 = _time.struct_time((2026, 6, 2, 12, 0, 0, 0, 0, 0))
    assert backend.all_ai_projected_month_cost(p, lt2) == 45.0


def test_budget_projected_alert_fires_once_and_dedupes(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "NOTIFY_STATE_PATH", tmp_path / "n.json")
    monkeypatch.setattr(backend, "all_ai_projected_month_cost", lambda *a, **k: 150.0)
    cfg = {"notificationsEnabled": True, "notificationThresholds": [90, 100], "monthlyBudget": 100.0}
    # mtd $50 (under budget, no real-spend crossing) but projected $150 -> on-pace alert.
    p = _cost_providers([{"inMonth": True, "cost": 50.0}])
    out = [n for n in backend.compute_notifications(p, cfg) if n["provider"] == "all"]
    assert len(out) == 1 and "on pace" in out[0]["title"]
    # Same state -> de-duped.
    assert [n for n in backend.compute_notifications(p, cfg) if n["provider"] == "all"] == []


def test_budget_projected_rearms_below_95pct(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "NOTIFY_STATE_PATH", tmp_path / "n.json")
    proj = {"v": 150.0}
    monkeypatch.setattr(backend, "all_ai_projected_month_cost", lambda *a, **k: proj["v"])
    cfg = {"notificationsEnabled": True, "notificationThresholds": [100], "monthlyBudget": 100.0}
    p = _cost_providers([{"inMonth": True, "cost": 50.0}])
    assert any("on pace" in n["title"] for n in backend.compute_notifications(p, cfg))
    # Projection drifting to 96% does NOT re-arm (hysteresis holds the armed state).
    proj["v"] = 96.0
    assert not any("on pace" in n["title"] for n in backend.compute_notifications(p, cfg))
    # Drop below 95% -> re-arm; climb back over budget -> fires again.
    proj["v"] = 90.0
    backend.compute_notifications(p, cfg)
    proj["v"] = 150.0
    assert any("on pace" in n["title"] for n in backend.compute_notifications(p, cfg))


def test_budget_projected_suppressed_when_mtd_over_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "NOTIFY_STATE_PATH", tmp_path / "n.json")
    monkeypatch.setattr(backend, "all_ai_projected_month_cost", lambda *a, **k: 300.0)
    cfg = {"notificationsEnabled": True, "notificationThresholds": [100], "monthlyBudget": 100.0}
    # Already over budget -> the real-spend 100% alert owns it; no separate on-pace alert.
    p = _cost_providers([{"inMonth": True, "cost": 130.0}])
    out = [n for n in backend.compute_notifications(p, cfg) if n["provider"] == "all"]
    assert len(out) == 1 and "reached" in out[0]["title"]
    assert not any("on pace" in n["title"] for n in out)


def test_budget_projected_tracks_while_disabled(tmp_path, monkeypatch):
    # Armed-set must track the projected crossing even while notifications are disabled,
    # so a later enable doesn't replay it.
    monkeypatch.setattr(backend, "NOTIFY_STATE_PATH", tmp_path / "n.json")
    monkeypatch.setattr(backend, "all_ai_projected_month_cost", lambda *a, **k: 150.0)
    p = _cost_providers([{"inMonth": True, "cost": 50.0}])
    off = {"notificationsEnabled": False, "notificationThresholds": [100], "monthlyBudget": 100.0}
    assert backend.compute_notifications(p, off) == []  # nothing emitted while off
    on = {"notificationsEnabled": True, "notificationThresholds": [100], "monthlyBudget": 100.0}
    # Now enabled, but the crossing was already armed -> not replayed.
    assert not any("on pace" in n["title"] for n in backend.compute_notifications(p, on))


def test_update_config_values_monthly_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "CONFIG_PATH", tmp_path / ".tallybar" / "config.json")
    assert backend.public_config({})["monthlyBudget"] == 0           # default surfaced
    assert backend.update_config_values({"monthlyBudget": 150.0})["monthlyBudget"] == 150.0
    assert backend.update_config_values({"monthlyBudget": "nonsense"})["monthlyBudget"] == 150.0  # garbage ignored
    assert backend.update_config_values({"monthlyBudget": -5})["monthlyBudget"] == 150.0          # out-of-range ignored
    assert backend.update_config_values({"monthlyBudget": 0})["monthlyBudget"] == 0.0             # 0 disables (valid)
    # A free-entry (non-preset) budget round-trips intact — the backend already
    # accepts any float 0..1e6, so the custom-value UI needs no backend change.
    assert backend.update_config_values({"monthlyBudget": 137.5})["monthlyBudget"] == 137.5


def test_update_config_values_custom_thresholds_roundtrip(tmp_path, monkeypatch):
    # An arbitrary custom threshold list (not the 80/90/100 presets) round-trips,
    # deduped + sorted, with out-of-range values dropped.
    monkeypatch.setattr(backend, "CONFIG_PATH", tmp_path / ".tallybar" / "config.json")
    out = backend.update_config_values({"notificationThresholds": [55, 73, 55, 120, 0, 99]})
    assert out["notificationThresholds"] == [55, 73, 99]  # dedup+sort; 120 and 0 out of (0,100]


# --- QoL batch 2026-07: weekly-80 alert, muted providers, actionUrl ----------

def test_weekly_alert_fires_once_and_rearms_on_reset(tmp_path, monkeypatch):
    """A weekly window crossing 80% fires ONE notification, de-duped per
    provider+label, re-armed only when usage drops back below the hysteresis floor
    (what a genuine window reset does). NOT keyed by resetAt — Claude's API recomputes
    resets_at per request, so a resetAt key churned and re-fired every refresh."""
    monkeypatch.setattr(backend, "NOTIFY_STATE_PATH", tmp_path / "notify_state.json")
    cfg = {"notificationsEnabled": True, "notificationThresholds": [100]}

    def providers(pct, reset_at):
        return {"claude": {"label": "Claude", "limits": [
            {"label": "Weekly", "percent": pct, "resetAt": reset_at, "windowMinutes": 10080},
        ]}}

    # Below 80 -> nothing.
    assert backend.compute_notifications(providers(60, "2026-07-10T00:00:00Z"), cfg) == []
    # Cross 80 -> one weekly alert.
    first = backend.compute_notifications(providers(85, "2026-07-10T00:00:00Z"), cfg)
    weekly = [n for n in first if n["threshold"] == 80 and n["provider"] == "claude"]
    assert len(weekly) == 1
    # Still over, resetAt drifted (server recomputes it each fetch) -> de-duped.
    assert [n for n in backend.compute_notifications(providers(90, "2026-07-10T00:00:00.181518Z"), cfg)
            if n["threshold"] == 80] == []
    # Dip into the hysteresis band (75-80) -> stays armed, no re-fire on the way back up.
    assert [n for n in backend.compute_notifications(providers(78, "2026-07-10T00:00:00Z"), cfg)
            if n["threshold"] == 80] == []
    assert [n for n in backend.compute_notifications(providers(85, "2026-07-10T00:00:00Z"), cfg)
            if n["threshold"] == 80] == []
    # Window resets (usage falls to ~0), climbs past 80 again -> re-fires once.
    assert backend.compute_notifications(providers(3, "2026-07-17T00:00:00Z"), cfg) == []
    again = backend.compute_notifications(providers(90, "2026-07-17T00:00:00Z"), cfg)
    assert len([n for n in again if n["threshold"] == 80 and n["provider"] == "claude"]) == 1


def test_weekly_alert_migrates_old_resetat_keys(tmp_path, monkeypatch):
    """An armed-set persisted by the old resetAt-keyed scheme neither replays the alert
    (the limit is still over 80 -> new key arms silently... it WOULD fire once since the
    new key isn't in previously_fired; verify exactly-once, then silence) nor leaks the
    stale timestamp keys forever."""
    state = tmp_path / "notify_state.json"
    monkeypatch.setattr(backend, "NOTIFY_STATE_PATH", state)
    state.write_text(json.dumps({"fired": ["weekly80|claude|2026-07-09T19:59:59.573816+00:00"]}))
    cfg = {"notificationsEnabled": True, "notificationThresholds": [100]}
    provs = {"claude": {"label": "Claude", "limits": [
        {"label": "Weekly", "percent": 85, "resetAt": "2026-07-09T20:00:00Z", "windowMinutes": 10080},
    ]}}
    backend.compute_notifications(provs, cfg)  # migration run (may fire once for the new key)
    fired = set(json.loads(state.read_text())["fired"])
    assert fired == {"weekly80|claude|Weekly"}  # old timestamp key pruned
    # Subsequent refreshes: silent.
    assert [n for n in backend.compute_notifications(provs, cfg) if n["threshold"] == 80] == []


def test_muted_provider_raises_no_notifications(tmp_path, monkeypatch):
    """A muted provider fires no status OR usage notifications."""
    monkeypatch.setattr(backend, "NOTIFY_STATE_PATH", tmp_path / "notify_state.json")
    provs = {"codex": {"label": "Codex", "status": "unauthorized",
                       "limits": [{"label": "Session", "percent": 100}]}}
    cfg = {"notificationsEnabled": True, "notificationThresholds": [90, 100],
           "mutedProviders": ["codex"]}
    assert backend.compute_notifications(provs, cfg) == []
    # Same provider, not muted -> it DOES notify (status + usage).
    cfg_unmuted = {**cfg, "mutedProviders": []}
    out = backend.compute_notifications(provs, cfg_unmuted)
    assert any(n["provider"] == "codex" for n in out)


def test_update_config_values_muted_providers_whitelist(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "CONFIG_PATH", tmp_path / ".tallybar" / "config.json")
    out = backend.update_config_values({"mutedProviders": ["gemini", "bogus", "claude"]})
    # Canonical order, unknowns dropped.
    assert out["mutedProviders"] == ["claude", "gemini"]
    # Public config surfaces it.
    assert backend.public_config(out)["mutedProviders"] == ["claude", "gemini"]


def test_public_config_defaults_muted_empty():
    assert backend.public_config({})["mutedProviders"] == []


def test_snapshot_carries_generatedat_timestamp():
    """The snapshot carries an ISO timestamp used for the staleness stamp."""
    import argparse
    import asyncio
    args = argparse.Namespace(timeout=6.0, background=True, no_network=True)
    snap = asyncio.run(backend.build_snapshot(args))
    assert isinstance(snap.get("timestamp"), str) and "T" in snap["timestamp"]


def test_gemini_unauthorized_carries_action_url():
    """Gemini's signed-out path surfaces a sign-in actionUrl."""
    from providers import gemini
    html_signed_out = (
        'cfb2h="build" FdrFJe="sid" '
        'href="https://accounts.google.com/v3/signin/identifier?flow=x"'
    )
    assert gemini._looks_signed_out(html_signed_out, None, "build", "sid")
    url = gemini._extract_signin_url(html_signed_out)
    assert url.startswith("https://accounts.google.com/v3/signin")
    # No URL in the HTML -> Gemini home fallback.
    assert gemini._extract_signin_url("") == gemini.GEMINI_SIGNIN_FALLBACK


def test_budget_alert_not_replayed_after_cost_scan_timeout(tmp_path, monkeypatch):
    """A timed-out cost scan leaves no costSummary, which reads as $0 month-to-date.
    Evaluating the budget against that re-armed every crossing, so the next good refresh
    replayed "monthly budget reached". The budget armed-state must HOLD instead."""
    monkeypatch.setattr(backend, "NOTIFY_STATE_PATH", tmp_path / "n.json")
    monkeypatch.setattr(backend, "all_ai_projected_month_cost", lambda *a, **k: 0.0)
    cfg = {"notificationsEnabled": True, "notificationThresholds": [100], "monthlyBudget": 100.0}
    over = _cost_providers([{"inMonth": True, "cost": 120.0}])
    no_cost = {"p0": {"label": "P0", "limits": []}}  # what a timed-out scan leaves behind

    def budget_alerts(providers, **kw):
        return [n for n in backend.compute_notifications(providers, cfg, **kw) if n["provider"] == "all"]

    assert len(budget_alerts(over)) == 1                    # first crossing fires
    assert budget_alerts(no_cost) == []                     # scan timed out: nothing, and no re-arm
    assert budget_alerts(over) == []                        # next good run: NOT replayed
    # Same with the explicit flag main() passes when diagnostics report a timeout, even if
    # a (carried-forward, stale) costSummary happens to read low.
    low = _cost_providers([{"inMonth": True, "cost": 1.0}])
    assert budget_alerts(low, cost_available=False) == []
    assert budget_alerts(over) == []
    # A genuine drop (month rollover) with cost data present still re-arms normally.
    assert budget_alerts(low) == []
    assert len(budget_alerts(over)) == 1


def test_main_passes_cost_timeout_to_notifications(tmp_path, monkeypatch, capsys):
    """main() must tell compute_notifications when the cost scan timed out."""
    import sys as _sys
    seen = {}

    async def fake_build(args):
        return {"ok": True, "timestamp": "2026-09-22T12:00:00+00:00", "providers": {},
                "diagnostics": {"cost_summary_timeout": True}, "config": {}}

    def fake_notify(providers, config, cost_available=True):
        seen["cost_available"] = cost_available
        return []

    monkeypatch.setattr(backend, "build_snapshot", fake_build)
    monkeypatch.setattr(backend, "compute_notifications", fake_notify)
    monkeypatch.setattr(backend, "save_snapshot", lambda s: None)
    monkeypatch.setattr(backend, "load_snapshot", lambda: {})
    monkeypatch.setattr(_sys, "argv", ["backend.py", "--once"])
    assert backend.main() == 0
    assert seen["cost_available"] is False
