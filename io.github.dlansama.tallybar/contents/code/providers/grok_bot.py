"""Grok Bot weekly usage, read through the Grok Bot desktop app's own sign-in.

Grok Bot (xAI's ``sand`` Electron app) meters a weekly allowance through Cursor's
dashboard service. This is a different product and a different meter from Grok Build
(``providers/grok.py``, the CLI's local ``~/.grok`` logs) — the two never share a tab.

The bearer token lives in the app's secret store, ``~/.config/Grok Bot/sand-secrets.json``
(layout from ``sand`` 0.63.0, ``dist/electron-main/main-core.cjs``): a JSON object of
strings, each either ``plaintext:v1:<base64>``, ``scoped:v1:<64 hex>:<base64>`` or the
base64 of an Electron safeStorage (Chromium ``v10``/``v11``) blob, which decrypts with the
Chromium Safe Storage password from KWallet. The token is never refreshed here, and the
refresh token and account profile (email) are never read.

Nothing secret — the token, the machine id, the account scope, the checksum — ever reaches
a message, a diagnostic or the snapshot. Error messages are built from fixed strings, the
HTTP status and the Connect error code only, never from a response body.
"""
from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import math
import re
import socket
import time
import urllib.error
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, Callable

from accounting import default_provider, now_iso
from cookies import decrypt_chromium_value
from crypto import KWalletClient, OpenSslEvp
from http_helpers import http_text as _default_http_text
from parsers import add_pace_detail, relative_reset

GROK_BOT_SECRETS_PATH = Path.home() / ".config" / "Grok Bot" / "sand-secrets.json"
GROK_BOT_LABEL = "Grok Bot"
GROK_BOT_SOURCE = "grok-bot"

_DASHBOARD_SERVICE = "https://api2.cursor.sh/aiserver.v1.DashboardService"
GROK_BOT_USAGE_URL = f"{_DASHBOARD_SERVICE}/GetSandUsageStatus"
GROK_BOT_PERIOD_URL = f"{_DASHBOARD_SERVICE}/GetCurrentPeriodUsage"

# The installed app's version, sent as the app sends it. A constant on purpose: reading it
# from the multi-MB app.asar on every refresh would cost more than it is worth.
SAND_CLIENT_VERSION = "0.63.0"
SAND_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Sand/{SAND_CLIENT_VERSION} Safari/537.36"
)

_ACCOUNTS_KEY = "cursor-accounts"
_MACHINE_ID_KEY = "cursor-machine-id"
_ACCESS_TOKEN_KEY = "cursor-access-token"
_TEAM_ID_KEY = "cursor-selected-team-id"
_PLAINTEXT_PREFIX = "plaintext:v1:"
_SCOPED_PREFIX = "scoped:v1:"
_SCOPED_RE = re.compile(r"scoped:v1:([0-9a-f]{64}):(.+)", re.S)
_MAX_SECRETS_BYTES = 1_000_000

# int32 max — the app's "no limit" sentinel for a spend limit (``wEe`` in main-app.cjs).
_UNLIMITED_CENTS = 2147483647
_WEEK_MINUTES = 10080

_NOT_SIGNED_IN = "Open Grok Bot and sign in to see its weekly usage"
_SIGN_IN_AGAIN = "Grok Bot rejected its saved sign-in — open Grok Bot and sign in again"
_UNDECRYPTABLE = "Couldn't decrypt the Grok Bot sign-in"


class _Unreadable(Exception):
    """A stored value that is present but would not decode or decrypt. Never carries the
    value itself."""


class _StoreError(Exception):
    """The secret store exists but could not be read or parsed."""


class _WalletUnavailable(Exception):
    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


def _js_shr(value: int, shift: int) -> int:
    """JavaScript's ``value >> shift``: the left operand is converted to int32 and the
    shift count is masked to five bits."""
    v = value & 0xFFFFFFFF
    if v & 0x80000000:
        v -= 1 << 32
    return v >> (shift & 31)


