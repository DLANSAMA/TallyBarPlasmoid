"""End-to-end coverage for cookies.py paths that test_cookies_schema.py never touches:

decrypt_chromium_value's CBC/GCM decrypt-and-retry logic (test_cookies_schema.py's DB
fixtures seed the `value` column non-empty/plaintext, so `read_chromium_cookies`'s
`if not value and encrypted:` guard never fires and decrypt_chromium_value is never
called even indirectly), plus the cookie-store discovery/collection orchestration
(discover_cookie_stores, discover_firefox_cookie_stores, cookiejar_for_domains,
_open_kwallet_and_collect_passwords, collect_browser_sessions) which previously had
zero direct test coverage (test_backend.py only ever patches collect_browser_sessions
wholesale).
"""

import hashlib
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

CODE_DIR = Path(__file__).parent.parent / "io.github.dlansama.tallybar" / "contents" / "code"
sys.path.insert(0, str(CODE_DIR))

import cookies  # noqa: E402
from cookies import (  # noqa: E402
    BrowserCookie,
    _open_kwallet_and_collect_passwords,
    collect_browser_sessions,
    cookiejar_for_domains,
    decrypt_chromium_value,
    discover_cookie_stores,
    discover_firefox_cookie_stores,
)
from crypto import LINUX_IV, SALT, OpenSslEvp  # noqa: E402

_OPENSSL = shutil.which("openssl")


@pytest.fixture
def evp():
    e = OpenSslEvp()
    if not e.available:
        pytest.skip("libcrypto unavailable")
    return e


def _encrypt_v10_cbc(password: bytes, plaintext: bytes) -> bytes:
    """Build a real Chromium-shaped v10 encrypted cookie value: PBKDF2-HMAC-SHA1(password,
    'saltysalt', 1 iter, 16 bytes) -> AES-128-CBC with Chromium's fixed all-space IV."""
    key = hashlib.pbkdf2_hmac("sha1", password, SALT, 1, 16)
    ct = subprocess.run(
        [_OPENSSL, "enc", "-aes-128-cbc", "-K", key.hex(), "-iv", LINUX_IV.hex()],
        input=plaintext, capture_output=True, check=True,
    ).stdout
    return b"v10" + ct


# --- decrypt_chromium_value -------------------------------------------------------

def test_decrypt_chromium_value_empty_bytes_returns_empty_string():
    assert decrypt_chromium_value(MagicMock(), "example.com", b"", "chrome", {}) == ""


def test_decrypt_chromium_value_unversioned_falls_back_to_plaintext_decode():
    # No v10/v11 prefix -> decode_plaintext(host, encrypted) path, not the crypto path at all.
    result = decrypt_chromium_value(MagicMock(), "example.com", b"already-plaintext", "chrome", {})
    assert result == "already-plaintext"


@pytest.mark.skipif(_OPENSSL is None, reason="openssl CLI not available")
def test_decrypt_chromium_value_v10_cbc_real_roundtrip(evp):
    # Real end-to-end CBC decrypt through the actual production entry point (not just
    # OpenSslEvp directly): correct family-bucketed password -> successful decode.
    password = b"correct-horse-battery-staple"
    plaintext = b"session-cookie-secret-value-1234"
    encrypted = _encrypt_v10_cbc(password, plaintext)
    passwords = {"chrome": [password]}

    result = decrypt_chromium_value(evp, "example.com", encrypted, "chrome", passwords)
    assert result == plaintext.decode()


@pytest.mark.skipif(_OPENSSL is None, reason="openssl CLI not available")
def test_decrypt_chromium_value_wrong_password_returns_none(evp):
    # A real v10 CBC ciphertext, but every candidate key is wrong: the CBC attempt's
    # padding/decode fails (caught by the inner `except Exception: pass`), the GCM
    # fallback (body is long enough to attempt) also fails on both sha1/sha256 key
    # derivations, and the function falls through every candidate to return None --
    # exactly the "real key isn't available" fallback path.
    encrypted = _encrypt_v10_cbc(b"the-real-password", b"a-fairly-long-secret-cookie-value-here")
    passwords = {"chrome": [b"totally-wrong-password"], "generic": [b"another-wrong-one"]}

    result = decrypt_chromium_value(evp, "example.com", encrypted, "chrome", passwords)
    assert result is None


def test_decrypt_chromium_value_no_matching_password_returns_none():
    # No candidate passwords supplied at all beyond the hardcoded "peanuts" fallback --
    # still must not raise, just report undecryptable.
    evp = OpenSslEvp()
    if not evp.available:
        pytest.skip("libcrypto unavailable")
    result = decrypt_chromium_value(evp, "example.com", b"v11" + b"\x00" * 40, "chrome", {})
    assert result is None


# --- discover_cookie_stores / discover_firefox_cookie_stores -----------------------

