"""Provider-specific API callers and data acquisition logic.

Each provider (Gemini, Claude, Codex/OpenAI, Antigravity) has its own fetch
path that returns a normalised provider dict suitable for the QML UI.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any

from accounting import (
    compact_token_count,
    now_iso,
)
from cookies import BrowserCookie, cookiejar_for_domains, has_session_cookies
from http_helpers import http_text, scrub_credentials
from io_helpers import atomic_write_text
from parsers import (
    parse_batchexecute_payload,
    parse_gemini_usage_info,
    parse_google_one_credits,
    relative_reset,
)


def _is_timeout_exc(exc: Exception) -> bool:
    """True for a socket/connect timeout (incl. one wrapped in urllib.error.URLError),
    so an in-fetch timeout is labelled status='timeout' rather than 'api-error'."""
    import socket
    import urllib.error
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return True
    return isinstance(exc, urllib.error.URLError) and isinstance(getattr(exc, "reason", None), (socket.timeout, TimeoutError))


GEMINI_DOMAINS = ("gemini.google.com", "google.com", "accounts.google.com")
GEMINI_USAGE_URL = "https://gemini.google.com/usage"
GEMINI_BATCH_URL = "https://gemini.google.com/_/BardChatUi/data/batchexecute"
GEMINI_USAGE_INFO_RPC = "jSf9Qc"  # BardFrontendService.GetUsageInfo
GEMINI_QUOTA_RPC = "qpEbW"  # BardFrontendService.CheckGeminiQuota
GOOGLE_ONE_DOMAINS = ("one.google.com", "google.com", "accounts.google.com")
GOOGLE_ONE_ACTIVITY_URL = "https://one.google.com/ai/activity"
GOOGLE_ONE_BATCH_URL = "https://one.google.com/_/SubscriptionsManagementUi/data/batchexecute"
GOOGLE_ONE_CREDITS_RPC = "DrWK4"


# ---------------------------------------------------------------------------
# Batchexecute RPC token cache (shared by Gemini and Google One)
#
# Both the Gemini /usage and Google One /ai/activity fetches are 2 round-trips: a GET of an
# HTML page ONLY to scrape three batchexecute tokens (at=SNlM0e / bl=cfb2h / sid=FdrFJe), then
# the POST RPC that returns the actual metered data. Those tokens are session-stable, so
# caching them lets a warm refresh skip the HTML GET and go straight to the live POST (measured
# ~0.3-0.6s saved per fetch). ONLY the tokens are cached — the metered data is always fetched
# live via the POST — and a stale token just fails/empties the POST and silently falls back to
# the full HTML scrape, so the displayed numbers can never be stale or wrong. The two pages
# carry different bl/sid (different web apps: BardChatUi vs SubscriptionsManagementUi), so each
# provider keeps its own cache file. 0600 atomic write (the tokens are session secrets).
# ---------------------------------------------------------------------------

_BATCH_TOKEN_TTL = 1800.0  # 30 min; staleness within the TTL self-heals via revalidation
_GEMINI_TOKEN_CACHE = Path.home() / ".tallybar" / "cache" / "gemini_tokens.json"
_GOOGLE_ONE_TOKEN_CACHE = Path.home() / ".tallybar" / "cache" / "google_one_tokens.json"


def _load_cached_batch_tokens(cache_path: Path) -> tuple[str, str, str] | None:
    """Cached (at, bl, sid) tokens from `cache_path` if present and younger than the TTL."""
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    ts, at, bl, sid = data.get("ts"), data.get("at"), data.get("bl"), data.get("sid")
    if not (isinstance(ts, (int, float)) and at and bl and sid):
        return None
    if time.time() - ts > _BATCH_TOKEN_TTL:
        return None
    return str(at), str(bl), str(sid)


def _save_cached_batch_tokens(cache_path: Path, at: str, bl: str, sid: str) -> None:
    try:
        atomic_write_text(cache_path,
                          json.dumps({"at": at, "bl": bl, "sid": sid, "ts": time.time()}))
    except OSError:
        pass  # regenerable cache — a failed write just means a full scrape next run


def _load_gemini_tokens() -> tuple[str, str, str] | None:
    return _load_cached_batch_tokens(_GEMINI_TOKEN_CACHE)


def _save_gemini_tokens(at: str, bl: str, sid: str) -> None:
    _save_cached_batch_tokens(_GEMINI_TOKEN_CACHE, at, bl, sid)


def _gemini_limits_via_post(jar: Any, timeout: float, at: str, bl: str, sid: str) -> list[dict[str, Any]] | None:
    """POST the Gemini usage RPC with the given tokens and return the parsed limits, or None
    if the call didn't cleanly yield data (non-2xx, unparseable, or transport error). The
    cached fast path treats None OR an empty list as 'fall back to the full HTML scrape', so a
    stale token never surfaces as a wrong/empty result."""
    try:
        rpc_status, text = post_gemini_batchexecute(jar, timeout, GEMINI_USAGE_INFO_RPC, at, bl, sid)
        if rpc_status < 200 or rpc_status >= 300:
            return None
        return parse_gemini_usage_info(parse_batchexecute_payload(text, GEMINI_USAGE_INFO_RPC))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Gemini web scraping
# ---------------------------------------------------------------------------

def extract_embedded_config_value(html: str, key: str) -> str | None:
    patterns = [
        rf'"{re.escape(key)}":"((?:\\.|[^"\\])*)"',
        rf'\["{re.escape(key)}","((?:\\.|[^"\\])*)"\]',
    ]
    # Add semantic fallbacks for Google WIZ keys in case they are renamed.
    if key == "SNlM0e":  # API token
        patterns.append(r'"\w{6}":"(A[0-9a-zA-Z_-]{50,150}:[0-9]{13})"')
    elif key == "cfb2h": # Build label
        patterns.append(r'"\w{5}":"(boq_[0-9a-zA-Z_-]+)"')
    elif key == "FdrFJe": # Session ID
        patterns.append(r'"\w{6}":"([-0-9a-zA-Z]+:[0-9]+)"')

    for pattern in patterns:
        match = re.search(pattern, html)
        if not match:
            continue
        raw = match.group(1)
        try:
            return json.loads(f'"{raw}"')
        except Exception:
            return raw
    return None


_SIGNIN_URL_RE = re.compile(r"accounts\.google\.com/(?:v3/signin|ServiceLogin|AccountChooser)")
# Pull the full sign-in URL out of the logged-out shell so the UI can open it.
_SIGNIN_FULL_URL_RE = re.compile(
    r"https://accounts\.google\.com/(?:v3/signin|ServiceLogin|AccountChooser)[^\s\"'\\<>]*"
)
GEMINI_SIGNIN_FALLBACK = "https://gemini.google.com"


def _extract_signin_url(html: str) -> str:
    """The concrete sign-in URL embedded in the logged-out Google shell, or the Gemini
    home fallback. Trims a trailing HTML-escaped '\\u003d' etc. by stopping at the first
    quote/space (handled by the regex character class)."""
    match = _SIGNIN_FULL_URL_RE.search(html or "")
    return match.group(0) if match else GEMINI_SIGNIN_FALLBACK


def _looks_signed_out(html: str, at: str | None, bl: str | None, sid: str | None) -> bool:
    """The logged-OUT shell of a Google WIZ app: the page still renders (build label
    ``cfb2h`` and session id ``FdrFJe`` embedded) but the auth token ``SNlM0e`` is absent
    and a sign-in URL is present. Distinguishes 'cookies no longer authenticate — sign back
    in' (actionable) from a genuine page-shape regression (tokens gone wholesale). Safe
    against false positives on logged-in pages: those carry SNlM0e, so this is never reached."""
    return not at and bool(bl) and bool(sid) and bool(_SIGNIN_URL_RE.search(html))


def post_gemini_batchexecute(
    jar: Any,
    timeout: float,
    rpc_id: str,
    at: str,
    bl: str,
    sid: str,
) -> tuple[int, str]:
    params = urllib.parse.urlencode(
        {
            "rpcids": rpc_id,
            "source-path": "/usage",
            "bl": bl,
            "f.sid": sid,
            "hl": "en",
            "_reqid": str(int(time.time()) % 900_000 + 100_000),
            "rt": "c",
        }
    )
    f_req = json.dumps([[[rpc_id, "[]", None, "generic"]]], separators=(",", ":"))
    body = urllib.parse.urlencode({"f.req": f_req, "at": at}).encode("utf-8")
    return http_text(
        f"{GEMINI_BATCH_URL}?{params}",
        jar,
        timeout,
        method="POST",
        body=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Origin": "https://gemini.google.com",
            "Referer": GEMINI_USAGE_URL,
            "X-Same-Domain": "1",
        },
    )


def run_gemini_web(cookies: list[BrowserCookie], timeout: float) -> dict[str, Any]:
    result: dict[str, Any] = {
        "label": "Gemini",
        "status": "missing-cookies",
        "source": "gemini-web",
        "message": "No gemini.google.com browser session cookies",
        "limits": [],
    }
    if not has_session_cookies(cookies, GEMINI_DOMAINS):
        return result

    jar = cookiejar_for_domains(cookies, GEMINI_DOMAINS)

    # Fast path: reuse cached tokens to skip the usage-HTML GET. Only a non-empty result
    # short-circuits; a miss/empty/failure falls through to the full scrape (limits are
    # still fetched live via the POST).
    cached = _load_gemini_tokens()
    if cached is not None:
        cached_limits = _gemini_limits_via_post(jar, timeout, *cached)
        if cached_limits:
            result.update(
                status="ok",
                message="Read gemini.google.com/usage metrics RPC",
                limits=cached_limits,
            )
            return result

    try:
        status, html = http_text(
            GEMINI_USAGE_URL,
            jar,
            timeout,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
    except Exception as exc:
        if _is_timeout_exc(exc):
            result.update(status="timeout", message="Gemini request timed out")
        else:
            result.update(status="api-error", message=scrub_credentials(str(exc))[:160])
        return result
    if status == 401 or status == 403:
        result.update(status="unauthorized", message=f"Gemini rejected session cookies ({status})",
                      actionUrl=GEMINI_SIGNIN_FALLBACK)
        return result
    if status < 200 or status >= 300:
        result.update(status="api-error", message=f"Gemini usage page returned HTTP {status}")
        return result

    at = extract_embedded_config_value(html, "SNlM0e")
    bl = extract_embedded_config_value(html, "cfb2h")
    sid = extract_embedded_config_value(html, "FdrFJe")
    if not at or not bl or not sid:
        if _looks_signed_out(html, at, bl, sid):
            result.update(
                status="unauthorized",
                message="Google session signed out — sign in to gemini.google.com in your browser",
                actionUrl=_extract_signin_url(html),
            )
        else:
            result.update(
                status="api-empty",
                message="Gemini usage page loaded without required RPC tokens",
            )
        return result

    try:
        rpc_status, text = post_gemini_batchexecute(jar, timeout, GEMINI_USAGE_INFO_RPC, at, bl, sid)
    except Exception as exc:
        if _is_timeout_exc(exc):
            result.update(status="timeout", message="Gemini request timed out")
        else:
            result.update(status="api-error", message=scrub_credentials(str(exc))[:160])
        return result
    if rpc_status == 401 or rpc_status == 403:
        result.update(status="unauthorized", message=f"Gemini quota RPC rejected session cookies ({rpc_status})",
                      actionUrl=GEMINI_SIGNIN_FALLBACK)
        return result
    if rpc_status < 200 or rpc_status >= 300:
        result.update(status="api-error", message=f"Gemini quota RPC returned HTTP {rpc_status}")
        return result

    try:
        payload = parse_batchexecute_payload(text, GEMINI_USAGE_INFO_RPC)
        limits = parse_gemini_usage_info(payload)
    except Exception as exc:
        result.update(status="api-error", message=f"Gemini usage parse failed: {scrub_credentials(str(exc))[:120]}")
        return result
    # If the POST succeeded and parsed limits, these scraped tokens are known-good — cache
    # them so the next refresh can skip this HTML GET.
    if limits:
        _save_gemini_tokens(at, bl, sid)
    # Show only the Code Assist quota windows (Session + Weekly). The consumer
    # gemini.google.com web-app quota ("Gemini Apps" etc.) is a different metric
    # and was confusing mixed in here, so it is intentionally not merged.

    result.update(
        status="ok" if limits else "api-empty",
        message="Read gemini.google.com/usage metrics RPC" if limits else "Gemini usage RPC returned no recognizable limits",
        limits=limits,
    )
    # Tier is NOT hardcoded: gemini.google.com never returns the subscription tier.
    # The real Google AI tier (Ultra/Pro/Free) is resolved from the shared Google
    # account in apply_local_cost_summaries() and propagated here.
    return result



# ---------------------------------------------------------------------------
# Google One AI credit balance (one.google.com/ai/activity)
# ---------------------------------------------------------------------------

def post_google_one_batchexecute(
    jar: Any,
    timeout: float,
    rpc_id: str,
    at: str,
    bl: str,
    sid: str,
) -> tuple[int, str]:
    params = urllib.parse.urlencode(
        {
            "rpcids": rpc_id,
            "source-path": "/ai/activity",
            "bl": bl,
            "f.sid": sid,
            "hl": "en",
            "soc-app": "727",
            "soc-platform": "1",
            "soc-device": "1",
            "_reqid": str(int(time.time()) % 900_000 + 100_000),
            "rt": "c",
        }
    )
    f_req = json.dumps([[[rpc_id, "[null,[]]", None, "1"]]], separators=(",", ":"))
    body = urllib.parse.urlencode({"f.req": f_req, "at": at}).encode("utf-8")
    return http_text(
        f"{GOOGLE_ONE_BATCH_URL}?{params}",
        jar,
        timeout,
        method="POST",
        body=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Origin": "https://one.google.com",
            "Referer": GOOGLE_ONE_ACTIVITY_URL,
            "X-Same-Domain": "1",
        },
    )


# Google One reuses the shared batchexecute token cache (see _load_cached_batch_tokens):
# the /ai/activity GET only scrapes at/bl/sid, so a warm refresh skips it and POSTs straight
# for the live balance. Its own cache file (its bl/sid differ from Gemini's BardChatUi app).
def _load_google_one_tokens() -> tuple[str, str, str] | None:
    return _load_cached_batch_tokens(_GOOGLE_ONE_TOKEN_CACHE)


def _save_google_one_tokens(at: str, bl: str, sid: str) -> None:
    _save_cached_batch_tokens(_GOOGLE_ONE_TOKEN_CACHE, at, bl, sid)


def _google_one_ok_result(result: dict[str, Any], credits: dict[str, Any]) -> dict[str, Any]:
    """Fill `result` with the ok creditBalance for a parsed `credits` dict (shared by the
    cached fast path and the full HTML-scrape path so both render identically)."""
    amount = credits["credits"]
    expiration = credits.get("expiration") or ""
    detail = f"Credits: {compact_token_count(amount)} available"
    if expiration:
        when = relative_reset(expiration)
        if when.startswith("Resets in "):
            when = when[len("Resets in "):]
        if when and when != "Reset due":
            detail += f" · expires in {when}"
    result.update(
        status="ok",
        message="Read one.google.com/ai/activity credit balance RPC",
        creditBalance={
            "label": "Credits",
            "amount": amount,
            "currency": "credits",
            "source": "google-one-ai",
            "detail": detail,
            "expiration": expiration,
            "fetchedAt": now_iso(),
        },
    )
    return result


def _google_one_credits_via_post(jar: Any, timeout: float, at: str, bl: str, sid: str) -> dict[str, Any] | None:
    """POST the batchexecute credit RPC with the given tokens and return the parsed credits
    dict, or None if the call didn't cleanly yield a balance (non-2xx, unparseable, empty,
    or a transport error). Used by the cached fast path, which treats None as 'fall back to
    the full HTML scrape' — so a stale/expired token never surfaces as an error."""
    try:
        rpc_status, text = post_google_one_batchexecute(jar, timeout, GOOGLE_ONE_CREDITS_RPC, at, bl, sid)
        if rpc_status < 200 or rpc_status >= 300:
            return None
        return parse_google_one_credits(parse_batchexecute_payload(text, GOOGLE_ONE_CREDITS_RPC))
    except Exception:
        return None