def cursor_checksum(machine_id: str, now_ms: int) -> str:
    """The ``x-cursor-checksum`` header exactly as Grok Bot's ``Tg``/``kut`` build it
    (``dist/electron-main/main-app.cjs``): six bytes of ``floor(now_ms / 1e6)``, obfuscated
    with a running state of 165 and base64url'd unpadded, then the raw machine id.

    The six bytes follow JavaScript semantics, not the obvious Python: ``>>`` works on int32
    with the shift count masked to five bits, so the app's ``t>>40`` and ``t>>32`` are
    ``t>>8`` and ``t`` — the first two bytes repeat the last two rather than being zero.
    The running state is the wrapped byte because ``kut`` writes into a ``Uint8Array``."""
    t = now_ms // 1_000_000
    out = bytearray(_js_shr(t, shift) & 0xFF for shift in (40, 32, 24, 16, 8, 0))
    state = 165
    for i, byte in enumerate(out):
        out[i] = ((byte ^ state) + (i % 256)) & 0xFF
        state = out[i]
    return base64.urlsafe_b64encode(bytes(out)).decode("ascii").rstrip("=") + machine_id


def _b64decode(text: str) -> bytes:
    """Lenient base64 (standard or url-safe, padding optional) — Node's
    ``Buffer.from(s, "base64")`` accepts both, so the store may hold either."""
    t = text.strip().replace("-", "+").replace("_", "/")
    t += "=" * (-len(t) % 4)
    try:
        return base64.b64decode(t, validate=True)
    except (binascii.Error, ValueError):
        raise _Unreadable from None


class _SecretReader:
    """Unwraps the app's stored values. KWallet is opened only when a value is actually
    encrypted, at most once, and closed straight after the passwords are read."""

    def __init__(self, wallet_factory: Callable[..., Any], timeout: float, background: bool) -> None:
        self._wallet_factory = wallet_factory
        self._timeout_ms = int(max(0.5, min(3.0, timeout / 4)) * 1000)
        self._background = background
        self._passwords: dict[str, list[bytes]] | None = None
        self._crypto: OpenSslEvp | None = None
        self.wallet_status = ""

    def _safe_storage_passwords(self) -> dict[str, list[bytes]]:
        if self._passwords is not None:
            return self._passwords
        try:
            wallet = self._wallet_factory(timeout_ms=self._timeout_ms, background=self._background)
        except Exception:
            # No session bus / no PyGObject: Chromium's "peanuts" fallback key is still tried.
            self.wallet_status = "unavailable"
            self._passwords = {}
            return self._passwords
        try:
            self.wallet_status = str(getattr(wallet, "status", "") or "")
            if self.wallet_status in ("wallet-locked", "wallet-state-unknown"):
                raise _WalletUnavailable(self.wallet_status)
            self._passwords = wallet.safe_storage_passwords()
        finally:
            wallet.close()
        return self._passwords

    def unwrap(self, value: Any) -> str:
        if not isinstance(value, str) or not value:
            raise _Unreadable
        if value.startswith(_PLAINTEXT_PREFIX):
            try:
                return _b64decode(value[len(_PLAINTEXT_PREFIX):]).decode("utf-8")
            except UnicodeDecodeError:
                raise _Unreadable from None
        if value.startswith(_SCOPED_PREFIX):
            match = _SCOPED_RE.fullmatch(value)
            if match is None:
                raise _Unreadable
            value = match.group(2)
        blob = _b64decode(value)
        if not blob.startswith((b"v10", b"v11")):
            raise _Unreadable
        passwords = self._safe_storage_passwords()
        if self._crypto is None:
            self._crypto = OpenSslEvp()
        plain = decrypt_chromium_value(self._crypto, "", blob, "chromium", passwords)
        if not plain:
            raise _Unreadable
        return plain


