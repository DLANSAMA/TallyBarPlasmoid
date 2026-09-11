"""Filesystem-hardening regression tests: sqlite read-only URI quoting, cookie
temp-dir placement under XDG_RUNTIME_DIR, and the stale cookie-dir sweep."""
import os
import sqlite3
import stat as stat_mod
import time
import urllib.parse
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "io.github.dlansama.tallybar" / "contents" / "code"))

import cookies  # noqa: E402


# --- (a) sqlite URI quoting: '?'/'#' in filename can't smuggle URI params ------

def _make_ro_db(path: Path) -> None:
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE t (x INTEGER)")
    con.execute("INSERT INTO t VALUES (1)")
    con.commit()
    con.close()


def test_quoted_uri_opens_read_only_with_special_chars(tmp_path):
    # A filename containing '?' and '#' — unquoted these would smuggle URI params
    # past mode=ro (or point the connection at the wrong file entirely).
    db_path = tmp_path / "weird?name#frag.db"
    _make_ro_db(db_path)

    uri = f"file:{urllib.parse.quote(str(db_path))}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        # Reads the intended file...
        assert con.execute("SELECT x FROM t").fetchone()[0] == 1
        # ...and the connection is genuinely read-only.
        with pytest.raises(sqlite3.OperationalError):
            con.execute("INSERT INTO t VALUES (2)")
    finally:
        con.close()


def test_unquoted_special_char_uri_is_not_read_only(tmp_path):
    # Guards the fix: the OLD unquoted form lets a '?' in the name break out of
    # mode=ro (the '#' fragment truncates the path so the file: query is ignored),
    # yielding a writable connection. The quoted form above must NOT.
    db_path = tmp_path / "hole#x.db"
    _make_ro_db(db_path)
    bad = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        # The '#' truncates the URI path, so mode=ro never applies: write succeeds.
        bad.execute("CREATE TABLE probe (y)")  # would raise if truly read-only
    finally:
        bad.close()


# --- (b) cookie temp-dir placement -------------------------------------------

def test_cookie_tmp_lands_in_xdg_runtime_dir(tmp_path, monkeypatch):
    runtime = tmp_path / "run"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))

    src = tmp_path / "Cookies"
    _make_ro_db(src)

    with cookies.sqlite_copy(src) as tmp:
        assert tmp is not None
        # The temp copy lives inside XDG_RUNTIME_DIR.
        assert str(tmp).startswith(str(runtime) + os.sep)


def test_cookie_tmp_falls_back_when_xdg_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    src = tmp_path / "Cookies"
    _make_ro_db(src)
    with cookies.sqlite_copy(src) as tmp:
        assert tmp is not None
        assert tmp.name == "Cookies"


def test_cookie_tmp_falls_back_when_xdg_nonexistent(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "does-not-exist"))
    src = tmp_path / "Cookies"
    _make_ro_db(src)
    with cookies.sqlite_copy(src) as tmp:
        assert tmp is not None  # falls back cleanly to default tempdir


# --- (c) stale cookie-dir sweep ----------------------------------------------

def test_sweep_removes_stale_owned_dir_and_spares_others(tmp_path, monkeypatch):
    # Point BOTH candidate bases at tmp_path so the sweep scans it.
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(cookies.tempfile, "gettempdir", lambda: str(tmp_path))

    old = 2 * 3600

    # Stale owned dir -> removed.
    stale = tmp_path / "tallybar-cookie-stale"
    stale.mkdir()
    os.utime(stale, (time.time() - old, time.time() - old))

    # Fresh dir -> kept.
    fresh = tmp_path / "tallybar-cookie-fresh"
    fresh.mkdir()

    # Symlink named like a cookie dir, pointing at a real dir -> never followed/deleted.
    real_target = tmp_path / "real-target"
    real_target.mkdir()
    (real_target / "sentinel").write_text("keep me")
    link = tmp_path / "tallybar-cookie-x"
    link.symlink_to(real_target)
    os.utime(link, (time.time() - old, time.time() - old), follow_symlinks=False)

    # Regular file named like a cookie dir -> left alone.
    regfile = tmp_path / "tallybar-cookie-y"
    regfile.write_text("data")
    os.utime(regfile, (time.time() - old, time.time() - old))

    cookies._sweep_stale_cookie_dirs()

    assert not stale.exists()
    assert fresh.exists()
    assert link.is_symlink()
    assert (real_target / "sentinel").exists()
    assert regfile.exists()
    # Confirm the symlink was genuinely a symlink and untouched.
    assert stat_mod.S_ISLNK(os.lstat(link).st_mode)


def test_sweep_never_raises_on_bad_base(monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/nonexistent/definitely/not/here")
    monkeypatch.setattr(cookies.tempfile, "gettempdir", lambda: "/nonexistent/either")
    cookies._sweep_stale_cookie_dirs()  # must not raise
