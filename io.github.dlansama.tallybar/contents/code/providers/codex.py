"""Provider-specific API callers and data acquisition logic.

Each provider (Gemini, Claude, Codex/OpenAI, Antigravity) has its own fetch
path that returns a normalised provider dict suitable for the QML UI.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
from typing import Any

from accounting import (
    codex_credit_balance,
    codex_rate_limit_rows,
    missing_cookie_provider,
)
from cookies import BrowserCookie, cookiejar_for_domains, has_session_cookies
from http_helpers import http_json_async, scrub_credentials
from parsers import (
    as_dict,
    extract_limits_from_json,
    normalize_tier,
)


OPENAI_COOKIE_DOMAINS = ("chatgpt.com", "openai.com")
OPENAI_ACCOUNT_CHECK_URL = "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"



# ---------------------------------------------------------------------------
# OpenAI / Codex cookie API
# ---------------------------------------------------------------------------

async def run_openai_cookie_api(cookies: list[BrowserCookie], timeout: float) -> dict[str, Any]:
    domains = OPENAI_COOKIE_DOMAINS
    if not has_session_cookies(cookies, domains):
        return missing_cookie_provider("Codex", "browser")
    jar = cookiejar_for_domains(cookies, domains)
    try:
        status, data = await http_json_async(
            OPENAI_ACCOUNT_CHECK_URL,
            jar,
            timeout,
        )
    except Exception as exc:
        import socket
        import urllib.error
        is_timeout = isinstance(exc, (socket.timeout, TimeoutError))
        if not is_timeout and isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, (socket.timeout, TimeoutError)):
            is_timeout = True
        return {
            "label": "Codex",
            "status": "timeout" if is_timeout else "api-error",
            "source": "browser-api",
            "message": "Codex API request timed out" if is_timeout else scrub_credentials(str(exc))[:160],
            "limits": [],
        }
    if status == 401 or status == 403:
        return {
            "label": "Codex",
            "status": "unauthorized",
            "source": "browser-api",
            "message": f"API rejected session cookies ({status})",
            "limits": [],
        }
    if status < 200 or status >= 300:
        return {
            "label": "Codex",
            "status": "api-error",
            "source": "browser-api",
            "message": f"API returned HTTP {status}",
            "limits": [],
        }
    limits = extract_limits_from_json(data)
    return {
        "label": "Codex",
        "status": "ok" if limits else "api-empty",
        "source": "browser-api",
        "message": "Usage API returned data" if limits else "API returned no recognizable limits",
        "limits": limits,
    }



# ---------------------------------------------------------------------------
# Codex JSON-RPC subprocess
# ---------------------------------------------------------------------------

class JsonRpcChild:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self.next_id = 1
        self.pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.stderr_tail: list[str] = []
        self.stdout_task = asyncio.create_task(self._read_stdout())
        self.stderr_task = asyncio.create_task(self._read_stderr())

    @classmethod
    async def start(cls, executable: str, arguments: list[str]) -> "JsonRpcChild":
        process = await asyncio.create_subprocess_exec(
            executable,
            *arguments,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return cls(process)

    async def _read_stdout(self) -> None:
        decoder = json.JSONDecoder()
        buffer = ""
        try:
            assert self.process.stdout is not None
            while True:
                chunk = await self.process.stdout.read(4096)
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", errors="replace")
                # Cap buffer to prevent memory exhaustion from malformed streams
                if len(buffer) > 16_000_000:
                    buffer = buffer[-4_000_000:]
                    # A blind tail-slice can cut mid-message, leaving an undecodable
                    # partial at the head that raw_decode fails on forever (the parser then
                    # never recovers and the RPC hangs until timeout). Messages are newline-
                    # delimited (see _write), so realign to the next message boundary; if the
                    # 4MB tail holds no newline at all, drop it rather than spin on garbage.
                    nl = buffer.find("\n")
                    buffer = buffer[nl + 1:] if nl != -1 else ""

                while buffer:
                    buffer = buffer.lstrip()
                    if not buffer:
                        break
                    try:
                        message, index = decoder.raw_decode(buffer)
                        buffer = buffer[index:]
                    except json.JSONDecodeError:
                        break
                        
                    if isinstance(message, dict):
                        message_id = message.get("id")
                        if isinstance(message_id, int):
                            future = self.pending.pop(message_id, None)
                            if future and not future.done():
                                future.set_result(message)
        except Exception as exc:
            self._fail_pending(exc)
        finally:
            self._fail_pending(RuntimeError("JSON-RPC child stdout closed"))

    async def _read_stderr(self) -> None:
        # Read fixed-size chunks and split on newlines ourselves rather than
        # StreamReader.readline() — a child that emits a long burst without a newline makes
        # readline raise LimitOverrunError (default 64 KiB stream limit) and kills the reader,
        # losing all subsequent stderr. Chunked reads keep a bounded working buffer and keep
        # draining. stderr is diagnostic only (last 8 lines), so chunk boundaries don't matter.
        try:
            assert self.process.stderr is not None
            buffer = ""
            while True:
                chunk = await self.process.stderr.read(4096)
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", errors="replace")
                if len(buffer) > 65536:  # bound the buffer if no newline ever arrives
                    buffer = buffer[-65536:]
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    self.stderr_tail.append(line.strip())
                    self.stderr_tail = self.stderr_tail[-8:]
            tail = buffer.strip()
            if tail:
                self.stderr_tail.append(tail)
                self.stderr_tail = self.stderr_tail[-8:]
        except Exception:
            pass

    def _fail_pending(self, exc: Exception) -> None:
        for future in list(self.pending.values()):
            if not future.done():
                future.set_exception(exc)
        self.pending.clear()

    async def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 5.0) -> dict[str, Any]:
        message_id = self.next_id
        self.next_id += 1
        payload: dict[str, Any] = {"id": message_id, "method": method}
        if params is not None:
            payload["params"] = params
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self.pending[message_id] = future
        self._write(payload)
        await self.drain()
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except Exception:
            self.pending.pop(message_id, None)
            raise

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)

    def _write(self, payload: dict[str, Any]) -> None:
        if self.process.stdin is None or self.process.stdin.is_closing():
            raise RuntimeError("JSON-RPC child stdin is closed")
        self.process.stdin.write(json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")

    async def drain(self) -> None:
        if self.process.stdin is not None and not self.process.stdin.is_closing():
            await self.process.stdin.drain()

    async def terminate(self) -> None:
        self._fail_pending(RuntimeError("JSON-RPC child terminated"))
        for task in (self.stdout_task, self.stderr_task):
            task.cancel()
        if self.process.returncode is None:
            # Suppress OSError (not just ProcessLookupError): terminate()/kill()
            # run in run_codex_rpc's finally, so a PermissionError/EPERM here must
            # not mask the real result/exception. ProcessLookupError ⊂ OSError.
            with contextlib.suppress(OSError):
                self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                with contextlib.suppress(OSError):
                    self.process.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.process.wait(), timeout=1.0)
        for task in (self.stdout_task, self.stderr_task):
            with contextlib.suppress(asyncio.CancelledError):
                await task


def _codex_rpc_error_message(error: Any) -> str:
    """Extract a human-readable message from a JSON-RPC error value."""
    if isinstance(error, dict):
        msg = error.get("message")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()
        return json.dumps(error)[:160]
    return str(error)


_CODEX_RPC_UNAUTH_HINTS = (
    "unauthenticated",
    "unauthorized",
    "not logged in",
    "not authenticated",
    "log in",
    "sign in",
    "401",
)


def _codex_rpc_looks_unauthenticated(text: str) -> bool:
    lowered = text.lower()
    return any(hint in lowered for hint in _CODEX_RPC_UNAUTH_HINTS)


async def run_codex_rpc(timeout: float) -> dict[str, Any]:
    codex = shutil.which("codex")
    result: dict[str, Any] = {
        "label": "Codex",
        "status": "missing-cli",
        "source": "json-rpc",
        "message": "codex CLI not found",
        "limits": [],
    }
    if not codex:
        return result

    child: JsonRpcChild | None = None
    try:
        child = await JsonRpcChild.start(codex, ["-s", "read-only", "-a", "untrusted", "app-server"])
        deadline = asyncio.get_running_loop().time() + timeout

        def remaining() -> float:
            return max(0.1, deadline - asyncio.get_running_loop().time())

        await child.request(
            "initialize",
            {"clientInfo": {"name": "tallybar-plasma", "version": "0.1.0"}},
            timeout=remaining(),
        )
        child.notify("initialized")
        await child.drain()
        message = await child.request("account/rateLimits/read", timeout=remaining())
        rpc_error = message.get("error") if isinstance(message, dict) else None
        if rpc_error is not None:
            error_text = _codex_rpc_error_message(rpc_error)
            status = "unauthorized" if _codex_rpc_looks_unauthenticated(error_text) else "api-error"
            result.update(
                status=status,
                message=scrub_credentials(f"Codex app-server error: {error_text}")[:160],
            )
            return result
        account_result = as_dict(message.get("result"))
        rate_limits = as_dict(account_result.get("rateLimits"))
        limits = codex_rate_limit_rows(rate_limits, account_result.get("rateLimitsByLimitId"))
        tier = ""
        # planType is nested INSIDE rateLimits on the live app-server payload
        # (verified 2026-09-11), not at the top level of the result — reading only
        # account_result left tier empty for every account. Check both.
        plan_type = (
            account_result.get("planType")
            or account_result.get("plan_type")
            or account_result.get("plan")
            or rate_limits.get("planType")
            or rate_limits.get("plan_type")
        )
        if isinstance(plan_type, str) and plan_type:
            # Shared keyword ladder (parsers.normalize_tier); an unrecognized plan keeps
            # its raw name title-cased rather than reading as no-plan.
            tier = normalize_tier(plan_type) or plan_type.title()

        provider: dict[str, Any] = {
            "label": "Codex",
            "status": "ok" if limits else "api-empty",
            "source": "codex-json-rpc",
            "message": "Read account/rateLimits via Codex app-server",
            "limits": limits,
            "tier": tier,
        }
        credits = codex_credit_balance(rate_limits)
        if credits is not None:
            provider["creditBalance"] = credits
        return provider
    except asyncio.TimeoutError:
        result.update(status="timeout", message="Codex JSON-RPC timed out")
        return result
    except Exception as exc:
        result.update(status="api-error", message=scrub_credentials(str(exc))[:160])
        return result
    finally:
        if child is not None:
            await child.terminate()