def _read_secrets(path: Path) -> dict[str, str] | None:
    """The store's string entries, or None when there is no store (never signed in here)."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(_MAX_SECRETS_BYTES + 1)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        raise _StoreError from None
    if len(raw) > _MAX_SECRETS_BYTES:
        raise _StoreError
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _StoreError from None
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def _json_object(text: str) -> dict[str, Any] | None:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _select_account(secrets: dict[str, str], reader: _SecretReader) -> tuple[str, dict[str, Any], Any] | None:
    """``(scope, account, active)`` for the signed-in account, or None when nobody is.

    The app writes ``cursor-accounts`` as plain JSON; a wrapped value is accepted too."""
    raw = secrets.get(_ACCOUNTS_KEY)
    if not raw:
        return None
    record = _json_object(raw)
    if record is None:
        record = _json_object(reader.unwrap(raw))
        if record is None:
            raise _Unreadable
    accounts = record.get("accounts")
    if not isinstance(accounts, dict) or not accounts:
        return None
    active = record.get("active")
    if isinstance(active, str) and active:
        scope = active
    elif len(accounts) == 1:
        scope = next(iter(accounts))
    else:
        return None
    account = accounts.get(scope)
    if not isinstance(account, dict):
        return None
    return scope, account, active


def _team_id(secrets: dict[str, str], scope: str, account: dict[str, Any], active: Any,
             reader: _SecretReader) -> int | None:
    """The selected team, if one decrypts to a positive integer. The account's own entry
    wins; a top-level entry counts only when it belongs to this account — scoped to it, or
    unscoped while this is the active account (``readSelectedTeamForAccount`` in the app)."""
    raw = account.get(_TEAM_ID_KEY)
    if not raw:
        top = secrets.get(_TEAM_ID_KEY)
        if top:
            match = _SCOPED_RE.fullmatch(top)
            owner = match.group(1) if match else (None if top.startswith(_SCOPED_PREFIX) else active)
            if owner == scope:
                raw = top
    if not raw:
        return None
    try:
        text = reader.unwrap(raw).strip()
    except (_Unreadable, _WalletUnavailable):
        return None
    if not text.isdigit():
        return None
    value = int(text)
    return value if 0 < value <= 2**53 - 1 else None


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _timestamp_iso(value: Any) -> str:
    """A Connect JSON timestamp — RFC 3339 string, ``{seconds, nanos}`` object, or a number
    (milliseconds above 1e12, else seconds) — as a UTC ISO string; "" when unusable."""
    stamp: dt.datetime | None = None
    try:
        if isinstance(value, str) and value.strip():
            stamp = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=dt.timezone.utc)
        elif isinstance(value, dict):
            seconds = float(value.get("seconds") or 0)
            nanos = float(value.get("nanos") or 0)
            if seconds > 0:
                stamp = dt.datetime.fromtimestamp(seconds + nanos / 1e9, dt.timezone.utc)
        else:
            number = _finite(value)
            if number is not None and number > 0:
                stamp = dt.datetime.fromtimestamp(number / 1000.0 if number > 1e12 else number, dt.timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return ""
    return stamp.astimezone(dt.timezone.utc).isoformat() if stamp is not None else ""


def map_grok_bot_weekly(usage: Any) -> dict[str, Any] | None:
    """The Weekly row from ``GetSandUsageStatus``, or None when there is no own-account
    percentage to show (pooled team allowance, or no finite ``usagePercent``). ``usagePercent``
    is already a percent."""
    if not isinstance(usage, dict) or usage.get("usesPooledEnterpriseAllowance") is True:
        return None
    percent = _finite(usage.get("usagePercent"))
    if percent is None:
        return None
    row: dict[str, Any] = {
        "label": "Weekly",
        "percent": max(0.0, min(100.0, percent)),
        "reset": "",
        "unit": "percent",
        "windowMinutes": _WEEK_MINUTES,
    }
    reset_iso = _timestamp_iso(usage.get("nextResetTimestampUtc"))
    if reset_iso:
        row["resetAt"] = reset_iso
        row["reset"] = relative_reset(reset_iso)
        add_pace_detail(row, reset_iso, _WEEK_MINUTES)
    return row


def map_grok_bot_on_demand(period: Any) -> dict[str, Any] | None:
    """The On-demand row from ``GetCurrentPeriodUsage.spendLimitUsage``: only when both cent
    fields are numbers and the individual limit is a real one (above 0, below the int32
    "unlimited" sentinel). Amounts are converted from cents to dollars."""
    if not isinstance(period, dict):
        return None
    spend = period.get("spendLimitUsage")
    if not isinstance(spend, dict):
        return None
    limit_cents = _finite(spend.get("individualLimit"))
    used_cents = _finite(spend.get("individualUsed"))
    if limit_cents is None or used_cents is None or not (0 < limit_cents < _UNLIMITED_CENTS):
        return None
    return {
        "label": "On-demand",
        "percent": max(0.0, min(100.0, used_cents / limit_cents * 100.0)),
        "used": used_cents / 100.0,
        "limit": limit_cents / 100.0,
        "unit": "usd",
    }


def _is_grok_brand(value: Any) -> bool:
    """``billingBrand`` is the SandBillingBrand enum (0 unspecified, 1 CURSOR, 2 GROK);
    Connect JSON may carry it as the number or the value name."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value == 2
    if isinstance(value, str):
        return value.strip().upper() in ("2", "GROK", "SAND_BILLING_BRAND_GROK")
    return False


