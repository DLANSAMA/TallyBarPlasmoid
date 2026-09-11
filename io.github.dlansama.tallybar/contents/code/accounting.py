"""Token accounting, model pricing, cost calculation, and usage-limit helpers.

No network I/O. Functions here transform raw usage dicts into the normalised
limit/cost shapes consumed by the QML UI. The local-log summarizers read the
provider jsonl logs from disk; to avoid re-parsing hundreds of MB of unchanged
logs on every 5-minute refresh they keep a per-file extract cache under
``~/.tallybar/cache/`` (keyed on path+mtime+size — only changed files reparse).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Any

from io_helpers import atomic_write_text
from parsers import as_dict, relative_reset
from pricing_data import get_pricing as _get_pricing

# Sentinel distinguishing "argument omitted" from an explicit None (lets a test
# pass cache_dir=None to force the no-cache path while production omits it).
_UNSET = object()


# ---------------------------------------------------------------------------
# Timestamp & formatting helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_timestamp(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone()


def compact_token_count(value: int | float) -> str:
    # Mirror the QML compactCount() exactly — carry at the 1000-of-a-unit boundary
    # (999.5K -> 1M) and trim trailing zeros (13.0M -> 13M, 1.20M -> 1.2M) — so a token
    # count renders identically in the Python cost lines and the QML bars/tooltips/graphs.
    # JS Math.round is half-UP, so use int(x + 0.5) rather than Python's half-even round().
    amount = max(0, int(float(value) + 0.5))
    largest_div = 1_000_000_000  # "B" — nothing bigger to carry a rounded-up value into.
    for div, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if amount >= div:
            # At the largest unit there is no bigger tier to escalate into: recursing
            # here (as the smaller tiers do below) lands on amount == round(n)*div, a
            # numeric fixed point once n itself is an integer >= 1000 — which recurses
            # forever (RecursionError) instead of ever escaping. So a value at/above its
            # own carry boundary is formatted directly against this (largest) unit.
            can_carry = div != largest_div
            n = amount / div
            if suffix == "K":
                rounded = int(n + 0.5)
                if can_carry and rounded >= 1000:
                    return compact_token_count(int(amount / 1000 + 0.5) * 1000)
                return f"{rounded}K"
            if n >= 100:
                rounded = int(n + 0.5)
                if can_carry and rounded >= 1000:
                    return compact_token_count(int(amount / div + 0.5) * div)
                return f"{rounded}{suffix}"
            if n >= 10:
                formatted = f"{n:.1f}".rstrip("0").rstrip(".")
                if can_carry and float(formatted) >= 100:
                    return compact_token_count(int(amount / div + 0.5) * div)
                return f"{formatted}{suffix}"
            formatted = f"{n:.2f}".rstrip("0").rstrip(".")
            if can_carry and float(formatted) >= 10:
                return compact_token_count(int(amount / div + 0.5) * div)
            return f"{formatted}{suffix}"
    return str(amount)


def exact_usd(amount: float) -> str:
    """Exact dollars to the cent, e.g. $151.44 / $1,234.56 — never the
    compact whole-dollar form (compact_usd) and never the monthly-sub price."""
    if amount < 0:
        amount = 0.0
    return f"${amount:,.2f}"


def compact_usd(amount: float) -> str:
    if amount < 0:
        amount = 0.0
    if amount >= 1000:
        return f"${amount:,.0f}"
    if amount >= 100:
        return f"${amount:.0f}"
    if amount >= 0.01:
        return f"${amount:.2f}"
    if amount > 0:
        return f"${amount:.4f}"
    return "$0.00"


# ---------------------------------------------------------------------------
# Model pricing — fetched from LiteLLM, cached to disk, embedded fallback
# ---------------------------------------------------------------------------

# Per-provider default pricing when the log doesn't record the exact model
# (Codex sessions, Gemini chats). Picks the modal/default CLI model.
DEFAULT_MODEL_FOR_PROVIDER = {
    "claude": "claude-sonnet-4",
    "codex":  "gpt-5",
    "gemini": "gemini-2.5-pro",
    "grok":   "grok-build",
}


def model_pricing(model: str | None) -> dict[str, float]:
    """Look up per-MTok pricing for a model (case-insensitive substring match).

    Pricing data is fetched from the LiteLLM community-maintained catalog,
    cached locally for 24h, and falls back to embedded defaults when offline.
    See pricing_data.py for the fetch/cache/fallback chain.
    """
    return _get_pricing(model)


def _provider_family_for_model(model: str | None) -> str | None:
    """Infer the provider family (claude/codex/gemini/grok) from a model-name string,
    so an unrecognised-but-non-empty model can fall back to its family's default
    pricing instead of billing $0."""
    m = (model or "").lower()
    if not m:
        return None
    if any(t in m for t in ("claude", "opus", "sonnet", "haiku", "anthropic")):
        return "claude"
    if "gemini" in m:
        return "gemini"
    if any(t in m for t in ("gpt", "codex", "openai", "davinci")) or \
            (len(m) >= 2 and m[0] == "o" and m[1].isdigit()):  # o3 / o4-mini / o1
        return "codex"
    if any(t in m for t in ("grok", "xai")):
        return "grok"
    return None


def _family_default_pricing(model: str | None) -> dict[str, float]:
    """Pricing for the family-default model of `model`'s inferred provider, or {}."""
    family = _provider_family_for_model(model)
    if family is None:
        return {}
    return model_pricing(DEFAULT_MODEL_FOR_PROVIDER[family])


def usage_cost_usd(usage: Any, model: str | None) -> float:
    """Compute USD cost for one assistant message's usage dict.

    Recognises Anthropic-style (input_tokens/cache_creation/cache_read/output_tokens)
    and OpenAI/Gemini-style (input_tokens/cached_input_tokens/output_tokens/reasoning_output_tokens)
    breakdowns. Reasoning tokens are billed as output. Cache-read tokens are billed at the
    discounted cache-read rate; cache-creation/write at the higher write rate (Anthropic only).
    """
    if not isinstance(usage, dict):
        return 0.0
    prices = model_pricing(model)
    if not prices and model:
        # A non-empty model the catalog doesn't know (a brand-new model that shipped
        # before LiteLLM catalogued it) would otherwise bill $0 while tokens still
        # count. Fall back to its provider family's default pricing instead. (The ledger
        # path _prices_for also avoids $0 on an unknown model, though it falls back to a
        # fixed Gemini default rather than a family-aware one; the local-log path billed $0.)
        prices = _family_default_pricing(model)
    if not prices:
        return 0.0

    def get(*keys: str) -> int:
        for k in keys:
            v = usage.get(k)
            if isinstance(v, (int, float)) and v > 0:
                return int(v)
        return 0

    input_tokens   = get("input_tokens", "inputTokens", "promptTokenCount", "input")
    output_tokens  = get("output_tokens", "outputTokens", "candidatesTokenCount", "output")
    tool           = get("tool", "tools", "toolTokens", "toolUsePromptTokenCount", "tool_use_prompt_token_count")
    cache_create   = get("cache_creation_input_tokens", "cacheCreationInputTokens")
    # Cache fields relate to input_tokens differently per provider:
    #  - Anthropic reports cache_creation_input_tokens AND cache_read_input_tokens
    #    SEPARATELY from (i.e. additive to) input_tokens — input_tokens already
    #    excludes them. They must NOT be subtracted off input_tokens.
    #  - OpenAI/Gemini report the cached portion (cached_input_tokens /
    #    cachedContentTokenCount / "cached") AS PART OF input_tokens, so it must be
    #    peeled out to avoid billing it at the full (uncached) input rate.
    cache_read_separate = get("cache_read_input_tokens", "cacheReadInputTokens")
    cached_within_input = get("cached_input_tokens", "cachedInputTokens",
                              "cachedContentTokenCount", "cached")
    # Thinking/reasoning tokens also differ by provider:
    #  - OpenAI's reasoning_output_tokens is a SUBSET of output_tokens (so it is
    #    already counted there — billing output_tokens covers it).
    #  - Gemini reports thoughts / thoughtsTokenCount SEPARATELY from
    #    candidatesTokenCount, so they are added on top at the output rate.
    # (Confirmed against real on-disk usage: Gemini sessions have
    #  total = input + output + thoughts + tool, so thoughts/tool are additive.)
    thoughts_separate = get("thoughtsTokenCount", "thoughts")

    uncached_input  = max(0, input_tokens - cached_within_input)
    billable_output = output_tokens + thoughts_separate
    cache_read_rate = prices.get("cache_read", prices.get("input", 0.0))
    cache_write_rate = prices.get("cache_write", prices.get("input", 0.0))
    # Anthropic bills 1-HOUR cache writes higher than 5-minute writes (2x vs 1.25x
    # base input). The flat cache_creation_input_tokens count is broken down under
    # usage["cache_creation"] into ephemeral_1h/5m; bill the 1h subset at the 1h
    # rate and the remainder at the default (5m) write rate. The two portions always
    # sum to cache_create, so a missing/partial/over-large breakdown can never over-
    # or under-count volume — it just falls back toward the 5m rate (matching other
    # providers and older logs that carry no breakdown, and the existing flat tests).
    cache_write_1h_rate = prices.get("cache_write_1h", cache_write_rate)
    cache_create_1h = 0
    cc_detail = usage.get("cache_creation")
    if isinstance(cc_detail, dict):
        eph_1h = cc_detail.get("ephemeral_1h_input_tokens")
        eph_1h = int(eph_1h) if isinstance(eph_1h, (int, float)) and eph_1h > 0 else 0
        # Defensive: if a record ever carries ONLY the nested breakdown and no flat
        # cache_creation_input_tokens, cache_create would be 0 and ALL cache-write tokens
        # would bill at $0. Derive the volume from the breakdown so they're not dropped.
        # On real data the flat field is always present (and equals the 1h+5m sum), so this
        # branch is a no-op and existing flat/breakdown billing is unchanged.
        if cache_create == 0:
            eph_5m = cc_detail.get("ephemeral_5m_input_tokens")
            eph_5m = int(eph_5m) if isinstance(eph_5m, (int, float)) and eph_5m > 0 else 0
            cache_create = eph_1h + eph_5m
        cache_create_1h = min(eph_1h, cache_create)

    cost = 0.0
    cost += uncached_input            * prices.get("input", 0.0)
    cost += billable_output           * prices.get("output", 0.0)
    cost += tool                      * prices.get("input", 0.0)
    cost += cache_create_1h           * cache_write_1h_rate
    cost += (cache_create - cache_create_1h) * cache_write_rate
    cost += cache_read_separate       * cache_read_rate
    cost += cached_within_input       * cache_read_rate
    return cost / 1_000_000.0


