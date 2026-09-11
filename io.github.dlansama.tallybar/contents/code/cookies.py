"""Browser cookie extraction: Chromium, Firefox, and session aggregation.

Reads cookies from browser SQLite stores, decrypts Chromium-encrypted values
using KWallet-stored safe-storage passwords, and assembles cookie jars for
HTTP requests to provider APIs.
"""
from __future__ import annotations

import contextlib
import dataclasses
import glob
import hashlib
import os
import shutil
import sqlite3
import stat
import tempfile
import time
import urllib.parse
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from typing import Any, Callable

from crypto import GCM_TAG_LEN, LINUX_IV, SALT, KWalletClient, OpenSslEvp


@dataclasses.dataclass(frozen=True)
class BrowserCookie:
    browser: str
    profile: str
    host: str
    name: str
    path: str
    value: str
    secure: bool
    expires_utc: int | None = None


@dataclasses.dataclass(frozen=True)
class CookieStore:
    browser: str
    family: str
    profile: str
    path: Path


def discover_cookie_stores(home: Path) -> list[CookieStore]:
    patterns: list[tuple[str, str, str]] = [
        ("Chrome", "chrome", ".config/google-chrome/*/Cookies"),
        ("Chrome", "chrome", ".config/google-chrome/*/Network/Cookies"),
        ("Chromium", "chromium", ".config/chromium/*/Cookies"),
        ("Chromium", "chromium", ".config/chromium/*/Network/Cookies"),
        ("Brave", "brave", ".config/BraveSoftware/Brave-Browser/*/Cookies"),
        ("Brave", "brave", ".config/BraveSoftware/Brave-Browser/*/Network/Cookies"),
        ("Edge", "chrome", ".config/microsoft-edge/*/Cookies"),
        ("Edge", "chrome", ".config/microsoft-edge/*/Network/Cookies"),
        ("Antigravity", "antigravity", ".config/Antigravity/Cookies"),
        ("Antigravity", "antigravity", ".config/Antigravity/Network/Cookies"),
        ("Antigravity Gemini", "electron", ".gemini/antigravity-browser-profile/*/Cookies"),
        ("Antigravity Gemini", "electron", ".gemini/antigravity-browser-profile/*/Network/Cookies"),
        ("AI Usage Monitor", "antigravity", ".config/ai-usage-monitor/Cookies"),
    ]
    stores: list[CookieStore] = []
    seen: set[Path] = set()
    for browser, family, pattern in patterns:
        for match in glob.glob(str(home / pattern)):
            path = Path(match)
            if not path.is_file() or path in seen:
                continue
            seen.add(path)
            profile = path.parent.name if path.parent.name != "Network" else path.parent.parent.name
            stores.append(CookieStore(browser, family, profile, path))
    return stores


def discover_firefox_cookie_stores(home: Path) -> list[Path]:
    patterns = [
        ".mozilla/firefox/*/cookies.sqlite",
        ".config/mozilla/firefox/*/cookies.sqlite",
        ".var/app/org.mozilla.firefox/.mozilla/firefox/*/cookies.sqlite",
    ]
    paths: list[Path] = []
    seen: set[Path] = set()
    for pattern in patterns:
        for match in glob.glob(str(home / pattern)):
            path = Path(match)
            if path.is_file() and path not in seen:
                seen.add(path)
                paths.append(path)
    return paths


def _cookie_tmp_base() -> str | None:
    # Prefer XDG_RUNTIME_DIR: a per-user 0700 tmpfs dir cleared at logout, so the
    # full browser cookie DB copy never lingers on shared /tmp. Fall back to the
    # default tempdir when the env var is unset or doesn't point at a real dir.
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base and os.path.isdir(base):
        return base
    return None


def _sweep_stale_cookie_dirs() -> None:
    # A SIGKILL mid-copy strands a tallybar-cookie-* dir until reboot. Best-effort
    # remove owned, real (non-symlink) dirs older than 1h from both candidate bases.
    # Never follows or deletes through a symlink; never raises.
    try:
        bases = {tempfile.gettempdir()}
        rt = _cookie_tmp_base()
        if rt:
            bases.add(rt)
        cutoff = time.time() - 3600
        for base in bases:
            try:
                names = os.listdir(base)
            except OSError:
                continue
            for name in names:
                if not name.startswith("tallybar-cookie-"):
                    continue
                target = os.path.join(base, name)
                try:
                    st = os.lstat(target)  # never follow symlinks
                except OSError:
                    continue
                if not stat.S_ISDIR(st.st_mode):
                    continue  # skips symlinks and regular files
                if st.st_uid != os.getuid():
                    continue
                if st.st_mtime >= cutoff:
                    continue
                shutil.rmtree(target, ignore_errors=True)
    except Exception:
        pass


