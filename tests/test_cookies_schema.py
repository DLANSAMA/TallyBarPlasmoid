"""read_chromium_cookies schema-detection: the value/is_secure/expires_utc column
fallbacks (cookies.py:244-247). If a fallback regressed, every provider would silently
go 'missing-cookies'. Uses a real on-disk Cookies SQLite DB + plaintext cookies (so no
decryption is needed) and domain_filter=() to match all rows."""

import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

CODE_DIR = Path(__file__).parent.parent / "io.github.dlansama.tallybar" / "contents" / "code"
sys.path.insert(0, str(CODE_DIR))

from cookies import read_chromium_cookies, CookieStore  # noqa: E402


def _make_cookies_db(path, with_optional_cols=True):
    con = sqlite3.connect(str(path))
    if with_optional_cols:
        con.execute("create table cookies (host_key text, name text, path text, "
                    "value text, encrypted_value blob, is_secure integer, expires_utc integer)")
        con.execute("insert into cookies values "
                    "('example.com','sid','/','plain-value', x'', 1, 99)")
    else:  # is_secure / expires_utc absent -> the 0-alias fallback must kick in
        con.execute("create table cookies (host_key text, name text, path text, "
                    "value text, encrypted_value blob)")
        con.execute("insert into cookies values ('example.com','sid','/','plain-value', x'')")
    con.commit()
    con.close()


def _store(db):
    return CookieStore(browser="Chrome", family="chromium", profile="Default", path=db)


def test_read_chromium_cookies_full_schema(tmp_path):
    db = tmp_path / "Cookies"
    _make_cookies_db(db, with_optional_cols=True)
    cookies, stats = read_chromium_cookies(_store(db), MagicMock(), {}, ())
    assert stats["rows"] == 1 and stats["errors"] == 0
    assert len(cookies) == 1
    c = cookies[0]
    assert c.host == "example.com" and c.value == "plain-value"
    assert c.secure is True and c.expires_utc == 99


def test_read_chromium_cookies_missing_optional_columns(tmp_path):
    db = tmp_path / "Cookies"
    _make_cookies_db(db, with_optional_cols=False)
    cookies, stats = read_chromium_cookies(_store(db), MagicMock(), {}, ())
    assert stats["rows"] == 1 and stats["errors"] == 0       # schema fallback, no crash
    assert len(cookies) == 1
    assert cookies[0].value == "plain-value"
    assert cookies[0].secure is False and cookies[0].expires_utc == 0


def test_chromium_key_candidates_tries_electron_before_generic():
    # The Antigravity Gemini (Electron) profile must try its own key ahead of the merged
    # generic pool, and family-first ordering must hold.
    from cookies import chromium_key_candidates
    passwords = {"electron": [b"e"], "chrome": [b"c"], "generic": [b"e", b"c", b"g"], "antigravity": []}
    cands = chromium_key_candidates("electron", passwords)
    assert cands[0] == b"e"                     # family first
    assert cands.index(b"e") < cands.index(b"g")  # electron key precedes the generic-only pool
    assert b"peanuts" in cands                  # hardcoded Chromium fallback still appended
