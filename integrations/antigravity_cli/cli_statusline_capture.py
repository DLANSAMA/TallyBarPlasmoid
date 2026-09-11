#!/usr/bin/env python3
"""Capture Antigravity CLI (gemini/agy) token usage via its statusLine push.

The gemini CLI invokes `settings.json` → `statusLine.command` on every status update,
piping a JSON telemetry context to the command's **stdin**. That context carries the
session's per-model token counts. This script:

  1. Reads that stdin JSON.
  2. (Debug only) When TALLYBAR_DEBUG is set, dumps the raw payload to
     ~/.tallybar/cli_statusline_raw.json (0600) so the exact schema can be confirmed from a
     real session. Off by default — it would otherwise persist session telemetry every tick.
  3. Extracts cumulative per-session token counts (defensively — tries the known shapes).
  4. Writes them to ~/.tallybar/antigravity_cli_usage.json, keyed by session id, in the SAME
     {trackingStarted, entries:{key:{d,u,c,o,t,x}}} shape as the IDE ledger + SDK shim, so
     providers/cost.py folds it straight into the Antigravity cost summary.
  5. Prints a short status string to stdout, so it still works AS a statusLine command.

Cumulative & idempotent: `total_usage`-style session metrics are cumulative, so we store the
latest snapshot per session id and OVERWRITE each update (never sum) — re-runs can't inflate.
Never raises: a statusLine command must not error or it breaks the CLI's status bar.

Wiring (done by the user / with permission — this script touches nothing until wired):
  ~/.gemini/antigravity-cli/settings.json →
    "statusLine": { "type": "command", "command": "<path-to-this-script>" }

NOTE: the exact field names are not yet 100%-confirmed from a live payload — the extractor
tries every plausible shape and the raw dump lets us lock it down on the first real run.
"""

from __future__ import annotations

import datetime as _dt
import json as _json
import os as _os
import sys as _sys
import tempfile as _tempfile
from pathlib import Path as _Path
from typing import Any, Optional

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover
    _fcntl = None  # type: ignore[assignment]

USAGE_PATH = _Path.home() / ".tallybar" / "antigravity_cli_usage.json"
_LOCK_PATH = _Path.home() / ".tallybar" / "antigravity_cli_usage.lock"
_RAW_DUMP = _Path.home() / ".tallybar" / "cli_statusline_raw.json"


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _first(d: dict, *names: str) -> Any:
    for n in names:
        if isinstance(d, dict) and n in d and d[n] is not None:
            return d[n]
    return None


def _tokens_from_block(tok: dict) -> dict[str, int]:
    """Map one model's token block onto ledger fields, trying known key spellings.

    Established mapping (from /stats + convertToStreamStats): prompt = input + cached, so
    uncached input = `input` (NOT `prompt`). total = prompt + candidates + thoughts.
    """
    uncached = _first(tok, "input", "input_tokens", "inputTokens", "uncachedInputTokens")
    cached = _first(tok, "cached", "cached_tokens", "cachedTokens", "cachedContentTokenCount", "cached_content_token_count")
    if uncached is None:  # only a combined prompt count available — subtract cached
        prompt = _first(tok, "prompt", "promptTokenCount", "prompt_token_count", "prompt_tokens")
        uncached = max(_int(prompt) - _int(cached), 0) if prompt is not None else 0
    return {
        "u": _int(uncached),
        "c": _int(cached),
        "o": _int(_first(tok, "candidates", "output", "output_tokens", "outputTokens", "candidatesTokenCount", "candidates_token_count")),
        "t": _int(_first(tok, "thoughts", "thinking", "thoughtsTokenCount", "thoughts_token_count", "thinking_tokens")),
        "x": _int(_first(tok, "tool", "tools", "toolTokens", "toolUsePromptTokenCount", "tool_use_prompt_token_count")),
    }


def _extract(payload: dict) -> tuple[Optional[str], dict[str, int]]:
    """Return (session_id, summed {u,c,o,t,x}) from a statusLine telemetry payload."""
    session_id = _first(payload, "sessionId", "session_id", "conversation_id") or (
        (payload.get("session") or {}).get("id") if isinstance(payload.get("session"), dict) else None
    )

    agg = {"u": 0, "c": 0, "o": 0, "t": 0, "x": 0}
    found = False

    # Shape 0 (CONFIRMED real gemini/antigravity-cli schema): context_window holds the
    # session's running totals. `total_input_tokens` is the current context-window input fill
    # (includes cache reads); current_usage.cache_read_input_tokens gives the cached portion, so
    # uncached input = total_input - cached. thoughts/tool aren't exposed here (left 0).
    cw = payload.get("context_window")
    if isinstance(cw, dict):
        total_in = _int(cw.get("total_input_tokens"))
        total_out = _int(cw.get("total_output_tokens"))
        cur = cw.get("current_usage") if isinstance(cw.get("current_usage"), dict) else {}
        cached = _int(cur.get("cache_read_input_tokens"))
        if total_in or total_out:
            return session_id, {"u": max(total_in - cached, 0), "c": cached, "o": total_out, "t": 0, "x": 0}

    # Shape 1: per-model metrics — {models|metrics.models}: {name: {tokens: {...}}}
    models = None
    for container in (payload, payload.get("metrics") if isinstance(payload.get("metrics"), dict) else None,
                      payload.get("session") if isinstance(payload.get("session"), dict) else None):
        if isinstance(container, dict) and isinstance(container.get("models"), dict):
            models = container["models"]
            break
    if models:
        for _name, mm in models.items():
            tok = mm.get("tokens") if isinstance(mm, dict) else None
            if isinstance(tok, dict):
                got = _tokens_from_block(tok)
                for k in agg:
                    agg[k] += got[k]
                found = True

    # Shape 2: a single top-level tokens block
    if not found and isinstance(payload.get("tokens"), dict):
        agg = _tokens_from_block(payload["tokens"])
        found = True

    # Shape 3: flat top-level token fields
    if not found and any(k in payload for k in ("totalTokens", "inputTokens", "total_token_count", "totalTokenCount")):
        agg = _tokens_from_block(payload)
        found = any(agg.values())

    return session_id, agg


