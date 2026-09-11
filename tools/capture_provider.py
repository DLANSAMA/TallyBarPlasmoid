#!/usr/bin/env python3
"""Capture a provider's RAW usage response, scrubbed, so a parser can be written against it.

The problem this solves: TallyBar can't support a provider until someone has seen
what that provider actually returns, and the maintainer will never hold a
subscription to every AI coding tool. This makes a capture cheap enough that
anyone with an account — free tier included — can produce one in a few seconds
and attach it to an issue.

    python3 tools/capture_provider.py copilot
    python3 tools/capture_provider.py cursor --out /tmp/cursor.json

It reads the credential the tool already stored locally, makes ONE request, scrubs
the response through the applet's own ``scrub_credentials``, and writes it to a
local 0600 file. It never uploads anything, never writes outside the path you
give it, and never touches the provider's own files.

WHY THIS EXISTS SEPARATELY FROM backend.py: the backend can only dump providers it
already implements. The whole point here is the providers it does NOT implement
yet, so the probe has to stand alone.

Both endpoints below are UNOFFICIAL — reverse-engineered from the vendors' own
editor clients and documented by third-party projects. They are what the vendor's
UI calls, not a supported API, and they can change without notice. That is
precisely why a capture is worth more than a guess.

Scrubbing is best-effort. READ THE FILE BEFORE YOU SHARE IT.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "io.github.dlansama.tallybar" / "contents" / "code"))
from http_helpers import scrub_credentials  # noqa: E402

TIMEOUT = 20.0


# ---------------------------------------------------------------------------
# Credential discovery (read-only; we never write to a provider's own files)
# ---------------------------------------------------------------------------

def _copilot_token() -> tuple[str | None, str]:
    """Copilot's editor plugins store an oauth_token per host. Returns (token, where)."""
    candidates = [
        Path.home() / ".config" / "github-copilot" / "apps.json",
        Path.home() / ".config" / "github-copilot" / "hosts.json",
        Path.home() / ".copilot" / "config.json",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        # Both shapes seen in the wild: {"<host>": {"oauth_token": ...}} and a flat dict.
        stack: list[Any] = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                tok = node.get("oauth_token") or node.get("token")
                if isinstance(tok, str) and tok:
                    return tok, str(path)
                stack.extend(node.values())
    return None, " / ".join(str(c) for c in candidates)


def _cursor_token() -> tuple[str | None, str]:
    """Cursor keeps a JWT in its VS Code-style globalStorage SQLite DB."""
    db = Path.home() / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    if not db.is_file():
        return None, str(db)
    # Copy before reading: the DB is live and we must not fight Cursor for the lock
    # (the same fetch-then-close discipline the Antigravity scan uses).
    with tempfile.TemporaryDirectory(prefix="tallybar-capture-") as tmp:
        os.chmod(tmp, 0o700)
        copy = Path(tmp) / "state.vscdb"
        copy.write_bytes(db.read_bytes())
        try:
            con = sqlite3.connect(f"file:{urllib.parse.quote(str(copy))}?mode=ro", uri=True)
            try:
                con.execute("PRAGMA busy_timeout = 3000")
                row = con.execute(
                    "SELECT value FROM ItemTable WHERE key = ?", ("cursorAuth/accessToken",)
                ).fetchone()
            finally:
                con.close()
        except sqlite3.Error as exc:
            return None, f"{db} (sqlite error: {exc})"
    if not row or not row[0]:
        return None, f"{db} (no cursorAuth/accessToken row — signed out?)"
    token = row[0]
    return (token.decode("utf-8", "replace") if isinstance(token, bytes) else str(token)), str(db)


# ---------------------------------------------------------------------------
# Probes — one request each, no retries, no side effects
# ---------------------------------------------------------------------------

def _request(url: str, headers: dict[str, str], body: bytes | None = None) -> dict[str, Any]:
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace") if exc.fp else ""
        status = exc.code
    except Exception as exc:
        return {"error": scrub_credentials(f"{exc.__class__.__name__}: {exc}")}
    try:
        parsed: Any = json.loads(raw)
    except Exception:
        parsed = raw[:4000]
    return {"httpStatus": status, "body": parsed}


def probe_copilot(token: str) -> dict[str, Any]:
    url = "https://api.github.com/copilot_internal/user"
    headers = {
        "authorization": f"token {token}",
        "accept": "application/json",
        "editor-version": "vscode/1.95.0",
        "editor-plugin-version": "copilot-chat/0.26.7",
        "user-agent": "GitHubCopilotChat/0.26.7",
        "x-github-api-version": "2025-04-01",
    }
    out = _request(url, headers)
    out["request"] = {"method": "GET", "url": url,
                      "headers": {k: ("<redacted>" if k == "authorization" else v)
                                  for k, v in headers.items()}}
    return out


def probe_cursor(token: str) -> dict[str, Any]:
    url = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage"
    headers = {"authorization": f"Bearer {token}", "content-type": "application/json"}
    out = _request(url, headers, body=b"{}")
    out["request"] = {"method": "POST", "url": url, "body": "{}",
                      "headers": {k: ("<redacted>" if k == "authorization" else v)
                                  for k, v in headers.items()}}
    return out


PROBES = {
    "copilot": (_copilot_token, probe_copilot,
                "Sign in to GitHub Copilot in VS Code / your editor (Copilot Free is enough)."),
    "cursor": (_cursor_token, probe_cursor,
               "Install Cursor and sign in (the free Hobby plan is enough; no card needed)."),
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("provider", choices=sorted(PROBES))
    ap.add_argument("--out", help="where to write the capture (default: ./capture-<provider>.json)")
    ap.add_argument("--print", action="store_true", dest="to_stdout",
                    help="also print the capture to stdout")
    args = ap.parse_args(argv)

    find_token, probe, how_to_get_it = PROBES[args.provider]
    token, where = find_token()
    if not token:
        print(f"No {args.provider} credential found.\n  looked in: {where}\n  {how_to_get_it}",
              file=sys.stderr)
        return 2

    result = probe(token)
    result["provider"] = args.provider
    result["credentialSource"] = where

    # Scrub the whole document, not just known fields: these responses carry ids,
    # emails and tokens we have not catalogued.
    text = scrub_credentials(json.dumps(result, indent=2, sort_keys=True))

    out = Path(args.out) if args.out else Path(f"capture-{args.provider}.json")
    fd = os.open(str(out), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text + "\n")

    print(f"Wrote {out} (0600).")
    print("Scrubbing is best-effort — READ IT before sharing.")
    if args.to_stdout:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
