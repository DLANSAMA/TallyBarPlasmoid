"""Pure parsing helpers for provider telemetry.

These functions take already-fetched payloads and return the normalized
``{label, percent, reset, unit}`` shape consumed by the QML UI. Keeping them
isolated from the I/O layer in backend.py lets them be unit-tested without
hitting cookie stores, subprocesses, or the network.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
from typing import Any


def as_dict(value: Any, default: Any = None) -> dict[str, Any]:
    """Narrow an arbitrary payload value to a dict.

    Returns ``value`` when it is a dict, else ``default`` when THAT is a dict,
    else ``{}``. Replaces the repeated
    ``d.get("k") if isinstance(d.get("k"), dict) else {}`` idiom, which called
    ``.get`` twice and which mypy cannot narrow (the isinstance guard is on a
    *different* expression than the value), producing the bulk of this tree's
    type errors. Behaviour is identical; the type is not.
    """
    if isinstance(value, dict):
        return value
    if isinstance(default, dict):
        return default
    return {}


def relative_reset(value: str) -> str:
    if not value:
        return ""
    try:
        reset = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return value
    now = dt.datetime.now(dt.timezone.utc)
    if reset.tzinfo is None:
        reset = reset.replace(tzinfo=dt.timezone.utc)
    seconds = int((reset - now).total_seconds())
    if seconds <= 0:
        return "Reset due"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"Resets in {days}d {hours}h"
    if hours:
        return f"Resets in {hours}h {minutes}m"
    return f"Resets in {minutes}m"


def first_number(data: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = data.get(key)
        # Reject bool explicitly: isinstance(True, int) is True, so without this a
        # boolean flag colliding with a numeric key would be silently read as 1.0/0.0
        # (mirrors the guard already in parse_google_one_credits).
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def parse_claude_credit_balance(data: Any) -> dict[str, Any] | None:
    """Parse Claude's prepaid credits payload.

    ``/api/organizations/{org_id}/prepaid/credits`` returns currency amounts in
    cents, including the top-level ``amount`` used by the Console billing page.
    """
    if not isinstance(data, dict):
        return None
    amount_cents = first_number(data, ("amount", "remaining_amount_cents", "remainingAmountCents"))
    if amount_cents is None:
        return None
    currency = str(data.get("currency") or data.get("currency_code") or data.get("currencyCode") or "USD").upper()
    return {
        "label": "Usage credits",
        "amount": amount_cents / 100.0,
        "currency": currency,
        "source": "claude-prepaid-credits",
    }


def claude_org_ids(organizations: Any) -> list[str]:
    if isinstance(organizations, dict):
        candidates = organizations.get("organizations") or organizations.get("data") or organizations.get("items") or []
    else:
        candidates = organizations
    ids: list[str] = []
    if isinstance(candidates, list):
        for item in candidates:
            if not isinstance(item, dict):
                continue
            value = item.get("uuid") or item.get("id") or item.get("organization_uuid")
            if value and str(value) not in ids:
                ids.append(str(value))
    return ids


# Most-specific-first: "enterprise-pro" is Enterprise, "max_plus" is Max, etc.
_TIER_KEYWORDS = (("enterprise", "Enterprise"), ("team", "Team"), ("ultra", "Ultra"),
                  ("max", "Max"), ("plus", "Plus"), ("pro", "Pro"), ("free", "Free"))


def normalize_tier(value: Any) -> str:
    """Canonical display name for a plan/tier string, or "" when no keyword matches.
    The ONE keyword ladder shared by the Claude (parse_claude_tier) and Codex
    (run_codex_rpc planType) tier parsers, so the two can't drift. NOT for
    Antigravity's userTier ids — those map to Google-branded names
    ("Google AI Ultra") in accounting.antigravity_user_tier."""
    if not isinstance(value, str):
        return ""
    normalized = value.strip().lower().replace("-", "_")
    for keyword, label in _TIER_KEYWORDS:
        if keyword in normalized:
            return label
    return ""


def parse_claude_tier(organizations: Any) -> str:
    if isinstance(organizations, dict):
        candidates = organizations.get("organizations") or organizations.get("data") or organizations.get("items") or []
    else:
        candidates = organizations
    if not isinstance(candidates, list):
        return ""

    for item in candidates:
        if not isinstance(item, dict):
            continue
        for key in ("rate_limit_tier", "rateLimitTier", "tier", "plan", "subscription", "subscription_tier"):
            tier = normalize_tier(item.get(key))
            if tier:
                return tier
    return ""


def google_timestamp_iso(value: Any) -> str:
    if not isinstance(value, list) or not value:
        return ""
    if isinstance(value[0], list):
        value = value[0]
    if not value:
        return ""
    try:
        seconds = float(value[0])
        nanos = float(value[1]) if len(value) > 1 and isinstance(value[1], (int, float)) else 0.0
        reset = dt.datetime.fromtimestamp(seconds + nanos / 1_000_000_000.0, tz=dt.timezone.utc)
    except Exception:
        return ""
    return reset.isoformat()


def normalize_reset_iso(value: Any) -> str:
    if not value:
        return ""
    try:
        reset = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return ""
    if reset.tzinfo is None:
        reset = reset.replace(tzinfo=dt.timezone.utc)
    return reset.astimezone(dt.timezone.utc).isoformat()


def short_duration(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    if total_seconds < 60:
        return "now"
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def usage_pace_detail(percent: float, reset_iso: str, window_minutes: int) -> dict[str, Any]:
    if not reset_iso or window_minutes <= 0:
        return {}
    try:
        reset = dt.datetime.fromisoformat(reset_iso.replace("Z", "+00:00"))
    except Exception:
        return {}
    if reset.tzinfo is None:
        reset = reset.replace(tzinfo=dt.timezone.utc)
    now = dt.datetime.now(dt.timezone.utc)
    duration_seconds = float(window_minutes) * 60.0
    time_until_reset = (reset - now).total_seconds()
    if time_until_reset <= 0 or time_until_reset > duration_seconds:
        return {}

    elapsed = max(0.0, min(duration_seconds, duration_seconds - time_until_reset))
    actual = max(0.0, min(100.0, float(percent)))
    if elapsed == 0 and actual > 0:
        return {}
    expected = max(0.0, min(100.0, elapsed / duration_seconds * 100.0))
    delta = actual - expected
    abs_delta = abs(delta)

    if abs_delta <= 2:
        left = "On pace"
        pace_percent = None
    elif delta >= 0:
        left = f"{int(round(abs_delta))}% in deficit"
        pace_percent = expected
    else:
        left = f"{int(round(abs_delta))}% in reserve"
        pace_percent = expected

    right = ""
    if elapsed > 0 and actual > 0:
        rate = actual / elapsed
        if rate > 0:
            seconds_until_empty = max(0.0, 100.0 - actual) / rate
            if seconds_until_empty >= time_until_reset:
                right = "Lasts to reset"
            else:
                right = f"Runs out in {short_duration(seconds_until_empty)}"
    elif elapsed > 0 and actual == 0:
        right = "Lasts to reset"

    detail: dict[str, Any] = {
        "resetAt": reset_iso,
        "windowMinutes": window_minutes,
        "detailLeftText": left,
        "paceOnTop": actual <= expected,
    }
    if right:
        detail["detailRightText"] = right
    if pace_percent is not None:
        detail["pacePercent"] = max(0.0, min(100.0, pace_percent))
    return detail


def add_pace_detail(limit: dict[str, Any], reset_iso: str, window_minutes: int) -> dict[str, Any]:
    limit.update(usage_pace_detail(float(limit.get("percent") or 0.0), reset_iso, window_minutes))
    return limit


def parse_batchexecute_payload(text: str, rpc_id: str) -> Any:
    stripped = text.lstrip(")]}'\n ")
    for line in stripped.splitlines():
        line = line.strip()
        if not line.startswith("[["):
            continue
        try:
            rows = json.loads(line)
        except Exception:
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not (isinstance(row, list) and len(row) >= 3):
                continue
            if row[0] == "wrb.fr" and row[1] == rpc_id and isinstance(row[2], str):
                try:
                    return json.loads(row[2])
                except json.JSONDecodeError:
                    continue
    return None


def parse_agy_usage(text: str) -> dict[str, dict[str, Any]]:
    """Parse the agy ``/usage`` "Model Quota" panel into per-model remaining quota.

    The panel renders one block per model::

          Claude Sonnet 4.6 (Thinking)
          ███████████ ░░░░░░░░░░░ ░░░░░░░░░░░ ░░░░░░░░░░░ ░░░░░░░░░░░ 20%
          20% remaining · Refreshes in 3h 0m

    where the bar/percent is the quota *remaining* ("Quota available" == 100%).
    Returns ``{model_label: {"remaining": float, "reset": str}}`` (reset already
    phrased as "Resets in …", empty when the quota is full).
    """
    lines = [ln.rstrip() for ln in (text or "").splitlines()]
    n = len(lines)
    models: dict[str, dict[str, Any]] = {}
    i = 0
    while i < n:
        name = lines[i].strip()
        bar = lines[i + 1] if i + 1 < n else ""
        # A model block is a name line whose next line is the segmented bar.
        if name and ("█" in bar or "░" in bar):
            status = lines[i + 2].strip() if i + 2 < n else ""
            remaining: float | None = None
            reset = ""
            if "Quota available" in status:
                remaining = 100.0
            else:
                m = re.search(r"(\d+(?:\.\d+)?)%\s*remaining", status)
                if m:
                    remaining = float(m.group(1))
                r = re.search(r"Refreshes in\s+(.+?)\s*$", status)
                if r:
                    reset = "Resets in " + r.group(1).strip()
            if remaining is None:
                bm = re.search(r"(\d+(?:\.\d+)?)%", bar)
                if bm:
                    remaining = float(bm.group(1))
            if remaining is not None:
                models[name] = {"remaining": max(0.0, min(100.0, remaining)), "reset": reset}
                i += 3
                continue
        i += 1
    return models


def group_agy_model_quota(models: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Collapse per-model agy quota into the plasmoid's two lanes (Gemini / Others).

    Each lane reports the *tightest* member (lowest remaining → highest used), since
    the underlying quotas reset together. Returns ``{lane: {"percent": used%, "reset": str}}``.
    """
    groups: dict[str, list[dict[str, Any]]] = {"Gemini": [], "Others": []}
    for label, info in models.items():
        low = label.lower()
        if "gemini" in low:
            groups["Gemini"].append(info)
        elif "claude" in low or "gpt" in low:
            groups["Others"].append(info)
    out: dict[str, dict[str, Any]] = {}
    for lane, members in groups.items():
        if not members:
            continue
        tightest = min(members, key=lambda m: m["remaining"])
        out[lane] = {
            "percent": max(0.0, min(100.0, 100.0 - tightest["remaining"])),
            "reset": tightest["reset"],
        }
    return out