def grok_bot_tier(usage: Any) -> str | None:
    """Plan label for the subtitle. A Grok-billed plan reads "<plan> week" so this tab is
    not mistaken for a second allowance beside Grok Build's."""
    if not isinstance(usage, dict):
        return None

    def text(key: str) -> str:
        value = usage.get(key)
        return value.strip() if isinstance(value, str) else ""

    if _is_grok_brand(usage.get("billingBrand")):
        return (text("grokPlanLabel") or "SuperGrok") + " week"
    return text("grokPlanLabel") or text("cursorPlanName") or None


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    return isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, (TimeoutError, socket.timeout))


def _connect_error_code(body: str) -> str:
    """The Connect error ``code`` (e.g. ``permission_denied``) — the only part of an error
    body that ever reaches a message, and only in this validated shape."""
    data = _json_object(body) if isinstance(body, str) else None
    code = data.get("code") if data else None
    return code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,40}", code) else ""


def _request_headers(bearer: str, checksum: str, team_id: int | None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
        "Authorization": f"Bearer {bearer}",
        "User-Agent": SAND_USER_AGENT,
        "x-cursor-client-type": "sand",
        "x-cursor-client-source": "sand-desktop",
        "x-cursor-client-version": SAND_CLIENT_VERSION,
        "x-cursor-client-os": "linux",
        "x-sand-box-namespace": "prod",
        # The app's value whenever privacy mode is not a training-allowed mode.
        "x-ghost-mode": "true",
    }
    # The app sends no checksum at all without a machine id.
    if checksum:
        headers["x-cursor-checksum"] = checksum
    if team_id is not None:
        headers["x-cursor-team-id"] = str(team_id)
    return headers


