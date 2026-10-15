"""Grok (xAI) / Grok Build telemetry.

The weekly SuperGrok credit-pool bar comes live from the billing endpoint the
grok CLI calls, signed in with the CLI's own access token, when the network is
allowed; otherwise, or when that fails, from the newest ``billing: fetched
credits config`` line in ``~/.grok/logs/unified.jsonl``. No browser cookies.

Cost history is handled separately by ``accounting.local_grok_token_summary``
(also reads the unified log).
"""
from __future__ import annotations

import datetime as dt
import json
import threading
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, Callable, Iterator

from accounting import default_provider, now_iso
from http_helpers import http_text as _default_http_text
from parsers import parse_grok_billing_config, relative_reset

GROK_HOME = Path.home() / ".grok"
GROK_LOG = GROK_HOME / "logs" / "unified.jsonl"
# Reverse-scan the unified log in chunks looking for billing events.
# Billing is logged at session start; a long agent session can push that line
# far from EOF (multi-MB), so a fixed "last 512KB" window is load-bearing-wrong.
# Chunked reverse scan finds the newest hit without re-reading the whole file
# in the common case (event near the end) while still covering older sessions.
_BILLING_CHUNK_BYTES = 256 * 1024
# The weekly bar is xAI's OWN creditUsagePercent, but it only lands in the log when
# the grok CLI actually runs — so its age is the age of the last session, not of our
# refresh. Past this threshold the provider marks itself `stale`, which the UI renders
# as a "(cached)" suffix on the subtitle. One hour: meaningfully not "just now", yet long
# enough not to tag a perfectly good reading on every quiet afternoon.
_BILLING_STALE_AFTER_SECONDS = 3600
_BILLING_MSG = "billing: fetched credits config"
# The live reading: the endpoint the grok CLI itself calls at session start, with the
# CLI's OIDC access token from ~/.grok/auth.json. The token lives about 6h and only
# the CLI renews it (TallyBar never reads the refresh token), so the live bar works
# until the token lapses and the log reading covers the rest.
_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
_LIVE_SOURCE = "grok-billing-api"
_TOKEN_EXPIRY_MARGIN_SECONDS = 60


def _parse_billing_line(line: str) -> dict[str, Any] | None:
    if _BILLING_MSG not in line:
        return None
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    if isinstance(record, dict) and record.get("msg") == _BILLING_MSG:
        return record
    return None


def _period_start_iso(event: dict[str, Any]) -> str:
    ctx = event.get("ctx") if isinstance(event.get("ctx"), dict) else None
    if not isinstance(ctx, dict):
        return ""
    config = ctx.get("config") if isinstance(ctx.get("config"), dict) else None
    if not isinstance(config, dict):
        return ""
    current = config.get("currentPeriod") if isinstance(config.get("currentPeriod"), dict) else None
    if isinstance(current, dict):
        start = current.get("start") or current.get("startTime")
        if isinstance(start, str) and start:
            return start
    start = config.get("billingPeriodStart") or config.get("billing_period_start")
    return start if isinstance(start, str) and start else ""


def _iter_billing_events_reverse(log_path: Path) -> Iterator[dict[str, Any]]:
    """Yield billing events newest-first via chunked reverse scan.

    File is append-only, so within each chunk lines are chronological; we emit
    a chunk's hits from last to first, then move to the previous chunk.
    """
    try:
        size = log_path.stat().st_size
    except OSError:
        return
    if size <= 0:
        return

    try:
        with log_path.open("rb") as handle:
            end = size
            carry = b""
            while end > 0:
                start = max(0, end - _BILLING_CHUNK_BYTES)
                handle.seek(start)
                chunk = handle.read(end - start)
                data = chunk + carry
                if start > 0:
                    nl = data.find(b"\n")
                    if nl < 0:
                        carry = data
                        end = start
                        continue
                    carry = data[:nl + 1]
                    data = data[nl + 1:]
                else:
                    carry = b""

                text = data.decode("utf-8", errors="replace")
                hits: list[dict[str, Any]] = []
                for line in text.splitlines():
                    parsed = _parse_billing_line(line)
                    if parsed is not None:
                        hits.append(parsed)
                for event in reversed(hits):
                    yield event
                end = start
    except OSError:
        return