class _Lock:
    def __init__(self) -> None:
        self._fd = None

    def __enter__(self):
        if _fcntl is None:
            return self
        try:
            _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
            try:
                _LOCK_PATH.parent.chmod(0o700)  # keep ~/.tallybar private on a CLI-first setup
            except OSError:
                pass
            self._fd = _os.open(str(_LOCK_PATH), _os.O_CREAT | _os.O_RDWR, 0o600)
            _fcntl.flock(self._fd, _fcntl.LOCK_EX)
        except OSError:
            self._fd = None
        return self

    def __exit__(self, *a):
        if self._fd is not None:
            try:
                _fcntl.flock(self._fd, _fcntl.LOCK_UN)
            finally:
                _os.close(self._fd)


def _load() -> dict[str, Any]:
    try:
        d = _json.loads(USAGE_PATH.read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("entries"), dict):
            return d
    except (OSError, _json.JSONDecodeError):
        pass
    return {"trackingStarted": None, "entries": {}}


def _save(d: dict[str, Any]) -> None:
    USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        USAGE_PATH.parent.chmod(0o700)  # match the backend writers; keep ~/.tallybar private
    except OSError:
        pass
    name = None
    try:
        with _tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(USAGE_PATH.parent), delete=False) as tmp:
            name = tmp.name  # track for cleanup the instant the file exists — BEFORE the
            # chmod/write/flush/fsync below, any of which can raise (ENOSPC/EIO) and would
            # otherwise leak this delete=False temp file past the except handler.
            _os.chmod(tmp.name, 0o600)
            tmp.write(_json.dumps(d))
            tmp.flush()
            _os.fsync(tmp.fileno())  # durability: flush to disk before the atomic replace
        _os.replace(name, USAGE_PATH)
        try:  # make the rename itself crash-durable (best-effort dir fsync)
            dfd = _os.open(str(USAGE_PATH.parent), _os.O_RDONLY)
            try:
                _os.fsync(dfd)
            finally:
                _os.close(dfd)
        except OSError:
            pass
    except BaseException:
        # Don't leak the uniquely-named temp file on a write/replace failure (the sole
        # caller swallows the exception, so without this it would accumulate silently).
        if name is not None:
            try:
                _os.unlink(name)
            except OSError:
                pass
        raise


def main() -> None:
    try:
        raw = _sys.stdin.read()
    except Exception:
        return
    if not raw:
        return

    # Debug aid (opt-in): dump the raw payload so the statusLine schema can be confirmed
    # from a live session. Off by default — it would otherwise persist session telemetry to
    # a world-readable file on every tick. When enabled, write it 0600 like the usage file.
    if _os.environ.get("TALLYBAR_DEBUG"):
        try:
            _RAW_DUMP.parent.mkdir(parents=True, exist_ok=True)
            try:
                _RAW_DUMP.parent.chmod(0o700)
            except OSError:
                pass
            fd = _os.open(str(_RAW_DUMP), _os.O_WRONLY | _os.O_CREAT | _os.O_TRUNC, 0o600)
            with _os.fdopen(fd, "w", encoding="utf-8") as handle:
                _os.fchmod(handle.fileno(), 0o600)  # enforce 0600 even if the file pre-existed looser
                handle.write(raw)
        except OSError:
            pass

    try:
        payload = _json.loads(raw)
    except _json.JSONDecodeError:
        return
    if not isinstance(payload, dict):
        return

    session_id, agg = _extract(payload)
    total = sum(agg.values())

    if total > 0:
        try:
            key = "cli:" + (session_id or "current")
            today = _dt.datetime.now().astimezone().date().isoformat()
            entry = {"d": today, **agg}
            m = payload.get("model")
            model_id = m.get("id") if isinstance(m, dict) else (m if isinstance(m, str) else None)
            if model_id:
                entry["model"] = model_id  # display name (e.g. "Gemini 3.5 Flash (High)"); cost.py normalizes it
            with _Lock():
                data = _load()
                if data.get("trackingStarted") is None:
                    data["trackingStarted"] = today
                data["entries"][key] = entry
                
                cutoff = (_dt.datetime.now().astimezone().date() - _dt.timedelta(days=35)).isoformat()
                for k in list(data["entries"].keys()):
                    if data["entries"][k].get("d", "") < cutoff:
                        del data["entries"][k]

                _save(data)
        except Exception:
            pass

    # Still behave as a statusLine: print a compact status string.
    try:
        model = _first(payload, "active_model", "model", "modelName") or ""
        if isinstance(model, dict):
            model = model.get("name") or model.get("id") or ""
        k = total / 1000.0
        tok_str = f"{k:.1f}K" if total >= 1000 else str(total)
        _sys.stdout.write(f"TallyBar · {tok_str} tok{(' · ' + str(model)) if model else ''}")
    except Exception:
        pass


if __name__ == "__main__":
    main()
