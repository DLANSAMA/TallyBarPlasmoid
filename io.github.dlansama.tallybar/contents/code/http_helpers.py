"""HTTP helper functions: JSON/text fetching, async wrappers, and provider timeout guards."""
from __future__ import annotations

import asyncio
import json
import re
import socket
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from typing import Any

from io_helpers import to_daemon_thread

socket.setdefaulttimeout(15.0)

# Caps on bytes read from a response, bounding memory against a runaway or hostile
# response. JSON APIs are small; HTML scraping (Gemini) needs a larger body cap.
# Error bodies are read only for the diagnostic message, so they stay tiny.
MAX_JSON_BODY_BYTES = 2_000_000
MAX_TEXT_BODY_BYTES = 4_000_000
MAX_JSON_ERROR_BYTES = 5_000
MAX_TEXT_ERROR_BYTES = 20_000


def scrub_credentials(text: str) -> str:
    """Redact sensitive bearer tokens and API keys from error messages.

    Covers the credentials this backend actually handles: HTTP bearer tokens, OpenAI keys,
    Claude browser session cookies (sessionKey=sk-ant-sid01-…), Google OAuth access/refresh
    tokens + client secrets (Antigravity Cloud Code), Google API-key literals (AIza…), OAuth
    body k/v forms (access_token/refresh_token=…), the Antigravity CSRF token (--csrf_token=…
    in process cmdlines), and the Gemini/Google-One batchexecute session tokens (at=SNlM0e /
    sid=FdrFJe). Also covers Google browser session cookies (SID/HSID/APISID/SAPISID/OSID and
    their __Secure- variants) and generic JWTs (eyJ.eyJ.sig, e.g. the Antigravity id_token).
    urllib's exception strings don't currently echo request URLs/bodies, so these are
    defense-in-depth against a future code path that does interpolate a token into a
    surfaced message."""
    if not isinstance(text, str):
        return text
    text = re.sub(r"Bearer\s+[a-zA-Z0-9_\-\.]+", "Bearer [REDACTED]", text)
    text = re.sub(r"sk-[a-zA-Z0-9_\-]+", "sk-[REDACTED]", text)
    text = re.sub(r"ya29\.[a-zA-Z0-9_\-]+", "ya29.[REDACTED]", text)        # Google OAuth access token
    text = re.sub(r"GOCSPX-[a-zA-Z0-9_\-]+", "GOCSPX-[REDACTED]", text)     # Google OAuth client secret
    text = re.sub(r"1//[a-zA-Z0-9_\-]{20,}", "1//[REDACTED]", text)         # Google OAuth refresh token
    text = re.sub(r"AIza[0-9A-Za-z_\-]{35}", "AIza[REDACTED]", text)        # Google API key literal
    text = re.sub(r"sessionKey=[^&\s\"']+", "sessionKey=[REDACTED]", text)  # Claude browser session cookie
    text = re.sub(r"csrf_token[\"']?\s*[=:]\s*[\"']?[^&\s\"']+", "csrf_token=[REDACTED]", text)  # Antigravity CSRF token
    text = re.sub(r"\b(access_token|refresh_token)=[^&\s\"']+", r"\1=[REDACTED]", text)    # OAuth body k/v
    text = re.sub(r"\b(at|f\.sid|sid)=[^&\s\"']+", r"\1=[REDACTED]", text)  # batchexecute session tokens
    # Google browser session cookies (SID/HSID/APISID/SAPISID/OSID + __Secure- variants).
    # '_' is a word char, so __Secure- has no leading \b; anchor on start-or-non-word instead.
    text = re.sub(r"(?i)(?<![\w-])__secure-[0-9]?p?(?:sid|apisid)\w*=[^&\s\"';]+",
                  "__Secure-[REDACTED]", text)
    text = re.sub(r"(?i)\b(s?sid|hsid|s?apisid|osid)=[^&\s\"';]+", r"\1=[REDACTED]", text)
    # JWTs (eyJ header . eyJ payload . sig) — e.g. the Antigravity id_token. Linear, no backtracking.
    text = re.sub(r"eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+",
                  "[REDACTED-JWT]", text)
    return text


