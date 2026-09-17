"""Rate-limit, quota-lane, and plan/tier formatting helpers."""
from __future__ import annotations

import datetime as dt
from typing import Any

from parsers import as_dict, relative_reset
from .formatting import compact_token_count, now_iso, numeric_value, _money_text


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


def antigravity_plan_status(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    user_status = as_dict(data.get("userStatus"), data)
    if not isinstance(user_status, dict):
        return {}
    plan_status = user_status.get("planStatus")
    return plan_status if isinstance(plan_status, dict) else {}


def antigravity_user_tier(data: Any) -> str:
    """The user's Google AI *subscription* tier, e.g. "Google AI Ultra"."""
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

    _pools = [
        (av, lim)
        for av, lim in ((prompt_available, prompt_limit), (flow_available, flow_limit))
        if av is not None and lim is not None and lim > 0
    ]
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
    mins = _window_minutes(limit)
    return _is_session_metric(limit) or (0 < mins <= 360)


def _is_weekly_window(limit: dict[str, Any]) -> bool:
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


def _extra_usage_detail(limit: dict[str, Any], provider_key: str, credit_balance: dict[str, Any] | None) -> str:
    balance_amount = credit_balance.get("amount") if credit_balance else None
    if credit_balance and isinstance(balance_amount, (int, float)) and balance_amount > 0:
        detail = str(credit_balance.get("detail") or "").strip()
        if detail:
            return detail
        currency = str(credit_balance.get("currency") or "").strip().lower()
        label = str(credit_balance.get("label") or "Usage credits").strip()
        amt = float(credit_balance.get("amount") or 0)
        if currency in ("credits", "credit"):
            return f"{label}: {compact_token_count(amt)} available"
        money = _money_text(amt, str(credit_balance.get("currency") or "USD"))
        if provider_key == "claude":
            return f"Credits: {money} available"
        return f"{label}: {money}"

    if not limit:
        return ""
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
    if reset:
        return reset
    pct = max(0.0, min(100.0, float(limit.get("percent") or 0)))
    return f"{pct:.0f}% used"


def _attach_grok_billing_week_detail(p_data: dict[str, Any]) -> None:
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
            break
        parts = [f"This week: {compact_token_count(int(tokens))} tokens"]
        cost = cs.get("billingWeekCost")
        if isinstance(cost, (int, float)) and cost > 0:
            parts.append(_money_text(float(cost), "USD"))
        limit["detailText"] = " · ".join(parts)
        break


def enrich_ui_formatting(providers: dict[str, Any]) -> None:
    for p_key, p_data in providers.items():
        if not isinstance(p_data, dict):
            continue

        if p_key == "grok":
            _attach_grok_billing_week_detail(p_data)

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
