#!/usr/bin/env python3
"""TallyBar Plasma telemetry backend — orchestrator and CLI entry point.

This is the entry point invoked by the Plasma DataSource. All domain logic
lives in submodules; this file re-exports every public name so that
``import backend`` continues to work for tests and any other consumer.
"""
from __future__ import annotations

import argparse
import asyncio
import calendar
import copy
import datetime as dt
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from accounting import default_provider, enrich_ui_formatting, now_iso
from cookies import collect_browser_sessions, host_matches_any, select_session_cookies
from http_helpers import bounded_provider, run_threaded_provider, scrub_credentials
from io_helpers import atomic_write_text as _atomic_write_text, flock_with_timeout, to_daemon_thread
from providers import (
    GEMINI_DOMAINS,
    apply_claude_statusline_fallback,
    apply_cost_summaries,
    apply_google_one_credits,
    choose_antigravity_result,
    compute_local_cost_summaries,
    google_one_credit_fresh,
    load_claude_statusline,
    run_antigravity_local,
    run_antigravity_remote,
    run_claude_api,
    run_codex_rpc,
    run_gemini_web,
    run_google_one_credits,
    run_grok_local,
    run_openai_cookie_api,
)

__all__ = [
    "CONFIG_PATH",
    "DEFAULT_NOTIFY_THRESHOLDS",
    "GEMINI_DOMAINS",
    "NOTIFY_STATE_PATH",
    "PROVIDER_ORDER",
    "SNAPSHOT_PATH",
    "all_ai_month_to_date_cost",
    "all_ai_projected_month_cost",
    "apply_claude_statusline_fallback",
    "apply_cost_summaries",
    "apply_google_one_credits",
    "bounded_provider",
    "build_snapshot",
    "carry_forward_partial_antigravity_lanes",
    "carry_forward_provider_last_good",
    "choose_antigravity_result",
    "collect_browser_sessions",
    "compute_local_cost_summaries",
    "compute_notifications",
    "cost_export",
    "default_provider",
    "enrich_ui_formatting",
    "flock_with_timeout",
    "google_one_credit_fresh",
    "host_matches_any",
    "load_claude_statusline",
    "load_config",
    "load_snapshot",
    "main",
    "now_iso",
    "public_config",
    "refresh_worst_case_seconds",
    "run_antigravity_local",
    "run_antigravity_remote",
    "run_claude_api",
    "run_codex_rpc",
    "run_gemini_web",
    "run_google_one_credits",
    "run_grok_local",
    "run_openai_cookie_api",
    "run_threaded_provider",
    "save_config",
    "save_snapshot",
    "scrub_credentials",
    "to_daemon_thread",
    "update_config_values",
    "update_refresh_interval",
]

PROVIDER_ORDER = ("codex", "claude", "gemini", "antigravity", "grok")
CONFIG_PATH = Path.home() / ".tallybar" / "config.json"
SNAPSHOT_PATH = Path.home() / ".tallybar" / "last_snapshot.json"
NOTIFY_STATE_PATH = Path.home() / ".tallybar" / "notify_state.json"
DEFAULT_NOTIFY_THRESHOLDS = (90, 100)
# Hysteresis gap for re-arming usage-threshold keys. A rolling-window limit (e.g. Codex's
# 5h session cap) flaps 100→98→100 at the ceiling; without a gap the 100-key re-arms on
# each dip and the critical popup re-fires every few minutes. Re-arm only when usage drops
# meaningfully below the threshold — a genuine window reset drops to ~0 and always re-arms.
_THRESHOLD_REARM_GAP = 10
# Monthly-budget alert levels: warn when approaching (80%) and at/over budget (100%).
# Distinct from the per-limit usage thresholds above (those are usage %, this is total $).
_BUDGET_THRESHOLDS = (80, 100)

# Provider statuses that mean the provider needs user action (sign-in / unlock) or hit a
# transient outage worth surfacing. compute_notifications fires ONE desktop notification per
# provider when it first enters one of these, re-armed only when it returns to a healthy
# status (_LIVE_PROVIDER_STATUSES) — so an outage alerts once per episode, never every refresh.
# wallet-state-unknown rides alongside wallet-locked (see the build_snapshot relabel): both mean
# the browser cookies were unreadable this run, not that the user signed out.
_ACTIONABLE_BAD_STATUSES = frozenset((
    "missing-cookies", "unauthorized", "wallet-locked", "wallet-state-unknown",
    "timeout", "api-error", "error",
))


def refresh_worst_case_seconds(timeout: float) -> float:
    """Upper bound on build_snapshot's wall time for a given per-provider ``timeout``.

    The phases run in sequence: browser-cookie collection (bounded at ``timeout + 1``), the
    provider TaskGroup (``timeout + 1``), then the lazy OpenAI cookie fallback, which is capped
    to whatever remains of this budget (at most ``timeout``). The cost scan overlaps them and
    is bounded from the start. The widget's refresh watchdog (main.qml ``refreshWatchdogMs``)
    must exceed this plus interpreter startup — otherwise a slow-but-healthy refresh is
    declared dead and its result dropped. → tests/test_backend.py::test_refresh_watchdog_*"""
    return 3 * timeout + 2


def _open_lock_0600(lock_path: Path):
    """Open ``lock_path`` for append at 0600 (owner-only) for flock use.

    A plain ``lock_path.open("a")`` creates the lockfile at 0644 under the default umask,
    leaving these ~/.tallybar coordination files group/world-readable. Create with an
    explicit 0600 mode via ``os.open`` (O_APPEND, never truncate), and best-effort
    tighten a pre-existing lockfile to 0600. flock semantics are unchanged."""
    fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except OSError:
        pass
    return os.fdopen(fd, "a")


def all_ai_month_to_date_cost(providers: dict[str, Any]) -> float:
    """Total month-to-date pay-per-use cost across EVERY provider, summed from each
    costSummary's calendar-month buckets (``monthlyTokenUsage[].cost`` where ``inMonth``).

    This is the true MONTH-to-date figure — deliberately NOT ``cost30d``, which is a
    trailing-30-day window and would mislabel a "monthly" budget near month boundaries.
    Guarded so a provider with no costSummary (or a degraded scan) contributes 0 rather
    than raising."""
    total = 0.0
    for provider in providers.values():
        if not isinstance(provider, dict):
            continue
        cost = provider.get("costSummary")
        if not isinstance(cost, dict):
            continue
        for bucket in cost.get("monthlyTokenUsage") or []:
            if isinstance(bucket, dict) and bucket.get("inMonth"):
                try:
                    total += float(bucket.get("cost") or 0.0)
                except (TypeError, ValueError):
                    pass
    return total


def all_ai_projected_month_cost(providers: dict[str, Any], now: time.struct_time | None = None) -> float:
    """Calendar-pace projection of full-month spend: ``mtd / day_of_month * days_in_month``.

    Deliberately uses the SAME calendar-month window as ``all_ai_month_to_date_cost`` (and
    the budget machinery), NOT each provider's ``projectedMonthlyCost`` — that figure is a
    trailing-7d burn × 30, a different basis that would clash with a calendar-month budget.
    Returns mtd unchanged before day 3 of the month (day-1/2 extrapolation is wild — the
    caller skips the alert then anyway)."""
    lt = now or time.localtime()
    day_of_month = lt.tm_mday
    days_in_month = calendar.monthrange(lt.tm_year, lt.tm_mon)[1]
    mtd = all_ai_month_to_date_cost(providers)
    if day_of_month < 3:
        return mtd
    return mtd / day_of_month * days_in_month


# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------

def load_config() -> dict[str, Any]:
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def public_config(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": str(CONFIG_PATH),
        "providers": config.get("providers", list(PROVIDER_ORDER)),
        "refreshIntervalMinutes": config.get("refreshIntervalMinutes"),
        "notificationsEnabled": config.get("notificationsEnabled", True),
        "notificationThresholds": config.get("notificationThresholds", list(DEFAULT_NOTIFY_THRESHOLDS)),
        "panelDisplayMode": config.get("panelDisplayMode", "percent"),
        "monthlyBudget": config.get("monthlyBudget", 0),
        # Per-provider mute. A muted provider is dropped from the panel's
        # attention/badge logic and from desktop notifications, but stays visible in the popup.
        "mutedProviders": config.get("mutedProviders", []),
    }