@contextlib.contextmanager
def sqlite_copy(path: Path):
    _sweep_stale_cookie_dirs()
    with tempfile.TemporaryDirectory(prefix="tallybar-cookie-", dir=_cookie_tmp_base()) as tmp_dir:
        os.chmod(tmp_dir, 0o700)
        tmp = Path(tmp_dir) / path.name
        
        def secure_copy(src: Path, dst: Path) -> None:
            # Open src with O_NOFOLLOW so the symlink refusal and the open are a
            # single atomic syscall — no TOCTOU window where src could be swapped
            # for a symlink between an is_symlink() check and the copy. ELOOP
            # (symlink) raises OSError. dst is created fresh in a 0700 temp dir at 0600.
            try:
                src_fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
            except OSError as exc:
                raise ValueError(f"Security error: Refusing to copy symlink {src}") from exc
            with os.fdopen(src_fd, "rb") as fsrc:
                dst_fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                with os.fdopen(dst_fd, "wb") as fdst:
                    shutil.copyfileobj(fsrc, fdst)

        success = False
        try:
            secure_copy(path, tmp)
            
            wal = path.with_name(path.name + "-wal")
            shm = path.with_name(path.name + "-shm")
            if wal.exists():
                try:
                    secure_copy(wal, tmp.with_name(tmp.name + "-wal"))
                except (OSError, ValueError):
                    pass
            if shm.exists():
                try:
                    secure_copy(shm, tmp.with_name(tmp.name + "-shm"))
                except (OSError, ValueError):
                    pass
            success = True
        except (OSError, ValueError):
            pass

        if success:
            yield tmp
        else:
            yield None


def chromium_key_candidates(family: str, passwords: dict[str, list[bytes]]) -> list[bytes]:
    candidates: list[bytes] = []
    # `family` first (most specific), then every known bucket. "electron" precedes the
    # generic catch-all so the Antigravity Gemini browser profile (an Electron app) tries
    # its own key before the merged pool. NOTE: a v11 cookie whose Electron Safe Storage
    # key was rotated in KWallet is unrecoverable by any key here — that's a keyring fact,
    # not a bug (those Gemini domains are also covered by the Chrome cookie store).
    for bucket in (family, "chrome", "chromium", "brave", "antigravity", "electron", "generic"):
        for password in passwords.get(bucket, []):
            if password not in candidates:
                candidates.append(password)
    # "peanuts" is Chromium's hardcoded fallback password on Linux if KWallet is bypassed.
    if b"peanuts" not in candidates:
        candidates.append(b"peanuts")
    return candidates


def decode_plaintext(host: str, plaintext: bytes) -> str | None:
    host_digest = hashlib.sha256(host.encode("utf-8")).digest()
    variants = [plaintext]
    if plaintext.startswith(host_digest):
        variants.insert(0, plaintext[32:])
    for value in variants:
        try:
            decoded = value.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if "\x00" not in decoded:
            return decoded
    return None


def decrypt_chromium_value(
    crypto: OpenSslEvp,
    host: str,
    encrypted: bytes,
    family: str,
    passwords: dict[str, list[bytes]],
) -> str | None:
    if not encrypted:
        return ""
    if not (encrypted.startswith(b"v10") or encrypted.startswith(b"v11")):
        return decode_plaintext(host, encrypted)

    body = encrypted[3:]
    for password in chromium_key_candidates(family, passwords):
        # Linux Chromium derives cookie keys with exactly one PBKDF2-HMAC-SHA1 iteration.
        # This is a hardcoded Chromium requirement, not a TallyBar design choice.
        key_128 = hashlib.pbkdf2_hmac("sha1", password, SALT, 1, 16)
        try:
            plaintext = crypto.decrypt_aes_128_cbc(key_128, LINUX_IV, body)
            decoded = decode_plaintext(host, plaintext)
            if decoded is not None:
                return decoded
        except Exception:
            pass

        if len(body) > 12 + GCM_TAG_LEN:
            nonce = body[:12]
            ciphertext = body[12:-GCM_TAG_LEN]
            tag = body[-GCM_TAG_LEN:]
            for digest in ("sha1", "sha256"):
                key_256 = hashlib.pbkdf2_hmac(digest, password, SALT, 1, 32)
                try:
                    plaintext = crypto.decrypt_aes_256_gcm(key_256, nonce, ciphertext, tag)
                    decoded = decode_plaintext(host, plaintext)
                    if decoded is not None:
                        return decoded
                except Exception:
                    pass
    return None


