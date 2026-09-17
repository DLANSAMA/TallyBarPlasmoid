#!/usr/bin/env python3
"""Capture Claude Code's official rate-limit data via its statusLine hook.

Claude Code pipes a JSON session blob to ``statusLine.command`` on stdin. Since
v2.1.x that blob carries the SUBSCRIBER'S REAL QUOTA — documented at
https://code.claude.com/docs/en/statusline:

    rate_limits.five_hour.used_percentage   0-100
    rate_limits.five_hour.resets_at         Unix epoch seconds
    rate_limits.seven_day.used_percentage / .resets_at
    rate_limits.spend_limit.used_percentage / .resets_at   (gateway only; may exceed 100)

Why this exists: TallyBar's normal Claude path scrapes claude.ai with browser
cookies, a path Cloudflare rate-limits (403/429) when polled too often. This route
is official, needs no cookies, no network and no
credentials of its own — Claude Code hands us the numbers it already has. It is a
COMPLEMENT, not a replacement: the blob only appears for Pro/Max subscribers, and
only after the first API response in a session, so it goes quiet whenever Claude
Code isn't running.

This script:
  1. Reads the statusLine JSON from stdin.
  2. Extracts whatever rate-limit windows are present.
  3. Writes them to ~/.tallybar/claude_statusline.json (atomic, 0600), stamped
     with the capture time so the backend can age the reading rather than assume
     it is current — the same mistake that made Grok's bar claim a 15-hour-old
     number was fresh.
  4. Prints a short line to stdout, so it still works AS a status line.

Never raises: a statusLine command that errors breaks Claude Code's status bar.

Wiring (done by the user — this touches nothing until wired). Add to
~/.claude/settings.json:

    "statusLine": {
      "type": "command",
      "command": "/path/to/TallyBarPlasmoid/integrations/claude_code/statusline_capture.py"
    }

This script consumes stdin, so it cannot be chained by piping. If you already
have a statusLine, call both from a small wrapper that reads stdin once and feeds
a copy to each.

NOTE on the inlined atomic write: the applet's rule is that every 0600 atomic
write goes through io_helpers.atomic_write_text. This file runs standalone from
~/.claude, outside the plasmoid, so it cannot import that module — the same
reason integrations/antigravity_cli/cli_statusline_capture.py inlines its own.
"""

from __future__ import annotations

import datetime as _dt
import json as _json
import os as _os
import sys as _sys
import tempfile as _tempfile
from pathlib import Path as _Path

STATE_DIR = _Path.home() / ".tallybar"
STATE_PATH = STATE_DIR / "claude_statusline.json"

# Windows documented on the statusline page. Each may be independently absent,
# and Claude Code DROPS a window once its resets_at has passed — so an absent
# window means "expired or not applicable", never "zero".
_WINDOWS = ("five_hour", "seven_day", "spend_limit")


def _clean_window(raw: object) -> dict[str, float] | None:
    """One window's {used_percentage, resets_at}, or None if unusable."""
    if not isinstance(raw, dict):
        return None
    pct = raw.get("used_percentage")
    if not isinstance(pct, (int, float)) or isinstance(pct, bool):
        return None
    out: dict[str, float] = {"usedPercent": float(pct)}
    resets = raw.get("resets_at")
    if isinstance(resets, (int, float)) and not isinstance(resets, bool) and resets > 0:
        out["resetsAt"] = float(resets)
    return out


def _extract(payload: object) -> dict[str, dict[str, float]]:
    if not isinstance(payload, dict):
        return {}
    limits = payload.get("rate_limits")
    if not isinstance(limits, dict):
        return {}
    found: dict[str, dict[str, float]] = {}
    for name in _WINDOWS:
        window = _clean_window(limits.get(name))
        if window is not None:
            found[name] = window
    return found


def _atomic_write(path: _Path, text: str) -> None:
    """0600 write via mkstemp + fsync + os.replace, with a best-effort dir fsync."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    name = None
    try:
        fd, name = _tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
        with _os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            _os.fsync(handle.fileno())
        _os.chmod(name, 0o600)
        _os.replace(name, path)
        name = None
        try:
            dfd = _os.open(str(path.parent), _os.O_RDONLY)
            try:
                _os.fsync(dfd)
            finally:
                _os.close(dfd)
        except OSError:
            pass
    finally:
        # Never leak the uniquely-named temp on failure — the caller swallows
        # exceptions, so without this they would accumulate silently.
        if name is not None:
            try:
                _os.unlink(name)
            except OSError:
                pass


def _status_text(found: dict[str, dict[str, float]]) -> str:
    if not found:
        return "TallyBar: no quota yet"
    parts = []
    for name, label in (("five_hour", "5h"), ("seven_day", "7d"), ("spend_limit", "$")):
        window = found.get(name)
        if window:
            parts.append(f"{label} {window['usedPercent']:.0f}%")
    return "TallyBar: " + " · ".join(parts)


def main() -> int:
    try:
        raw = _sys.stdin.read()
    except Exception:
        print("TallyBar: no input")
        return 0

    try:
        payload = _json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}

    found = _extract(payload)

    # Only write when we actually have a window. An empty capture must NOT clobber
    # a good earlier one — rate_limits is absent before the session's first API
    # response, so a blank write here would blind the widget for no reason.
    if found:
        try:
            _atomic_write(STATE_PATH, _json.dumps({
                "capturedAt": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                "rateLimits": found,
            }, indent=2, sort_keys=True))
        except Exception:
            pass  # a status line must never fail loudly

    print(_status_text(found))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        # Absolute backstop: print something and exit clean rather than break the bar.
        print("TallyBar")
        raise SystemExit(0)