def _explicit_total(usage: Any) -> int | None:
    """The record's own total-token field, if it carries a positive one.

    OpenAI/codex records ship an explicit ``total_tokens`` (etc.) that need NOT
    equal input+output+cached — cached is a SUBSET of prompt there, so the
    breakdown sum would double-count. Returns None when no explicit field is
    present (Anthropic records, and Gemini records that only carry a breakdown),
    signalling the caller to fall back to the breakdown sum.
    """
    if not isinstance(usage, dict):
        return None
    for key in ("total_tokens", "totalTokens", "totalTokenCount", "total"):
        value = usage.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    return None


def usage_token_total(usage: Any) -> int:
    total = _explicit_total(usage)
    if total is not None:
        return total
    # fallback: reconstruct total using breakdown elements
    in_val, out_val, cached_val = usage_token_breakdown(usage)
    return in_val + out_val + cached_val


def usage_token_total_and_breakdown(usage: Any) -> tuple[int, tuple[int, int, int]]:
    """``(total, (uncached_input, output, cached))`` computing the breakdown ONCE.

    Behaviour-identical to pairing ``usage_token_total(usage)`` with
    ``usage_token_breakdown(usage)`` but without recomputing the breakdown: the
    per-record local-log summarizers need BOTH figures, and every Anthropic
    record (plus Gemini records carrying a breakdown but no total) has no
    explicit total, so the old pairing computed the same breakdown twice per
    record (~150k breakdown calls vs ~75k total calls per refresh). The total
    still prefers an explicit total field (which need not equal the breakdown
    sum — see ``_explicit_total``); only when absent is it the breakdown sum.
    """
    breakdown = usage_token_breakdown(usage)
    total = _explicit_total(usage)
    if total is None:
        total = breakdown[0] + breakdown[1] + breakdown[2]
    return total, breakdown


def usage_token_breakdown(usage: Any) -> tuple[int, int, int]:
    """(billable input, output incl. reasoning, cached) token counts for one
    message — mirrors how usage_cost_usd categorises tokens so the breakdown
    lanes add up to the cost."""
    if not isinstance(usage, dict):
        return (0, 0, 0)

    def get(*keys: str) -> int:
        for k in keys:
            v = usage.get(k)
            if isinstance(v, (int, float)) and v > 0:
                return int(v)
        return 0

    input_tokens = get("input_tokens", "inputTokens", "promptTokenCount", "input")
    output_tokens = get("output_tokens", "outputTokens", "candidatesTokenCount", "output")
    # Gemini reports thoughts separately from candidatesTokenCount; count them as
    # output (OpenAI's reasoning_output_tokens is already inside output_tokens).
    thoughts_separate = get("thoughtsTokenCount", "thoughts")
    tool = get("tool", "tools", "toolTokens", "toolUsePromptTokenCount", "tool_use_prompt_token_count")
    cache_create = get("cache_creation_input_tokens", "cacheCreationInputTokens")
    cache_read_separate = get("cache_read_input_tokens", "cacheReadInputTokens")
    cached_within_input = get("cached_input_tokens", "cachedInputTokens",
                              "cachedContentTokenCount", "cached")
    # input_tokens already EXCLUDES Anthropic cache_creation (it is additive, not a
    # subset), so only the OpenAI/Gemini cached-subset is peeled out — matching usage_cost_usd.
    uncached_input = max(0, input_tokens - cached_within_input) + tool
    cached_total = cache_create + cache_read_separate + cached_within_input
    return (uncached_input, output_tokens + thoughts_separate, cached_total)


# NOTE: get_tier_monthly_cost was removed — it was dead code (never called)
# that contained stale hardcoded subscription prices. Tier information is
# fetched from live APIs; subscription costs are not displayed by TallyBar.

def cost_breakdown_line(month_in: int, month_out: int, month_cached: int) -> str:
    parts = []
    if month_in > 0:
        parts.append(f"Input {compact_token_count(month_in)}")
    if month_out > 0:
        parts.append(f"Output {compact_token_count(month_out)}")
    if month_cached > 0:
        parts.append(f"Cached {compact_token_count(month_cached)}")
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Token bucket helpers (weekly / monthly)
# ---------------------------------------------------------------------------

def empty_weekly_token_buckets(current: dt.datetime) -> dict[str, dict[str, Any]]:
    today = current.date()
    buckets: dict[str, dict[str, Any]] = {}
    for offset in range(6, -1, -1):
        day = today - dt.timedelta(days=offset)
        buckets[day.isoformat()] = {
            "date": day.isoformat(),
            "day": day.strftime("%a"),
            "tokens": 0,
            "cost": 0.0,
            "models": {},
        }
    return buckets


def empty_hourly_token_buckets(current: dt.datetime) -> dict[int, dict[str, Any]]:
    # 24 buckets for *today*, one per local hour (0..23). The Day view in the cost
    # popout buckets today's usage by the hour it occurred. ``label`` is a short
    # axis tag ("12a", "1a", … "11p"); the frontend renders a longer form in tooltips.
    buckets: dict[int, dict[str, Any]] = {}
    for hour in range(24):
        h12 = hour % 12 or 12
        buckets[hour] = {
            "hour": hour,
            "label": f"{h12}{'a' if hour < 12 else 'p'}",
            "tokens": 0,
            "cost": 0.0,
            "models": {},
        }
    return buckets


def empty_monthly_token_buckets(current: dt.datetime) -> dict[str, dict[str, Any]]:
    month_start = current.date().replace(day=1)
    next_month = (month_start.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    days_in_month = (next_month - month_start).days
    leading_blanks = (month_start.weekday() + 1) % 7
    buckets: dict[str, dict[str, Any]] = {}

    for cell in range(42):
        month_day_index = cell - leading_blanks
        day = month_start + dt.timedelta(days=month_day_index)
        in_month = 0 <= month_day_index < days_in_month
        key = day.isoformat()
        buckets[key] = {
            "date": key,
            "inMonth": in_month,
            "tokens": 0,
            "cost": 0.0,
            "models": {},
        }
    return buckets


def bucket_add(bucket: dict[str, Any], tokens: int, cost: float, model: Any) -> None:
    """Accumulate one usage record into a day/hour/month bucket, including its
    per-model attribution (rendered by the cost popout's pinned click-tooltip)."""
    bucket["tokens"] += tokens
    bucket["cost"] += cost
    slot = bucket.setdefault("models", {}).setdefault(str(model), {"cost": 0.0, "tokens": 0})
    slot["cost"] += cost
    slot["tokens"] += tokens


def weekly_token_usage(buckets: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "date": item["date"],
            "day": item["day"],
            "tokens": int(item.get("tokens", 0)),
            # Bucket cost and per-model rows must round at the SAME precision so parts
            # reconcile with the whole (pinned by test_hourly_residual_fold).
            "cost": round(float(item.get("cost", 0.0)), 6),
            "label": compact_token_count(item.get("tokens", 0)),
            "models": model_breakdown_rows(item.get("models"), limit=4),
        }
        for item in buckets.values()
    ]


def monthly_token_usage(buckets: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "date": item["date"],
            "inMonth": bool(item.get("inMonth", False)),
            "tokens": int(item.get("tokens", 0)),
            "cost": round(float(item.get("cost", 0.0)), 6),
            "label": compact_token_count(item.get("tokens", 0)),
            "models": model_breakdown_rows(item.get("models"), limit=4),
        }
        for item in buckets.values()
    ]


def hourly_token_usage(buckets: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "hour": int(item["hour"]),
            "label": item["label"],
            "tokens": int(item.get("tokens", 0)),
            "cost": round(float(item.get("cost", 0.0)), 6),
            "models": model_breakdown_rows(item.get("models"), limit=4),
        }
        for item in buckets.values()
    ]


def trailing_week_days() -> float:
    """Effective number of days in the trailing 7-day burn window.

    The window covers 6 complete days plus today's elapsed fraction, so the
    per-day rate is unbiased for steady spend (at midnight today's bucket is
    empty → 6.0; at end of day → 7.0, the old constant).  Both
    accounting.token_summary and providers.cost.antigravity_ledger_cost_summary
    use this helper so they can't drift apart.
    """
    _now = dt.datetime.now()
    elapsed = (_now.hour * 3600 + _now.minute * 60 + _now.second) / 86400.0
    return 6.0 + elapsed


def model_breakdown_rows(model_costs: dict[str, dict[str, Any]] | None, limit: int = 6) -> list[dict[str, Any]]:
    """Turn a {model: {cost, tokens}} accumulator into a top-N list sorted by cost
    descending — the per-model attribution shown in the cost section / export."""
    if not model_costs:
        return []
    rows = [
        {"model": str(name), "cost": round(float(v.get("cost", 0.0)), 6), "tokens": int(v.get("tokens", 0))}
        for name, v in model_costs.items()
        if (float(v.get("cost", 0.0)) > 0.0 or int(v.get("tokens", 0)) > 0)
    ]
    rows.sort(key=lambda r: (r["cost"], r["tokens"]), reverse=True)
    return rows[:limit]


