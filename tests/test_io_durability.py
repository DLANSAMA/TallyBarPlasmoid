"""Tests for the 2026-05-31 external-audit hardening: the shared fsync_dir helper,
the atomic 0600 OAuth-credential write (mkstemp, SEC-2/SEC-6), and the Gemini in-fetch
timeout classification (ERR-2)."""

import json
import socket
import stat
import sys
import urllib.error
from pathlib import Path

CODE_DIR = Path(__file__).parent.parent / "io.github.dlansama.tallybar" / "contents" / "code"
sys.path.insert(0, str(CODE_DIR))

import io_helpers  # noqa: E402
import providers.antigravity as agmod  # noqa: E402
import providers.gemini as gmod  # noqa: E402


def test_fsync_dir_is_best_effort(tmp_path):
    # Real dir: must not raise. Bogus path: must not raise (best-effort, swallows OSError).
    io_helpers.fsync_dir(tmp_path)
    io_helpers.fsync_dir(tmp_path / "does-not-exist")


def test_save_antigravity_credentials_atomic_0600(tmp_path):
    path = tmp_path / "nested" / "creds.json"
    agmod.save_antigravity_credentials(path, {"not_a_token": True}, {"access_token": "abc"})
    # File exists, is exactly 0600, holds valid JSON, and left no temp file behind.
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert isinstance(json.loads(path.read_text()), dict)
    leftovers = [p.name for p in path.parent.iterdir() if p.name != "creds.json"]
    assert leftovers == []


def test_save_antigravity_credentials_merges_token(tmp_path):
    path = tmp_path / "creds.json"
    raw = {"token": {"access_token": "old", "token_type": "Bearer"}}
    agmod.save_antigravity_credentials(path, raw, {"access_token": "new", "refresh_token": "r"})
    data = json.loads(path.read_text())
    assert data["token"]["access_token"] == "new"
    assert data["token"]["refresh_token"] == "r"


def test_gemini_is_timeout_exc():
    assert gmod._is_timeout_exc(TimeoutError()) is True
    assert gmod._is_timeout_exc(socket.timeout()) is True
    assert gmod._is_timeout_exc(urllib.error.URLError(socket.timeout())) is True
    assert gmod._is_timeout_exc(urllib.error.URLError(TimeoutError())) is True
    # Non-timeouts must NOT be classified as timeouts.
    assert gmod._is_timeout_exc(ValueError("boom")) is False
    assert gmod._is_timeout_exc(urllib.error.URLError(ConnectionRefusedError())) is False


def test_no_duplicate_atomic_write_definitions():
    # ARCH-4 (extended 2026-07): the fsync/atomic-write recipe must live ONLY in
    # io_helpers — guard against a future re-introduction of a local copy in the
    # writers. backend's _atomic_write_text must BE the shared helper, not a fork.
    import accounting
    import backend
    import pricing_data
    import providers.cost as costmod
    for mod in (backend, pricing_data, costmod, agmod, accounting):
        assert not hasattr(mod, "_fsync_dir"), f"{mod.__name__} reintroduced a local _fsync_dir"
        assert getattr(mod, "fsync_dir", io_helpers.fsync_dir) is io_helpers.fsync_dir
    assert backend._atomic_write_text is io_helpers.atomic_write_text
    for mod in (pricing_data, costmod, agmod, accounting):
        assert getattr(mod, "atomic_write_text", io_helpers.atomic_write_text) is io_helpers.atomic_write_text


def test_atomic_write_text_perms_and_no_leftovers(tmp_path):
    # The one shared recipe: 0600 target, 0700 parent, no orphaned temp files.
    target = tmp_path / "sub" / "file.json"
    io_helpers.atomic_write_text(target, "{}")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert [p.name for p in target.parent.iterdir()] == ["file.json"]