def _latest_billing_event(log_path: Path) -> dict[str, Any] | None:
    """Return the billing event that best represents current weekly usage.

    Grok sometimes logs ``billing: fetched credits config`` **without**
    ``creditUsagePercent`` (``historyLen == 0``) — right after a weekly reset,
    and also intermittently mid-period.  Always taking the newest line then
    either fails parse or would show a false 0%.  Selection, newest-first:

    1. Newest event with an explicit percent → use it (common path; early exit).
    2. Newest period-only event (no percent) is remembered; keep scanning.
    3. On finding an older explicit-percent event:
       - if the period-only event's period *start* is strictly newer → return
         the period-only event (fresh week @ 0% until first metered report);
       - otherwise → return the explicit-percent event (same period; ignore
         the incomplete newer snapshot).
    4. Only period-only events in the file → return the newest of those.
    """
    if not log_path.is_file():
        return None

    pending_period_only: dict[str, Any] | None = None
    pending_start = ""

    for event in _iter_billing_events_reverse(log_path):
        parsed = parse_grok_billing_config(event)
        if parsed is None:
            continue
        if parsed.get("percentExplicit"):
            if pending_period_only is not None and pending_start:
                older_start = _period_start_iso(event)
                # ISO-8601 timestamps with a fixed offset compare lexicographically
                # in chronological order — real Grok logs use +00:00 throughout.
                if older_start and pending_start > older_start:
                    return pending_period_only
            return event
        if pending_period_only is None:
            pending_period_only = event
            pending_start = _period_start_iso(event)

    return pending_period_only


def _parse_period_ts(s: Any) -> dt.datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        ts = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=dt.timezone.utc)


def roll_period_forward(start: dt.datetime | None, end: dt.datetime, period_type: str,
                        now: dt.datetime) -> tuple[dt.datetime | None, dt.datetime | None, bool]:
    """The billing period CURRENT at ``now``, given the last one the CLI logged.

    Returns ``(start, end, ended)``. The billing event is only written when the grok CLI
    runs, so after a quiet week the newest event describes a period that is already over —
    and its ``creditUsagePercent`` describes nothing current. A WEEKLY period is projected
    forward in whole periods (xAI's weekly pool rolls from the period boundary); any other
    type that has ended returns ``(None, None, True)`` — the current window is unknowable."""
    if end > now:
        return start, end, False
    if "WEEK" not in (period_type or "").upper():
        return None, None, True
    length = (end - start) if start is not None and end > start else dt.timedelta(days=7)
    periods = int((now - end) / length) + 1
    new_start = end + (periods - 1) * length
    return new_start, new_start + length, True


def grok_billing_period(log_path: Path | None = None,
                        now: dt.datetime | None = None) -> tuple[dt.datetime, dt.datetime] | None:
    """Return the current billing-period (start, end) from the latest billing event, or None.

    Parses the ``currentPeriod.start`` / ``currentPeriod.end`` ISO strings from the most-recent
    usable ``billing: fetched credits config`` event (same selection as the Weekly bar).
    Trailing ``Z`` is normalised to ``+00:00``; a naive datetime is assumed UTC.  Returns
    ``None`` when the log is missing, the event is absent, or the period strings are not both
    parseable / ``start < end``. A period that has already ENDED is rolled forward to the one
    current at ``now`` (see ``roll_period_forward``) — else "This week" counted last week's
    tokens — or ``None`` when a non-weekly period ended and the current window is unknown.
    """
    path = log_path if log_path is not None else GROK_LOG
    event = _latest_billing_event(path)
    if event is None:
        return None
    parsed = parse_grok_billing_config(event)
    if parsed is None:
        return None
    period = parsed.get("period")
    if not isinstance(period, dict):
        return None
    start = _parse_period_ts(period.get("start"))
    end = _parse_period_ts(period.get("end"))
    if start is None or end is None or start >= end:
        return None
    current = now or dt.datetime.now(dt.timezone.utc)
    new_start, new_end, _ended = roll_period_forward(start, end, str(period.get("type") or ""), current)
    if new_start is None or new_end is None:
        return None
    return (new_start, new_end)