def token_summary(
    today_tokens: int,
    month_tokens: int,
    source: str,
    today_cost: float = 0.0,
    month_cost: float = 0.0,
    tier: str | None = None,
    breakdown: tuple[int, int, int] | None = None,
    daily: list[dict[str, Any]] | None = None,
    monthly: list[dict[str, Any]] | None = None,
    hourly: list[dict[str, Any]] | None = None,
    model_costs: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    if month_tokens <= 0 and today_tokens <= 0:
        return None

    history = daily or []
    month_history = monthly or []
    hour_history = hourly or []
    # "Last 7 days" is exactly the daily (weekly) buckets summed — so the figure always
    # equals the Week graph's bars. Derived here so callers don't thread extra params.
    week_tokens = sum(int(d.get("tokens", 0)) for d in history)
    week_cost = sum(float(d.get("cost", 0.0)) for d in history)
    # Burn rate = trailing-7-day average $/day; projected month = that rate × 30.
    burn_rate = week_cost / trailing_week_days()
    model_rows = model_breakdown_rows(model_costs)

    # Raw numeric fields (alongside the pre-formatted display strings) so the UI's
    # panel-display modes, the burn-rate line, and the `--cost` export can read numbers
    # without re-parsing "$1,234.56" strings.
    raw: dict[str, Any] = {
        "costToday": round(float(today_cost), 6),
        "cost7d": round(float(week_cost), 6),
        "cost30d": round(float(month_cost), 6),
        "tokensToday": int(today_tokens),
        "tokens7d": int(week_tokens),
        "tokens30d": int(month_tokens),
        "burnRatePerDay": round(burn_rate, 6),
        "projectedMonthlyCost": round(burn_rate * 30.0, 6),
        "modelBreakdown": model_rows,
    }

    # Pay-per-use cost: what the tracked tokens WOULD cost at API pricing.
    # We deliberately do NOT show the monthly-subscription price here.
    if today_cost > 0.0 or month_cost > 0.0:
        result: dict[str, Any] = {
            "title": "Cost (if pay-per-use)",
            "today": f"Today: {exact_usd(today_cost)} · {compact_token_count(today_tokens)} tok",
            "last7Days": f"Last 7 days: {exact_usd(week_cost)} · {compact_token_count(week_tokens)} tok",
            "last30Days": f"Last 30 days: {exact_usd(month_cost)} · {compact_token_count(month_tokens)} tok",
            "source": source,
            "weeklyTokenUsage": history,
            "monthlyTokenUsage": month_history,
            "hourlyTokenUsage": hour_history,
            **raw,
        }
        if breakdown:
            line = cost_breakdown_line(*breakdown)
            if line:
                result["breakdown"] = line
        return result

    return {
        "title": "Tokens",
        "today": f"Today: {compact_token_count(today_tokens)} tokens",
        "last7Days": f"Last 7 days: {compact_token_count(week_tokens)} tokens",
        "last30Days": f"Last 30 days: {compact_token_count(month_tokens)} tokens",
        "source": source,
        "weeklyTokenUsage": history,
        "monthlyTokenUsage": month_history,
        "hourlyTokenUsage": hour_history,
        **raw,
    }


# ---------------------------------------------------------------------------
# Per-file log-parse cache
#
# Claude/Codex write append-only jsonl session logs that can total ~1 GB. The
# old summarizers re-opened and json.loads-ed EVERY line of EVERY file on each
# refresh (~2 s for Claude alone). Instead we extract each file's usage records
# ONCE, keyed by (path, mtime_ns, size), and reuse them while the file is
# unchanged — so a refresh only parses files that actually grew since last time.
#
# The cache is a pure function of the files' bytes (no clock/window baked in):
# every record the file ever had is stored, and the time-window/dedup gating is
# re-applied by the summarizer each call. So an unchanged file's cached records
# stay correct forever, and a stale/corrupt/format-bumped cache simply triggers
# a full reparse (it's regenerable — never authoritative).
# ---------------------------------------------------------------------------

_PARSE_CACHE_DIR = Path.home() / ".tallybar" / "cache"
_CACHE_SCHEMA_VERSION = 1          # bump to invalidate ALL parse caches at once
_CLAUDE_PARSE_VERSION = 1          # bump when _parse_claude_file's output shape changes
_CODEX_PARSE_VERSION = 1           # bump when _parse_codex_file's output shape changes
_GROK_PARSE_VERSION = 2            # bump when _parse_grok_file's output shape changes
_GEMINI_PARSE_VERSION = 1          # bump when _parse_gemini_file's output shape changes


def _load_parse_cache(cache_path: Path, parse_version: int) -> dict[str, dict[str, Any]]:
    """Return ``{path: {mtime, size, records}}`` from a prior run, or ``{}`` if the
    cache is missing, unreadable, malformed, or written by an older parser/schema
    (in which case every file is reparsed and the cache rebuilt)."""
    try:
        with cache_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if (not isinstance(data, dict)
            or data.get("schema") != _CACHE_SCHEMA_VERSION
            or data.get("version") != parse_version
            or not isinstance(data.get("files"), dict)):
        return {}
    return data["files"]


def _save_parse_cache(cache_path: Path, parse_version: int,
                      files: dict[str, dict[str, Any]]) -> None:
    """Atomically persist the per-file record cache via the shared io_helpers 0600
    recipe. Best-effort: any failure is swallowed — the cache is regenerable, so a
    failed write just means a reparse next run."""
    try:
        body = json.dumps({"schema": _CACHE_SCHEMA_VERSION, "version": parse_version,
                            "files": files}, separators=(",", ":"))
    except (TypeError, ValueError):
        return
    try:
        atomic_write_text(cache_path, body)
    except OSError:
        pass


def _cached_log_records(root: Path, pattern: str, cache_path: Path | None,
                        parser, parse_version: int, walker=None,
                        deadline: float | None = None) -> list[dict[str, Any]]:
    """Walk ``root.rglob(pattern)`` and return every file's extracted records, served
    from the per-file cache when (mtime_ns, size) is unchanged and freshly parsed via
    ``parser(path)`` otherwise. ``cache_path=None`` disables caching entirely (every
    file parsed fresh — the path tests use). rglob order is preserved so the caller's
    dedup/tie-breaking is identical to the old line-by-line parse. ``walker(root)``,
    when given, replaces the rglob iterator — Gemini's walker prunes the multi-GB
    antigravity* subtrees, a prune that must not regress to rglob.

    ``deadline`` is an optional cooperative wall-clock ``time.time()`` cutoff (mirrors
    the Antigravity RPC harvest's deadline checks): checked before each file, and on
    expiry the walk stops early and returns whatever records were gathered so far — a
    slow cold parse can't burn the whole cost-scan budget with no checkpoint. To keep
    the on-disk cache a pure function of file bytes (never a partial/inconsistent
    snapshot), the cache save is skipped entirely when the walk was cut short — the
    next run simply resumes from the last COMPLETE save rather than persisting a
    partial file map that would look like an authoritative full scan."""
    old_files = _load_parse_cache(cache_path, parse_version) if cache_path is not None else {}
    new_files: dict[str, dict[str, Any]] = {}
    records: list[dict[str, Any]] = []
    changed = False
    complete = True
    for path in (walker(root) if walker is not None else root.rglob(pattern)):
        if deadline is not None and time.time() >= deadline:
            complete = False
            break
        try:
            st = path.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):   # reject symlinks/dirs (lstat doesn't follow)
            continue
        key = str(path)
        prev = old_files.get(key)
        if (prev is not None and prev.get("mtime") == st.st_mtime_ns
                and prev.get("size") == st.st_size):
            recs = prev.get("records") or []
        else:
            recs = parser(path)
            if recs is None:               # unreadable — skip, don't cache (retry next run)
                continue
            changed = True
        new_files[key] = {"mtime": st.st_mtime_ns, "size": st.st_size, "records": recs}
        records.extend(recs)
    if cache_path is not None and complete and (changed or len(new_files) != len(old_files)):
        _save_parse_cache(cache_path, parse_version, new_files)
    return records


def _resolve_cache_path(explicit_dir, provided_root, name: str) -> Path | None:
    """Pick the cache file for a summarizer. ``explicit_dir is _UNSET`` (production,
    default root) -> the real ~/.tallybar/cache file; a caller-provided root (tests)
    -> None (no cache) unless they pass an explicit cache dir; ``None`` -> no cache."""
    if explicit_dir is _UNSET:
        return None if provided_root is not None else _PARSE_CACHE_DIR / name
    if explicit_dir is None:
        return None
    return Path(explicit_dir) / name


def _parse_claude_file(path: Path) -> list[dict[str, Any]] | None:
    """Extract Claude assistant usage records from one jsonl file (no time filter — the
    summarizer applies the window). Returns None if the file can't be opened (so the
    cache skips rather than memoizes a transient read error)."""
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        return None
    out: list[dict[str, Any]] = []
    with handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("type") != "assistant":
                continue
            message = as_dict(record.get("message"))
            usage = message.get("usage") or record.get("usage")
            if usage_token_total(usage) <= 0:
                continue
            out.append({
                "t": record.get("timestamp"),
                "m": message.get("model") or record.get("model"),
                "u": usage,
                "r": str(record.get("requestId") or record.get("uuid") or ""),
            })
    return out


def _parse_codex_file(path: Path) -> list[dict[str, Any]] | None:
    """Extract Codex usage records from one jsonl file, tracking the latest model line
    so mid-session switches price subsequent turns correctly (the resolved model is
    stored per record). No time filter — the summarizer applies the window."""
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        return None
    out: list[dict[str, Any]] = []
    with handle:
        current_model: str | None = None
        for line in handle:
            # turn_context / session_meta events name the active model. Track the LATEST one.
            if '"model"' in line:
                try:
                    meta = json.loads(line)
                except json.JSONDecodeError:
                    meta = None
                if isinstance(meta, dict):
                    payload = as_dict(meta.get("payload"), meta)
                    m = payload.get("model") if isinstance(payload, dict) else None
                    if isinstance(m, str) and m:
                        current_model = m
            if "last_token_usage" not in line and "total_token_usage" not in line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = as_dict(record.get("payload"))
            info = as_dict(payload.get("info"))
            usage = info.get("last_token_usage") or info.get("total_token_usage") or {}
            if usage_token_total(usage) <= 0:
                continue
            out.append({"t": record.get("timestamp"), "m": current_model, "u": usage})
    return out


def _display_grok_model(model: str | None) -> str:
    """Short *display* label for the cost popout model breakdown.

    Catalog ids are long (``grok-composer-2.5-fast``); the UI wants compact
    names: ``Grok 4.6``, ``Grok 4.5``, ``Composer 2.5``, ``Grok Build``.
    Pricing still uses the raw id via ``_grok_pricing_model`` — this is
    display-only.
    """
    if not model or not str(model).strip():
        return "Grok Build"
    m = str(model).strip()
    # Already a human label (including our own short forms).
    if " " in m:
        return m
    low = m.lower().replace("_", "-")

    if "composer" in low:
        # grok-composer-2.5-fast → Composer 2.5 (drop trailing "fast"/tags)
        ver = re.search(r"composer-?(\d+(?:\.\d+)*)", low)
        return f"Composer {ver.group(1)}" if ver else "Composer"

    if "build" in low:
        return "Grok Build"

    # grok-4.6 / grok-4.5 / grok-4-5 / grok-4.20-0309-reasoning → Grok 4.6 / Grok 4.5 / Grok 4.20
    ver = re.match(r"grok-?(\d+(?:\.\d+)*)", low)
    if ver:
        return f"Grok {ver.group(1)}"

    if low.startswith("grok-"):
        return "Grok " + m[5:].replace("-", " ").strip().title()
    return m