def parse_google_one_credits(data: Any) -> dict[str, Any] | None:
    """Parse the Google One AI credit balance from the DrWK4 RPC response.

    Expected inner payload structure:
        [[[null,[],[]],[null,[]],null,[[credits,[sec,nanos]]],[sec,nanos]],null,true,true]

    Field locations:
        credits     -> inner[3][0][0]
        expiration  -> inner[3][0][1]  ([seconds, nanos]), with inner[4] as a fallback
    """
    try:
        if not isinstance(data, list) or not data:
            return None
        inner = data[0] if isinstance(data[0], list) else data
        if not isinstance(inner, list) or len(inner) < 4:
            return None
        credit_pool = inner[3]
        if not isinstance(credit_pool, list) or not credit_pool:
            return None
        first_pool = credit_pool[0]
        if not isinstance(first_pool, list) or not first_pool:
            return None
        credits = first_pool[0]
        if not isinstance(credits, (int, float)) or isinstance(credits, bool):
            return None
        if not math.isfinite(credits):
            return None
        expiration_ts = first_pool[1] if len(first_pool) > 1 else None
        if not expiration_ts and len(inner) > 4:
            expiration_ts = inner[4]
        expiration_iso = google_timestamp_iso(expiration_ts) if expiration_ts else ""
        return {
            "credits": int(credits),
            "expiration": expiration_iso,
        }
    except (IndexError, TypeError):
        return None