# Weekly-alert threshold (feature 6): fire ONE desktop notification the first time any
# provider's weekly window crosses this used-% — independent of the user's configurable
# usage thresholds, re-armed when usage falls back below the hysteresis floor (a
# genuine window reset drops usage to ~0). Keyed provider|label, NOT resetAt.
_WEEKLY_ALERT_THRESHOLD = 80


def _is_weekly_limit(limit: dict[str, Any]) -> bool:
    """A weekly-reset usage window. Matches the QML pace detector: an exact 'weekly' label,
    a label starting 'week', or a 7-day (10080-minute) window."""
    if not isinstance(limit, dict):
        return False
    label = str(limit.get("label") or "").strip().lower()
    if label == "weekly" or label.startswith("week"):
        return True
    wm = limit.get("windowMinutes")
    return isinstance(wm, (int, float)) and int(wm) == 10080


def _load_notify_state() -> set[str]:
    """The set of (provider|label|threshold) crossings already notified. A crossing
    stays 'fired' until usage drops back below the threshold (which removes it and
    re-arms it) — so the user is alerted once per crossing, not every refresh."""
    try:
        data = json.loads(NOTIFY_STATE_PATH.read_text(encoding="utf-8"))
        fired = data.get("fired") if isinstance(data, dict) else None
        return set(str(k) for k in fired) if isinstance(fired, list) else set()
    except Exception:
        return set()


def _save_notify_state(fired: set[str]) -> None:
    try:
        _atomic_write_text(NOTIFY_STATE_PATH, json.dumps({"fired": sorted(fired)}))
    except Exception:
        pass


def _status_notification(pkey: str, plabel: str, status: str, provider: dict[str, Any]) -> dict[str, Any]:
    """Desktop-notification payload for a provider that just entered an actionable-bad
    status. The wording lives here so it stays in one place; the QML side only reads
    title/body/urgency. Shape mirrors the usage/budget notifications (provider/label/urgency)
    so a caller can filter uniformly."""
    msg = str(provider.get("message") or "").strip()
    if status == "wallet-locked":
        title = f"{plabel}: KWallet locked"
        body = f"Unlock KWallet so TallyBar can read {plabel}'s session usage."
    elif status == "wallet-state-unknown":
        title = f"{plabel}: KWallet unavailable"
        body = f"TallyBar couldn't check KWallet, so {plabel}'s session usage is unavailable."
    elif status == "missing-cookies":
        title = f"{plabel}: sign-in needed"
        body = f"TallyBar can't read {plabel}'s browser session — sign in, then refresh."
    elif status == "unauthorized":
        title = f"{plabel}: sign-in needed"
        body = msg or f"{plabel} rejected the saved session — sign in again to restore usage."
    elif status == "timeout":
        title = f"{plabel}: not responding"
        body = f"The {plabel} usage request timed out."
    else:  # api-error
        title = f"{plabel}: usage error"
        body = msg or f"Couldn't load {plabel} usage."
    return {
        "provider": pkey,
        "providerLabel": plabel,
        "label": "Status",
        "status": status,
        "urgency": "normal",
        "title": title,
        "body": body,
    }