def _billing_event_age_seconds(ts_iso: str) -> float | None:
    stamp = _parse_period_ts(ts_iso)
    if stamp is None:
        return None
    return (dt.datetime.now(dt.timezone.utc) - stamp).total_seconds()


def _grok_access_token(home: Path) -> str | None:
    """The grok CLI's unexpired grok.com access token from ``auth.json``, or None.

    ``auth.json`` maps ``<issuer>::<id>`` to a sign-in record; the latest-expiring
    record with a ``key`` wins. A record without a parseable ``expires_at`` is
    skipped, and so is one within ``_TOKEN_EXPIRY_MARGIN_SECONDS`` of expiry.
    ``refresh_token`` is never read: only the CLI renews the sign-in.
    """
    try:
        data = json.loads((home / "auth.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    cutoff = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
        seconds=_TOKEN_EXPIRY_MARGIN_SECONDS
    )
    best: tuple[dt.datetime, str] | None = None
    for record in data.values():
        if not isinstance(record, dict):
            continue
        key = record.get("key")
        expires = _parse_period_ts(record.get("expires_at"))
        if not isinstance(key, str) or not key or expires is None or expires <= cutoff:
            continue
        if best is None or expires > best[0]:
            best = (expires, key)
    return best[1] if best else None


def _live_billing_event(
    home: Path,
    timeout: float,
    http: Callable[..., tuple[int, str]],
    log_event: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """The billing endpoint's reading shaped like a log event, or None to fall back.

    Every failure (no usable token, a network error, a stall past ``timeout``, a
    non-200 reply, an unreadable body, a period that is unreadable or already over)
    returns None and the caller keeps the log reading; nothing from the request
    reaches a message, so the token can't either. The endpoint omits the plan name,
    so the tier comes from the log reading's event when there is one.
    """
    token = _grok_access_token(home)
    if token is None:
        return None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    reply = _call_within(
        timeout, lambda: http(_BILLING_URL, CookieJar(), timeout, "GET", None, headers)
    )
    try:
        status, body = reply
        payload = json.loads(body) if status == 200 else None
    except (TypeError, ValueError):
        return None
    config = payload.get("config") if isinstance(payload, dict) else None
    if not isinstance(config, dict):
        return None
    ctx: dict[str, Any] = {"config": config}
    log_ctx = log_event.get("ctx") if isinstance(log_event, dict) else None
    tier = log_ctx.get("subscriptionTier") if isinstance(log_ctx, dict) else None
    if isinstance(tier, str) and tier:
        ctx["subscriptionTier"] = tier
    event = {"ts": now_iso(), "ctx": ctx}
    parsed = parse_grok_billing_config(event)
    if parsed is None:
        return None
    period = parsed.get("period") or {}
    if "end" in period:
        end = _parse_period_ts(period["end"])
        if end is None or end <= dt.datetime.now(dt.timezone.utc):
            return None
    if not parsed.get("percentExplicit") and log_event is not None:
        # The same rule as _latest_billing_event: a reading without a percent is 0%
        # only when it opens a NEW period; mid-period it would hide the last real one.
        logged = parse_grok_billing_config(log_event)
        log_start = _period_start_iso(log_event)
        if (logged or {}).get("percentExplicit") and not (
            log_start and _period_start_iso(event) > log_start
        ):
            return None
    return event


def _call_within(budget: float, func: Callable[[], Any]) -> Any:
    """``func()`` on a daemon thread, or None if it raises or outlasts ``budget`` seconds.

    urllib's timeout bounds each socket operation, not the whole request, and not the
    DNS lookup at all, so a stalled request could outlast the provider's deadline and
    lose the log reading with it. The abandoned thread is a daemon: it can't hold up
    the one-shot backend's exit.
    """
    box: list[Any] = []

    def run() -> None:
        try:
            box.append(func())
        except Exception:
            pass

    worker = threading.Thread(target=run, name="tallybar-grok-billing", daemon=True)
    worker.start()
    worker.join(budget)
    return box[0] if box else None


def run_grok_local(
    timeout: float = 12.0,
    log_path: Path | None = None,
    grok_home: Path | None = None,
    now: dt.datetime | None = None,
    network: bool = False,
    http: Callable[..., tuple[int, str]] | None = None,
) -> dict[str, Any]:
    """Build a Grok provider dict from Grok Build's billing reading.

    The bar is the weekly SuperGrok credit pool (``creditUsagePercent``, tier,
    period). With ``network`` it is read live from the billing endpoint using the
    CLI's unexpired access token; otherwise, or when that fails, it comes from the
    newest ``billing: fetched credits config`` event in ``~/.grok/logs/unified.jsonl``.
    ``log_path`` / ``grok_home`` / ``http`` override the defaults for tests.

    Statuses:
      - ``not-running`` when ``~/.grok`` is absent or unauthenticated
      - ``api-empty`` when the log exists but has no usable billing event yet
      - ``ok`` when a credits bar can be synthesised
    """
    home = grok_home if grok_home is not None else GROK_HOME
    path = log_path if log_path is not None else (home / "logs" / "unified.jsonl")

    result = {
        **default_provider("Grok", "local-grok-build"),
        "status": "api-empty",
        "message": "No Grok Build usage data yet",
        "limits": [],
    }

    if not home.is_dir():
        result.update(
            status="not-running",
            message="Grok Build data dir (~/.grok) not found",
        )
        return result

    event = _latest_billing_event(path)
    source = "local-grok-build"
    if network:
        live = _live_billing_event(
            home, max(1.0, min(8.0, timeout / 2)), http or _default_http_text, event
        )
        if live is not None:
            event, source = live, _LIVE_SOURCE
    if event is None:
        auth = home / "auth.json"
        if auth.is_file():
            result["message"] = "Logged in; no credit usage snapshot in recent logs"
        else:
            result.update(
                status="not-running",
                message="Grok Build installed but not authenticated",
            )
        return result

    parsed = parse_grok_billing_config(event)
    if parsed is None:
        result["message"] = "Billing event present but unparseable"
        return result

    # fetchedAt is when the NUMBER was captured (by the CLI into the log, or by our live
    # request), not when we read the file.
    captured_at = str(event.get("ts") or "").strip()
    result.update(
        status="ok",
        message="Grok Build usage",
        source=source,
        limits=parsed.get("limits") or [],
        fetchedAt=captured_at or now_iso(),
    )
    age = _billing_event_age_seconds(captured_at)
    if age is not None and age > _BILLING_STALE_AFTER_SECONDS:
        result["stale"] = True
    tier = parsed.get("tier")
    if tier:
        result["tier"] = tier
    period = parsed.get("period")
    if period:
        result["period"] = period
        _roll_ended_period(result, period, now or dt.datetime.now(dt.timezone.utc))
    return result


def _roll_ended_period(result: dict[str, Any], period: dict[str, Any], now: dt.datetime) -> None:
    """An ended period's percentage describes nothing current: the pool has reset, so the
    bar reads 0% (the CLI hasn't reported usage in the new period yet) and the reset moves
    to the projected end of the current period. Mutates ``result`` in place."""
    end = _parse_period_ts(period.get("end"))
    if end is None or not result.get("limits"):
        return
    start = _parse_period_ts(period.get("start"))
    new_start, new_end, ended = roll_period_forward(start, end, str(period.get("type") or ""), now)
    if not ended:
        return
    limit = dict(result["limits"][0])
    limit["percent"] = 0.0
    limit.pop("resetAt", None)
    limit["reset"] = ""
    rolled: dict[str, Any] = {k: v for k, v in period.items() if k not in ("start", "end")}
    if new_start is not None and new_end is not None:
        limit["resetAt"] = new_end.isoformat()
        limit["reset"] = relative_reset(limit["resetAt"])
        rolled.update(start=new_start.isoformat(), end=new_end.isoformat(), projected=True)
    result["limits"] = [limit] + list(result["limits"][1:])
    result["period"] = rolled
    result["message"] = "Grok billing period ended — 0% until the next Grok session reports usage"