def parse_gemini_usage_info(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, list) or len(data) < 2 or not isinstance(data[1], list):
        return []
    labels = {
        1: "Session",
        2: "Weekly",
    }
    windows: list[tuple[int, dict[str, Any]]] = []
    for entry in data[1]:
        if not isinstance(entry, list) or len(entry) < 4:
            continue
        window = entry[2]
        # window is the numeric window code (1=Session, 2=Weekly). Guard it before
        # labels.get(): an unhashable value (list/dict) from a malformed payload would
        # raise TypeError and drop the whole Gemini provider row to a parse error.
        if not isinstance(window, (int, float)) or isinstance(window, bool):
            continue
        label = labels.get(int(window))
        if not label:
            continue
        usage_fraction = entry[1]
        if not isinstance(usage_fraction, (int, float)):
            continue
        reset_iso = google_timestamp_iso(entry[3])
        percent = max(0.0, min(100.0, float(usage_fraction) * 100.0))
        limit = {
            "label": label,
            "percent": percent,
            "reset": relative_reset(reset_iso),
            "unit": "%",
        }
        if reset_iso:
            add_pace_detail(limit, reset_iso, 300 if window == 1 else 10080)
        windows.append(
            (
                int(window),
                limit,
            )
        )
    return [limit for _, limit in sorted(windows, key=lambda item: item[0])]


