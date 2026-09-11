"""Tests for io_helpers.sweep_stale_temp_files — best-effort deletion of orphaned
atomic-write temps (crashed mkstemp leftovers + .tmp recipe files), never touching
.bak/.corrupt/.json or symlinks/dirs, never recursing, never raising."""

import os
import sys
import time
from pathlib import Path

CODE_DIR = Path(__file__).parent.parent / "io.github.dlansama.tallybar" / "contents" / "code"
sys.path.insert(0, str(CODE_DIR))

import io_helpers  # noqa: E402

STALE = 10 * 86400  # 10 days ago
MAX_AGE = 86400


def _age(path: Path, seconds_ago: float) -> None:
    t = time.time() - seconds_ago
    os.utime(path, (t, t))


def test_sweep_removes_only_stale_matching_regular_files(tmp_path):
    # Two stale matching regular files — must be removed.
    mkstemp_orphan = tmp_path / "tmpAbCd1234"
    mkstemp_orphan.write_text("x")
    _age(mkstemp_orphan, STALE)

    dot_tmp = tmp_path / "foo.json.abcd1234.tmp"
    dot_tmp.write_text("x")
    _age(dot_tmp, STALE)

    # Fresh .tmp — matches name but too new, must survive.
    fresh_tmp = tmp_path / "bar.json.zzzz9999.tmp"
    fresh_tmp.write_text("x")
    _age(fresh_tmp, 0)

    # Stale symlink named like an mkstemp orphan — must NOT be followed/removed.
    symlink_target = tmp_path / "real_target"
    symlink_target.write_text("t")
    stale_symlink = tmp_path / "tmpaaaaaaaa"
    stale_symlink.symlink_to(symlink_target)
    _age(symlink_target, STALE)

    # Stale ledger.json / .bak / .corrupt — non-matching, must survive.
    ledger = tmp_path / "ledger.json"
    ledger.write_text("l")
    _age(ledger, STALE)
    bak = tmp_path / "ledger.json.bak"
    bak.write_text("b")
    _age(bak, STALE)
    corrupt = tmp_path / "ledger.json.corrupt"
    corrupt.write_text("c")
    _age(corrupt, STALE)

    # Stale subdirectory named like an mkstemp orphan — must NOT be removed (not regular).
    subdir = tmp_path / "tmpbbbbbbbb"
    subdir.mkdir()
    _age(subdir, STALE)

    removed = io_helpers.sweep_stale_temp_files((tmp_path,), max_age_seconds=MAX_AGE)

    assert removed == 2
    assert not mkstemp_orphan.exists()
    assert not dot_tmp.exists()
    # Everything else survives.
    assert fresh_tmp.exists()
    assert stale_symlink.is_symlink()
    assert symlink_target.exists()
    assert ledger.exists()
    assert bak.exists()
    assert corrupt.exists()
    assert subdir.is_dir()


def test_sweep_does_not_recurse(tmp_path):
    subdir = tmp_path / "sub"
    subdir.mkdir()
    nested = subdir / "tmpAbCd1234"
    nested.write_text("x")
    _age(nested, STALE)

    removed = io_helpers.sweep_stale_temp_files((tmp_path,), max_age_seconds=MAX_AGE)
    assert removed == 0
    assert nested.exists()


def test_sweep_nonexistent_dir_is_harmless(tmp_path):
    missing = tmp_path / "does-not-exist"
    # Must not raise and must report zero.
    assert io_helpers.sweep_stale_temp_files((missing,)) == 0


def test_sweep_prefix_mode_only_removes_matching_prefix_tmp(tmp_path):
    # Prefix mode narrows a third-party dir: only <prefix>*.tmp is eligible.
    match = tmp_path / "oauth_creds.json.abcd1234.tmp"
    match.write_text("x")
    _age(match, STALE)

    # Stale NON-matching .tmp (another tool's file) — must survive in prefix mode.
    other_tmp = tmp_path / "somethingelse.abcd1234.tmp"
    other_tmp.write_text("x")
    _age(other_tmp, STALE)

    # Stale bare mkstemp orphan — the generic rule does NOT apply in prefix mode, must survive.
    mkstemp_orphan = tmp_path / "tmpAbCd1234"
    mkstemp_orphan.write_text("x")
    _age(mkstemp_orphan, STALE)

    # A file with the prefix but NOT ending .tmp — must survive.
    prefix_no_tmp = tmp_path / "oauth_creds.json.bak"
    prefix_no_tmp.write_text("x")
    _age(prefix_no_tmp, STALE)

    removed = io_helpers.sweep_stale_temp_files(
        (tmp_path,), max_age_seconds=MAX_AGE, only_prefix="oauth_creds.json.")

    assert removed == 1
    assert not match.exists()
    assert other_tmp.exists()
    assert mkstemp_orphan.exists()
    assert prefix_no_tmp.exists()


def test_sweep_prefix_mode_respects_age(tmp_path):
    # Fresh prefix-matching file is spared.
    fresh = tmp_path / "antigravity-oauth-token.zzzz9999.tmp"
    fresh.write_text("x")
    _age(fresh, 0)
    removed = io_helpers.sweep_stale_temp_files(
        (tmp_path,), max_age_seconds=MAX_AGE, only_prefix="antigravity-oauth-token.")
    assert removed == 0
    assert fresh.exists()


def test_sweep_bare_mkstemp_needs_exactly_eight_chars(tmp_path):
    # "tmp" + 8 word chars matches; other lengths do not (fullmatch).
    good = tmp_path / "tmp12345678"
    good.write_text("x")
    _age(good, STALE)
    too_short = tmp_path / "tmp1234567"
    too_short.write_text("x")
    _age(too_short, STALE)

    removed = io_helpers.sweep_stale_temp_files((tmp_path,), max_age_seconds=MAX_AGE)
    assert removed == 1
    assert not good.exists()
    assert too_short.exists()