def _grok_cli_pricing_model(model: str | None) -> str:
    """Pricing key for usage read out of ``~/.grok`` — i.e. Grok Build CLI usage.

    The CLI runs a **build-line** model, but nothing on disk says so. Verified
    2026-09-11: ``grok usage <session-id>`` reports ``primaryModelId`` as
    ``grok-4.6-build`` for every session, while BOTH on-disk sources the
    summarizer reads — ``unified.jsonl`` and each session's ``summary.json`` —
    record the bare ``grok-4.6``. That bare string misses
    ``_grok_pricing_model``'s ``"build" in low`` branch and falls through to the
    version regex, so CLI turns were being priced on xAI's **API** row
    ($2.00/$6.00/$0.50 per MTok) instead of the build row ($1.00/$2.00/$0.20) —
    a ~2.4x overstatement on a cache-heavy workload, and contrary to the
    documented intent that build/CLI models bill like build.

    xAI publishes no ``grok-4.6-build`` price (checked against docs.x.ai/docs/models
    on 2026-09-11: only ``grok-4.6`` and ``grok-build-0.1`` are listed), so the
    build row is an estimate — but a well-bounded one. xAI's own per-session
    accounting (``costUsdTicks``) for these sessions comes out BELOW even the
    build row, so the true rate is at or under it; the API row is the one answer
    we can rule out.

    Composer and explicitly-``build``-named ids keep their existing mapping —
    only a BARE ``grok-<version>`` is redirected, since that is the shape the
    CLI's own logs emit.

    Not folded into ``_grok_pricing_model``: that function must keep mapping
    ``grok-4.6`` to the API row for any future non-CLI caller (and its tests
    pin exactly that).
    """
    key = _grok_pricing_model(model)
    if re.fullmatch(r"grok-\d+(?:\.\d+)*", key):
        return "grok-build-0.1"
    return key


def _grok_pricing_model(model: str | None) -> str:
    """Map a raw (or display) Grok model name onto a catalog/fallback pricing key.

    Accepts either catalog ids (``grok-4.6`` / ``grok-4.5``) or short labels
    (``Composer 2.5``) so callers can pass whichever they have. Composer has
    no public list price yet → bill like build while still *showing* as
    Composer.
    """
    if not model or not str(model).strip():
        return DEFAULT_MODEL_FOR_PROVIDER["grok"]
    raw = str(model).strip()
    low = raw.lower().replace("_", "-")

    if "composer" in low:
        ver = re.search(r"composer-?(\d+(?:\.\d+)*)", low) or re.search(
            r"composer\s+(\d+(?:\.\d+)*)", low
        )
        if ver:
            return f"grok-composer-{ver.group(1)}-fast"
        return "grok-composer-2.5-fast"
    if "build" in low:
        return "grok-build-0.1"
    # Short "Grok 4.6" / "Grok 4.5" or raw "grok-4.6" / "grok-4.5"
    ver = re.search(r"(?:grok[-\s]?)(\d+(?:\.\d+)*)", low)
    if ver:
        return f"grok-{ver.group(1)}"
    if low.startswith("grok-"):
        return raw
    return raw


def _parse_grok_file(path: Path) -> list[dict[str, Any]] | None:
    """Extract Grok Build inference usage records from one unified.jsonl file.

    Tracks the active model per session id from ``model changed``,
    ``backend_search: model switch``, and catalog fields so mid-session switches
    (e.g. grok-4.5 ↔ grok-composer-2.5-fast) price and display correctly.

    Token fields map onto the OpenAI-style shape ``usage_cost_usd`` already
    understands: ``prompt_tokens`` includes ``cached_prompt_tokens`` (subset),
    and ``reasoning_tokens`` is a subset of ``completion_tokens``.

    No time filter — the summarizer applies the window. Returns None if the
    file can't be opened (so the cache skips rather than memoizes a read error).
    """
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        return None
    out: list[dict[str, Any]] = []
    model_by_sid: dict[str, str] = {}
    last_global_model: str | None = None

    def _remember(model: Any, sid_key: str) -> None:
        nonlocal last_global_model
        if not isinstance(model, str) or not model.strip():
            return
        name = model.strip()
        last_global_model = name
        if sid_key:
            model_by_sid[sid_key] = name

    with handle:
        for line in handle:
            # Cheap pre-filter: most lines are tool/phase noise.
            if (
                "shell.turn.inference_done" not in line
                and "model changed" not in line
                and "model switch" not in line
                and "current_model_id" not in line
                and "new_model" not in line
                and "session_model_id" not in line
            ):
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            msg = record.get("msg")
            ctx = as_dict(record.get("ctx"))
            sid = record.get("sid")
            sid_key = str(sid) if isinstance(sid, str) and sid else ""

            if msg == "model changed":
                _remember(ctx.get("model"), sid_key)
                continue

            if msg == "backend_search: model switch":
                _remember(ctx.get("new_model") or ctx.get("model"), sid_key)
                continue

            # Catalog / parent-config events sometimes carry the session model.
            if msg != "shell.turn.inference_done":
                for key in (
                    "current_model_id",
                    "session_model_id",
                    "global_model_id",
                    "new_model",
                    "model_id",
                    "canonical_model",
                    "effective_model",
                ):
                    if key in ctx:
                        _remember(ctx.get(key), sid_key)
                        break
                continue

            prompt = ctx.get("prompt_tokens")
            completion = ctx.get("completion_tokens")
            if not isinstance(prompt, (int, float)) and not isinstance(completion, (int, float)):
                continue
            prompt_i = int(prompt) if isinstance(prompt, (int, float)) and prompt > 0 else 0
            completion_i = int(completion) if isinstance(completion, (int, float)) and completion > 0 else 0
            cached = ctx.get("cached_prompt_tokens")
            cached_i = int(cached) if isinstance(cached, (int, float)) and cached > 0 else 0
            # Clamp cache to input so a malformed log can't invert uncached_input.
            if cached_i > prompt_i:
                cached_i = prompt_i

            usage = {
                "input_tokens": prompt_i,
                "cached_input_tokens": cached_i,
                "output_tokens": completion_i,
            }
            if usage_token_total(usage) <= 0:
                continue

            model = model_by_sid.get(sid_key) or last_global_model
            loop = ctx.get("loop_index")
            # sid+loop+ts keeps multi-loop turns unique without relying on file order.
            dedup = f"{sid_key}:{loop}:{record.get('ts')}"
            out.append({
                "t": record.get("ts"),
                "m": model,
                "u": usage,
                "r": dedup,
            })
    return out


# ---------------------------------------------------------------------------
# Local provider log summarizers
# ---------------------------------------------------------------------------