def run_grok_bot(
    timeout: float = 12.0,
    *,
    background: bool = False,
    secrets_path: Path | None = None,
    http_text: Callable[..., tuple[int, str]] | None = None,
    wallet_factory: Callable[..., Any] | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Grok Bot's weekly usage bar (and an On-demand row when a spend limit is set).

    Statuses: ``ok``; ``not-running`` (no secret store, nobody signed in); ``unauthorized``
    (a stored value would not decrypt, or the server answered 401); ``wallet-locked`` /
    ``wallet-state-unknown`` (straight from the KWallet client); ``api-error``; ``timeout``.
    ``secrets_path``, ``http_text``, ``wallet_factory`` and ``now_ms`` are injectable for
    tests."""
    result: dict[str, Any] = default_provider(GROK_BOT_LABEL, GROK_BOT_SOURCE)
    http = http_text if http_text is not None else _default_http_text
    reader = _SecretReader(wallet_factory if wallet_factory is not None else KWalletClient, timeout, background)
    deadline = time.monotonic() + timeout

    def fail(status: str, message: str) -> dict[str, Any]:
        result.update(status=status, message=message)
        return result

    try:
        secrets = _read_secrets(secrets_path if secrets_path is not None else GROK_BOT_SECRETS_PATH)
    except _StoreError:
        return fail("api-error", "Couldn't read Grok Bot's sign-in store")
    if not secrets:
        return fail("not-running", _NOT_SIGNED_IN)

    try:
        selected = _select_account(secrets, reader)
        if selected is None:
            return fail("not-running", _NOT_SIGNED_IN)
        scope, account, active = selected
        token_raw = account.get(_ACCESS_TOKEN_KEY)
        if not isinstance(token_raw, str) or not token_raw:
            return fail("not-running", _NOT_SIGNED_IN)
        bearer = reader.unwrap(token_raw).strip()
        machine_id = ""
        if secrets.get(_MACHINE_ID_KEY):
            machine_id = reader.unwrap(secrets[_MACHINE_ID_KEY]).strip()
    except _WalletUnavailable as exc:
        if exc.status == "wallet-locked":
            return fail("wallet-locked", "KWallet is locked — unlock it to read the Grok Bot sign-in")
        return fail("wallet-state-unknown", "KWallet couldn't be checked, so the Grok Bot sign-in is unreadable")
    except _Unreadable:
        if reader.wallet_status and reader.wallet_status != "ok":
            return fail("unauthorized", _UNDECRYPTABLE + " (KWallet unavailable)")
        return fail("unauthorized", _UNDECRYPTABLE)

    # A bearer or machine id with spaces/control characters would split or corrupt a header.
    if not re.fullmatch(r"[\x21-\x7e]+", bearer):
        return fail("unauthorized", _UNDECRYPTABLE)
    if not re.fullmatch(r"[\x21-\x7e]{1,127}", machine_id):
        machine_id = ""
    team_id = _team_id(secrets, scope, account, active, reader)
    checksum = cursor_checksum(machine_id, now_ms if now_ms is not None else int(time.time() * 1000)) if machine_id else ""
    headers = _request_headers(bearer, checksum, team_id)

    def post(url: str, budget: float) -> tuple[int, str]:
        return http(url, CookieJar(), budget, "POST", b"{}", headers)

    try:
        status, body = post(GROK_BOT_USAGE_URL, max(0.5, deadline - time.monotonic()))
    except Exception as exc:  # noqa: BLE001 - the message is built from the class only, never str(exc)
        if _is_timeout(exc):
            return fail("timeout", f"{GROK_BOT_LABEL} telemetry timed out")
        if isinstance(exc, urllib.error.URLError):
            return fail("api-error", "Couldn't reach the Grok Bot usage service")
        return fail("api-error", f"Grok Bot usage request failed ({exc.__class__.__name__})")

    if status == 401:
        return fail("unauthorized", _SIGN_IN_AGAIN)
    if status == 415:
        return fail("api-error", "Grok Bot's usage service refused the JSON request (HTTP 415)")
    if status != 200:
        code = _connect_error_code(body)
        return fail("api-error", f"Grok Bot usage request failed (HTTP {status}{', ' + code if code else ''})")
    usage = _json_object(body) if isinstance(body, str) else None
    if usage is None:
        return fail("api-error", "Grok Bot returned an unreadable usage response")

    limits: list[dict[str, Any]] = []
    weekly = map_grok_bot_weekly(usage)
    if weekly is not None:
        limits.append(weekly)
        # The on-demand spend only matters next to a weekly bar, and only on a plan with an
        # included allowance; its failure never costs the weekly bar.
        remaining = deadline - time.monotonic()
        if usage.get("hasNonZeroIncludedLimit") is True and remaining >= 1.5:
            try:
                period_status, period_body = post(GROK_BOT_PERIOD_URL, remaining - 0.5)
            except Exception:  # noqa: BLE001 - optional row
                period_status, period_body = 0, ""
            if period_status == 200:
                on_demand = map_grok_bot_on_demand(_json_object(period_body) if isinstance(period_body, str) else None)
                if on_demand is not None:
                    limits.append(on_demand)

    if weekly is not None:
        message = "Grok Bot weekly usage"
    elif usage.get("usesPooledEnterpriseAllowance") is True:
        message = "This account uses your team's pooled Grok Bot allowance"
    else:
        message = "Grok Bot reported no weekly usage figure for this account"
    result.update(status="ok", message=message, limits=limits, fetchedAt=now_iso())
    tier = grok_bot_tier(usage)
    if tier:
        result["tier"] = tier
    return result
