"""Suite-wide isolation from the developer's real home directory.

Every test runs against an empty per-test home: ``Path.home()`` is redirected in-process
(unless the test sets ``HOME`` itself),
and every ``Path`` constant a backend or integration module bound under the real home at
import time (``Path.home() / ".tallybar" / …``) is rewritten to the same place under it.
Without this, any test that reached ``compute_local_cost_summaries`` scanned the real
~/.claude, ~/.codex and ~/.gemini logs and wrote the real parse caches, cost archive and
locks, rewriting the live widget's current-month archive with test data. Tests that need a
specific file still patch the path they exercise. Subprocesses keep the real ``HOME`` (the
offscreen render tests rely on the user's fonts), so a test that runs a script against
state must pass its own ``HOME``.

Between tests the constants rest on a throwaway session home, never the real one: a worker
thread a test left running (a cost scan cut by its timeout is orphaned by design) outlives
the test's monkeypatch, and whatever it then saves goes wherever the constant points.
→ tests/test_test_isolation.py

The Claude Code statusLine capture is read by EVERY build_snapshot call as a fallback, so
an unpatched run would pick up whatever the developer's live hook last wrote — and a test
asserting a failing Claude status would pass or fail depending on whether Claude Code ran
recently on that machine. It keeps its own explicit fixture below.
"""
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CODE = _REPO / "io.github.dlansama.tallybar" / "contents" / "code"
_INTEGRATIONS = _REPO / "integrations"
_REAL_HOME = Path.home()
_SESSION_HOME = Path(tempfile.mkdtemp(prefix="tallybar-tests-home-"))
atexit.register(shutil.rmtree, _SESSION_HOME, ignore_errors=True)
# Homes a module constant may point into: the session home it rests on between tests, the
# real one (a constant built from $HOME rather than Path.home()), and the previous test's
# home (a module first imported DURING a test binds its constants under that test's home,
# and each later test moves them on, so nothing ever points further back).
_HOMES = [_SESSION_HOME, _REAL_HOME, _SESSION_HOME]

sys.path.insert(0, str(_CODE))


def _session_home(cls):
    env = os.environ.get("HOME")
    return Path(env) if env and Path(env) != _REAL_HOME else _SESSION_HOME


# Installed before any backend module is imported, and never undone, so the import-time
# constants bind under the session home and a test's teardown restores them to it.
Path.home = classmethod(_session_home)  # type: ignore[method-assign]


def code_modules():
    """The backend and integration modules loaded so far."""
    roots = (str(_CODE), str(_INTEGRATIONS))
    return [m for m in list(sys.modules.values())
            if str(getattr(m, "__file__", None) or "").startswith(roots)]


def _rest_on_session_home() -> None:
    """Move every module constant still under the real home onto the session home, for good."""
    for module in code_modules():
        for name, value in list(vars(module).items()):
            moved = _rehome(value, _SESSION_HOME, (_REAL_HOME,))
            if moved is not value:
                setattr(module, name, moved)


def _rehome(value, home: Path, homes=None):
    """``value`` moved to ``home`` if it is a Path (or a tuple of Paths) under one of
    ``homes`` (default: every home a constant may point into); the same object otherwise."""
    homes = _HOMES if homes is None else homes
    if isinstance(value, Path):
        if value.is_relative_to(_REPO):  # the checkout may itself live under the home
            return value
        for old in homes:
            if value.is_relative_to(old):
                return home / value.relative_to(old)
        return value
    if isinstance(value, tuple) and value and all(isinstance(v, Path) for v in value):
        moved = tuple(_rehome(v, home, homes) for v in value)
        return moved if moved != value else value
    return value


def pytest_collection_finish(session):
    _rest_on_session_home()


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    home = tmp_path / "_isolated_home"
    home.mkdir()

    def _home(cls):
        # A test that points HOME somewhere itself (to sandbox a script it imports) wins.
        env = os.environ.get("HOME")
        return Path(env) if env and Path(env) != _REAL_HOME else home

    _rest_on_session_home()
    monkeypatch.setattr(Path, "home", classmethod(_home))
    for module in code_modules():
        for name, value in list(vars(module).items()):
            moved = _rehome(value, home)
            if moved is not value:
                monkeypatch.setattr(module, name, moved)
    # The resolved price catalog is a process-wide memo; drop it so no test bills from a
    # catalog another test (or the real pricing cache) bootstrapped.
    import pricing_data
    pricing_data.invalidate_cache()
    _HOMES[2] = home


@pytest.fixture(autouse=True)
def _isolate_claude_statusline(tmp_path, monkeypatch):
    import providers.claude as claude_mod
    monkeypatch.setattr(claude_mod, "CLAUDE_STATUSLINE_PATH", tmp_path / "claude_statusline.json")