def compute_notifications(providers: dict[str, Any], config: dict[str, Any],
                          cost_available: bool = True) -> list[dict[str, Any]]:
    """Detect usage-limit threshold crossings and return only the NEWLY-crossed ones
    (de-duped via NOTIFY_STATE_PATH so each crossing alerts once). The QML side fires
    these via notify-send. Returns [] when notifications are disabled — but still keeps
    the armed-set tracking current usage so toggling notifications off→on neither
    replays old crossings nor suppresses a fresh one.

    ``cost_available=False`` (the cost scan timed out or errored this run) HOLDS the
    budget armed-state untouched, exactly like a transiently-failed provider holds its
    usage keys: a missing costSummary reads as $0 month-to-date, and evaluating the budget
    against that would re-arm every crossing and replay the alert on the next good run."""
    enabled = config.get("notificationsEnabled", True) is not False
    raw_thresholds = config.get("notificationThresholds") or list(DEFAULT_NOTIFY_THRESHOLDS)
    if not isinstance(raw_thresholds, (list, tuple)):
        # A hand-edited config.json can hold a scalar here; iterating it would raise and
        # (before the main() tail guard) kill the whole refresh's stdout.
        raw_thresholds = list(DEFAULT_NOTIFY_THRESHOLDS)
    thresholds = sorted({int(t) for t in raw_thresholds if isinstance(t, (int, float)) and 0 < t <= 100})
    if not thresholds:
        return []

    import fcntl
    NOTIFY_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_path = NOTIFY_STATE_PATH.parent / ".notify.lock"
    # "a" not "w": opening for write truncates the lockfile to 0 bytes BEFORE the lock is
    # held (a TOCTOU, same as config.json's .config.lock). Bounded non-blocking wait
    # so two concurrent --once invocations can't lost-update the armed-set
    # (a process that loses the race raises, caught by main()'s post-snapshot tail guard,
    # which degrades to notifications=[] for that run rather than crashing stdout).
    with _open_lock_0600(lock_path) as lock_fd:
        if not flock_with_timeout(lock_fd, 5.0):
            raise TimeoutError("could not acquire notify-state lock (another write in progress)")
        try:
            previously_fired = _load_notify_state()
            # Seed from the persisted armed-set so a crossing stays fired across refreshes — and,
            # crucially, is NOT re-armed when a provider transiently fails. A timed-out/errored
            # provider comes back as default_provider with limits:[], so the loop below never
            # touches its keys and they're preserved; only a limit we ACTUALLY observe this run
            # can change its armed state (the explicit discard handles a genuine drop). Rebuilding
            # now_fired from scratch each run (the old behaviour) made any transient fetch failure
            # indistinguishable from "dropped below threshold" and replayed an already-sent alert.
            now_fired: set[str] = set(previously_fired)
            notifications: list[dict[str, Any]] = []
            # Muted providers raise no desktop notifications (status, usage
            # thresholds, or the weekly-80 alert). Their armed-set keys are simply left
            # untouched (like a transient failure), so unmuting doesn't replay old crossings.
            raw_muted = config.get("mutedProviders")
            muted = {str(m) for m in raw_muted} if isinstance(raw_muted, (list, tuple)) else set()
            for pkey, provider in providers.items():
                if not isinstance(provider, dict):
                    continue
                if pkey in muted:
                    continue
                plabel = str(provider.get("label") or pkey.title())
                # Status-transition alerts: fire ONCE when a provider first enters an
                # actionable-bad status (sign-in / unlock / timeout / api-error), re-armed only
                # when it returns to a healthy status. Same armed-set + de-dup as the usage
                # crossings; keys are namespaced "status|<provider>", disjoint from the usage
                # ("<provider>|<label>|<pct>") and budget ("budget|all|...") keys. An ambiguous
                # status (loading / not-running / no-port / …) HOLDS the armed state — it must
                # not re-arm, or a provider that flaps error→not-running→error would refire.
                status = str(provider.get("status") or "")
                skey = f"status|{pkey}"
                if status in _ACTIONABLE_BAD_STATUSES:
                    if skey not in previously_fired and enabled:
                        notifications.append(_status_notification(pkey, plabel, status, provider))
                    now_fired.add(skey)
                elif status in _LIVE_PROVIDER_STATUSES:
                    now_fired.discard(skey)  # recovered -> re-arm for the next outage
                for limit in provider.get("limits", []) or []:
                    if not isinstance(limit, dict) or limit.get("isExtraUsage"):
                        continue  # skip credit/extra-usage rows — they're not capacity limits
                    label = str(limit.get("label") or "").strip() or "Usage"
                    try:
                        pct = float(limit.get("percent") or 0.0)
                    except (TypeError, ValueError):
                        continue
                    # Weekly-window 80% alert (feature 6): independent of the user's usage
                    # thresholds. Fire ONCE when a weekly window first crosses 80% used, keyed
                    # by provider+label — NOT by resetAt: Claude's API recomputes resets_at on
                    # every request (microsecond drift each fetch), so a resetAt-based key churned
                    # every refresh and re-fired the alert every 5 minutes. Re-arm when usage
                    # drops back below the threshold with hysteresis (a genuine window reset
                    # drops usage to ~0; the 5-point gap stops a value hovering at the boundary
                    # from flapping fire→re-arm→fire). Label in the key also stops a provider's
                    # two weekly rows (Claude "Weekly" + "Fable") from sharing one armed slot.
                    if _is_weekly_limit(limit):
                        wkey = f"weekly80|{pkey}|{label}"
                        # One-time migration: drop old-format resetAt-timestamp keys.
                        for stale in [k for k in now_fired
                                      if k.startswith(f"weekly80|{pkey}|") and "T" in k.rsplit("|", 1)[-1]]:
                            now_fired.discard(stale)
                        if pct >= _WEEKLY_ALERT_THRESHOLD:
                            if wkey not in previously_fired and enabled:
                                notifications.append({
                                    "provider": pkey,
                                    "providerLabel": plabel,
                                    "label": label,
                                    "percent": round(pct, 1),
                                    "threshold": _WEEKLY_ALERT_THRESHOLD,
                                    "urgency": "normal",
                                    "title": f"{plabel}: weekly usage past {_WEEKLY_ALERT_THRESHOLD}%",
                                    "body": f"{label} weekly window is at {pct:.0f}% used.",
                                })
                            now_fired.add(wkey)
                        elif pct < _WEEKLY_ALERT_THRESHOLD - 5:
                            now_fired.discard(wkey)
                        # else 75–80%: hold armed state (hysteresis band)
                    # This limit WAS observed this run: arm each threshold it currently exceeds.
                    # Re-arm (discard) a key only when usage drops meaningfully below the
                    # threshold (hysteresis) — a rolling-window limit flapping at the ceiling
                    # (100→98→100) would otherwise re-fire the critical popup on each dip. In the
                    # gap band [t - _THRESHOLD_REARM_GAP, t) hold the current armed state; a genuine
                    # window reset drops to ~0 and clears the gap, re-arming for the next crossing.
                    for t in thresholds:
                        key = f"{pkey}|{label}|{t}"
                        if pct >= t:
                            now_fired.add(key)
                        elif pct < t - _THRESHOLD_REARM_GAP:
                            now_fired.discard(key)
                        # else: gap band — hold armed state (no add, no discard)
                    # Notify on the HIGHEST threshold this limit currently exceeds, so a jump
                    # straight past 90→100 alerts once at 100, not twice.
                    crossed = [t for t in thresholds if pct >= t]
                    if not crossed:
                        continue
                    top = max(crossed)
                    key = f"{pkey}|{label}|{top}"
                    if key in previously_fired:
                        continue  # already alerted for this crossing
                    if not enabled:
                        continue  # state still tracks the crossing; just don't emit while off
                    at_limit = top >= 100
                    notifications.append({
                        "provider": pkey,
                        "providerLabel": plabel,
                        "label": label,
                        "percent": round(pct, 1),
                        "threshold": top,
                        "urgency": "critical" if at_limit else "normal",
                        "title": f"{plabel}: {label} {'limit reached' if at_limit else f'{top}% used'}",
                        "body": (f"{label} is at {pct:.0f}% — limit reached." if at_limit
                                 else f"{label} usage has passed {top}% ({pct:.0f}%)."),
                    })
            # Monthly-budget crossings: total month-to-date spend vs the user's optional budget.
            # Shares the same armed-set + de-dup as the usage crossings above — its keys are
            # namespaced "budget|all|<pct>", disjoint from the "<provider>|<label>|<pct>" usage
            # keys, so a single state write covers both and neither re-fires the other. As with
            # usage, the armed-set tracks the crossing even while notifications are disabled (so a
            # later enable doesn't replay it), and only EMITS while enabled.
            try:
                budget = float(config.get("monthlyBudget") or 0.0)
            except (TypeError, ValueError):
                budget = 0.0
            # No cost data this run -> hold (don't evaluate against a phantom $0). Also holds
            # when no provider carries a costSummary at all (nothing to compare against).
            has_cost = cost_available and any(
                isinstance(p, dict) and isinstance(p.get("costSummary"), dict) for p in providers.values())
            if budget > 0.0 and has_cost:
                mtd = all_ai_month_to_date_cost(providers)
                pct = (mtd / budget) * 100.0
                for t in _BUDGET_THRESHOLDS:
                    key = f"budget|all|{t}"
                    if pct >= t:
                        now_fired.add(key)
                    else:
                        now_fired.discard(key)
                crossed = [t for t in _BUDGET_THRESHOLDS if pct >= t]
                if crossed:
                    top = max(crossed)
                    key = f"budget|all|{top}"
                    if key not in previously_fired and enabled:
                        at_limit = top >= 100
                        notifications.append({
                            "provider": "all",
                            "providerLabel": "All AI",
                            "label": "Monthly budget",
                            "percent": round(pct, 1),
                            "threshold": top,
                            "urgency": "critical" if at_limit else "normal",
                            "title": ("All AI: monthly budget reached" if at_limit
                                      else f"All AI: {top}% of monthly budget"),
                            "body": f"${mtd:,.2f} of ${budget:,.0f} this month ({pct:.0f}%).",
                        })
                # Predictive crossing: warn ONCE when projected full-month spend first
                # crosses the budget while actual spend is still under it — the real-spend 100%
                # alert above owns the crossing once mtd reaches budget. Calendar-pace projection
                # (all_ai_projected_month_cost). Hysteresis: arm at projected >= budget, re-arm
                # (discard) only once projected falls back below 95% of budget, so a projection
                # hovering at the boundary doesn't flap. Day-of-month < 3 → projection == mtd, so
                # the arm condition (projected >= budget while mtd < budget) can't trip early.
                proj_key = "budget|all|projected"
                lt = time.localtime()
                projected = all_ai_projected_month_cost(providers, lt)
                if mtd < budget and projected >= budget:
                    if proj_key not in previously_fired and enabled:
                        dim = calendar.monthrange(lt.tm_year, lt.tm_mon)[1]
                        elapsed_pct = lt.tm_mday / dim * 100.0
                        notifications.append({
                            "provider": "all",
                            "providerLabel": "All AI",
                            "label": "Monthly budget",
                            "percent": round((projected / budget) * 100.0, 1),
                            "threshold": 100,
                            "urgency": "normal",
                            "title": "All AI: on pace to exceed monthly budget",
                            "body": (f"Projected ${projected:,.2f} vs ${budget:,.0f} budget "
                                     f"({elapsed_pct:.0f}% of month elapsed)."),
                        })
                    now_fired.add(proj_key)
                elif projected < budget * 0.95:
                    now_fired.discard(proj_key)
                # else (95%–100% of budget, or mtd >= budget): hold the current armed state.
            if now_fired != previously_fired:
                _save_notify_state(now_fired)  # only rewrite+fsync when the armed-set actually changed
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    return notifications


def save_config(config: dict[str, Any]) -> dict[str, Any]:
    _atomic_write_text(CONFIG_PATH, json.dumps(config, indent=2, sort_keys=True) + "\n")
    return config


def save_snapshot(snapshot: dict[str, Any]) -> None:
    """Atomically cache the latest snapshot so the widget can paint last-known
    values instantly on cold start. Best-effort: never let a write failure
    affect the snapshot that is being printed to stdout."""
    try:
        _atomic_write_text(SNAPSHOT_PATH, json.dumps(snapshot, separators=(",", ":")))
    except Exception:
        pass


def load_snapshot() -> dict[str, Any]:
    try:
        with SNAPSHOT_PATH.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# Statuses that mean a provider actually produced usable data this run. build_snapshot
