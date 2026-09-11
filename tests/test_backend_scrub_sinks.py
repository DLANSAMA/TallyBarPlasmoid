"""The backend orchestrator's diagnostic sinks embed raw str(exc); several land in
diagnostics dicts (and get_task_result's lands in a provider 'message' that can reach a
desktop-notification body). Any of those could leak a bearer token / OAuth secret from an
exception string. Assert the reachable sinks route through scrub_credentials + length cap,
and that the coordination lock files are created 0600."""

import os
import stat
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

CODE_DIR = Path(__file__).parent.parent / "io.github.dlansama.tallybar" / "contents" / "code"
sys.path.insert(0, str(CODE_DIR))

import backend  # noqa: E402

SECRET = "Bearer ya29.SECRETTOKENVALUE1234567890"
REDACTED = "[REDACTED]"


def _args():
    args = MagicMock()
    args.no_network = False
    args.timeout = 0.05
    return args


async def _benign_rpc(*a, **k):
    return {"label": "Codex", "status": "not-running", "limits": []}


async def _benign_threaded(func, *a, **k):
    return {"status": "ok", "limits": []}


@pytest.mark.asyncio
async def test_browser_sink_scrubs_and_caps():
    # collect_browser_sessions failing must land a SCRUBBED, capped message in
    # diagnostics['browser'] (sink at backend.py cookie-collection except).
    async def boom(*a, **k):
        raise RuntimeError("cookie read failed: " + SECRET + " " + "x" * 400)

    with patch("backend.load_config", return_value={}), \
         patch("backend.collect_browser_sessions", side_effect=boom), \
         patch("backend.run_threaded_provider", side_effect=_benign_threaded), \
         patch("backend.run_codex_rpc", side_effect=_benign_rpc), \
         patch("backend.compute_local_cost_summaries", side_effect=lambda deadline=None: {}):
        res = await backend.build_snapshot(_args())
    msg = res["diagnostics"]["browser"]["message"]
    assert "ya29." not in msg
    assert REDACTED in msg
    assert len(msg) <= 160


@pytest.mark.asyncio
async def test_cost_summary_error_sink_scrubs_and_caps():
    # A cost-scan failure lands in diagnostics['cost_summary_error']; must be scrubbed + capped.
    def raise_secret(deadline=None):
        raise ValueError("cost scan blew up with token " + SECRET + " " + "y" * 400)

    with patch("backend.load_config", return_value={}), \
         patch("backend.collect_browser_sessions", return_value=([], {})), \
         patch("backend.run_threaded_provider", side_effect=_benign_threaded), \
         patch("backend.run_codex_rpc", side_effect=_benign_rpc), \
         patch("backend.compute_local_cost_summaries", side_effect=raise_secret):
        res = await backend.build_snapshot(_args())
    diag = res["diagnostics"]["cost_summary_error"]
    assert "ya29." not in diag
    assert REDACTED in diag
    assert len(diag) <= 160


@pytest.mark.asyncio
async def test_orchestrator_error_sink_scrubs_and_caps():
    # A non-group Exception escaping the TaskGroup block lands in
    # diagnostics['orchestrator_error']. Force it by making the cookie-collection succeed but a
    # provider task path raise a plain Exception via run_codex_rpc coroutine that raises before
    # the group can bundle — simplest: patch asyncio.TaskGroup out is fragile, so instead drive
    # the get_task_result sink, which shares the same str(exc) shape and is the notification-
    # reachable one.
    async def boom_rpc(*a, **k):
        raise RuntimeError("codex rpc failed: " + SECRET + " " + "z" * 400)

    # run_codex_rpc raises inside bounded_provider, which returns the fallback rather than
    # raising, so this exercises get_task_result only if the task itself carries an exception.
    # bounded_provider swallows to fallback; to hit get_task_result's exc branch we let the
    # antigravity threaded task's future carry the exception.
    async def boom_threaded(func, *a, **k):
        if func.__name__.startswith("run_antigravity_local"):
            raise RuntimeError("antigravity failed: " + SECRET + " " + "q" * 400)
        return {"status": "ok", "limits": []}

    with patch("backend.load_config", return_value={}), \
         patch("backend.collect_browser_sessions", return_value=([], {})), \
         patch("backend.run_threaded_provider", side_effect=boom_threaded), \
         patch("backend.run_codex_rpc", side_effect=_benign_rpc), \
         patch("backend.compute_local_cost_summaries", side_effect=lambda deadline=None: {}):
        res = await backend.build_snapshot(_args())
    # The TaskGroup bundles the child RuntimeError; a single-Exception group surfaces via the
    # orchestrator_error diagnostic (capped [:300]). It must be scrubbed.
    orch = res["diagnostics"]["orchestrator_error"]
    assert "ya29." not in orch
    assert REDACTED in orch
    assert len(orch) <= 300


def test_lock_files_created_0600(tmp_path):
    lock_path = tmp_path / ".notify.lock"
    with backend._open_lock_0600(lock_path) as fd:
        assert fd.writable()
    mode = stat.S_IMODE(os.stat(lock_path).st_mode)
    assert mode == 0o600

    # Pre-existing 0644 lockfile is tightened to 0600 on open.
    loose = tmp_path / ".config.lock"
    loose.write_text("")
    os.chmod(loose, 0o644)
    with backend._open_lock_0600(loose):
        pass
    assert stat.S_IMODE(os.stat(loose).st_mode) == 0o600