def test_discover_cookie_stores_finds_and_dedupes(tmp_path):
    home = tmp_path
    chrome_default = home / ".config" / "google-chrome" / "Default"
    chrome_default.mkdir(parents=True)
    (chrome_default / "Cookies").write_bytes(b"sqlite-stub")

    chrome_network = home / ".config" / "google-chrome" / "Default" / "Network"
    chrome_network.mkdir(parents=True)
    (chrome_network / "Cookies").write_bytes(b"sqlite-stub")

    chromium_profile = home / ".config" / "chromium" / "Profile 1"
    chromium_profile.mkdir(parents=True)
    (chromium_profile / "Cookies").write_bytes(b"sqlite-stub")

    # A directory literally named "Cookies" must be skipped by the is_file() guard,
    # not mistaken for a store.
    brave_default = home / ".config" / "BraveSoftware" / "Brave-Browser" / "Default"
    brave_default.mkdir(parents=True)
    (brave_default / "Cookies").mkdir()

    stores = discover_cookie_stores(home)
    by_path = {str(s.path): s for s in stores}

    assert str(chrome_default / "Cookies") in by_path
    assert by_path[str(chrome_default / "Cookies")].browser == "Chrome"
    assert by_path[str(chrome_default / "Cookies")].profile == "Default"

    # Network/Cookies must resolve its profile from the grandparent, not "Network".
    net_key = str(chrome_network / "Cookies")
    assert net_key in by_path
    assert by_path[net_key].profile == "Default"

    chromium_key = str(chromium_profile / "Cookies")
    assert chromium_key in by_path
    assert by_path[chromium_key].browser == "Chromium"
    assert by_path[chromium_key].profile == "Profile 1"

    # The directory-named-Cookies path must be absent (skipped, not crashed on).
    assert str(brave_default / "Cookies") not in by_path

    # No duplicate Path entries.
    paths = [s.path for s in stores]
    assert len(paths) == len(set(paths))


def test_discover_firefox_cookie_stores_finds_profiles(tmp_path):
    home = tmp_path
    profile = home / ".mozilla" / "firefox" / "abc123.default-release"
    profile.mkdir(parents=True)
    (profile / "cookies.sqlite").write_bytes(b"sqlite-stub")

    # A non-file (directory) must not be reported.
    decoy = home / ".mozilla" / "firefox" / "decoy.default"
    decoy.mkdir(parents=True)
    (decoy / "cookies.sqlite").mkdir()

    paths = discover_firefox_cookie_stores(home)
    assert paths == [profile / "cookies.sqlite"]


# --- cookiejar_for_domains -----------------------------------------------------------

def test_cookiejar_for_domains_filters_and_normalizes_domain():
    cookies_list = [
        BrowserCookie(browser="Chrome", profile="Default", host="claude.ai", name="sid",
                      path="/", value="abc123", secure=True, expires_utc=0),
        BrowserCookie(browser="Chrome", profile="Default", host="unrelated.com", name="sid",
                      path="/", value="xyz", secure=False, expires_utc=0),
    ]
    jar = cookiejar_for_domains(cookies_list, ("claude.ai",))
    entries = list(jar)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.name == "sid"
    assert entry.value == "abc123"
    assert entry.domain == ".claude.ai"
    assert entry.secure is True


# --- _open_kwallet_and_collect_passwords --------------------------------------------

class _FakeKWallet:
    """Minimal KWalletClient stand-in that records whether diagnostics() was called
    before close() -- the ordering the real code depends on (see the comment above
    _open_kwallet_and_collect_passwords in cookies.py)."""

    def __init__(self, background: bool = False) -> None:
        self.background = background
        self.closed = False

    def __enter__(self) -> "_FakeKWallet":
        return self

    def __exit__(self, *exc_info) -> None:
        self.closed = True

    def safe_storage_passwords(self) -> dict[str, list[bytes]]:
        return {"chrome": [b"fake-password"]}

    def diagnostics(self, passwords=None) -> dict:
        return {"available": not self.closed, "background": self.background,
                "keys": {k: len(v) for k, v in (passwords or {}).items()}}


def test_open_kwallet_and_collect_passwords_diagnostics_before_close(monkeypatch):
    monkeypatch.setattr(cookies, "KWalletClient", _FakeKWallet)

    passwords, diagnostics = _open_kwallet_and_collect_passwords(background=True)

    assert passwords == {"chrome": [b"fake-password"]}
    # This is the load-bearing assertion: diagnostics() must see the wallet as still
    # "available" (not yet closed) because it was built inside the `with` block.
    assert diagnostics["available"] is True
    assert diagnostics["background"] is True
    assert diagnostics["keys"] == {"chrome": 1}


# --- collect_browser_sessions (full async pipeline) ---------------------------------

def _make_plaintext_chromium_db(path: Path, host: str, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute(
        "create table cookies (host_key text, name text, path text, value text, "
        "encrypted_value blob, is_secure integer, expires_utc integer)"
    )
    con.execute(
        "insert into cookies values (?, 'sid', '/', ?, x'', 1, 0)",
        (host, value),
    )
    con.commit()
    con.close()


@pytest.mark.asyncio
async def test_collect_browser_sessions_end_to_end(tmp_path, monkeypatch):
    home = tmp_path
    monkeypatch.setattr(cookies.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(cookies, "KWalletClient", _FakeKWallet)

    db_path = home / ".config" / "google-chrome" / "Default" / "Cookies"
    _make_plaintext_chromium_db(db_path, "claude.ai", "plaintext-session-value")

    found_cookies, diagnostics = await collect_browser_sessions(timeout=5.0, background=True)

    assert len(found_cookies) == 1
    cookie = found_cookies[0]
    assert cookie.host == "claude.ai"
    assert cookie.value == "plaintext-session-value"
    assert cookie.browser == "Chrome"

    assert diagnostics["matchedCookies"] == 1
    assert diagnostics["kwallet"]["available"] is True
    assert len(diagnostics["stores"]) == 1
    assert diagnostics["stores"][0]["matched"] == 1
