"""Cookie jars are built from ONE unexpired browser profile — the most recently used.

Before: every profile was merged into one jar, the LAST store read (Firefox) won each
(domain, path, name) collision, and expiry was ignored. On the dev machine a Firefox
profile last used four months before the live Chrome one shadowed Chrome's cookies, and
some of the Firefox cookies it sent had already expired.
"""
import asyncio
import sqlite3
import time
from unittest.mock import MagicMock

from cookies import (BrowserCookie, _chromium_time_unix, _unix_time_any_unit, cookiejar_for_domains,
                     has_session_cookies, read_chromium_cookies, read_firefox_cookies,
                     select_session_cookies, CookieStore)

NOW = 1_790_000_000.0  # 2026-09-21


def _c(browser, name, value, *, host=".claude.ai", expires=None, used=None, profile="Default"):
    return BrowserCookie(browser, profile, host, name, "/", value, True,
                         expires_unix=expires, last_used_unix=used)


def test_time_normalization_matches_real_store_formats():
    # Values observed in the dev machine's real stores (2026-09-23).
    assert abs(_chromium_time_unix(13434609475043128) - 1790135875.04) < 1  # us since 1601
    assert _chromium_time_unix(0) is None                                   # session cookie
    assert abs(_unix_time_any_unit(1779234857318) - 1779234857.318) < 1e-3  # Firefox expiry: ms
    assert abs(_unix_time_any_unit(1779234546662825) - 1779234546.66) < 1e-2  # lastAccessed: us
    assert _unix_time_any_unit(1779234857) == 1779234857.0                    # legacy seconds
    assert _unix_time_any_unit(0) is None and _unix_time_any_unit(None) is None


def test_expired_cookies_are_dropped():
    cookies = [_c("Chrome", "sessionKey", "EXPIRED", expires=NOW - 1),
               _c("Chrome", "lastActiveOrg", "ok", expires=NOW + 3600)]
    assert [c.value for c in select_session_cookies(cookies, ("claude.ai",), now=NOW)] == ["ok"]


def test_most_recently_used_profile_wins_regardless_of_read_order():
    live = _c("Chrome", "sessionKey", "LIVE", expires=NOW + 365 * 86400, used=NOW - 60)
    stale = _c("Firefox", "sessionKey", "STALE", expires=NOW + 365 * 86400, used=NOW - 120 * 86400,
               profile="old.default")
    for order in ([live, stale], [stale, live]):
        jar = cookiejar_for_domains(order, ("claude.ai",))
        assert [c.value for c in jar] == ["LIVE"]


def test_profiles_are_never_mixed():
    """Two Google accounts: never splice SID from one profile with HSID from another."""
    a = [_c("Chrome", "SID", "A-sid", host=".google.com", used=NOW - 10),
         _c("Chrome", "HSID", "A-hsid", host=".google.com", used=NOW - 10)]
    b = [_c("Firefox", "SID", "B-sid", host=".google.com", used=NOW - 9_000_000, profile="p"),
         _c("Firefox", "APISID", "B-apisid", host=".google.com", used=NOW - 9_000_000, profile="p")]
    chosen = select_session_cookies(a + b, ("google.com",), now=NOW)
    assert {c.value for c in chosen} == {"A-sid", "A-hsid"}


def test_stores_without_access_times_fall_back_to_cookie_count_then_order():
    few = [_c("Brave", "sessionKey", "brave")]
    many = [_c("Chromium", "sessionKey", "chromium"), _c("Chromium", "lastActiveOrg", "x")]
    assert {c.browser for c in select_session_cookies(few + many, ("claude.ai",), now=NOW)} == {"Chromium"}
    tie = [_c("Brave", "sessionKey", "first"), _c("Edge", "sessionKey", "second")]
    assert [c.value for c in select_session_cookies(tie, ("claude.ai",), now=NOW)] == ["first"]


def test_all_expired_means_missing_cookies_not_an_unauthenticated_request():
    from providers.claude import run_claude_api
    expired = [_c("Chrome", "sessionKey", "old", expires=time.time() - 60)]
    assert has_session_cookies(expired, ("claude.ai",)) is False
    result = asyncio.run(run_claude_api(expired, 1.0))
    assert result["status"] == "missing-cookies"


def test_readers_populate_normalized_times(tmp_path):
    chrome_db = tmp_path / "Cookies"
    con = sqlite3.connect(str(chrome_db))
    con.execute("create table cookies (host_key text, name text, path text, value text, encrypted_value blob, "
                "is_secure integer, expires_utc integer, last_access_utc integer)")
    con.execute("insert into cookies values ('.claude.ai','sessionKey','/','v', x'', 1, "
                "13469166523089900, 13434609475043128)")
    con.commit()
    con.close()
    [c], _ = read_chromium_cookies(CookieStore("Chrome", "chrome", "Default", chrome_db), MagicMock(), {}, ())
    assert abs(c.last_used_unix - 1790135875.04) < 1 and c.expires_unix > c.last_used_unix

    ff_db = tmp_path / "cookies.sqlite"
    con = sqlite3.connect(str(ff_db))
    con.execute("create table moz_cookies (host text, name text, path text, value text, isSecure integer, "
                "expiry integer, lastAccessed integer)")
    con.execute("insert into moz_cookies values ('.claude.ai','sessionKey','/','v',1,1813793236218,1779234546662825)")
    con.commit()
    con.close()
    [f], _ = read_firefox_cookies(ff_db, ())
    assert abs(f.expires_unix - 1813793236.218) < 1e-3       # ms expiry normalized
    assert abs(f.last_used_unix - 1779234546.66) < 1e-2

    # Older Firefox schema without lastAccessed still reads (alias fallback).
    old_db = tmp_path / "old.sqlite"
    con = sqlite3.connect(str(old_db))
    con.execute("create table moz_cookies (host text, name text, path text, value text, isSecure integer, expiry integer)")
    con.execute("insert into moz_cookies values ('.claude.ai','sessionKey','/','v',1,1813793236)")
    con.commit()
    con.close()
    [o], _ = read_firefox_cookies(old_db, ())
    assert o.last_used_unix is None and o.expires_unix == 1813793236.0