# The Google One AI credit pool changes on the order of hours/days, not the 60s widget
# refresh. Re-fetching it every refresh means an authenticated one.google.com batchexecute
# RPC each cycle. Throttle it exactly like the Claude credit/overage GETs
# (claude.CREDIT_REFRESH_SECONDS): reuse the previous snapshot's balance when it was fetched
# less than this many seconds ago, skipping the RPC entirely.
GOOGLE_ONE_REFRESH_SECONDS = 300


def google_one_credit_fresh(prev: Any) -> dict[str, Any] | None:
    """Return the previous snapshot's Antigravity ``creditBalance`` iff it is the Google One
    AI credit pool (``source == "google-one-ai"``) and was fetched within
    ``GOOGLE_ONE_REFRESH_SECONDS`` — else None (stale/absent/other-source → re-fetch).

    ``prev`` is the previous snapshot's *antigravity* provider dict (the Google One balance
    lands there via ``apply_google_one_credits``). Mirrors ``claude._credit_balance_fresh``,
    including the None-safety on every step and rejecting negative ages from a clock jump."""
    if not isinstance(prev, dict):
        return None
    cb = prev.get("creditBalance")
    if not isinstance(cb, dict):
        return None
    if cb.get("source") != "google-one-ai":
        return None
    fetched = cb.get("fetchedAt")
    if not isinstance(fetched, str) or not fetched:
        return None
    try:
        # now_iso() emits local time with a UTC offset; the Z-replace is a belt for
        # foreign/legacy stamps.
        ts = dt.datetime.fromisoformat(fetched.replace("Z", "+00:00"))
    except Exception:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    age = (dt.datetime.now(dt.timezone.utc) - ts).total_seconds()
    if age < 0 or age >= GOOGLE_ONE_REFRESH_SECONDS:
        return None
    return cb