# always returns top-level ok=True (per-provider fallbacks carry the real status), so
# the cache-quality gate keys off these instead.
_LIVE_PROVIDER_STATUSES = frozenset(("ok", "cookies-ready"))


def _snapshot_has_live_data(snapshot: Any) -> bool:
    """True if any provider in the snapshot reached a healthy status. Used to avoid
    overwriting the last-known-GOOD cache with a fully-degraded snapshot (every
    provider timed out/errored during a transient network/KWallet outage), which
    would make cold start paint a wall of 'timed out' until the next live refresh."""
    if not isinstance(snapshot, dict):
        return False
    providers = snapshot.get("providers")
    if not isinstance(providers, dict):
        return False
    return any(isinstance(p, dict) and p.get("status") in _LIVE_PROVIDER_STATUSES
               for p in providers.values())


def carry_forward_partial_antigravity_lanes(provider: dict[str, Any],
                                            cached: Any) -> dict[str, Any]:
    """Freeze last-known Antigravity session lanes across a ``partial`` cloud read.

    A ``partial`` Antigravity result is authenticated but carries NO session lanes — the full
    per-model quota endpoint 403'd and only the Gemini-only daily quota answered. Because the OTHER
    providers are live, ``_snapshot_has_live_data`` is True and this snapshot overwrites the cache,
    so the last-known lanes would simply vanish (blank bars). Graft the previous snapshot's lanes
    and tag them ``stale`` so the widget shows the frozen prior reading instead — never mistaken for
    a fresh confident read (the status stays ``partial``). A later full ``ok`` read replaces them
    wholesale. No-op for any other status or when the cache has no Antigravity lanes. Mutates and
    returns ``provider``."""
    if not isinstance(provider, dict):
        return provider
    if provider.get("status") != "partial" or provider.get("limits"):
        return provider
    prev = ((cached or {}).get("providers") or {}).get("antigravity") or {} if isinstance(cached, dict) else {}
    prev_limits = prev.get("limits")
    if isinstance(prev_limits, list) and prev_limits:
        provider["limits"] = prev_limits
        provider["stale"] = True
    return provider


# Provider statuses that represent a TRANSIENT failure (network blip, Cloudflare
# challenge, one-off timeout) — safe to paper over with the last-known-good reading for
# a bounded grace window. A genuinely-expired cookie also surfaces as "unauthorized",
# so the window is deliberately short: after it elapses the real error resurfaces.
_TRANSIENT_PROVIDER_STATUSES = frozenset(("timeout", "api-error", "unauthorized", "error"))


def carry_forward_provider_last_good(provider: dict[str, Any], cached: Any,
                                     *, max_stale_seconds: int = 900) -> dict[str, Any]:
    """Freeze the last-known-good reading over a TRANSIENT provider failure.

    Claude's internal API occasionally trips a Cloudflare challenge / rate limit, surfacing as
    a 403 (``unauthorized``) or 429 (``api-error``) even though the cookie is valid — a rare,
    intermittent blip. Because the OTHER providers are live, ``_snapshot_has_live_data`` is True
    and the failing snapshot overwrites the cache, so Claude's good numbers would flip to an
    error card on every such blip. Guard: when ``provider`` has a transient-failure status AND no
    usable limits, and the cached snapshot holds a HEALTHY entry (live status + non-empty limits)
    that is younger than ``max_stale_seconds``, return a copy of the CACHED-good provider with its
    ``status`` forced to ``"ok"`` (so the card renders normally) plus ``stale: True`` /
    ``staleAsOf: <original good fetch time>``.

    The staleness clock is the CACHED snapshot's top-level ``timestamp`` — the moment that reading
    was fetched. Crucially, once carried forward the cached entry itself already carries
    ``staleAsOf`` (set on the PRIOR run), so we PRESERVE any pre-existing ``staleAsOf`` rather than
    resetting it to the current cache timestamp. That stops the window from ratcheting: repeated
    carry-forwards keep pointing at the ORIGINAL good fetch, so age keeps growing and the 15-min
    grace truly expires. Beyond the window (or with no healthy cache) we return ``provider``
    unchanged, so a real expired cookie surfaces its "sign in" error after the grace period.

    No-op for any non-transient status, when the provider already has limits, or when the cache
    lacks a healthy entry for this provider key. Never mutates ``cached``.
    """
    if not isinstance(provider, dict):
        return provider
    if provider.get("status") not in _TRANSIENT_PROVIDER_STATUSES:
        return provider
    if provider.get("limits"):
        return provider
    label = provider.get("label")
    if not isinstance(cached, dict):
        return provider
    providers = cached.get("providers")
    if not isinstance(providers, dict):
        return provider
    # Find the matching cached entry by label (the provider dicts don't carry a stable key
    # inside themselves; the caller passes the same label-bearing dict it stored).
    cached_entry = None
    for entry in providers.values():
        if isinstance(entry, dict) and entry.get("label") == label:
            cached_entry = entry
            break
    if not isinstance(cached_entry, dict):
        return provider
    if cached_entry.get("status") not in _LIVE_PROVIDER_STATUSES:
        return provider
    cached_limits = cached_entry.get("limits")
    if not (isinstance(cached_limits, list) and cached_limits):
        return provider

    # Age off the cached snapshot's own top-level fetch time.
    cached_ts = cached.get("timestamp")
    try:
        ts = dt.datetime.fromisoformat(str(cached_ts).replace("Z", "+00:00"))
    except Exception:
        return provider
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    age = (dt.datetime.now(dt.timezone.utc) - ts).total_seconds()

    # staleAsOf is the ORIGINAL good-fetch time: prefer a value already stamped on the cached
    # entry (a prior carry-forward), else the cache timestamp itself. This is what age must be
    # measured against so the window can't ratchet forward across repeated carry-forwards.
    prior_stale_as_of = cached_entry.get("staleAsOf")
    if isinstance(prior_stale_as_of, str) and prior_stale_as_of:
        try:
            origin = dt.datetime.fromisoformat(prior_stale_as_of.replace("Z", "+00:00"))
            if origin.tzinfo is None:
                origin = origin.replace(tzinfo=dt.timezone.utc)
            age = (dt.datetime.now(dt.timezone.utc) - origin).total_seconds()
            stale_as_of = prior_stale_as_of
        except Exception:
            stale_as_of = str(cached_ts)
    else:
        stale_as_of = str(cached_ts)

    if age < 0 or age > max_stale_seconds:
        return provider

    frozen = copy.deepcopy(cached_entry)
    # The cost summary is LOCAL data (parsed from ~/.claude, ~/.codex, … this run), not part
    # of the network reading being frozen — carrying the cached one would shadow this run's
    # fresh scan for the whole grace window. apply_cost_summaries attaches the fresh one.
    frozen.pop("costSummary", None)
    frozen["status"] = "ok"
    frozen["stale"] = True
    frozen["staleAsOf"] = stale_as_of
    return frozen


_COST_EXPORT_KEYS = (
    "costToday", "cost7d", "cost30d", "tokensToday", "tokens7d", "tokens30d",
    "burnRatePerDay", "projectedMonthlyCost", "modelBreakdown",
)