def _query_sqlite_with_wal_retry(
    tmp: Path,
    query_fn: Callable[[sqlite3.Connection], list[BrowserCookie]],
) -> list[BrowserCookie]:
    """Open `tmp` read-only and run `query_fn` against it. A torn/corrupted copy of
    an actively-written -wal or -shm sidecar can raise sqlite3.DatabaseError not just
    at connect() time but also once a pragma/execute/fetchall actually walks the
    corrupted page — so this wraps the WHOLE query, not just the connect(). On
    DatabaseError from anywhere in query_fn, strip the sidecars and retry once
    against just the main file (the same fallback previously reachable only from a
    connect()-time failure). query_fn must return a fresh list each call (no
    caller-visible partial state to roll back)."""

    def _attempt() -> list[BrowserCookie]:
        con = sqlite3.connect(f"file:{urllib.parse.quote(str(tmp))}?mode=ro", uri=True, timeout=1.0)
        try:
            con.row_factory = sqlite3.Row
            return query_fn(con)
        finally:
            try:
                con.close()
            except Exception:
                pass

    try:
        return _attempt()
    except sqlite3.DatabaseError:
        wal = tmp.with_name(tmp.name + "-wal")
        shm = tmp.with_name(tmp.name + "-shm")
        if wal.exists():
            try:
                wal.unlink()
            except Exception:
                pass
        if shm.exists():
            try:
                shm.unlink()
            except Exception:
                pass
        return _attempt()


def read_chromium_cookies(
    store: CookieStore,
    crypto: OpenSslEvp,
    passwords: dict[str, list[bytes]],
    domain_filter: tuple[str, ...],
) -> tuple[list[BrowserCookie], dict[str, Any]]:
    stats: dict[str, Any] = {
        "browser": store.browser,
        "profile": store.profile,
        "path": str(store.path),
        "rows": 0,
        "decrypted": 0,
        "matched": 0,
        "errors": 0,
    }
    cookies: list[BrowserCookie] = []
    with sqlite_copy(store.path) as tmp:
        if not tmp:
            stats["errors"] = 1
            return cookies, stats

        def _query(con: sqlite3.Connection) -> list[BrowserCookie]:
            # Fresh per attempt: on a wal/shm-strip retry the whole query re-runs, so
            # the counters below must not double-count a discarded partial attempt.
            stats["rows"] = 0
            stats["matched"] = 0
            stats["decrypted"] = 0
            stats["errors"] = 0
            found: list[BrowserCookie] = []
            columns = {row["name"] for row in con.execute("pragma table_info(cookies)")}
            value_col = "value" if "value" in columns else "'' as value"
            secure_col = "is_secure" if "is_secure" in columns else "0 as is_secure"
            expiry_col = "expires_utc" if "expires_utc" in columns else "0 as expires_utc"
            rows = con.execute(
                f"""
                select host_key, name, path, {value_col}, encrypted_value, {secure_col}, {expiry_col}
                from cookies
                """
            )
            for row in rows:
                stats["rows"] += 1
                host = str(row["host_key"] or "")
                if domain_filter and not host_matches_any(host, domain_filter):
                    continue
                stats["matched"] += 1
                value = str(row["value"] or "")
                encrypted = bytes(row["encrypted_value"] or b"")
                if not value and encrypted:
                    decrypted = decrypt_chromium_value(crypto, host, encrypted, store.family, passwords)
                    if decrypted is None:
                        stats["errors"] += 1
                        continue
                    value = decrypted
                    stats["decrypted"] += 1
                if not value:
                    continue
                found.append(
                    BrowserCookie(
                        browser=store.browser,
                        profile=store.profile,
                        host=host,
                        name=str(row["name"] or ""),
                        path=str(row["path"] or "/"),
                        value=value,
                        secure=bool(row["is_secure"]),
                        expires_utc=int(row["expires_utc"] or 0),
                    )
                )
            return found

        try:
            cookies = _query_sqlite_with_wal_retry(tmp, _query)
        except Exception:
            stats["errors"] += 1
    return cookies, stats


def read_firefox_cookies(path: Path, domain_filter: tuple[str, ...]) -> tuple[list[BrowserCookie], dict[str, Any]]:
    stats: dict[str, Any] = {
        "browser": "Firefox",
        "profile": path.parent.name,
        "path": str(path),
        "rows": 0,
        "decrypted": 0,
        "matched": 0,
        "errors": 0,
    }
    cookies: list[BrowserCookie] = []
    with sqlite_copy(path) as tmp:
        if not tmp:
            stats["errors"] = 1
            return cookies, stats

        def _query(con: sqlite3.Connection) -> list[BrowserCookie]:
            # Fresh per attempt: on a wal/shm-strip retry the whole query re-runs, so
            # the counters below must not double-count a discarded partial attempt.
            stats["rows"] = 0
            stats["matched"] = 0
            stats["errors"] = 0
            found: list[BrowserCookie] = []
            for row in con.execute("select host, name, path, value, isSecure, expiry from moz_cookies"):
                stats["rows"] += 1
                host = str(row["host"] or "")
                if domain_filter and not host_matches_any(host, domain_filter):
                    continue
                stats["matched"] += 1
                value = str(row["value"] or "")
                if value:
                    found.append(
                        BrowserCookie(
                            browser="Firefox",
                            profile=path.parent.name,
                            host=host,
                            name=str(row["name"] or ""),
                            path=str(row["path"] or "/"),
                            value=value,
                            secure=bool(row["isSecure"]),
                            expires_utc=int(row["expiry"] or 0),
                        )
                    )
            return found

        try:
            cookies = _query_sqlite_with_wal_retry(tmp, _query)
        except Exception:
            stats["errors"] += 1
    return cookies, stats