def run_google_one_credits(cookies: list[BrowserCookie], timeout: float) -> dict[str, Any]:
    """Fetch the Google One AI credit balance via the DrWK4 batchexecute RPC.

    Returns a result dict whose ``creditBalance`` (when status is ``ok``) is the
    real Ultra/Pro AI credit pool, used to replace the misleading Code Assist
    "Pro" prompt/flow figures in the Antigravity section.
    """
    result: dict[str, Any] = {
        "status": "missing-cookies",
        "source": "google-one-ai",
        "message": "No one.google.com browser session cookies",
        "creditBalance": None,
    }
    if not has_session_cookies(cookies, GOOGLE_ONE_DOMAINS):
        return result

    jar = cookiejar_for_domains(cookies, GOOGLE_ONE_DOMAINS)

    # Fast path: reuse cached tokens to skip the activity-HTML GET. Any miss/failure falls
    # through to the full scrape below — the balance is still fetched live via the POST.
    cached = _load_google_one_tokens()
    if cached is not None:
        credits = _google_one_credits_via_post(jar, timeout, *cached)
        if credits is not None:
            return _google_one_ok_result(result, credits)

    try:
        status, html = http_text(
            GOOGLE_ONE_ACTIVITY_URL,
            jar,
            timeout,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
    except Exception as exc:
        if _is_timeout_exc(exc):
            result.update(status="timeout", message="Google One request timed out")
        else:
            result.update(status="api-error", message=scrub_credentials(str(exc))[:160])
        return result
    if status == 401 or status == 403:
        result.update(status="unauthorized", message=f"Google One rejected session cookies ({status})")
        return result
    if status < 200 or status >= 300:
        result.update(status="api-error", message=f"Google One activity page returned HTTP {status}")
        return result

    at = extract_embedded_config_value(html, "SNlM0e")
    bl = extract_embedded_config_value(html, "cfb2h")
    sid = extract_embedded_config_value(html, "FdrFJe")
    if not at or not bl or not sid:
        if _looks_signed_out(html, at, bl, sid):
            result.update(
                status="unauthorized",
                message="Google session signed out — sign in to one.google.com in your browser",
            )
        else:
            result.update(
                status="api-empty",
                message="Google One activity page loaded without required RPC tokens",
            )
        return result

    try:
        rpc_status, text = post_google_one_batchexecute(jar, timeout, GOOGLE_ONE_CREDITS_RPC, at, bl, sid)
    except Exception as exc:
        if _is_timeout_exc(exc):
            result.update(status="timeout", message="Google One request timed out")
        else:
            result.update(status="api-error", message=scrub_credentials(str(exc))[:160])
        return result
    if rpc_status == 401 or rpc_status == 403:
        result.update(status="unauthorized", message=f"Google One credits RPC rejected session cookies ({rpc_status})")
        return result
    if rpc_status < 200 or rpc_status >= 300:
        result.update(status="api-error", message=f"Google One credits RPC returned HTTP {rpc_status}")
        return result

    try:
        payload = parse_batchexecute_payload(text, GOOGLE_ONE_CREDITS_RPC)
        credits = parse_google_one_credits(payload)
    except Exception as exc:
        result.update(status="api-error", message=f"Google One credits parse failed: {scrub_credentials(str(exc))[:120]}")
        return result
    if credits is None:
        result.update(status="api-empty", message="Google One credits RPC returned no recognizable balance")
        return result

    # Cache the fresh, known-good tokens so the next refresh can skip this HTML GET.
    _save_google_one_tokens(at, bl, sid)
    return _google_one_ok_result(result, credits)