def _require_https(url: str) -> None:
    """Reject any non-``https://`` URL before it reaches an opener carrying cookies.

    These two helpers are the CREDENTIALED path — every caller hands them a CookieJar
    holding real browser session cookies (claude.ai, gemini.google.com, chatgpt.com), and
    urllib's default opener follows redirects. Without this gate a ``file://``/``ftp://``
    URL would be fetched by urllib's other handlers, and a plaintext ``http://`` hop would
    put a non-Secure cookie on the wire. Every call site passes an ``https://`` module
    constant today, so this costs nothing and pins that invariant.

    The LOCAL language-server calls deliberately do NOT come through here — they build
    their own request in ``providers/antigravity.post_local_json`` (self-signed loopback
    TLS, no cookie jar), so this gate can stay strict without a loopback exemption.

    Raises ValueError, which the callers' existing ``except Exception`` turns into a
    scrubbed ``api-error`` status rather than a crash."""
    if not isinstance(url, str) or not url.lower().startswith("https://"):
        raise ValueError(f"refusing non-https request URL: {str(url)[:60]!r}")


def http_json(url: str, jar: CookieJar, timeout: float, method: str = "GET", body: bytes | None = None) -> tuple[int, Any]:
    _require_https(url)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": "Mozilla/5.0 TallyBar Plasma/0.1",
    }
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_JSON_BODY_BYTES)
            text = raw.decode("utf-8", errors="replace")
            try:
                return int(response.status), json.loads(text)
            except Exception:
                return int(response.status), {"text": text[:1000]}
    except urllib.error.HTTPError as exc:
        raw = exc.read(MAX_JSON_ERROR_BYTES)
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            data = {"error": exc.reason}
        return int(exc.code), data


def http_text(
    url: str,
    jar: CookieJar,
    timeout: float,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, str]:
    _require_https(url)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    request_headers = {
        "Accept": "*/*",
        "User-Agent": "Mozilla/5.0 TallyBar Plasma/0.1",
    }
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, data=body, method=method, headers=request_headers)
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_TEXT_BODY_BYTES)
            return int(response.status), raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read(MAX_TEXT_ERROR_BYTES)
        return int(exc.code), raw.decode("utf-8", errors="replace")


async def http_json_async(
    url: str,
    jar: CookieJar,
    timeout: float,
    method: str = "GET",
    body: bytes | None = None,
) -> tuple[int, Any]:
    return await to_daemon_thread(http_json, url, jar, timeout, method, body)


async def run_threaded_provider(
    func: Any,
    *args: Any,
    timeout: float,
    fallback: dict[str, Any],
) -> dict[str, Any]:
    try:
        return await asyncio.wait_for(to_daemon_thread(func, *args), timeout=timeout)
    except asyncio.TimeoutError:
        result = dict(fallback)
        result.update(status="timeout", message=f"{fallback['label']} telemetry timed out")
        return result
    except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
        if isinstance(exc, (TimeoutError, socket.timeout)) or (isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, (TimeoutError, socket.timeout))):
            result = dict(fallback)
            result.update(status="timeout", message=f"{fallback['label']} telemetry timed out")
            return result
        result = dict(fallback)
        result.update(status="api-error", message=scrub_credentials(str(exc))[:160])
        return result
    except Exception as exc:
        result = dict(fallback)
        result.update(status="api-error", message=scrub_credentials(str(exc))[:160])
        return result


async def bounded_provider(coro: Any, timeout: float, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        result = dict(fallback)
        result.update(status="timeout", message=f"{fallback['label']} telemetry timed out")
        return result
    except Exception as exc:
        result = dict(fallback)
        result.update(status="api-error", message=scrub_credentials(str(exc))[:160])
        return result
