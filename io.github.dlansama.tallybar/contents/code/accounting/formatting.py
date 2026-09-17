"""Timestamp, currency, and token-count formatting helpers."""
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