def parse_claude_usage(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    limits: list[dict[str, Any]] = []

    five_hour = data.get("five_hour") or data.get("fiveHour") or {}
    if isinstance(five_hour, dict):
        remaining = first_number(five_hour, ("remaining_fraction", "remainingFraction"))
        utilization = first_number(five_hour, ("utilization",))
        consumed = first_number(five_hour, ("consumed", "used"))
        limit = first_number(five_hour, ("limit", "total"))
        reset = str(five_hour.get("resets_at") or five_hour.get("reset_at") or five_hour.get("resetAt") or data.get("sessionResetsAt") or "")
        reset_iso = normalize_reset_iso(reset)
        if remaining is not None:
            limits.append(add_pace_detail(
                {
                    "label": "Session",
                    "percent": max(0.0, min(100.0, (1.0 - remaining) * 100.0)),
                    "reset": relative_reset(reset),
                    "unit": "%",
                },
                reset_iso,
                300,
            ))
        elif utilization is not None:
            limits.append(add_pace_detail(
                {
                    "label": "Session",
                    "percent": max(0.0, min(100.0, utilization)),
                    "reset": relative_reset(reset),
                    "unit": "%",
                },
                reset_iso,
                300,
            ))
        elif consumed is not None and limit and limit > 0:
            limits.append(add_pace_detail(
                {
                    "label": "Session",
                    "percent": max(0.0, min(100.0, consumed / limit * 100.0)),
                    "reset": relative_reset(reset),
                    "unit": "%",
                },
                reset_iso,
                300,
            ))

    seven_day = data.get("seven_day") or data.get("sevenDay") or {}
    if isinstance(seven_day, dict):
        utilization = first_number(seven_day, ("utilization",))
        consumed = first_number(seven_day, ("consumed", "used"))
        limit = first_number(seven_day, ("limit", "total"))
        reset = str(seven_day.get("resets_at") or seven_day.get("reset_at") or seven_day.get("resetAt") or "")
        reset_iso = normalize_reset_iso(reset)
        if utilization is not None:
            limits.append(add_pace_detail(
                {
                    "label": "Weekly",
                    "percent": max(0.0, min(100.0, utilization)),
                    "reset": relative_reset(reset),
                    "unit": "%",
                },
                reset_iso,
                10080,
            ))
        elif consumed is not None and limit and limit > 0:
            limits.append(add_pace_detail(
                {
                    "label": "Weekly",
                    "percent": max(0.0, min(100.0, consumed / limit * 100.0)),
                    "reset": relative_reset(reset),
                    "unit": "%",
                },
                reset_iso,
                10080,
            ))

    # Per-model 7-day windows: Anthropic keys these "seven_day_<model>" (snake_case)
    # or "sevenDay<Model>" (camelCase) — seven_day_sonnet/seven_day_opus are the
    # known ones, but Anthropic can add a dedicated pool for any new model (e.g. a
    # newly-released one like Fable) at any time. Discover them from the payload
    # itself instead of hardcoding each model name, so a new pool shows up
    # automatically without a code change. A payload uses one casing convention
    # consistently; the "seen" dedup below only guards against something never
    # observed in practice (both stylings present at once for the same model).
    seen_model_groups: set[str] = set()
    model_windows: list[tuple[str, dict[str, Any]]] = []
    for key, value in data.items():
        if not isinstance(value, dict) or not value:
            continue
        if key.startswith("seven_day_") and len(key) > len("seven_day_"):
            suffix = key[len("seven_day_"):]
        elif key.startswith("sevenDay") and len(key) > len("sevenDay") and key[len("sevenDay")].isupper():
            suffix = re.sub(r"(?<!^)(?=[A-Z])", "_", key[len("sevenDay"):]).lower()
        else:
            continue
        group = suffix.lower()
        if group in seen_model_groups:
            continue  # snake_case/camelCase duplicate of an already-captured model
        seen_model_groups.add(group)
        model_windows.append((group.replace("_", " ").title(), value))

    # A match for one model's window must not short-circuit another's, since a
    # payload can carry several simultaneously (independent per-model caps).
    for model_label, model_window in model_windows:
        utilization = first_number(model_window, ("utilization",))
        consumed = first_number(model_window, ("consumed", "used"))
        limit = first_number(model_window, ("limit", "total"))
        reset = str(model_window.get("resets_at") or model_window.get("reset_at") or model_window.get("resetAt") or "")
        reset_iso = normalize_reset_iso(reset)
        if utilization is not None:
            limits.append(add_pace_detail(
                {
                    "label": model_label,
                    "percent": max(0.0, min(100.0, utilization)),
                    "reset": relative_reset(reset),
                    "unit": "%",
                },
                reset_iso,
                10080,
            ))
        elif consumed is not None and limit and limit > 0:
            limits.append(add_pace_detail(
                {
                    "label": model_label,
                    "percent": max(0.0, min(100.0, consumed / limit * 100.0)),
                    "reset": relative_reset(reset),
                    "unit": "%",
                },
                reset_iso,
                10080,
            ))

    # Newer payloads carry a "limits" ARRAY of window objects (kind/group, percent,
    # resets_at, scope) alongside — and eventually instead of — the flat five_hour /
    # seven_day / seven_day_<model> keys. A model-scoped weekly cap can ship ONLY
    # here: the live Fable window is {"kind": "weekly_scoped", "scope": {"model":
    # {"display_name": "Fable"}}} with every seven_day_<model> flat key null
    # (verified live 2026-07-04). "is_active" is NOT an existence gate — the
    # plainly-live session window arrives with is_active: false — so entries are
    # emitted regardless of it. Rows already produced from the flat keys win;
    # array entries only fill in what's missing (label-level dedup).
    array_limits = data.get("limits")
    if isinstance(array_limits, list):
        emitted = {str(row.get("label", "")).strip().lower() for row in limits}
        for entry in array_limits:
            if not isinstance(entry, dict):
                continue
            percent = first_number(entry, ("percent", "utilization"))
            if percent is None:
                continue
            kind = str(entry.get("kind") or "").lower()
            group = str(entry.get("group") or "").lower()
            label = ""
            scope = entry.get("scope")
            if isinstance(scope, dict):
                model_scope = scope.get("model")
                if isinstance(model_scope, dict):
                    label = str(model_scope.get("display_name") or model_scope.get("displayName") or "").strip()
                if not label:
                    surface = scope.get("surface")
                    if isinstance(surface, dict):
                        label = str(surface.get("display_name") or surface.get("displayName") or "").strip()
                    elif isinstance(surface, str):
                        label = surface.strip()
            session_like = "session" in kind or group == "session"
            if not label:
                label = "Session" if session_like else "Weekly"
            if label.lower() in emitted:
                continue
            emitted.add(label.lower())
            reset = str(entry.get("resets_at") or entry.get("reset_at") or entry.get("resetAt") or "")
            limits.append(add_pace_detail(
                {
                    "label": label,
                    "percent": max(0.0, min(100.0, float(percent))),
                    "reset": relative_reset(reset),
                    "unit": "%",
                },
                normalize_reset_iso(reset),
                300 if session_like else 10080,
            ))

    extra = data.get("extra_usage") or data.get("extraUsage") or {}
    if isinstance(extra, dict):
        utilization = first_number(extra, ("utilization",))
        # `used_credits`/`monthly_credit_limit` come from Anthropic in CENTS; all
        # other aliases are already in dollars — UNLESS the block carries its own
        # "decimal_places" minor-unit descriptor, which is authoritative for every
        # money field in it. The live /usage extra_usage block sends
        # {"monthly_limit": 2000, "used_credits": 1904.0, "decimal_places": 2}:
        # both values are minor units ($20.00 / $19.04), so alias-based scaling
        # alone would mix units in one row ($19.04 of $2000.00 — the real cap is
        # $20.00, verified live 2026-07-04).
        _CENTS_CONSUMED = ("used_credits", "usedCredits")
        _CENTS_LIMIT = ("monthly_credit_limit", "monthlyCreditLimit")
        _dp = extra.get("decimal_places")
        if not isinstance(_dp, (int, float)) or isinstance(_dp, bool):
            _dp = extra.get("decimalPlaces")
        _dp_scale = None
        if isinstance(_dp, (int, float)) and not isinstance(_dp, bool) and 0 <= int(_dp) <= 6:
            _dp_scale = 10.0 ** -int(_dp)
        consumed_raw = None
        consumed_is_cents = False
        for _k in (*_CENTS_CONSUMED, "monthly_consumed", "monthlyConsumed", "consumed", "cost"):
            _v = extra.get(_k)
            if isinstance(_v, (int, float)):
                consumed_raw = float(_v)
                consumed_is_cents = _k in _CENTS_CONSUMED
                break
        limit_raw = None
        limit_is_cents = False
        for _k in (*_CENTS_LIMIT, "credit_limit", "creditLimit", "monthly_limit", "monthlyLimit", "limit"):
            _v = extra.get(_k)
            if isinstance(_v, (int, float)):
                limit_raw = float(_v)
                limit_is_cents = _k in _CENTS_LIMIT
                break

        # Pair-consistency: when decimal_places is absent the minor-unit denomination
        # is a property of the payload's currency representation, not per-field.  A
        # server that writes used_credits in cents writes its limit in cents too.  We
        # therefore key the scaling decision off whether EITHER side matched a
        # *_credits / *credit_limit* alias and apply it to BOTH.  No live capture of a
        # dp-less mixed pair (cents consumed + dollar limit) exists; this is hardening
        # toward the code's stated intent at lines 596-599.
        _pair_is_cents = consumed_is_cents or limit_is_cents

        def _scale_money(raw: float | None) -> float | None:
            if raw is None:
                return None
            if _dp_scale is not None:
                return raw * _dp_scale
            return raw / 100.0 if _pair_is_cents else raw

        consumed = _scale_money(consumed_raw)
        limit = _scale_money(limit_raw)
        currency = str(extra.get("currency") or extra.get("currency_code") or extra.get("currencyCode") or "USD")
        if utilization is not None:
            monthly = {
                "label": "Monthly",
                "percent": max(0.0, min(100.0, utilization)),
                "reset": "Enterprise spend limit" if extra.get("is_enabled") else "",
                "unit": currency,
                "currency": currency,
            }
            if consumed is not None:
                monthly["used"] = float(consumed)
            if limit is not None:
                monthly["limit"] = float(limit)
            limits.append(monthly)
        elif consumed is not None and limit and limit > 0:
            limits.append(
                {
                    "label": "Monthly",
                    "percent": max(0.0, min(100.0, consumed / limit * 100.0)),
                    "reset": "Enterprise spend limit",
                    "unit": currency,
                    "used": float(consumed),
                    "limit": float(limit),
                    "currency": currency,
                }
            )

    return limits or extract_limits_from_json(data)


def antigravity_model_entries(data: Any) -> list[tuple[str, str, float, str]]:
    if not isinstance(data, dict):
        return []

    configs: list[Any] = []
    user_status = data.get("userStatus", {})
    if isinstance(user_status, dict):
        cascade = user_status.get("cascadeModelConfigData", {})
        if isinstance(cascade, dict):
            configs.extend(cascade.get("clientModelConfigs") or [])
    configs.extend(data.get("clientModelConfigs") or [])

    found: list[tuple[str, str, float, str]] = []
    for item in configs:
        if not isinstance(item, dict):
            continue
        quota = item.get("quotaInfo") or {}
        if not isinstance(quota, dict) or "remainingFraction" not in quota:
            continue
        try:
            remaining = float(quota["remainingFraction"])
        except Exception:
            continue
        label = str(item.get("label") or item.get("displayName") or item.get("name") or item.get("modelId") or "")
        model_id = str(item.get("modelId") or item.get("id") or "")
        found.append((label, model_id, remaining, str(quota.get("resetTime") or "")))

    models = data.get("models")
    if isinstance(models, dict):
        for model_id, model in models.items():
            if not isinstance(model, dict):
                continue
            quota = model.get("quotaInfo") or {}
            if not isinstance(quota, dict) or "remainingFraction" not in quota:
                continue
            try:
                remaining = float(quota["remainingFraction"])
            except Exception:
                continue
            label = str(model.get("displayName") or model.get("label") or model_id)
            found.append((label, str(model_id), remaining, str(quota.get("resetTime") or "")))

    buckets = data.get("buckets")
    if isinstance(buckets, list):
        for bucket in buckets:
            if not isinstance(bucket, dict) or "remainingFraction" not in bucket:
                continue
            try:
                remaining = float(bucket["remainingFraction"])
            except Exception:
                continue
            model_id = str(bucket.get("modelId") or bucket.get("model") or bucket.get("id") or "")
            label = str(bucket.get("displayName") or bucket.get("label") or model_id)
            found.append((label, model_id, remaining, str(bucket.get("resetTime") or "")))
    return found


def parse_antigravity_limits(data: Any) -> list[dict[str, Any]]:
    found = antigravity_model_entries(data)

    def choose(*predicates: Any) -> tuple[str, str, float, str] | None:
        # Predicates are tried in priority order (outer loop), not entries: every
        # entry is checked against predicate[0] before predicate[1] is tried at
        # all. Since later predicates here are boolean supersets of earlier ones
        # (e.g. "has 'low'" vs "no condition on 'low'"), an entries-outer any()
        # would collapse to the loosest predicate and let upstream list order
        # decide which effort tier wins (e.g. Low masking High or vice versa).
        for predicate in predicates:
            for label, model_id, remaining, reset in found:
                lowered = f"{label} {model_id}".lower()
                if predicate(lowered):
                    return label, model_id, remaining, reset
        return None

    gemini_pro = choose(
        lambda value: "gemini" in value and "pro" in value and "low" in value,
        lambda value: "gemini" in value and "pro" in value and "low" not in value,
        lambda value: "gemini" in value and "pro" in value,
    )
    gemini_flash = choose(
        lambda value: "gemini" in value and "flash" in value and "high" in value,
        lambda value: "gemini" in value and "flash" in value,
    )
    claude = choose(
        lambda value: "claude" in value and "sonnet" in value and "thinking" not in value,
        lambda value: "claude" in value,
    )
    gpt_oss = choose(
        lambda value: "gpt" in value or "openai" in value,
    )

    # Antigravity exposes a separate quota per model, but the UI only needs two
    # bars: the Gemini family (Pro + Flash) and everything else (Claude +
    # GPT OSS). Each group reports the usage of its most-consumed member — the
    # tightest constraint — since the underlying quotas reset together.
    def group(
        display: str,
        members: list[tuple[tuple[str, str, float, str] | None, str]],
        show_sublabel: bool = False,
    ) -> dict[str, Any] | None:
        seen: set[str] = set()
        best: tuple[str, str, float, str] | None = None
        best_percent = -1.0
        present_short: list[str] = []
        for member, short in members:
            if not member:
                continue
            key = f"{member[0]}:{member[1]}"
            if key in seen:
                continue
            seen.add(key)
            present_short.append(short)
            percent = max(0.0, min(100.0, (1.0 - member[2]) * 100.0))
            if percent > best_percent:
                best_percent = percent
                best = member
        if best is None:
            return None
        row: dict[str, Any] = {
            "label": display,
            "percent": max(0.0, min(100.0, best_percent)),
            "reset": relative_reset(best[3]),
            "unit": "%",
        }
        # A faint "Claude · GPT"-style subtitle disambiguates the catch-all
        # group; the underlying models that are actually present drive it.
        if show_sublabel and present_short:
            row["sublabel"] = " · ".join(present_short)
        return row

    limits: list[dict[str, Any]] = []
    for entry in (
        group("Gemini", [(gemini_pro, "Pro"), (gemini_flash, "Flash")]),
        group("Others", [(claude, "Claude"), (gpt_oss, "GPT")], show_sublabel=True),
    ):
        if entry:
            limits.append(entry)
    return limits


def parse_antigravity_quota_summary(data: Any) -> list[dict[str, Any]]:
    """Parse ``/v1internal:retrieveUserQuotaSummary`` into per-group, per-window usage lanes.

    This is the EXACT source agy's ``/usage`` shows. The response is::

        {"groups": [{"displayName": "Gemini Models",
                     "buckets": [{"window": "weekly"|"5h", "remainingFraction": 0.91,
                                  "resetTime": "2026-06-28T03:18:59Z", ...}, ...]}, ...]}

    Each group (Gemini, Claude+GPT) carries a 5-hour bucket and a weekly bucket; ``% used =
    (1 - remainingFraction) * 100``. We emit FOUR lanes (both windows per group) ordered
    group-major — each group's 5-hour lane then its weekly lane, kept together — so the expanded
    view reads "Gemini 5h / Gemini weekly / Claude·GPT 5h / Claude·GPT weekly". Lane shape mirrors
    ``parse_antigravity_limits`` (``label``/``percent``/``reset``/``unit``/``sublabel``) so the
    same downstream rendering applies. Returns ``[]`` on any shape it doesn't recognise (caller
    falls back to the legacy quota read)."""
    if not isinstance(data, dict):
        return []
    groups = data.get("groups")
    if not isinstance(groups, list):
        return []
    lanes: list[dict[str, Any]] = []
    for grp in groups:
        if not isinstance(grp, dict):
            continue
        display = str(grp.get("displayName") or "").lower()
        # Map the API group name to TallyBar's short lane label. "Gemini Models" -> "Gemini";
        # "Claude and GPT models" (bucketId prefix "3p") -> "Claude · GPT".
        if "gemini" in display:
            short = "Gemini"
        elif "claude" in display or "gpt" in display or "3p" in display:
            short = "Claude · GPT"
        else:
            short = (grp.get("displayName") or "Models")
        buckets = grp.get("buckets")
        if not isinstance(buckets, list):
            continue
        # Collect this group's two windows, then emit them 5-hour-first so each group's lanes
        # stay adjacent regardless of the order the API lists the buckets in (it returns weekly
        # first). group-major output keeps "the Geminis together and the Claude/GPTs together".
        by_window: dict[str, dict[str, Any]] = {}
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            frac = bucket.get("remainingFraction")
            if not isinstance(frac, (int, float)) or isinstance(frac, bool):
                continue
            window = str(bucket.get("window") or "").lower()
            if window not in ("5h", "weekly"):
                # Fall back to the bucketId suffix ("gemini-5h" / "3p-weekly") if window is absent.
                bid = str(bucket.get("bucketId") or "").lower()
                window = "5h" if bid.endswith("5h") else "weekly" if bid.endswith("weekly") else ""
                if not window:
                    continue
            percent = max(0.0, min(100.0, (1.0 - float(frac)) * 100.0))
            by_window[window] = {
                "label": short,
                "percent": percent,
                "reset": relative_reset(str(bucket.get("resetTime") or "")),
                "unit": "%",
                "sublabel": "5-hour" if window == "5h" else "Weekly",
                "window": window,
            }
        for window in ("5h", "weekly"):
            if window in by_window:
                lanes.append(by_window[window])
    return lanes


def extract_limits_from_json(data: Any, preferred_labels: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    limits: list[dict[str, Any]] = []

    def add(label: str, percent: float, reset: str = "", unit: str = "%") -> None:
        if not (0 <= percent <= 1000):
            return
        percent = max(0.0, min(100.0, percent))
        normalized = label.strip().replace("_", " ").title() or "Usage"
        if any(existing["label"] == normalized for existing in limits):
            return
        limits.append({"label": normalized, "percent": percent, "reset": reset, "unit": unit})

    def walk(node: Any, label: str = "Usage", depth: int = 0) -> None:
        # The depth cap bounds recursion on a pathologically nested response (the
        # limits>=4 guard alone never trips when no limit-shaped nodes exist).
        if len(limits) >= 4 or depth > 12:
            return
        if isinstance(node, dict):
            lower = {str(k).lower(): v for k, v in node.items()}
            reset = str(
                lower.get("resetdescription")
                or lower.get("reset")
                or lower.get("resetsat")
                or lower.get("nextresettime")
                or ""
            )
            for key in ("usedpercent", "usagepercent", "percentage", "percent"):
                if key in lower and isinstance(lower[key], (int, float)):
                    add(label, float(lower[key]), reset)
                    break
            if "remainingfraction" in lower and isinstance(lower["remainingfraction"], (int, float)):
                add(label, (1.0 - float(lower["remainingfraction"])) * 100.0, reset)
            elif "remaining" in lower and "total" in lower:
                try:
                    remaining = float(lower["remaining"])
                    total = float(lower["total"])
                    if total > 0:
                        add(label, (1.0 - remaining / total) * 100.0, reset)
                except Exception:
                    pass
            elif "used" in lower and ("limit" in lower or "total" in lower):
                try:
                    used = float(lower["used"])
                    total = float(lower["limit"] if "limit" in lower else lower["total"])
                    if total > 0:
                        add(label, used / total * 100.0, reset)
                except Exception:
                    pass
            for key, value in node.items():
                child_label = str(key)
                if child_label.lower() in ("data", "result", "usage", "quota", "quotainfo", "ratelimits"):
                    child_label = label
                walk(value, child_label, depth + 1)
        elif isinstance(node, list):
            for value in node:
                walk(value, label, depth + 1)

    for label in preferred_labels:
        if isinstance(data, dict) and label in data:
            walk(data[label], label)
    walk(data)
    return limits[:4]


def parse_grok_billing_config(event: Any) -> dict[str, Any] | None:
    """Parse a Grok Build ``billing: fetched credits config`` unified-log event.

    Real shape (from ``~/.grok/logs/unified.jsonl``)::

        {"msg": "billing: fetched credits config",
         "ctx": {
           "config": {
             "creditUsagePercent": 85.0,
             "currentPeriod": {
               "type": "USAGE_PERIOD_TYPE_WEEKLY",
               "start": "2026-07-04T13:20:44.991585+00:00",
               "end": "2026-07-11T13:20:44.991585+00:00"
             },
             "billingPeriodEnd": "2026-07-11T13:20:44.991585+00:00",
             ...
           },
           "subscriptionTier": "SuperGrok"
         }}

    Returns ``{"limits": [...], "tier": str|None, "period": {...},
    "percentExplicit": bool}`` or ``None`` when the payload has neither a usable
    ``creditUsagePercent`` nor a period window.

    Grok often omits ``creditUsagePercent`` when ``historyLen`` is 0 (fresh
    weekly period, and occasionally mid-period incomplete snapshots).  Missing
    percent with a valid period defaults to **0%** and sets
    ``percentExplicit=False`` so the provider can prefer an older same-period
    snapshot that still carries a real percent (see
    ``providers.grok._latest_billing_event``).
    """
    if not isinstance(event, dict):
        return None
    ctx = event.get("ctx") if isinstance(event.get("ctx"), dict) else event
    if not isinstance(ctx, dict):
        return None
    config = as_dict(ctx.get("config"), ctx)

    percent = first_number(config, (
        "creditUsagePercent", "credit_usage_percent", "usagePercent", "usage_percent",
    ))
    percent_explicit = percent is not None
    if percent is not None:
        percent = max(0.0, min(100.0, float(percent)))

    reset_iso = ""
    period_info: dict[str, Any] = {}
    current_period = config.get("currentPeriod") if isinstance(config.get("currentPeriod"), dict) else None
    if current_period is not None:
        end = current_period.get("end") or current_period.get("endTime")
        start = current_period.get("start") or current_period.get("startTime")
        ptype = current_period.get("type") or current_period.get("periodType")
        if isinstance(end, str) and end:
            reset_iso = end
        if isinstance(start, str) and start:
            period_info["start"] = start
        if isinstance(end, str) and end:
            period_info["end"] = end
        if isinstance(ptype, str) and ptype:
            period_info["type"] = ptype
    if not reset_iso:
        end = config.get("billingPeriodEnd") or config.get("billing_period_end")
        if isinstance(end, str) and end:
            reset_iso = end
            period_info.setdefault("end", end)
    if not period_info.get("start"):
        start = config.get("billingPeriodStart") or config.get("billing_period_start")
        if isinstance(start, str) and start:
            period_info["start"] = start

    has_period = bool(period_info.get("start") or period_info.get("end") or reset_iso)
    if percent is None:
        # Fresh week / incomplete snapshot: treat as 0% only when the period
        # window is present so we still know the reset time.
        if not has_period:
            return None
        percent = 0.0

    # Label: SuperGrok weekly pool is the primary bar users care about.
    # Avoid the words "credits"/"monthly"/"extra" — enrich_ui_formatting marks
    # those as isExtraUsage for Claude prepaid / on-demand lanes. "Weekly" also
    # matches _is_weekly_metric for pace-detail clearing.
    period_type = str(period_info.get("type") or "").upper()
    if "WEEK" in period_type:
        label = "Weekly"
    elif "MONTH" in period_type:
        label = "Plan"  # not "Monthly" — see isExtraUsage heuristic above
    else:
        label = "Usage"

    limit: dict[str, Any] = {
        "label": label,
        "percent": percent,
        "reset": relative_reset(reset_iso) if reset_iso else "",
        "unit": "percent",
    }
    if reset_iso:
        limit["resetAt"] = reset_iso

    tier_raw = ctx.get("subscriptionTier") or ctx.get("subscription_tier") or config.get("subscriptionTier")
    tier = str(tier_raw).strip() if isinstance(tier_raw, str) and tier_raw.strip() else None

    return {
        "limits": [limit],
        "tier": tier,
        "period": period_info or None,
        "percentExplicit": percent_explicit,
    }