def host_matches_any(host: str, domains: tuple[str, ...]) -> bool:
    normalized = host.lstrip(".").lower()
    for domain in domains:
        domain = domain.lstrip(".").lower()
        if normalized == domain or normalized.endswith("." + domain):
            return True
    return False


def cookiejar_for_domains(cookies: list[BrowserCookie], domains: tuple[str, ...]) -> CookieJar:
    jar = CookieJar()
    for item in cookies:
        if not host_matches_any(item.host, domains):
            continue
        domain = item.host if item.host.startswith(".") else "." + item.host
        jar.set_cookie(
            Cookie(
                version=0,
                name=item.name,
                value=item.value,
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=True,
                domain_initial_dot=domain.startswith("."),
                path=item.path or "/",
                path_specified=True,
                secure=item.secure,
                expires=None,
                discard=True,
                comment=None,
                comment_url=None,
                rest={},
                rfc2109=False,
            )
        )
    return jar


def _open_kwallet_and_collect_passwords(background: bool) -> tuple[dict[str, list[bytes]], dict[str, Any]]:
    # Synchronous D-Bus work (connect/open/list/read/close), meant to run off the event-loop
    # thread — see the call site in collect_browser_sessions for why. diagnostics() is built here,
    # before the `with` block closes the wallet: its `available` field reads the live handle, so
    # building it after close() would wrongly report `available: false` on an otherwise-successful run.
    with KWalletClient(background=background) as kwallet:
        passwords = kwallet.safe_storage_passwords()
        return passwords, kwallet.diagnostics(passwords)


async def collect_browser_sessions(timeout: float, background: bool = False) -> tuple[list[BrowserCookie], dict[str, Any]]:
    import asyncio
    from io_helpers import to_daemon_thread  # daemon worker: a stuck cookie read can't gate exit
    home = Path.home()
    domain_filter = (
        "claude.ai",
        "chatgpt.com",
        "openai.com",
        "auth.openai.com",
        "antigravity.google.com",
        "gemini.google.com",
        "google.com",
        "accounts.google.com",
        "generativelanguage.googleapis.com",
    )
    # KWalletClient's connect/open/list-entries/read-password/close calls are synchronous
    # Gio D-Bus round-trips (call_sync). Run the whole sequence on a daemon thread like every
    # other blocking fetch in this module — otherwise a wedged/slow kwalletd parks the event-loop
    # thread itself, and the outer asyncio.wait_for(timeout) can't fire until that call returns,
    # stalling the snapshot past its budget. The wallet is closed inside the thread (end of the
    # `with` in _open_kwallet_and_collect_passwords) right after passwords are extracted, before
    # the cookie-file reads below even start; `diagnostics()` only reads cached attributes, so it's
    # safe to call from the event loop afterward.
    passwords, kwallet_diagnostics = await to_daemon_thread(_open_kwallet_and_collect_passwords, background)
    crypto_evp = OpenSslEvp()
    cookies: list[BrowserCookie] = []
    stats: list[dict[str, Any]] = []

    chromium_tasks = [
        to_daemon_thread(read_chromium_cookies, store, crypto_evp, passwords, domain_filter)
        for store in discover_cookie_stores(home)
    ]
    firefox_tasks = [
        to_daemon_thread(read_firefox_cookies, ff_path, domain_filter)
        for ff_path in discover_firefox_cookie_stores(home)
    ]

    if chromium_tasks or firefox_tasks:
        # Honor the caller-supplied timeout as an internal bound (build_snapshot
        # also wraps this call, but a truthful signature should enforce its own
        # deadline). On timeout this raises, which the orchestrator turns into an
        # empty cookie jar + "timeout" browser diagnostic.
        results = await asyncio.wait_for(
            asyncio.gather(*chromium_tasks, *firefox_tasks), timeout
        )
        for store_cookies, store_stats in results:
            cookies.extend(store_cookies)
            stats.append(store_stats)

    return cookies, {
        "kwallet": kwallet_diagnostics,
        "stores": stats,
        "matchedCookies": len(cookies),
    }
