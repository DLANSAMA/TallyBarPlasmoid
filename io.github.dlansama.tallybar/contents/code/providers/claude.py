"""Provider-specific API callers and data acquisition logic.

Each provider (Gemini, Claude, Codex/OpenAI, Antigravity) has its own fetch
path that returns a normalised provider dict suitable for the QML UI.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path
from typing import Any

from accounting import (
    missing_cookie_provider,
    now_iso,
)
from cookies import BrowserCookie, cookiejar_for_domains, host_matches_any
from http_helpers import http_json_async, scrub_credentials
from parsers import (
    claude_org_ids,
    normalize_tier,
    parse_claude_credit_balance,
    parse_claude_tier,
    parse_claude_usage,
)


def claude_tier_from_credentials() -> str | None:
    """Best-effort, zero-network tier label from the Claude Code OAuth creds file
    (``~/.claude/.credentials.json`` → ``claudeAiOauth.subscriptionType`` or, as a
    fallback, ``rateLimitTier``), mapped through the SAME ``normalize_tier`` ladder
    the /organizations API path uses so the display label matches (e.g. "max" or
    "default_claude_max_20x" → "Max"). Read-only; never logs the token. Any error
    (missing file / bad JSON / missing keys) → None."""
    try:
        raw = (Path.home() / ".claude" / ".credentials.json").read_text(encoding="utf-8")
        oauth = json.loads(raw).get("claudeAiOauth")
        if not isinstance(oauth, dict):
            return None
        tier = normalize_tier(oauth.get("subscriptionType")) or normalize_tier(oauth.get("rateLimitTier"))
        return tier or None
    except Exception:
        return None


CLAUDE_DOMAINS = ("claude.ai",)
CLAUDE_API_BASE = "https://claude.ai/api"
CLAUDE_ORGANIZATIONS_URL = f"{CLAUDE_API_BASE}/organizations"

# The prepaid-credit balance and the monthly overage spend cap change on the order of
# hours/days, not the 60s widget refresh. Re-fetching them every refresh is the bulk of
# the botty call volume that trips claude.ai's Cloudflare rate limiter. Throttle: reuse
# the previous snapshot's values when they were fetched less than this many seconds ago,
# skipping the two GETs entirely.
CREDIT_REFRESH_SECONDS = 300


def _credit_balance_fresh(prev: Any) -> dict[str, Any] | None:
    """Return the previous snapshot's Claude ``creditBalance`` iff it was fetched within
    ``CREDIT_REFRESH_SECONDS`` — else None (stale/absent → re-fetch). None-safe."""
    if not isinstance(prev, dict):
        return None
    cb = prev.get("creditBalance")
    if not isinstance(cb, dict):
        return None
    fetched = cb.get("fetchedAt")
    if not isinstance(fetched, str) or not fetched:
        return None
    try:
        # now_iso() emits local time with a UTC offset (accounting.now_iso uses
        # astimezone().isoformat()); the Z-replace is a belt for foreign/legacy stamps.
        ts = dt.datetime.fromisoformat(fetched.replace("Z", "+00:00"))
    except Exception:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    age = (dt.datetime.now(dt.timezone.utc) - ts).total_seconds()
    if age < 0 or age >= CREDIT_REFRESH_SECONDS:
        return None
    return cb


def _prev_monthly_limit(prev: Any) -> dict[str, Any] | None:
    """The previous snapshot's 'Monthly' (overage) limit row, if present — carried forward
    when the credit/overage GETs are throttled. None-safe."""
    if not isinstance(prev, dict):
        return None
    for lim in prev.get("limits") or []:
        if isinstance(lim, dict) and str(lim.get("label", "")).lower() == "monthly":
            return lim
    return None


async def _claude_get(url: str, jar: Any, timeout: float) -> tuple[int, Any] | None:
    """GET + JSON-decode, returning (status, data) or None on any failure — so a single
    endpoint's error is treated as 'absent' (matching the old per-request suppress/continue)
    instead of aborting the concurrent per-org batch."""
    try:
        return await http_json_async(url, jar, timeout)
    except Exception:
        return None



# ---------------------------------------------------------------------------
# Claude browser API
# ---------------------------------------------------------------------------

async def run_claude_api(cookies: list[BrowserCookie], timeout: float,
                         prev: dict[str, Any] | None = None) -> dict[str, Any]:
    domains = CLAUDE_DOMAINS
    if not any(host_matches_any(cookie.host, domains) for cookie in cookies):
        return missing_cookie_provider("Claude")
    jar = cookiejar_for_domains(cookies, domains)
    # Zero-network fallback tier from the local OAuth creds file (read once). Stamped
    # onto the base result so EVERY degraded/early return (timeout, api-error,
    # unauthorized, api-empty) still shows a tier; the /organizations API result
    # (parse_claude_tier) overrides it on the success path when present.
    disk_tier = claude_tier_from_credentials()
    result: dict[str, Any] = {
        "label": "Claude",
        "status": "api-empty",
        "source": "browser-api",
        "message": "Cookies found, no usage fields returned",
        "limits": [],
    }
    if disk_tier:
        result["tier"] = disk_tier
    try:
        status, data = await http_json_async(CLAUDE_ORGANIZATIONS_URL, jar, timeout)
    except Exception as exc:
        import socket
        import urllib.error
        is_timeout = isinstance(exc, (socket.timeout, TimeoutError))
        if not is_timeout and isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, (socket.timeout, TimeoutError)):
            is_timeout = True
        if is_timeout:
            result.update(status="timeout", message="Claude API request timed out")
        else:
            result.update(status="api-error", message=scrub_credentials(str(exc))[:160])
        return result
    if status == 401 or status == 403:
        # Surface a sign-in URL so the UI's error state is clickable.
        result.update(status="unauthorized", message=f"API rejected session cookies ({status})",
                      actionUrl="https://claude.ai")
        return result
    if status < 200 or status >= 300:
        result.update(status="api-error", message=f"API returned HTTP {status}")
        return result

    # API wins when present; else keep the disk fallback already stamped on result.
    tier = parse_claude_tier(data) or disk_tier
    if tier:
        result["tier"] = tier

    limits: list[dict[str, Any]] = []
    credit_balance: dict[str, Any] | None = None
    overage_limit: dict[str, Any] | None = None

    # Throttle the two SLOW-changing endpoints (prepaid credits + overage spend cap): if the
    # previous snapshot fetched them under CREDIT_REFRESH_SECONDS ago, carry those values
    # forward and skip the GETs entirely. This is the bulk of the per-refresh call volume that
    # trips claude.ai's Cloudflare rate limiter. None-safe on a missing/degraded prev.
    carried_credit = _credit_balance_fresh(prev)
    carried_monthly = _prev_monthly_limit(prev) if carried_credit is not None else None
    skip_credit_calls = carried_credit is not None
    if skip_credit_calls:
        credit_balance = carried_credit
        overage_limit = carried_monthly

    for org_id in claude_org_ids(data):
        base = f"{CLAUDE_ORGANIZATIONS_URL}/{org_id}"
        # usage_summary is the PREFERRED source; only fetch the (heavier) /usage endpoint when
        # usage_summary produced no limits. Sequentialized (not gathered): Claude's ~0.9s
        # latency against a 12s budget makes the extra round-trip free, and it saves one GET
        # every refresh where usage_summary succeeds (the common case). The credit/overage
        # GETs are fired concurrently WITH usage_summary only when not throttled.
        extra_calls = []
        if not skip_credit_calls:
            extra_calls = [
                _claude_get(f"{base}/prepaid/credits", jar, timeout),
                _claude_get(f"{base}/overage_spend_limit", jar, timeout),
            ]
        summary_call = _claude_get(f"{base}/usage_summary", jar, timeout)
        results = await asyncio.gather(summary_call, *extra_calls)
        summary = results[0]
        cr = results[1] if not skip_credit_calls else None
        ov = results[2] if not skip_credit_calls else None

        if not skip_credit_calls:
            if credit_balance is None and cr is not None and cr[0] == 200:
                credit_balance = parse_claude_credit_balance(cr[1])
                if credit_balance is not None:
                    credit_balance["fetchedAt"] = now_iso()
                else:
                    # A3: the endpoint answered 200 but had no parseable prepaid balance
                    # (accounts with no prepaid credits). Stamp an amount-less sentinel so
                    # _credit_balance_fresh sees a fetchedAt and throttles the two slow GETs
                    # for CREDIT_REFRESH_SECONDS — otherwise /prepaid/credits + /overage
                    # fire on EVERY refresh, the bulk of the Cloudflare-tripping call volume.
                    # Safe against the UI: _extra_usage_detail only renders a credit row when
                    # creditBalance["amount"] > 0 (accounting.py), and hasExtraUsage() gates on
                    # the resulting formattedExtraUsageDetail being non-empty — an amount-less
                    # dict yields "" and shows nothing.
                    credit_balance = {"fetchedAt": now_iso()}

            # Upstream Swift fetches a dedicated endpoint for the Extra-usage spend cap.
            # See ClaudeWebAPIFetcher.fetch / parseOverageSpendLimit.
            if overage_limit is None and ov is not None and ov[0] == 200 and isinstance(ov[1], dict):
                if ov[1].get("is_enabled") is True or ov[1].get("isEnabled") is True:
                    # parse_claude_usage already handles the cents → dollars conversion
                    # when this dict is given as data["extra_usage"]; reuse it.
                    parsed = parse_claude_usage({"extra_usage": ov[1]})
                    if parsed:
                        overage_limit = parsed[0]

        # usage_summary preferred; fall back to /usage only if it yielded no limits.
        org_limits: list[dict[str, Any]] = []
        if summary is not None and summary[0] == 200:
            org_limits = parse_claude_usage(summary[1])
        if not org_limits:
            usage = await _claude_get(f"{base}/usage", jar, timeout)
            if usage is not None and usage[0] == 200:
                org_limits = parse_claude_usage(usage[1])
        if org_limits:
            limits = org_limits
            break
    if not limits:
        limits = parse_claude_usage(data)
    # Merge in the dedicated overage endpoint result if usage_summary didn't already
    # produce a "Monthly" row.
    if overage_limit is not None and not any(
        str(l.get("label", "")).lower() == "monthly" for l in limits
    ):
        limits.append(overage_limit)
    if credit_balance is not None:
        result["creditBalance"] = credit_balance
    result.update(
        status="ok" if limits else "api-empty",
        message="Usage API returned data" if limits else "API returned no recognizable limits",
        limits=limits,
    )
    return result



