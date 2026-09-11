"""The Claude Code statusLine capture integration.

Driven as a real subprocess with an isolated HOME, because that is exactly how
Claude Code invokes it — piping session JSON to stdin and rendering stdout.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "integrations" / "claude_code" / "statusline_capture.py"

# Verbatim from https://code.claude.com/docs/en/statusline (checked 2026-09-11).
SAMPLE = {
    "model": {"display_name": "Opus"},
    "rate_limits": {
        "five_hour": {"used_percentage": 23.5, "resets_at": 1738425600},
        "seven_day": {"used_percentage": 41.2, "resets_at": 1738857600},
        "spend_limit": {"used_percentage": 62.8, "resets_at": 1740787200},
    },
}


def _run(payload, home):
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home)}
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run([sys.executable, str(SCRIPT)], input=text, env=env,
                          capture_output=True, text=True, timeout=30)


def _state(home):
    return json.loads((home / ".tallybar" / "claude_statusline.json").read_text())


def test_captures_every_documented_window(tmp_path):
    proc = _run(SAMPLE, tmp_path)
    assert proc.returncode == 0
    state = _state(tmp_path)
    assert set(state["rateLimits"]) == {"five_hour", "seven_day", "spend_limit"}
    assert state["rateLimits"]["five_hour"] == {"usedPercent": 23.5, "resetsAt": 1738425600.0}
    # capturedAt exists so the backend can AGE the reading rather than assume it is
    # current — the mistake that had Grok's bar presenting a 15h-old number as fresh.
    assert state["capturedAt"]
    assert "5h 24%" in proc.stdout      # still usable as an actual status line


def test_written_state_is_owner_only(tmp_path):
    _run(SAMPLE, tmp_path)
    path = tmp_path / ".tallybar" / "claude_statusline.json"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"


def test_absent_rate_limits_does_not_clobber_a_good_capture(tmp_path):
    """rate_limits is absent until the session's first API response. Writing an
    empty capture then would blind the widget for no reason."""
    _run(SAMPLE, tmp_path)
    proc = _run({"model": {"display_name": "Opus"}}, tmp_path)
    assert proc.returncode == 0
    assert set(_state(tmp_path)["rateLimits"]) == {"five_hour", "seven_day", "spend_limit"}


def test_partial_windows_are_kept(tmp_path):
    """Each window may be independently absent — Claude Code drops one once its
    resets_at passes, which means 'expired', not 'zero'."""
    _run({"rate_limits": {"seven_day": {"used_percentage": 12.0}}}, tmp_path)
    state = _state(tmp_path)
    assert set(state["rateLimits"]) == {"seven_day"}
    assert state["rateLimits"]["seven_day"] == {"usedPercent": 12.0}   # no resetsAt key


@pytest.mark.parametrize("junk", ["", "not json", "[]", '{"rate_limits": "nope"}',
                                  '{"rate_limits": {"five_hour": {"used_percentage": null}}}'])
def test_never_fails_on_bad_input(tmp_path, junk):
    """A statusLine command that errors breaks Claude Code's status bar."""
    proc = _run(junk, tmp_path)
    assert proc.returncode == 0
    assert proc.stdout.strip()
    assert not (tmp_path / ".tallybar" / "claude_statusline.json").exists()


def test_booleans_are_not_accepted_as_percentages(tmp_path):
    """bool is a subclass of int; True would otherwise render as 100%."""
    _run({"rate_limits": {"five_hour": {"used_percentage": True}}}, tmp_path)
    assert not (tmp_path / ".tallybar" / "claude_statusline.json").exists()
