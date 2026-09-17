"""Token and cost bucket aggregation (weekly, monthly, hourly)."""
from __future__ import annotations

import datetime as dt
from typing import Any

from .formatting import compact_token_count, exact_usd
from .pricing import cost_breakdown_line


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
    """Effective number of days in the trailing 7-day burn window."""
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
    week_tokens = sum(int(d.get("tokens", 0)) for d in history)
    week_cost = sum(float(d.get("cost", 0.0)) for d in history)
    burn_rate = week_cost / trailing_week_days()
    model_rows = model_breakdown_rows(model_costs)

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
