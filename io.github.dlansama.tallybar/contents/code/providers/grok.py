"""Grok (xAI) / Grok Build local telemetry.

Live bars come from Grok Build's ``billing: fetched credits config`` lines in
``~/.grok/logs/unified.jsonl`` (weekly SuperGrok credit pool + tier). No
browser cookies or network required.

Cost history is handled separately by ``accounting.local_grok_token_summary``
(also reads the unified log).
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Iterator

from accounting import default_provider, now_iso
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
# refresh. Observed 15.3h old while the widget read "Updated just now". Past this
# threshold the provider marks itself `stale`, which the UI already renders as a
# "(cached)" suffix on the subtitle. One hour: the widget refreshes every 5 minutes,
# so an hour-old vendor number is meaningfully not "just now" — but short enough
# thresholds would tag a perfectly good reading on every quiet afternoon.
_BILLING_STALE_AFTER_SECONDS = 3600
_BILLING_MSG = "billing: fetched credits config"


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
    """Return ``currentPeriod.start`` (or billingPeriodStart) from a raw event, else ``\"\"``."""
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
            carry = b""  # incomplete line from the previous (higher) chunk
            while end > 0:
                start = max(0, end - _BILLING_CHUNK_BYTES)
                handle.seek(start)
                chunk = handle.read(end - start)
                data = chunk + carry
                # When mid-file, the first partial line belongs to the next-lower chunk.
                if start > 0:
                    nl = data.find(b"\n")
                    if nl < 0:
                        carry = data
                        end = start
                        continue
                    carry = data[:nl + 1]  # remainder for the next iteration
                    data = data[nl + 1:]
                else:
                    carry = b""

                text = data.decode("utf-8", errors="replace")
                hits: list[dict[str, Any]] = []
                for line in text.splitlines():
                    parsed = _parse_billing_line(line)
                    if parsed is not None:
                        hits.append(parsed)
                # Newest within this chunk first.
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
        # Period window present but no explicit percent (defaults to 0% in parse).
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


# ---------------------------------------------------------------------------
# Main provider entry-point
# ---------------------------------------------------------------------------

def _billing_event_age_seconds(ts_iso: str) -> float | None:
    """Age in seconds of a billing event's own timestamp, or None if unusable."""
    if not ts_iso:
        return None
    try:
        stamp = dt.datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - stamp).total_seconds()


def run_grok_local(
    timeout: float = 12.0,  # noqa: ARG001 — signature parity with other local providers
    log_path: Path | None = None,
    grok_home: Path | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Build a Grok provider dict from local Grok Build state.

    The bars come from the ``billing: fetched credits config`` events in
    ``~/.grok/logs/unified.jsonl`` — the weekly SuperGrok credit-pool bar
    (``creditUsagePercent``, tier, period). ``log_path`` / ``grok_home``
    override the defaults for tests.

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
    if event is None:
        # Auth present but no billing lines yet — still a useful degraded state.
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

    # fetchedAt is when the NUMBER was captured (by the CLI, into the log), not when
    # we read the file — stamping now_iso() here claimed a freshness we never had.
    captured_at = str(event.get("ts") or "").strip()
    result.update(
        status="ok",
        message="Grok Build usage",
        source="local-grok-build",
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