def cost_export(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Project a snapshot down to a clean per-provider cost/token summary for the
    `--cost` flag — a stable, script-friendly contract (ccusage-style) carrying only
    the raw numeric cost fields, not the formatted display strings or the full snapshot."""
    out: dict[str, Any] = {"generatedAt": snapshot.get("timestamp"), "providers": {}}
    total_cost30 = total_burn = 0.0
    for name, provider in (snapshot.get("providers") or {}).items():
        # The backend only ever attaches "costSummary" (apply_local_cost_summaries /
        # antigravity_ledger_cost_summary); there is no top-level "cost" provider key.
        cost = provider.get("costSummary")
        if not isinstance(cost, dict):
            continue
        entry = {k: cost.get(k) for k in _COST_EXPORT_KEYS if k in cost}
        if not entry:
            continue
        out["providers"][name] = entry
        total_cost30 += float(cost.get("cost30d") or 0.0)
        total_burn += float(cost.get("burnRatePerDay") or 0.0)
    out["totals"] = {
        "cost30d": round(total_cost30, 6),
        "burnRatePerDay": round(total_burn, 6),
        "projectedMonthlyCost": round(total_burn * 30.0, 6),
    }
    # Surface a degraded run: if the (slow) trajectory cost scan timed out, no provider
    # carries a costSummary, so {providers:{}, totals: all-zero} would be byte-identical to
    # a legitimate no-spend result. Flag it so a script reading --cost can tell a timed-out
    # scan from genuine $0 spend (the --cost branch also exits non-zero when this is set).
    diags = snapshot.get("diagnostics")
    if isinstance(diags, dict) and diags.get("cost_summary_timeout"):
        out["degraded"] = True
        out["degradedReason"] = "cost_summary_timeout"
    return out


def update_refresh_interval(minutes: float) -> dict[str, Any]:
    import fcntl
    value = float(minutes)
    allowed = (1.0, 2.0, 5.0, 15.0, 30.0)
    if value not in allowed:
        raise ValueError("refresh interval must be one of 1, 2, 5, 15, or 30 minutes")

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.parent.chmod(0o700)
    lock_path = CONFIG_PATH.parent / ".config.lock"
    # "a" not "w": opening for write truncates the lockfile to 0 bytes BEFORE the lock is held
    # (a TOCTOU). Acquire non-blocking with a bounded wait so a stuck holder can't hang this
    # one-shot config-write process forever.
    with _open_lock_0600(lock_path) as lock_fd:
        if not flock_with_timeout(lock_fd, 5.0):
            raise TimeoutError("could not acquire config lock (another write in progress)")
        try:
            config = load_config()
            config["refreshIntervalMinutes"] = int(value) if value.is_integer() else value
            return save_config(config)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)


_PANEL_DISPLAY_MODES = ("percent", "reset", "cost", "pace")


def update_config_values(updates: dict[str, Any]) -> dict[str, Any]:
    """Whitelisted, validated load-modify-save of the user-tunable config keys
    (notifications + panel display), under the same exclusive flock as
    update_refresh_interval so concurrent writes can't lost-update config.json.
    Unknown/invalid keys are ignored rather than raising."""
    import fcntl
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.parent.chmod(0o700)
    lock_path = CONFIG_PATH.parent / ".config.lock"
    with _open_lock_0600(lock_path) as lock_fd:  # 0600 + "a" (no truncate before locking)
        if not flock_with_timeout(lock_fd, 5.0):  # bounded, non-blocking
            raise TimeoutError("could not acquire config lock (another write in progress)")
        try:
            config = load_config()
            if "notificationsEnabled" in updates:
                config["notificationsEnabled"] = bool(updates["notificationsEnabled"])
            if "panelDisplayMode" in updates:
                mode = str(updates["panelDisplayMode"])
                if mode in _PANEL_DISPLAY_MODES:
                    config["panelDisplayMode"] = mode
            if "notificationThresholds" in updates:
                raw = updates["notificationThresholds"]
                if isinstance(raw, list):
                    vals = sorted({int(x) for x in raw if isinstance(x, (int, float)) and 0 < x <= 100})
                    if vals:
                        config["notificationThresholds"] = vals
            if "refreshIntervalMinutes" in updates:
                try:
                    mv = float(updates["refreshIntervalMinutes"])
                    if mv in (1.0, 2.0, 5.0, 15.0, 30.0):
                        config["refreshIntervalMinutes"] = int(mv) if mv.is_integer() else mv
                except (TypeError, ValueError):
                    pass
            if "providers" in updates:
                raw = updates["providers"]
                if isinstance(raw, list):
                    # Keep the canonical order, drop unknowns/dupes, require non-empty
                    # (an empty provider set would leave the widget with no tabs).
                    chosen = [p for p in PROVIDER_ORDER if p in raw]
                    if chosen:
                        config["providers"] = chosen
            if "mutedProviders" in updates:
                # Whitelist to known providers, keep canonical order, drop dupes.
                raw = updates["mutedProviders"]
                if isinstance(raw, list):
                    config["mutedProviders"] = [p for p in PROVIDER_ORDER if p in raw]
            if "monthlyBudget" in updates:
                # Optional monthly USD budget for the cross-provider spend alert. 0 (or
                # absent) disables it. Clamp to a sane range; ignore garbage rather than raise.
                try:
                    budget = float(updates["monthlyBudget"])
                    if 0.0 <= budget <= 1_000_000.0:
                        config["monthlyBudget"] = budget
                except (TypeError, ValueError):
                    pass
            return save_config(config)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Snapshot orchestrator
# ---------------------------------------------------------------------------

async def build_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config()
    providers = {
        "antigravity": {**default_provider("Antigravity", "local-antigravity"), "accentColor": "#30b795"},
        "gemini": {**default_provider("Gemini", "gemini-web"), "accentColor": "#2f74f5"},
        "codex": {**default_provider("Codex", "json-rpc"), "accentColor": "#218df4"},
        "claude": {**default_provider("Claude", "browser-api"), "accentColor": "#c07f63"},
        # Grok: local-only (context gauge from ~/.grok session signals + token/cost history
        # from ~/.grok logs). run_grok_local fills limits; apply_cost_summaries adds the card.
        "grok": {**default_provider("Grok", "local-grok-logs"), "accentColor": "#1d9bf0"},
    }
    diagnostics: dict[str, Any] = {}

    # Per-phase wall clock, surfaced as diagnostics.timings. The refresh is a one-shot
    # process on a 5-minute timer, so a phase that quietly grows costs the user battery and
    # disk on every tick with nothing in the snapshot to show it — exactly how the 2026-06-07
    # "Cost scan timed out" regression (110+ cascades re-queried per run) stayed invisible
    # until it started tripping the deadline. Monotonic, so a clock step can't produce a
    # negative. These do NOT sum to `total`: the cost scan deliberately overlaps the provider
    # fetches, and `cost_scan` measures launch -> clean await, not CPU time.
    phase_start = time.monotonic()
    snapshot_deadline = phase_start + refresh_worst_case_seconds(args.timeout)
    timings: dict[str, int] = {}

    def mark(name: str, since: float) -> None:
        timings[name] = int((time.monotonic() - since) * 1000)

    # Load the last-known-good snapshot ONCE up front and reuse it for: (1) the Claude
    # credit/overage throttle (prev creditBalance.fetchedAt gates the two slow GETs), (2)
    # the Antigravity partial-lane carry-forward, and (3) the transient-failure carry-forward
    # for claude/gemini/codex below. One disk read, not three.
    cached_snapshot = load_snapshot()
    prev_providers = cached_snapshot.get("providers") if isinstance(cached_snapshot, dict) else None
    prev_claude = prev_providers.get("claude") if isinstance(prev_providers, dict) else None
    prev_antigravity = prev_providers.get("antigravity") if isinstance(prev_providers, dict) else None
    # Google One AI credit-pool throttle: reuse the previous snapshot's balance when it was
    # fetched < GOOGLE_ONE_REFRESH_SECONDS (300s) ago, skipping the authenticated
    # one.google.com batchexecute RPC entirely. Mirrors the Claude CREDIT_REFRESH_SECONDS
    # carry-forward (providers/claude.py). The carried fetchedAt is deliberately NOT
    # re-stamped, so the carry is bounded at 300s of age (not indefinite).
    carried_g1 = google_one_credit_fresh(prev_antigravity)

    # Kick off the local cost scan NOW so its CPU/disk work (parsing ~/.claude, ~/.codex,
    # ~/.gemini logs + the Antigravity ledger) overlaps the network provider fetches below
    # instead of running serially after them. It is provider-result-INDEPENDENT — it never
    # touches `providers` and returns a separate {provider: summary} map — so it's safe to
    # start before the providers are known, and a torn-write on timeout is impossible (the
    # merge happens later, only after a clean await; no deepcopy needed). Double-bounded
    # exactly like the old serial version: the outer asyncio.wait_for(timeout) here, plus a
    # cooperative wall-clock `deadline` threaded into the Antigravity ledger's per-DB loop.
    cost_deadline = time.time() + args.timeout
    cost_scan_start = time.monotonic()
    cost_scan = asyncio.ensure_future(
        asyncio.wait_for(
            to_daemon_thread(compute_local_cost_summaries, cost_deadline),
            timeout=args.timeout,
        )
    )

    cookie_task = asyncio.create_task(collect_browser_sessions(args.timeout, args.background))

    cookie_start = time.monotonic()
    try:
        cookies, browser_stats = await asyncio.wait_for(cookie_task, timeout=args.timeout + 1.0)
        diagnostics["browser"] = browser_stats
    except asyncio.TimeoutError:
        cookies = []
        diagnostics["browser"] = {"status": "timeout", "message": "Browser cookie collection timed out"}
    except Exception as exc:
        cookies = []
        diagnostics["browser"] = {"status": "error", "message": scrub_credentials(str(exc))[:160]}
    mark("cookies", cookie_start)

    provider_start = time.monotonic()
    api_tasks = {}
    local_tasks = {}
    to_expired = False
    try:
        async with asyncio.timeout(args.timeout + 1.0) as to:
            async with asyncio.TaskGroup() as group:
                local_tasks["antigravity"] = group.create_task(
                    run_threaded_provider(
                        run_antigravity_local,
                        args.timeout,
                        timeout=args.timeout,
                        fallback=providers["antigravity"],
                    )
                )
                local_tasks["codex"] = group.create_task(
                    bounded_provider(run_codex_rpc(args.timeout), args.timeout, providers["codex"])
                )
                # Grok is local-only (reads ~/.grok session signals for the context gauge);
                # runs regardless of --no-network, like the other local_tasks.
                local_tasks["grok"] = group.create_task(
                    run_threaded_provider(
                        run_grok_local,
                        args.timeout,
                        timeout=args.timeout,
                        fallback=providers["grok"],
                    )
                )
                if not args.no_network:
                    api_tasks["antigravity_remote"] = group.create_task(
                        run_threaded_provider(
                            run_antigravity_remote,
                            args.timeout,
                            timeout=args.timeout,
                            fallback=providers["antigravity"],
                        )
                    )
                    api_tasks["gemini"] = group.create_task(
                        run_threaded_provider(
                            run_gemini_web,
                            cookies,
                            args.timeout,
                            timeout=args.timeout,
                            fallback=providers["gemini"],
                        )
                    )
                    api_tasks["claude"] = group.create_task(
                        bounded_provider(run_claude_api(cookies, args.timeout, prev_claude), args.timeout, providers["claude"])
                    )
                    # The OpenAI cookie-API GET is a FALLBACK for a failed codex CLI
                    # RPC — it is fired lazily (post-group, serialized) only when the
                    # local `codex` RPC result is not "ok". In steady state (healthy
                    # codex CLI) it is skipped entirely, dropping one authenticated
                    # OpenAI call (a second Cloudflare-flaggable surface) per refresh.
                    # Skip the one.google.com credit RPC entirely when the previous
                    # snapshot's balance is still fresh (< 300s); it's carried forward at
                    # the apply site below instead.
                    if carried_g1 is None:
                        api_tasks["google_one"] = group.create_task(
                            run_threaded_provider(
                                run_google_one_credits,
                                cookies,
                                args.timeout,
                                timeout=args.timeout,
                                fallback={"label": "Google One", "status": "error", "creditBalance": None},
                            )
                        )
                    # Expired or missing pricing cache: refresh concurrently in the TaskGroup
                    from pricing_data import PRICING_CACHE_PATH, PRICING_CACHE_TTL, refresh_pricing
                    cache_missing = not PRICING_CACHE_PATH.exists()
                    cache_expired = False
                    if not cache_missing:
                        try:
                            cache_expired = (time.time() - PRICING_CACHE_PATH.stat().st_mtime) > PRICING_CACHE_TTL
                        except Exception:
                            cache_expired = True
                    if cache_missing or cache_expired:
                        # Guard the pricing refresh: it is a background cache write
                        # whose result isn't read this run, so its failure must NEVER
                        # propagate out of the TaskGroup (which would cancel every
                        # sibling provider task and blank the whole snapshot).
                        async def _safe_refresh_pricing(t: float) -> None:
                            try:
                                await refresh_pricing(t)
                            except Exception as exc:  # noqa: BLE001 - swallow fetch errors so a pricing-refresh failure can't cancel sibling tasks; CancelledError/SystemExit are NOT Exception, so timeout/shutdown still propagate
                                diagnostics["pricing_refresh_error"] = scrub_credentials(f"{exc.__class__.__name__}: {str(exc)}")[:160]
                        group.create_task(_safe_refresh_pricing(args.timeout))
    except TimeoutError:
        to_expired = True
    except BaseExceptionGroup as eg:
        # asyncio.TaskGroup bundles child failures into a group. An ExceptionGroup (all-Exception
        # leaves) subclasses Exception, but a BaseExceptionGroup (any BaseException leaf — e.g.
        # CancelledError, KeyboardInterrupt) does NOT, so it would slip past a plain `except
        # Exception` and crash build_snapshot. Catch the group base, record the Exception
        # leaves as diagnostics, and re-raise any BaseException leaves so cancellation/shutdown
        # still propagates correctly instead of being silently dropped.
        if "to" in locals():
            to_expired = to.expired()
        exc_part, base_part = eg.split(Exception)
        if exc_part is not None:
            leaves: list[str] = []

            def _collect(group: BaseExceptionGroup) -> None:  # flatten nested groups
                for e in group.exceptions:
                    if isinstance(e, BaseExceptionGroup):
                        _collect(e)
                    else:
                        leaves.append(f"{e.__class__.__name__}: {e}")

            _collect(exc_part)
            diagnostics["orchestrator_error"] = scrub_credentials("; ".join(leaves))[:300]
        if base_part is not None:
            raise base_part
    except Exception as exc:
        diagnostics["orchestrator_error"] = scrub_credentials(f"{exc.__class__.__name__}: {str(exc)}")[:160]
    mark("providers", provider_start)

    def get_task_result(task: asyncio.Task | None, fallback: dict[str, Any], label: str) -> dict[str, Any]:
        if task is None:
            return fallback
        # Defensive backstop: by the time we get here the `async with TaskGroup`
        # block has exited, so __aexit__ has already awaited every child — each task
        # is guaranteed done() (result, exception, or cancelled). This branch is
        # therefore unreachable in practice; kept only to fail safe if that ever changes.
        if not task.done():
            status = "timeout" if to_expired else "api-error"
            msg = f"{label} telemetry timed out" if to_expired else f"{label} telemetry failed"
            return {**fallback, "status": status, "message": msg}
        if task.cancelled():
            status = "timeout" if to_expired else "api-error"
            msg = f"{label} telemetry timed out" if to_expired else f"{label} telemetry cancelled"
            return {**fallback, "status": status, "message": msg}
        exc = task.exception()
        if exc is not None:
            return {**fallback, "status": "api-error", "message": scrub_credentials(str(exc))[:160]}
        return task.result()

    providers["antigravity"] = get_task_result(
        local_tasks.get("antigravity"),
        providers["antigravity"],
        "Antigravity"
    )
    providers["codex"] = get_task_result(
        local_tasks.get("codex"),
        providers["codex"],
        "Codex"
    )
    providers["grok"] = get_task_result(
        local_tasks.get("grok"),
        providers["grok"],
        "Grok"
    )
        
    antigravity_remote = None
    if "antigravity_remote" in api_tasks:
        antigravity_remote = get_task_result(
            api_tasks["antigravity_remote"],
            {**providers["antigravity"], "source": "remote-oauth"},
            "Antigravity"
        )
        
    providers["antigravity"] = choose_antigravity_result(providers["antigravity"], antigravity_remote)
    providers["antigravity"] = carry_forward_partial_antigravity_lanes(
        providers["antigravity"], cached_snapshot)

    if not args.no_network:
        providers["gemini"] = get_task_result(
            api_tasks.get("gemini"),
            providers["gemini"],
            "Gemini"
        )
        providers["claude"] = get_task_result(
            api_tasks.get("claude"),
            providers["claude"],
            "Claude"
        )

        # Lazy OpenAI cookie fallback: only fire the authenticated chatgpt.com GET
        # when the local codex CLI RPC path did NOT succeed. bounded_provider applies
        # its own timeout, so this post-group await is bounded even outside the
        # TaskGroup's asyncio.timeout wrapper (intended — it's the rare fallback path).
        if providers["codex"].get("status") != "ok":
            # Capped to what's left of the refresh budget so refresh_worst_case_seconds is a
            # guarantee the widget's watchdog can rely on, not just the usual case.
            fallback_budget = min(args.timeout, max(0.1, snapshot_deadline - time.monotonic()))
            codex_cookie = await bounded_provider(
                run_openai_cookie_api(cookies, fallback_budget),
                fallback_budget,
                default_provider("Codex", "browser-api"),
            )
            if codex_cookie["status"] == "ok":
                providers["codex"] = codex_cookie

        # Read google_one only when the task finished cleanly — guard .exception()
        # too, else a done-with-exception task would re-raise here (outside any
        # try) and abort the whole snapshot instead of just dropping the credit bar.
        if carried_g1 is not None:
            # Throttled: the RPC was skipped above. Re-apply the carried balance so the
            # credit bar keeps rendering — without this the code path below would apply
            # None (no task) and drop the bar. fetchedAt is NOT re-stamped (bounded 300s).
            apply_google_one_credits(providers["antigravity"], {"status": "ok", "creditBalance": carried_g1})
        else:
            google_one_task = api_tasks.get("google_one")
            google_one_result = None
            if (google_one_task is not None and google_one_task.done()
                    and not google_one_task.cancelled() and google_one_task.exception() is None):
                google_one_result = google_one_task.result()
            apply_google_one_credits(providers["antigravity"], google_one_result)

    else:
        for provider, domains in {
            "gemini": GEMINI_DOMAINS,
            "claude": ("claude.ai",),
        }.items():
            count = len(select_session_cookies(cookies, domains))
            providers[provider].update(
                status="cookies-ready" if count else "missing-cookies",
                source="browser-cookies",
                message=f"{count} matching cookies decrypted" if count else "No matching browser cookies",
            )
        # Google One credits need the network; drop the misleading plan-status
        # credit figures so they aren't shown with stale/wrong numbers offline.
        apply_google_one_credits(providers["antigravity"], None)

    browser_status = (diagnostics.get("browser") or {}).get("kwallet", {}).get("status")
    # wallet-locked (isOpen said "closed") and wallet-state-unknown (isOpen couldn't be
    # checked during a background refresh — crypto.py) both mean the browser cookies were
    # unreadable this run, NOT that the user is signed out. Relabel the resulting
    # missing-cookies so the UI offers the KWallet remediation (unlock button / degraded
    # banner) instead of a misleading "sign in again". Keep the distinct status so the
    # message stays honest about which case it is.
    if browser_status in ("wallet-locked", "wallet-state-unknown"):
        relabel_message = (
            "KWallet is locked; background refresh skipped credential access"
            if browser_status == "wallet-locked"
            else "KWallet lock state could not be checked during background refresh"
        )
        for provider in ("gemini", "claude"):
            if providers[provider]["status"] == "missing-cookies":
                providers[provider].update(
                    status=browser_status,
                    message=relabel_message,
                )

    # Bounded last-known-good carry-forward for the network providers. A transient blip
    # (Cloudflare challenge / one-off timeout) on Claude/Gemini/Codex — but with the other
    # providers live — would otherwise overwrite the cache with an error card. Freeze the last
    # good reading for a short grace window off the SAME cached snapshot loaded up top; beyond
    # the window the real error resurfaces. No-op for non-transient statuses (e.g. the offline
    # branch's cookies-ready/missing-cookies) or when the provider already has limits.
    for _name in ("claude", "gemini", "codex"):
        providers[_name] = carry_forward_provider_last_good(providers[_name], cached_snapshot)

    # Claude Code statusLine fallback (integrations/claude_code/statusline_capture.py): when
    # the claude.ai cookie path produced no live limits — Cloudflare 403/429, signed out,
    # wallet locked, or --no-network — use the quota Claude Code itself handed the hook.
    # Runs AFTER the carry-forward so a carried last-good reading is replaced only by a
    # NEWER capture. A live cookie reading is never touched. Local file read, no network.
    providers["claude"] = apply_claude_statusline_fallback(providers["claude"], load_claude_statusline())

    # Merge the cost summaries computed CONCURRENTLY since the top of build_snapshot
    # (the scan overlapped the network fetches above). apply_cost_summaries propagates the
    # Antigravity tier to Gemini and attaches each summary — it runs ONLY after this clean
    # await, so a scan timeout (caught -> diagnostic) leaves the snapshot exactly as the
    # providers produced it, with no half-written cost summary. The scan never touched
    # `providers`, so unlike the old deepcopy/merge-back there is nothing to roll back.
    try:
        summaries = await cost_scan
        apply_cost_summaries(providers, summaries)
    except (TimeoutError, asyncio.TimeoutError):
        diagnostics["cost_summary_timeout"] = True
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
        raise  # cooperative cancellation / process-stop must propagate, never be masked here
    except BaseException as exc:  # noqa: BLE001 - enrichment must never break the snapshot. A
        # BaseException raised inside the cost-scan daemon thread is set on the future by
        # to_daemon_thread, so `await cost_scan` re-raises it — and a bare `except Exception`
        # would let it slip past and crash build_snapshot, printing NO JSON for the whole
        # refresh. Record it and ship the freshly-computed provider data unenriched.
        diagnostics["cost_summary_error"] = scrub_credentials(f"{exc.__class__.__name__}: {str(exc)}")[:160]
    mark("cost_scan", cost_scan_start)
    enrich_ui_formatting(providers)

    mark("total", phase_start)
    diagnostics["timings"] = timings

    return {
        "ok": True,
        "timestamp": now_iso(),
        "providers": providers,
        "diagnostics": diagnostics,
        "config": public_config(config),
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _screen_locked() -> bool:
    """True if the session screen is locked, via the freedesktop ScreenSaver D-Bus
    interface (owned by kscreenlocker on Plasma 6). FAIL-OPEN: any error — missing
    ``busctl``, no D-Bus, a non-KDE locker with different semantics, a timeout — returns
    False so a refresh is never wrongly suppressed. Used only to skip background refreshes
    while locked; a foreground/manual refresh always runs."""
    try:
        proc = subprocess.run(
            ["busctl", "--user", "--timeout=1", "call",
             "org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver",
             "org.freedesktop.ScreenSaver", "GetActive"],
            capture_output=True, timeout=2,
        )
    except Exception:
        return False
    if proc.returncode != 0:
        return False
    # GetActive returns a single boolean; busctl prints it as "b true" / "b false".
    return proc.stdout.decode("utf-8", "replace").strip().split() == ["b", "true"]


def main() -> int:
    parser = argparse.ArgumentParser(description="TallyBar Plasma telemetry backend")
    parser.add_argument("--once", action="store_true", help="print one JSON telemetry snapshot")
    parser.add_argument("--timeout", type=float, default=12.0, help="per-provider timeout in seconds")
    parser.add_argument("--no-network", action="store_true", help="skip remote dashboard API calls")
    parser.add_argument("--background", action="store_true", help="skip credential actions that can trigger GUI prompts")
    parser.add_argument("--pretty", action="store_true", help="pretty-print JSON")
    parser.add_argument("--set-refresh-interval", type=float, help="write the refresh interval to the TallyBar config")
    parser.add_argument("--last-snapshot", action="store_true", help="print the cached last snapshot (no network) and exit")
    parser.add_argument("--cost", action="store_true", help="print a clean per-provider cost/token summary (JSON) and exit")
    parser.add_argument("--set-config", type=str, help="merge a JSON object of whitelisted config keys (notificationsEnabled, panelDisplayMode, notificationThresholds, providers, mutedProviders, monthlyBudget, refreshIntervalMinutes) and exit")
    args = parser.parse_args()

    if args.set_config is not None:
        try:
            updates = json.loads(args.set_config)
            if not isinstance(updates, dict):
                raise ValueError("--set-config expects a JSON object")
            config = update_config_values(updates)
        except (ValueError, json.JSONDecodeError, TimeoutError) as exc:
            # TimeoutError is raised by update_config_values on config-lock contention
            # (another --set-config/--set-refresh-interval writer holding .config.lock) —
            # map it to the same clean non-zero exit instead of an uncaught traceback.
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps({"ok": True, "config": public_config(config)}, indent=2 if args.pretty else None))
        return 0

    if args.last_snapshot:
        cached = load_snapshot()
        print(json.dumps(cached, separators=(",", ":")))
        return 0

    if args.cost:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
        fatal = False
        try:
            snapshot = asyncio.run(build_snapshot(args))
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:  # noqa: BLE001 - mirror the --once tail guard below:
            # build_snapshot is contracted to always RETURN a snapshot, but a BaseException
            # escaping the orchestrator (e.g. the TaskGroup's re-raised base_part, or the cost
            # scan) would otherwise crash --cost with a bare traceback and empty stdout instead
            # of the documented graceful degrade. Fall back to the last cached snapshot (or an
            # empty one) so cost_export still has something to project.
            snapshot = load_snapshot() or {"ok": False, "timestamp": now_iso(), "providers": {}}
            snapshot.setdefault("diagnostics", {})["fatal"] = scrub_credentials(f"{exc.__class__.__name__}: {str(exc)}")[:160]
            fatal = True
        export = cost_export(snapshot)
        print(json.dumps(export, indent=2 if args.pretty else None, separators=None if args.pretty else (",", ":")))
        # Non-zero exit when the cost scan was degraded (timed out) OR the fatal fallback above
        # fired, so a shell consumer can detect either case even without parsing the "degraded"
        # flag out of the JSON.
        return 3 if (fatal or export.get("degraded")) else 0

    if args.set_refresh_interval is not None:
        try:
            config = update_refresh_interval(args.set_refresh_interval)
        except (ValueError, TimeoutError) as exc:
            # TimeoutError is raised by update_refresh_interval on config-lock contention —
            # map it to the same clean non-zero exit instead of an uncaught traceback.
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps({"ok": True, "config": public_config(config)}, indent=2 if args.pretty else None))
        return 0

    if not args.once:
        parser.error("only --once is supported by the Plasma executable datasource")

    # Skip background (widget-timer) refreshes while the screen is locked — no
    # network, disk, or ledger work happens behind a locked screen. Gated to
    # ``--background`` only so manual/foreground refreshes (including the KWallet
    # unlock path, which omits --background) always run. Re-paint the cached snapshot so
    # the widget keeps showing last-known values (the staleness line explains the age);
    # do NOT save_snapshot (mustn't clobber last-known-good) and emit no notifications.
    if args.background and _screen_locked():
        cached = load_snapshot() or {"ok": False, "timestamp": now_iso(), "providers": {}}
        cached["notifications"] = []
        cached.setdefault("diagnostics", {})["refresh_skipped"] = "screen-locked"
        print(json.dumps(cached, indent=2 if args.pretty else None, separators=None if args.pretty else (",", ":")))
        return 0

    # Best-effort sweep of orphaned atomic-write temps (crashed mkstemp / .tmp leftovers) so
    # ~/.tallybar can't slowly fill with them (observed: stale July tmp* files + a 101 MB
    # claude_logs.json.<rand>.tmp). Never fatal — wrapped so a sweep failure can't touch stdout.
    try:
        from io_helpers import sweep_stale_temp_files
        _tallybar = Path.home() / ".tallybar"
        # ~/.tallybar (and its subdirs) are exclusively ours — sweep generically.
        sweep_stale_temp_files((
            _tallybar,
            _tallybar / "cache",
            _tallybar / "antigravity",
        ))
        # Third-party dirs the widget only writes ONE named file into: narrow the sweep to
        # that file's atomic-write temps (prefix + .tmp) so we never touch another tool's
        # tmpXXXXXXXX / *.tmp leftovers there.
        _gemini = Path.home() / ".gemini"
        sweep_stale_temp_files(
            (_gemini / "antigravity-cli",), only_prefix="antigravity-oauth-token.")
        # save_antigravity_credentials realpath-resolves onto ~/.gemini/oauth_creds.json, so a
        # crashed write can strand oauth_creds.json.<rand>.tmp directly in ~/.gemini.
        sweep_stale_temp_files((_gemini,), only_prefix="oauth_creds.json.")
    except Exception:
        pass

    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    try:
        snapshot = asyncio.run(build_snapshot(args))
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001 - build_snapshot is contracted to always RETURN
        # a snapshot (every provider has a fallback dict), but a BaseException escaping the
        # orchestrator (e.g. the TaskGroup's re-raised base_part, or the cost scan) would crash
        # to stderr with EMPTY stdout — and the widget JSON.parses stdout, so an empty refresh
        # surfaces as a parse error instead of a graceful degrade. Emit the last cached snapshot
        # (or a minimal degraded one) so the widget always gets parseable JSON. Don't touch the
        # cold-start cache here — a fatal must never clobber last-known-good.
        fallback = load_snapshot() or {"ok": False, "timestamp": now_iso(), "providers": {}}
        fallback["notifications"] = []
        fallback.setdefault("diagnostics", {})["fatal"] = scrub_credentials(f"{exc.__class__.__name__}: {str(exc)}")[:160]
        print(json.dumps(fallback, indent=2 if args.pretty else None, separators=None if args.pretty else (",", ":")))
        return 0
    # Always print the fresh snapshot (the widget shows the live state, including any
    # 'timed out' bars). But only overwrite the cold-start cache when this snapshot
    # has live data, OR when the existing cache is itself degraded/absent — so a
    # transient full outage can't clobber the last-known-good values cold start paints.
    # The cache write and notification pass are enrichment on top of an already-built
    # snapshot: a failure in either (e.g. a corrupted config value) must degrade to
    # printing the snapshot without them — never to empty stdout.
    try:
        if _snapshot_has_live_data(snapshot) or not _snapshot_has_live_data(load_snapshot()):
            save_snapshot(snapshot)
        # Threshold notifications are attached AFTER the cache write so the cached snapshot
        # never carries them — otherwise the cold-start cacheLoader would re-fire stale
        # alerts. Only this live --once path emits them (not --cost); the QML fires each via
        # KNotification. State de-dup lives in compute_notifications.
        diags_raw = snapshot.get("diagnostics")
        diags = diags_raw if isinstance(diags_raw, dict) else {}
        cost_ok = not (diags.get("cost_summary_timeout") or diags.get("cost_summary_error"))
        snapshot["notifications"] = compute_notifications(snapshot.get("providers") or {}, snapshot.get("config") or {},
                                                          cost_available=cost_ok)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001 - same rationale as the build_snapshot guard
        snapshot["notifications"] = []
        snapshot.setdefault("diagnostics", {})["post_snapshot_error"] = scrub_credentials(f"{exc.__class__.__name__}: {str(exc)}")[:160]
    try:
        out = json.dumps(snapshot, indent=2 if args.pretty else None, separators=None if args.pretty else (",", ":"))
    except (TypeError, ValueError) as exc:
        # A non-JSON-serializable value that slipped into the snapshot must not leave the
        # widget with empty stdout: fall back to the disk cache, which is JSON by construction.
        fallback = load_snapshot() or {"ok": False, "timestamp": now_iso(), "providers": {}}
        fallback["notifications"] = []
        fallback.setdefault("diagnostics", {})["fatal"] = scrub_credentials(f"snapshot-serialize: {str(exc)}")[:160]
        out = json.dumps(fallback, indent=2 if args.pretty else None, separators=None if args.pretty else (",", ":"))
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