def local_claude_token_summary(
    projects_dir: Path | None = None,
    now: dt.datetime | None = None,
    tier: str | None = None,
    cache_dir: Any = _UNSET,
    deadline: float | None = None,
) -> dict[str, str] | None:
    root = projects_dir or (Path.home() / ".claude" / "projects")
    if not root.is_dir():
        return None
    cache_path = _resolve_cache_path(cache_dir, projects_dir, "claude_logs.json")

    current = now.astimezone() if now is not None else dt.datetime.now(dt.timezone.utc).astimezone()
    today = current.date()
    # Anchor the rolling 30-day window to midnight so the month/30-day gate is
    # date-based, matching the daily/"today" .date() gates below (and cost.py's
    # m30_iso). Otherwise a same-clock-time event ~30 days ago is excluded from the
    # month total while a boundary event still counts toward "today" — making
    # "Today" exceed its contribution to "Last 30 days" at the day boundary.
    thirty_days_ago = (current - dt.timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
    current_month_start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    history_start = min(thirty_days_ago, current_month_start)
    today_tokens = 0
    month_tokens = 0
    today_cost = 0.0
    month_cost = 0.0
    month_in = month_out = month_cached = 0
    model_costs: dict[str, dict[str, Any]] = {}
    daily = empty_weekly_token_buckets(current)
    monthly_buckets = empty_monthly_token_buckets(current)
    hourly = empty_hourly_token_buckets(current)
    # Claude Code writes ONE jsonl line per content block of a streamed reply: every
    # line repeats the same requestId and the same input/cache counts, but
    # output_tokens is a GROWING snapshot — tiny on the first line, complete on the
    # final (stop_reason-bearing) line. So we must dedup by requestId and keep the
    # line with the LARGEST output (the final usage), NOT the first-seen one — else
    # output is billed at a partial value (~20% output undercount on real data, and
    # the captured partial even varies with file-traversal order). Mirrors how
    # ccusage folds each message.id+requestId to its final usage. Records with no
    # dedup key (no requestId and no uuid) can't be folded, so they're kept as-is.
    best: dict[str, dict[str, Any]] = {}   # request_key -> {usage, model, timestamp, out}
    keyless: list[dict[str, Any]] = []

    def _out_tokens(u: Any) -> int:
        if isinstance(u, dict):
            v = u.get("output_tokens")
            if isinstance(v, (int, float)) and v > 0:
                return int(v)
        return 0

    # Records come from the per-file cache (only changed files are reparsed). They are
    # time-unfiltered and in rglob/file order, so applying the window gate then the
    # requestId dedup here is identical to the old inline line-by-line scan.
    for record in _cached_log_records(root, "*.jsonl", cache_path,
                                       _parse_claude_file, _CLAUDE_PARSE_VERSION,
                                       deadline=deadline):
        timestamp = parse_timestamp(record["t"])
        if timestamp is None or timestamp < history_start:
            continue
        usage = record["u"]
        model = record["m"] or DEFAULT_MODEL_FOR_PROVIDER["claude"]
        entry = {"usage": usage, "model": model, "timestamp": timestamp, "out": _out_tokens(usage)}
        request_key = record["r"]
        if not request_key:
            keyless.append(entry)
            continue
        prev = best.get(request_key)
        if prev is None or entry["out"] > prev["out"]:
            best[request_key] = entry

    for entry in (*best.values(), *keyless):
        usage = entry["usage"]
        model = entry["model"]
        timestamp = entry["timestamp"]
        tokens, (bi, bo, bc) = usage_token_total_and_breakdown(usage)
        cost = usage_cost_usd(usage, model)
        day_key = timestamp.date().isoformat()
        if timestamp >= thirty_days_ago:
            month_tokens += tokens
            month_cost += cost
            month_in += bi
            month_out += bo
            month_cached += bc
            slot = model_costs.setdefault(str(model), {"cost": 0.0, "tokens": 0})
            slot["cost"] += cost
            slot["tokens"] += tokens
        if day_key in daily:
            bucket_add(daily[day_key], tokens, cost, model)
        if day_key in monthly_buckets and monthly_buckets[day_key].get("inMonth"):
            bucket_add(monthly_buckets[day_key], tokens, cost, model)
        if timestamp.date() == today:
            today_tokens += tokens
            today_cost += cost
            bucket_add(hourly[timestamp.hour], tokens, cost, model)

    return token_summary(today_tokens, month_tokens, "local-claude-logs", today_cost, month_cost, tier,
                         breakdown=(month_in, month_out, month_cached),
                         daily=weekly_token_usage(daily),
                         monthly=monthly_token_usage(monthly_buckets),
                         hourly=hourly_token_usage(hourly),
                         model_costs=model_costs)


def local_codex_token_summary(
    sessions_dir: Path | None = None,
    now: dt.datetime | None = None,
    tier: str | None = None,
    cache_dir: Any = _UNSET,
    deadline: float | None = None,
) -> dict[str, str] | None:
    root = sessions_dir or (Path.home() / ".codex" / "sessions")
    if not root.is_dir():
        return None
    cache_path = _resolve_cache_path(cache_dir, sessions_dir, "codex_logs.json")

    current = now.astimezone() if now is not None else dt.datetime.now(dt.timezone.utc).astimezone()
    today = current.date()
    # Anchor the rolling 30-day window to midnight so the month/30-day gate is
    # date-based, matching the daily/"today" .date() gates below (and cost.py's
    # m30_iso). Otherwise a same-clock-time event ~30 days ago is excluded from the
    # month total while a boundary event still counts toward "today" — making
    # "Today" exceed its contribution to "Last 30 days" at the day boundary.
    thirty_days_ago = (current - dt.timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
    current_month_start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    history_start = min(thirty_days_ago, current_month_start)
    today_tokens = 0
    month_tokens = 0
    today_cost = 0.0
    month_cost = 0.0
    month_in = month_out = month_cached = 0
    model_costs: dict[str, dict[str, Any]] = {}
    daily = empty_weekly_token_buckets(current)
    monthly_buckets = empty_monthly_token_buckets(current)
    hourly = empty_hourly_token_buckets(current)

    # Records come from the per-file cache (only changed files are reparsed); the active
    # model is resolved at extraction time (per-file, in order), so each record already
    # carries its priced model. Applying the window gate here matches the old inline scan.
    for record in _cached_log_records(root, "*.jsonl", cache_path,
                                       _parse_codex_file, _CODEX_PARSE_VERSION,
                                       deadline=deadline):
        timestamp = parse_timestamp(record["t"])
        if timestamp is None or timestamp < history_start:
            continue
        usage = record["u"]
        tokens, (bi, bo, bc) = usage_token_total_and_breakdown(usage)
        if tokens <= 0:
            continue
        model = record["m"] or DEFAULT_MODEL_FOR_PROVIDER["codex"]
        cost = usage_cost_usd(usage, model)
        day_key = timestamp.date().isoformat()
        if timestamp >= thirty_days_ago:
            month_tokens += tokens
            month_cost += cost
            month_in += bi
            month_out += bo
            month_cached += bc
            slot = model_costs.setdefault(str(model), {"cost": 0.0, "tokens": 0})
            slot["cost"] += cost
            slot["tokens"] += tokens
        if day_key in daily:
            bucket_add(daily[day_key], tokens, cost, model)
        if day_key in monthly_buckets and monthly_buckets[day_key].get("inMonth"):
            bucket_add(monthly_buckets[day_key], tokens, cost, model)
        if timestamp.date() == today:
            today_tokens += tokens
            today_cost += cost
            bucket_add(hourly[timestamp.hour], tokens, cost, model)

    return token_summary(today_tokens, month_tokens, "local-codex-logs", today_cost, month_cost, tier,
                         breakdown=(month_in, month_out, month_cached),
                         daily=weekly_token_usage(daily),
                         monthly=monthly_token_usage(monthly_buckets),
                         hourly=hourly_token_usage(hourly),
                         model_costs=model_costs)


def _load_grok_session_models(sessions_dir: Path | None = None) -> dict[str, str]:
    """sid → current_model_id from ``~/.grok/sessions/**/summary.json``.

    Used only to fill inference rows that predate any ``model changed`` line in
    the unified log (common for the first turns of a session). Not part of the
    parse cache — summaries can update without the log growing.
    """
    root = sessions_dir or (Path.home() / ".grok" / "sessions")
    if not root.is_dir():
        return {}
    out: dict[str, str] = {}
    try:
        paths = root.rglob("summary.json")
    except OSError:
        return {}
    for path in paths:
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        model = data.get("current_model_id")
        if not isinstance(model, str) or not model.strip():
            continue
        info = as_dict(data.get("info"))
        sid = info.get("id") if isinstance(info, dict) else None
        if not isinstance(sid, str) or not sid:
            sid = path.parent.name
        if isinstance(sid, str) and sid:
            out[sid] = model.strip()
    return out


def local_grok_token_summary(
    logs_dir: Path | None = None,
    sessions_dir: Path | None = None,
    now: dt.datetime | None = None,
    tier: str | None = None,
    cache_dir: Any = _UNSET,
    deadline: float | None = None,
    week_start: dt.datetime | None = None,
    week_end: dt.datetime | None = None,
) -> dict[str, str] | None:
    """Summarise pay-per-use cost/tokens from Grok Build's unified.jsonl.

    Default root is ``~/.grok/logs`` (single ``unified.jsonl`` today; rglob so a
    rotated ``unified.jsonl.*`` would also be picked up). Same windowing,
    buckets, and parse-cache contract as Claude/Codex local summarizers.

    Optional ``week_start``/``week_end`` (both timezone-aware) set an exact billing-week
    window for accumulating ``billingWeekTokens`` / ``billingWeekCost`` into the returned
    summary. When omitted, neither key is present.

    ``sessions_dir`` overrides the default ``~/.grok/sessions`` used for sid→model lookup.
    ``deadline`` is a cooperative time.time() cutoff passed through to _cached_log_records.
    """
    root = logs_dir or (Path.home() / ".grok" / "logs")
    if not root.is_dir():
        return None
    cache_path = _resolve_cache_path(cache_dir, logs_dir, "grok_logs.json")

    current = now.astimezone() if now is not None else dt.datetime.now(dt.timezone.utc).astimezone()
    today = current.date()
    # Anchor the rolling 30-day window to midnight (see local_codex_token_summary).
    thirty_days_ago = (current - dt.timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
    current_month_start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    history_start = min(thirty_days_ago, current_month_start)
    today_tokens = 0
    month_tokens = 0
    today_cost = 0.0
    month_cost = 0.0
    month_in = month_out = month_cached = 0
    model_costs: dict[str, dict[str, Any]] = {}
    daily = empty_weekly_token_buckets(current)
    monthly_buckets = empty_monthly_token_buckets(current)
    hourly = empty_hourly_token_buckets(current)
    week_tokens_billing = 0
    week_cost_billing = 0.0
    live_days: dict[str, dict[str, Any]] = {}

    # Dedup by request key (sid:loop:ts) — keeps first-seen; each inference_done is
    # a completed generation, so collisions only happen if the same line is scanned twice.
    seen: set[str] = set()
    # Fill model gaps from session summaries (sid → current_model_id). Log-only
    # tracking misses the first turns of a session before any "model changed" line.
    # When logs_dir is provided (tests), derive sessions from sibling dir; otherwise default.
    if sessions_dir is not None:
        sessions_root = sessions_dir
    elif logs_dir is not None:
        sibling = logs_dir.parent / "sessions"
        sessions_root = sibling if sibling.is_dir() else Path.home() / ".grok" / "sessions"
    else:
        sessions_root = Path.home() / ".grok" / "sessions"
    session_models = _load_grok_session_models(sessions_root)

    for record in _cached_log_records(root, "unified.jsonl*", cache_path,
                                       _parse_grok_file, _GROK_PARSE_VERSION,
                                       deadline=deadline):
        timestamp = parse_timestamp(record["t"])
        if timestamp is None or timestamp < history_start:
            continue
        request_key = str(record.get("r") or "")
        if request_key:
            if request_key in seen:
                continue
            seen.add(request_key)
        usage = record["u"]
        tokens, (bi, bo, bc) = usage_token_total_and_breakdown(usage)
        if tokens <= 0:
            continue
        raw_model = record.get("m")
        if not raw_model and request_key:
            sid = request_key.split(":", 1)[0]
            raw_model = session_models.get(sid)
        # Short labels for the popout ("Grok 4.6", "Grok 4.5", "Composer 2.5");
        # price off the raw catalog id so we don't lose precision after prettifying.
        display_model = _display_grok_model(raw_model)
        # CLI usage: bare "grok-4.6" on disk is really grok-4.6-build (see helper).
        cost = usage_cost_usd(usage, _grok_cli_pricing_model(raw_model or display_model))
        day_key = timestamp.date().isoformat()
        if timestamp >= thirty_days_ago:
            month_tokens += tokens
            month_cost += cost
            month_in += bi
            month_out += bo
            month_cached += bc
            slot = model_costs.setdefault(str(display_model), {"cost": 0.0, "tokens": 0})
            slot["cost"] += cost
            slot["tokens"] += tokens
        # Per-day accumulation for the archive (see merge_grok_archive): the log this
        # was parsed from holds only ~2 days, so these have to be banked somewhere.
        lrec = live_days.setdefault(day_key, {"tokens": 0, "cost": 0.0, "in": 0, "out": 0,
                                              "cached": 0, "models": {}})
        lrec["tokens"] += tokens
        lrec["cost"] += cost
        lrec["in"] += bi
        lrec["out"] += bo
        lrec["cached"] += bc
        lslot = lrec["models"].setdefault(str(display_model), {"cost": 0.0, "tokens": 0})
        lslot["cost"] += cost
        lslot["tokens"] += tokens
        if day_key in daily:
            bucket_add(daily[day_key], tokens, cost, display_model)
        if day_key in monthly_buckets and monthly_buckets[day_key].get("inMonth"):
            bucket_add(monthly_buckets[day_key], tokens, cost, display_model)
        if timestamp.date() == today:
            today_tokens += tokens
            today_cost += cost
            bucket_add(hourly[timestamp.hour], tokens, cost, display_model)
        if week_start is not None and timestamp >= week_start and (week_end is None or timestamp < week_end):
            week_tokens_billing += tokens
            week_cost_billing += cost

    # Bank what we just parsed, then replay any archived day the log has since dropped.
    # Only days absent from this parse are replayed — a day we DID see is already counted
    # in every bucket above, and adding the archived copy would double it.
    #
    # ONLY when reading the REAL log. An injected logs_dir/cache_dir means fixture data is
    # driving this, and banking that into the shared ~/.tallybar archive writes invented
    # days into the user's live history — which is what happened the first time this
    # shipped: a test run put 2026-05-28 plus three July days, at a tidy $0.01/1k tokens,
    # into a real archive. Expressed as a plain condition rather than raise/except: the
    # first fix used a sentinel exception inside this try, referenced the wrong parameter
    # name, and the bare `except Exception` swallowed the NameError — silently disabling
    # the archive for everyone instead of just for tests.
    fixture_driven = logs_dir is not None or cache_dir is not _UNSET
    if not fixture_driven:
        try:
            for day, rec in merge_grok_archive(live_days, today.isoformat()).items():
                if day in live_days:
                    continue
                try:
                    dday = dt.date.fromisoformat(day)
                except ValueError:
                    continue
                d_tokens = int(rec.get("tokens") or 0)
                d_cost = float(rec.get("cost") or 0.0)
                if d_tokens <= 0 and d_cost <= 0.0:
                    continue
                models = as_dict(rec.get("models"))
                if dday >= thirty_days_ago.date():
                    month_tokens += d_tokens
                    month_cost += d_cost
                    month_in += int(rec.get("in") or 0)
                    month_out += int(rec.get("out") or 0)
                    month_cached += int(rec.get("cached") or 0)
                    for mname, mrec in models.items():
                        slot = model_costs.setdefault(str(mname), {"cost": 0.0, "tokens": 0})
                        slot["cost"] += float(mrec.get("cost") or 0.0)
                        slot["tokens"] += int(mrec.get("tokens") or 0)
                for bucket_map in (daily, monthly_buckets):
                    if day not in bucket_map:
                        continue
                    if bucket_map is monthly_buckets and not bucket_map[day].get("inMonth"):
                        continue
                    for mname, mrec in (models or {"Grok Build": {"cost": d_cost,
                                                                  "tokens": d_tokens}}).items():
                        bucket_add(bucket_map[day], int(mrec.get("tokens") or 0),
                                   float(mrec.get("cost") or 0.0), str(mname))
        except Exception:
            pass  # history is a nice-to-have; never break the snapshot for it

    summary = token_summary(today_tokens, month_tokens, "local-grok-logs", today_cost, month_cost, tier,
                            breakdown=(month_in, month_out, month_cached),
                            daily=weekly_token_usage(daily),
                            monthly=monthly_token_usage(monthly_buckets),
                            hourly=hourly_token_usage(hourly),
                            model_costs=model_costs)
    if summary is not None and week_start is not None:
        summary["billingWeekTokens"] = int(week_tokens_billing)
        summary["billingWeekCost"] = round(float(week_cost_billing), 6)
    return summary


# ---------------------------------------------------------------------------
# Grok daily archive
#
# ~/.grok/logs/unified.jsonl is a SINGLE rolling log that xAI truncates in place —
# measured on a live machine: 3.5 MB holding ~47 hours (2026-09-09..2026-09-11),
# no rotated siblings, and a 187M -> 99.5M token drop observed between two refreshes
# two hours apart. Sessions persist ~30 days but carry no per-call billing, and
# `grok usage` is backed by the same short-lived store, so ~2 days is the ceiling
# xAI offers. Every other provider writes per-session files that survive.
#
# So Grok history has to be ACCUMULATED rather than re-read. Each refresh folds the
# days it can currently see into an append-only archive; days that scroll out of the
# log survive there. This is the same discipline as cost_archive.json (atomic 0600 +
# .bak mirror + .corrupt quarantine + change-gated write).
# ---------------------------------------------------------------------------

GROK_ARCHIVE_PATH = Path.home() / ".tallybar" / "grok_archive.json"
_GROK_ARCHIVE_KEEP_DAYS = 120  # the UI never shows more than ~42; bounded but generous


def _load_grok_archive() -> dict[str, Any]:
    """Never-load-into-empty: recover from ``.bak``, quarantine a corrupt main.

    A transient read failure that returned {} would let the next write replace real
    history with whatever two days the log happens to hold — the exact erosion this
    archive exists to prevent.
    """
    main = GROK_ARCHIVE_PATH
    bak = main.with_name(main.name + ".bak")
    for idx, path in enumerate((main, bak)):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("days"), dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
        if idx == 0:
            try:
                main.replace(main.with_name(main.name + ".corrupt"))
            except OSError:
                pass
    return {"version": 1, "days": {}}


def _save_grok_archive(archive: dict[str, Any]) -> None:
    """Atomic 0600 write + .bak mirror through the one shared io_helpers recipe."""
    payload = json.dumps(archive)
    try:
        atomic_write_text(GROK_ARCHIVE_PATH, payload)
    except OSError:
        return
    try:
        atomic_write_text(GROK_ARCHIVE_PATH.with_name(GROK_ARCHIVE_PATH.name + ".bak"), payload)
    except OSError:
        pass


def merge_grok_archive(live_days: dict[str, dict[str, Any]],
                       today_iso: str,
                       keep_days: int = _GROK_ARCHIVE_KEEP_DAYS) -> dict[str, dict[str, Any]]:
    """Fold ``live_days`` into the archive and return the union.

    **A day is only ever replaced upward.** Within a calendar day usage is monotonic,
    and truncation can only ever REMOVE records — so a live figure lower than the
    archived one means the log has lost part of that day, not that usage shrank. Taking
    the max per day is what makes truncation harmless; a plain overwrite would let the
    rolling log eat its own history one day at a time.

    Comparison is on tokens (the primitive the cost is derived from), and the whole day
    record moves together so cost, the in/out/cached split and the per-model map stay
    mutually consistent rather than being max'd field-by-field into a blend that never
    existed.

    Change-gated like the ledger's ``new_marks != marks``: an unchanged union skips the
    write entirely, so a quiet refresh costs no disk I/O. Best-effort throughout —
    Grok history is a nice-to-have and must never break a snapshot.
    """
    archive = _load_grok_archive()
    days: dict[str, Any] = dict(archive.get("days") or {})
    before = json.dumps(days, sort_keys=True)

    for day, rec in (live_days or {}).items():
        prev = days.get(day)
        if not isinstance(prev, dict) or int(rec.get("tokens") or 0) >= int(prev.get("tokens") or 0):
            days[day] = rec

    # Bound the file. Prune by date string (ISO sorts lexicographically).
    if len(days) > keep_days:
        for stale in sorted(days)[:-keep_days]:
            days.pop(stale, None)

    if json.dumps(days, sort_keys=True) != before:
        archive["days"] = days
        archive["version"] = 1
        archive["updatedAt"] = today_iso
        _save_grok_archive(archive)
    return days


def _gemini_session_files(base: Path):
    """gemini-cli session files under ``base``, pruning antigravity* subtrees: ~/.gemini
    also hosts the multi-GB Antigravity CLI/Desktop/IDE/browser-profile trees (which hold
    no session files), and rglob-ing them os.scandir's ~300K entries for nothing. Do NOT
    revert this to rglob."""
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if not d.startswith("antigravity")]
        for fn in filenames:
            if fn.startswith("session-") and fn.endswith(".json"):
                yield Path(dirpath) / fn


def _parse_gemini_file(path: Path) -> list[dict[str, Any]] | None:
    """Extract gemini-cli usage records from one session-*.json (no time filter — the
    summarizer applies the window each call; parse caches must stay pure functions of
    file bytes). Returns None if the file can't be opened (so the cache skips rather
    than memoizes a transient read error)."""
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        return None
    with handle:
        try:
            session = json.load(handle)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return []
    if not isinstance(session, dict) or not isinstance(session.get("messages"), list):
        return []
    out: list[dict[str, Any]] = []
    for msg in session["messages"]:
        if not isinstance(msg, dict) or msg.get("type") != "gemini":
            continue
        ts = msg.get("timestamp")
        tokens_data = msg.get("tokens")
        if not isinstance(ts, str) or not isinstance(tokens_data, dict):
            continue
        if usage_token_total(tokens_data) <= 0:
            continue
        out.append({"t": ts, "m": msg.get("model"), "u": tokens_data})
    return out


def local_gemini_token_summary(
    gemini_dir: Path | None = None,
    now: dt.datetime | None = None,
    tier: str | None = None,
    cache_dir: Any = _UNSET,
    deadline: float | None = None,
) -> dict[str, str] | None:
    root = gemini_dir or (Path.home() / ".gemini")
    if not root.is_dir():
        return None
    cache_path = _resolve_cache_path(cache_dir, gemini_dir, "gemini_logs.json")

    current = now.astimezone() if now is not None else dt.datetime.now(dt.timezone.utc).astimezone()
    today = current.date()
    # Anchor the rolling 30-day window to midnight so the month/30-day gate is
    # date-based, matching the daily/"today" .date() gates below (and cost.py's
    # m30_iso). Otherwise a same-clock-time event ~30 days ago is excluded from the
    # month total while a boundary event still counts toward "today" — making
    # "Today" exceed its contribution to "Last 30 days" at the day boundary.
    thirty_days_ago = (current - dt.timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
    current_month_start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    history_start = min(thirty_days_ago, current_month_start)
    today_tokens = 0
    month_tokens = 0
    today_cost = 0.0
    month_cost = 0.0
    month_in = month_out = month_cached = 0
    model_costs: dict[str, dict[str, Any]] = {}
    daily = empty_weekly_token_buckets(current)
    monthly_buckets = empty_monthly_token_buckets(current)
    hourly = empty_hourly_token_buckets(current)

    # Records come from the per-file cache (only changed session files are reparsed —
    # previously every refresh re-read and re-json.load'ed every session file). They are
    # time-unfiltered, so the window gate is applied here each call, like the other three
    # provider summarizers.
    for record in _cached_log_records(root, "session-*.json", cache_path,
                                       _parse_gemini_file, _GEMINI_PARSE_VERSION,
                                       walker=_gemini_session_files, deadline=deadline):
        timestamp = parse_timestamp(record.get("t") or "")
        if timestamp is None or timestamp < history_start:
            continue
        tokens_data = record.get("u")
        if not isinstance(tokens_data, dict):
            continue
        tokens, (bi, bo, bc) = usage_token_total_and_breakdown(tokens_data)
        if tokens <= 0:
            continue

        model = record.get("m") or DEFAULT_MODEL_FOR_PROVIDER["gemini"]
        cost = usage_cost_usd(tokens_data, model)

        day_key = timestamp.date().isoformat()
        if timestamp >= thirty_days_ago:
            month_tokens += tokens
            month_cost += cost
            month_in += bi
            month_out += bo
            month_cached += bc
            slot = model_costs.setdefault(str(model), {"cost": 0.0, "tokens": 0})
            slot["cost"] += cost
            slot["tokens"] += tokens
        if day_key in daily:
            bucket_add(daily[day_key], tokens, cost, model)
        if day_key in monthly_buckets and monthly_buckets[day_key].get("inMonth"):
            bucket_add(monthly_buckets[day_key], tokens, cost, model)
        if timestamp.date() == today:
            today_tokens += tokens
            today_cost += cost
            bucket_add(hourly[timestamp.hour], tokens, cost, model)

    return token_summary(today_tokens, month_tokens, "local-gemini-logs", today_cost, month_cost, tier,
                         breakdown=(month_in, month_out, month_cached),
                         daily=weekly_token_usage(daily),
                         monthly=monthly_token_usage(monthly_buckets),
                         hourly=hourly_token_usage(hourly),
                         model_costs=model_costs)


# ---------------------------------------------------------------------------
# Usage-limit helpers
# ---------------------------------------------------------------------------

def merge_usage_limits(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        for limit in group:
            label = str(limit.get("label") or "").strip()
            key = label.lower()
            if not label or key in seen:
                continue
            seen.add(key)
            merged.append(limit)
    return merged


def _codex_window_label(default_label: str, window: Any) -> str:
    """Label a Codex quota lane from its ACTUAL window length, not its position.

    ``account/rateLimits/read`` returns lanes positionally (``primary`` /
    ``secondary``) and the durations vary by plan: a Plus account's primary is a
    300-minute (5-hour) window, but a **free** account's primary is 43200 minutes
    (30 days) with no secondary at all. Labelling by position alone rendered that
    30-day lane as "Session · Resets in 29d 23h" — verified against a live
    ``planType: "free"`` response on 2026-09-11.

    Mapping (minutes): <=360 "Session"; <=20160 "Weekly"; above that "Plan".

    **"Plan", never "Monthly"** — ``enrich_ui_formatting`` flags any label
    containing "monthly"/"credits"/"extra"/"on-demand" as ``isExtraUsage`` and
    renders it as a separate on-demand lane, which a plan window is not. Grok's
    parser avoids the same word for the same reason.

    An absent or unparseable duration keeps the positional default, so every
    previously-correct payload is unchanged.
    """
    if not isinstance(window, dict):
        return default_label
    mins = window.get("windowDurationMins")
    if not isinstance(mins, (int, float)) or isinstance(mins, bool) or mins <= 0:
        return default_label
    if mins <= 360:
        return "Session"
    if mins <= 20160:
        return "Weekly"
    return "Plan"


def codex_limit_from_window(key: str, label: str, window: Any) -> dict[str, Any] | None:
    if not isinstance(window, dict):
        return None
    percent = window.get("usedPercent")
    if not isinstance(percent, (int, float)):
        return None
    resets_at = window.get("resetsAt")
    if isinstance(resets_at, (int, float)) and resets_at > 0:
        reset_text = relative_reset(
            dt.datetime.fromtimestamp(float(resets_at), tz=dt.timezone.utc).isoformat()
        )
    else:
        reset_text = str(window.get("resetDescription") or "")
    limit: dict[str, Any] = {
        "label": label,
        "percent": max(0.0, min(100.0, float(percent))),
        "reset": reset_text,
        "unit": "credits" if key == "credits" else "%",
    }
    for used_key in ("used", "usageTotal", "usedAmount", "consumed"):
        used = window.get(used_key)
        if isinstance(used, (int, float)):
            limit["used"] = float(used)
            break
    for limit_key in ("limit", "usageLimit", "total", "cap"):
        cap = window.get(limit_key)
        if isinstance(cap, (int, float)):
            limit["limit"] = float(cap)
            break
    currency = window.get("currency") or window.get("currencyCode")
    if isinstance(currency, str) and currency.strip():
        limit["currency"] = currency.strip().upper()
    return limit


def numeric_value(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").strip())
        except ValueError:
            return None
    return None


def codex_spark_window(rate_limits_by_id: Any) -> dict[str, Any] | None:
    if not isinstance(rate_limits_by_id, dict):
        return None
    for limit_id, data in rate_limits_by_id.items():
        if not isinstance(data, dict):
            continue
        label = f"{limit_id} {data.get('limitName') or ''}".lower()
        if "spark" not in label and "bengalfox" not in label:
            continue
        secondary = data.get("secondary")
        if isinstance(secondary, dict) and isinstance(secondary.get("usedPercent"), (int, float)):
            return secondary
        primary = data.get("primary")
        if isinstance(primary, dict) and isinstance(primary.get("usedPercent"), (int, float)):
            return primary
    return None


def codex_credit_balance(rate_limits: Any) -> dict[str, Any] | None:
    if not isinstance(rate_limits, dict):
        return None
    credits = rate_limits.get("credits")
    if not isinstance(credits, dict):
        return None
    # Select the first key that is actually PRESENT (not the first truthy one):
    # a real balance of 0 (exhausted credits) is falsy, so an `or` chain would skip
    # it and report a different field's value. Mirrors antigravity_credit_state's
    # is-not-None handling.
    amount = None
    for key in ("balance", "remaining", "available", "amount"):
        candidate = numeric_value(credits.get(key))
        if candidate is not None:
            amount = candidate
            break
    if amount is None:
        return None
    return {
        "label": "Credits",
        "amount": amount,
        "currency": "credits",
        "source": "codex-rate-limit-credits",
        "detail": f"Credits: {compact_token_count(amount)} available",
        "fetchedAt": now_iso(),
    }


def codex_rate_limit_rows(rate_limits: Any, rate_limits_by_id: Any = None) -> list[dict[str, Any]]:
    if not isinstance(rate_limits, dict):
        return []
    rows: list[dict[str, Any]] = []
    seen_labels: set[str] = set()
    for key, label in (
        ("primary", "Session"),
        ("secondary", "Weekly"),
        ("spark", "Spark"),
        ("tertiary", "Spark"),
        ("credits", "Credits"),
    ):
        window = rate_limits.get(key)
        # Window-derived label for the two positional lanes; Spark/Credits keep theirs.
        if key in ("primary", "secondary"):
            label = _codex_window_label(label, window)
        if label in seen_labels:
            continue
        row = codex_limit_from_window(key, label, window)
        if row is None:
            continue
        seen_labels.add(label)
        rows.append(row)
    if "Spark" not in seen_labels:
        row = codex_limit_from_window("spark", "Spark", codex_spark_window(rate_limits_by_id))
        if row is not None:
            rows.insert(2 if len(rows) >= 2 else len(rows), row)
    return rows


# ---------------------------------------------------------------------------
# Antigravity plan / tier / credit helpers
# ---------------------------------------------------------------------------

def antigravity_plan_status(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    user_status = as_dict(data.get("userStatus"), data)
    if not isinstance(user_status, dict):
        return {}
    plan_status = user_status.get("planStatus")
    return plan_status if isinstance(plan_status, dict) else {}


def antigravity_user_tier(data: Any) -> str:
    """The user's Google AI *subscription* tier, e.g. "Google AI Ultra".

    Lives at userStatus.userTier (id "g1-ultra-tier"/"g1-pro-tier"/"free-tier",
    name "Google AI Ultra"/...). This is the plan the user actually pays for and
    is the source of truth across users. It must NOT be confused with
    planStatus.planInfo (the Code Assist *feature* tier, e.g. "Pro"/TEAMS_TIER_PRO),
    which can read "Pro" even for a Google AI Ultra subscriber.
    """
    if not isinstance(data, dict):
        return ""
    user_status = as_dict(data.get("userStatus"), data)
    if not isinstance(user_status, dict):
        return ""
    user_tier = as_dict(user_status.get("userTier"))
    name = str(user_tier.get("name") or "").strip()
    if name:
        return name
    tier_id = str(user_tier.get("id") or "").strip().lower()
    if "ultra" in tier_id:
        return "Google AI Ultra"
    if "pro" in tier_id:
        return "Google AI Pro"
    if "free" in tier_id:
        return "Free"
    return ""


def antigravity_tier(data: Any) -> str:
    # The Google AI subscription tier (userTier) is the source of truth; the
    # Code Assist feature tier in planStatus is only a fallback.
    subscription = antigravity_user_tier(data)
    if subscription:
        return subscription
    plan_status = antigravity_plan_status(data)
    plan_info = as_dict(plan_status.get("planInfo"))
    plan_name = str(plan_info.get("planName") or "").strip()
    if plan_name:
        return plan_name
    teams_tier = str(plan_info.get("teamsTier") or "").strip().lower()
    if teams_tier.endswith("_pro"):
        return "Pro"
    if "ultimate" in teams_tier:
        return "Ultimate"
    if "enterprise" in teams_tier:
        return "Enterprise"
    return ""


def antigravity_credit_state(data: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    plan_status = antigravity_plan_status(data)
    plan_info = as_dict(plan_status.get("planInfo"))
    prompt_available = numeric_value(plan_status.get("availablePromptCredits"))
    flow_available = numeric_value(plan_status.get("availableFlowCredits"))
    prompt_limit = numeric_value(plan_info.get("monthlyPromptCredits"))
    flow_limit = numeric_value(plan_info.get("monthlyFlowCredits"))
    all_available = [v for v in (prompt_available, flow_available) if v is not None]
    if not all_available:
        return None, None

    # Include a pool (available AND limit) only when BOTH sides are known; this
    # prevents an orphan available (missing/zero limit) from deflating usage by
    # netting against the other pool's limit (e.g. prompt_avail=500 with
    # flow_limit=None would undercount prompt usage).
    _pools = [
        (av, lim)
        for av, lim in ((prompt_available, prompt_limit), (flow_available, flow_limit))
        if av is not None and lim is not None and lim > 0
    ]
    # Balance display shows all available credits regardless of whether the limit is known.
    available_total = sum(all_available)
    limit_total = sum(lim for _, lim in _pools) if _pools else 0
    parts: list[str] = []
    if prompt_available is not None:
        parts.append(f"{compact_token_count(prompt_available)} prompt")
    if flow_available is not None:
        parts.append(f"{compact_token_count(flow_available)} flow")
    detail = f"Credits: {' / '.join(parts)} available"
    balance = {
        "label": "Credits",
        "amount": available_total,
        "currency": "credits",
        "source": "antigravity-plan-status",
        "detail": detail,
        "fetchedAt": now_iso(),
    }
    if limit_total <= 0:
        return balance, None

    paired_available_total = sum(av for av, _ in _pools)
    used_total = max(0.0, limit_total - paired_available_total)
    limit = {
        "label": "Credits",
        "percent": max(0.0, min(100.0, used_total / limit_total * 100.0)),
        "reset": "Monthly credit pool",
        "unit": "credits",
        "used": used_total,
        "limit": limit_total,
    }
    return balance, limit


# ---------------------------------------------------------------------------
# Default / empty provider shapes
# ---------------------------------------------------------------------------

def default_provider(label: str, source: str) -> dict[str, Any]:
    return {
        "label": label,
        "status": "idle",
        "source": source,
        "message": "No usage data",
        "limits": [],
    }


def missing_cookie_provider(label: str, source: str = "browser") -> dict[str, Any]:
    return {
        "label": label,
        "status": "missing-cookies",
        "source": source,
        "message": "No matching browser cookies",
        "limits": [],
    }


def _is_weekly_metric(limit: dict[str, Any]) -> bool:
    return str(limit.get("label") or "").strip().lower() == "weekly"


def _is_session_metric(limit: dict[str, Any]) -> bool:
    return str(limit.get("label") or "").strip().lower() == "session"


def _window_minutes(limit: dict[str, Any]) -> float:
    mins = limit.get("windowMinutes")
    if isinstance(mins, (int, float)) and not isinstance(mins, bool) and mins > 0:
        return float(mins)
    return 0.0


def _is_session_window(limit: dict[str, Any]) -> bool:
    """Session-length lane, by ACTUAL window length or by label."""
    mins = _window_minutes(limit)
    return _is_session_metric(limit) or (0 < mins <= 360)


def _is_weekly_window(limit: dict[str, Any]) -> bool:
    """Weekly-length lane, by ACTUAL window length or by label.

    Anthropic returns per-model caps as their own lanes keyed by model name
    (``seven_day_fable`` -> label "Fable"), and parse_claude_usage discovers them
    from the payload rather than hardcoding names. Those lanes carry
    ``windowMinutes: 10080`` — they ARE weekly windows — but the label-only
    predicate matched the literal string "weekly", so a per-model lane was
    neither session nor weekly and fell through every display branch: it skipped
    the polished ``_metric_pace_line`` AND escaped the left/right blanking,
    leaking the internal phrasing ("3% in deficit") straight to the UI. Worse,
    "in deficit" means ahead of pace, which the weekly lane renders as
    "Ahead (+4%)" — the same state described by opposite-sounding words.

    Verified against a live subscriber account with per-model caps: lanes
    Session(300), Weekly(10080), <model>(10080), and a money lane with no window.

    Upper bound 20160 keeps Codex's 43200-minute plan window out, matching
    _codex_window_label's bands. Label match is kept so lanes with no reported
    window (Grok's, notably) behave exactly as before.
    """
    mins = _window_minutes(limit)
    return _is_weekly_metric(limit) or (1440 < mins <= 20160)


def _raw_detail_left(limit: dict[str, Any]) -> str:
    return str(limit.get("detailLeftText") or limit.get("detailLeft") or limit.get("paceText") or limit.get("pace") or "").strip()


def _raw_detail_right(limit: dict[str, Any]) -> str:
    return str(limit.get("detailRightText") or limit.get("detailRight") or "").strip()


def _is_pace_generated_detail(limit: dict[str, Any]) -> bool:
    window = limit.get("windowMinutes")
    if isinstance(window, (int, float)) and window > 0:
        return len(_raw_detail_left(limit)) > 0
    return False


def _metric_pace_line(limit: dict[str, Any]) -> str:
    if not _is_weekly_window(limit) or not _is_pace_generated_detail(limit):
        return ""
    expected = limit.get("pacePercent")
    actual = limit.get("percent")
    left = _raw_detail_left(limit)
    if isinstance(expected, (int, float)):
        delta = round(float(actual or 0) - float(expected))
        if abs(delta) <= 2:
            left = "On pace"
        elif delta < 0:
            left = f"Behind ({delta}%)"
        else:
            left = f"Ahead (+{delta}%)"
    right = _raw_detail_right(limit).replace("until reset", "to reset")
    if right:
        return f"Pace: {left} · {right}"
    return f"Pace: {left}"


def _currency_symbol(unit: str) -> str:
    n = str(unit or "").upper()
    if n in ("USD", "COST"):
        return "$"
    if n == "EUR": return "EUR"
    if n == "GBP": return "GBP"
    return n if n else "$"


def _money_text(value: float, unit: str) -> str:
    symbol = _currency_symbol(unit)
    if symbol == "$":
        return f"$ {value:.2f}"
    return f"{symbol} {value:.2f}"


def _extra_usage_detail(limit: dict[str, Any], provider_key: str, credit_balance: dict[str, Any] | None) -> str:
    # Bind once: the isinstance guard and the comparison must see the SAME value
    # (mypy narrows a name, not two separate .get() calls). `credit_balance` stays
    # in the condition so the body keeps its non-None narrowing.
    balance_amount = credit_balance.get("amount") if credit_balance else None
    if credit_balance and isinstance(balance_amount, (int, float)) and balance_amount > 0:
        detail = str(credit_balance.get("detail") or "").strip()
        if detail: return detail
        currency = str(credit_balance.get("currency") or "").strip().lower()
        label = str(credit_balance.get("label") or "Usage credits").strip()
        amt = float(credit_balance.get("amount") or 0)
        if currency in ("credits", "credit"):
            return f"{label}: {compact_token_count(amt)} available"
        money = _money_text(amt, str(credit_balance.get("currency") or "USD"))
        if provider_key == "claude":
            return f"Credits: {money} available"
        return f"{label}: {money}"
        
    if not limit: return ""
    unit = str(limit.get("currency") or limit.get("unit") or "USD").upper()
    used = limit.get("used")
    cap = limit.get("limit")
    if isinstance(used, (int, float)) and isinstance(cap, (int, float)) and cap > 0:
        if unit in ("CREDITS", "CREDIT"):
            return f"This month: {used:.0f} / {cap:.0f} credits"
        return f"This month: {_money_text(float(used), unit)} / {_money_text(float(cap), unit)}"
    
    reset = str(limit.get("reset") or "").strip()
    if "$" in reset:
        return "This month: " + reset.replace(" of ", " / ")
    if reset: return reset
    pct = max(0.0, min(100.0, float(limit.get("percent") or 0)))
    return f"{pct:.0f}% used"


def _attach_grok_billing_week_detail(p_data: dict[str, Any]) -> None:
    """Attach a billing-week detail line to Grok's first Weekly limit when cost data is present.

    Sets ``limit["detailText"]`` so the existing per-limit formatting loop picks it up as
    ``formattedDetailText``.  No-ops when costSummary is absent, billingWeekTokens is zero/absent,
    or the limit already carries a detailText/detail.
    """
    cs = p_data.get("costSummary")
    if not isinstance(cs, dict):
        return
    tokens = cs.get("billingWeekTokens")
    if not isinstance(tokens, (int, float)) or tokens <= 0:
        return
    limits = p_data.get("limits") or []
    for limit in limits:
        if not _is_weekly_metric(limit):
            continue
        if limit.get("detailText") or limit.get("detail"):
            break  # already populated — leave it
        parts = [f"This week: {compact_token_count(int(tokens))} tokens"]
        cost = cs.get("billingWeekCost")
        if isinstance(cost, (int, float)) and cost > 0:
            parts.append(_money_text(float(cost), "USD"))
        limit["detailText"] = " · ".join(parts)
        break  # only the first Weekly limit


def enrich_ui_formatting(providers: dict[str, Any]) -> None:
    for p_key, p_data in providers.items():
        if not isinstance(p_data, dict):
            continue

        # Grok: inject billing-week detail before the per-limit loop so formattedDetailText picks it up.
        if p_key == "grok":
            _attach_grok_billing_week_detail(p_data)

        # Determine extra usage limit
        limits = p_data.get("limits") or []
        extra_limit = None
        for limit in limits:
            unit = str(limit.get("unit") or "").lower()
            label = str(limit.get("label") or "").lower()
            if unit in ("usd", "cost", "currency") or "on-demand" in label or "extra" in label or "monthly" in label or "credits" in label:
                limit["isExtraUsage"] = True
                extra_limit = limit
            else:
                limit["isExtraUsage"] = False

        if "creditBalance" in p_data or extra_limit:
            p_data["formattedExtraUsageDetail"] = _extra_usage_detail(extra_limit or {}, p_key, p_data.get("creditBalance"))

        for limit in limits:
            pct = max(0.0, min(100.0, float(limit.get("percent") or 0)))
            if pct < 10 and pct != round(pct):
                limit["formattedUsedText"] = f"{pct:.1f}% used"
            else:
                limit["formattedUsedText"] = f"{pct:.0f}% used"
                
            pace = _metric_pace_line(limit)
            if pace:
                limit["formattedDetailText"] = pace
            else:
                limit["formattedDetailText"] = str(limit.get("detailText") or limit.get("detail") or "").strip()

            is_session_or_weekly = _is_session_window(limit) or _is_weekly_window(limit)
            if is_session_or_weekly and _is_pace_generated_detail(limit):
                limit["formattedDetailLeft"] = ""
                limit["formattedDetailRight"] = ""
            else:
                limit["formattedDetailLeft"] = _raw_detail_left(limit)
                limit["formattedDetailRight"] = _raw_detail_right(limit)
