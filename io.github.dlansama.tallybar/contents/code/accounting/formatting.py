from __future__ import annotations

import datetime as dt
from typing import Any


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


_COUNT_TIERS = ((1_000, "K"), (1_000_000, "M"), (1_000_000_000, "B"))


def _format_in_tier(amount: int, div: int, suffix: str) -> tuple[str, int | None]:
    """Render `amount` in one unit tier; the second item is the rounded amount to
    re-render one tier up when the text would otherwise reach the next boundary."""
    n = amount / div
    carried = int(n + 0.5) * div
    if suffix == "K":
        rounded = int(n + 0.5)
        return f"{rounded}K", carried if rounded >= 1000 else None
    if n >= 100:
        rounded = int(n + 0.5)
        return f"{rounded}{suffix}", carried if rounded >= 1000 else None
    if n >= 10:
        formatted = f"{n:.1f}".rstrip("0").rstrip(".")
        return f"{formatted}{suffix}", carried if float(formatted) >= 100 else None
    formatted = f"{n:.2f}".rstrip("0").rstrip(".")
    return f"{formatted}{suffix}", carried if float(formatted) >= 10 else None


def compact_token_count(value: int | float) -> str:
    # Mirror the QML compactCount() exactly — carry at the 1000-of-a-unit boundary
    # (999.5K -> 1M) and trim trailing zeros (13.0M -> 13M, 1.20M -> 1.2M) — so a token
    # count renders identically in the Python cost lines and the QML bars/tooltips/graphs.
    # JS Math.round is half-UP, so use int(x + 0.5) rather than Python's half-even round().
    amount = max(0, int(float(value) + 0.5))
    while True:
        tiers = [t for t in _COUNT_TIERS if amount >= t[0]]
        if not tiers:
            return str(amount)
        div, suffix = tiers[-1]
        text, carried = _format_in_tier(amount, div, suffix)
        if carried is None or div == _COUNT_TIERS[-1][0]:
            return text
        amount = carried


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


def numeric_value(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").strip())
        except ValueError:
            return None
    return None


def _currency_symbol(unit: str) -> str:
    n = str(unit or "").upper()
    if n in ("USD", "COST"):
        return "$"
    if n == "EUR":
        return "EUR"
    if n == "GBP":
        return "GBP"
    return n if n else "$"


def _money_text(value: float, unit: str) -> str:
    symbol = _currency_symbol(unit)
    if symbol == "$":
        return f"$ {value:.2f}"
    return f"{symbol} {value:.2f}"
