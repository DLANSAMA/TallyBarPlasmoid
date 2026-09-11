"""OpenSSL EVP crypto primitives and KDE KWallet D-Bus client.

These are low-level building blocks used by the cookie-decryption layer.
"""
from __future__ import annotations

import ctypes
import ctypes.util
from typing import Any

APP_ID = "io.github.dlansama.tallybar"
# These constants are dictated by Chromium's Linux cookie encryption implementation.
# Modifying them will break the ability to decrypt any Chromium cookies.
SALT = b"saltysalt"
LINUX_IV = b" " * 16
GCM_TAG_LEN = 16


class CryptoError(RuntimeError):
    pass


class OpenSslEvp:
    EVP_CTRL_GCM_SET_IVLEN = 0x09
    EVP_CTRL_GCM_SET_TAG = 0x11

    def __init__(self) -> None:
        self.available = False
        try:
            lib_name = ctypes.util.find_library("crypto") or "libcrypto.so.3"
            self.lib = ctypes.CDLL(lib_name)
            self._configure()
            self.available = True
        except (OSError, AttributeError):
            pass

    def _configure(self) -> None:
        lib = self.lib
        lib.EVP_CIPHER_CTX_new.restype = ctypes.c_void_p
        lib.EVP_CIPHER_CTX_free.argtypes = [ctypes.c_void_p]
        lib.EVP_aes_128_cbc.restype = ctypes.c_void_p
        lib.EVP_aes_256_gcm.restype = ctypes.c_void_p
        lib.EVP_DecryptInit_ex.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        lib.EVP_DecryptInit_ex.restype = ctypes.c_int
        lib.EVP_DecryptUpdate.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        lib.EVP_DecryptUpdate.restype = ctypes.c_int
        lib.EVP_DecryptFinal_ex.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
        ]
        lib.EVP_DecryptFinal_ex.restype = ctypes.c_int
        lib.EVP_CIPHER_CTX_ctrl.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_void_p,
        ]
        lib.EVP_CIPHER_CTX_ctrl.restype = ctypes.c_int

    @staticmethod
    def _wipe(*buffers) -> None:
        """Best-effort zero of sensitive ctypes buffers (AES key copies, decrypted
        plaintext) so they don't linger in process heap until GC. Defense-in-depth:
        the immutable bytes args and the returned bytes can't be wiped, so this only
        reduces residency of the mutable buffers this code allocates."""
        for buf in buffers:
            if buf is not None:
                try:
                    ctypes.memset(ctypes.addressof(buf), 0, ctypes.sizeof(buf))
                except Exception:
                    pass

    def decrypt_aes_128_cbc(self, key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
        if not self.available: raise CryptoError("libcrypto unavailable")
        # Validate sizes BEFORE libcrypto: create_string_buffer allocates exactly
        # len(x)+1 bytes, but AES-128 unconditionally reads 16 key/IV bytes — a short
        # key/IV would make OpenSSL over-read adjacent heap. Reject cleanly instead.
        if len(key) != 16:
            raise CryptoError(f"AES-128-CBC requires a 16-byte key (got {len(key)})")
        if len(iv) != 16:
            raise CryptoError(f"AES-128-CBC requires a 16-byte IV (got {len(iv)})")
        return self._decrypt(self.lib.EVP_aes_128_cbc(), key, iv, ciphertext)

    def decrypt_aes_256_gcm(self, key: bytes, nonce: bytes, ciphertext: bytes, tag: bytes) -> bytes:
        if not self.available: raise CryptoError("libcrypto unavailable")
        # Validate sizes BEFORE libcrypto (see decrypt_aes_128_cbc): AES-256 reads 32
        # key bytes regardless of the buffer size, and the GCM tag must be the standard
        # 16 bytes so an over-/under-long tag can't be validated against a wrong length.
        if len(key) != 32:
            raise CryptoError(f"AES-256-GCM requires a 32-byte key (got {len(key)})")
        if not nonce:
            raise CryptoError("AES-256-GCM requires a non-empty nonce")
        if len(tag) != GCM_TAG_LEN:
            raise CryptoError(f"AES-256-GCM requires a {GCM_TAG_LEN}-byte tag (got {len(tag)})")
        ctx = self.lib.EVP_CIPHER_CTX_new()
        if not ctx:
            raise CryptoError("EVP_CIPHER_CTX_new failed")
        key_buf = iv_buf = out = in_buf = tag_buf = None
        try:
            cipher = self.lib.EVP_aes_256_gcm()
            if self.lib.EVP_DecryptInit_ex(ctx, cipher, None, None, None) != 1:
                raise CryptoError("EVP_DecryptInit_ex failed")
            if self.lib.EVP_CIPHER_CTX_ctrl(ctx, self.EVP_CTRL_GCM_SET_IVLEN, len(nonce), None) != 1:
                raise CryptoError("EVP_CIPHER_CTX_ctrl ivlen failed")
            key_buf = ctypes.create_string_buffer(key)
            iv_buf = ctypes.create_string_buffer(nonce)
            if self.lib.EVP_DecryptInit_ex(ctx, None, None, key_buf, iv_buf) != 1:
                raise CryptoError("EVP_DecryptInit_ex key failed")

            out = ctypes.create_string_buffer(len(ciphertext) + 16)
            out_len = ctypes.c_int(0)
            in_buf = ctypes.create_string_buffer(ciphertext)
            if self.lib.EVP_DecryptUpdate(ctx, out, ctypes.byref(out_len), in_buf, len(ciphertext)) != 1:
                raise CryptoError("EVP_DecryptUpdate failed")
            total = out_len.value

            tag_buf = ctypes.create_string_buffer(tag)
            if self.lib.EVP_CIPHER_CTX_ctrl(ctx, self.EVP_CTRL_GCM_SET_TAG, len(tag), tag_buf) != 1:
                raise CryptoError("EVP_CIPHER_CTX_ctrl tag failed")

            final_len = ctypes.c_int(0)
            if self.lib.EVP_DecryptFinal_ex(ctx, ctypes.byref(out, total), ctypes.byref(final_len)) != 1:
                raise CryptoError("AES-GCM authentication failed")
            total += final_len.value
            return out.raw[:total]
        finally:
            self.lib.EVP_CIPHER_CTX_free(ctx)
            self._wipe(key_buf, iv_buf, out, in_buf, tag_buf)

    def _decrypt(self, cipher: int, key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
        ctx = self.lib.EVP_CIPHER_CTX_new()
        if not ctx:
            raise CryptoError("EVP_CIPHER_CTX_new failed")
        key_buf = iv_buf = out = in_buf = None
        try:
            key_buf = ctypes.create_string_buffer(key)
            iv_buf = ctypes.create_string_buffer(iv)
            if self.lib.EVP_DecryptInit_ex(ctx, cipher, None, key_buf, iv_buf) != 1:
                raise CryptoError("EVP_DecryptInit_ex failed")

            out = ctypes.create_string_buffer(len(ciphertext) + 32)
            out_len = ctypes.c_int(0)
            in_buf = ctypes.create_string_buffer(ciphertext)
            if self.lib.EVP_DecryptUpdate(ctx, out, ctypes.byref(out_len), in_buf, len(ciphertext)) != 1:
                raise CryptoError("EVP_DecryptUpdate failed")
            total = out_len.value

            final_len = ctypes.c_int(0)
            if self.lib.EVP_DecryptFinal_ex(ctx, ctypes.byref(out, total), ctypes.byref(final_len)) != 1:
                raise CryptoError("EVP_DecryptFinal_ex failed")
            total += final_len.value
            return out.raw[:total]
        finally:
            self.lib.EVP_CIPHER_CTX_free(ctx)
            self._wipe(key_buf, iv_buf, out, in_buf)


class KWalletClient:
    def __init__(self, timeout_ms: int = 3000, background: bool = False) -> None:
        self.timeout_ms = timeout_ms
        self.background = background
        self.service = ""
        self.path = ""
        self.wallet = ""
        self.handle = -1
        self.status = "unavailable"
        self.message = "KWallet service unavailable"
        self.locked = False
        self._bus = None
        self._gio = None
        self._glib = None
        self._connect()

    def close(self) -> None:
        if self.handle >= 0:
            try:
                self._wallet_call("close", "(ibs)", (self.handle, False, APP_ID), "(i)")
            except Exception:
                pass
            self.handle = -1

    def __enter__(self) -> "KWalletClient":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    @property
    def available(self) -> bool:
        return self.handle >= 0

    def diagnostics(self, passwords: dict[str, list[bytes]] | None = None) -> dict[str, Any]:
        return {
            "available": self.available,
            "service": self.service,
            "wallet": self.wallet,
            "status": self.status,
            "locked": self.locked,
            "background": self.background,
            "message": self.message,
            "safeStorageKeys": {key: len(value) for key, value in (passwords or {}).items()},
        }

    def _connect(self) -> None:
        try:
            import gi

            gi.require_version("Gio", "2.0")
            from gi.repository import Gio, GLib
        except Exception:
            self.status = "unavailable"
            self.message = "PyGObject Gio bindings unavailable"
            return

        self._gio = Gio
        self._glib = GLib
        self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        for service, path in (
            ("org.kde.kwalletd6", "/modules/kwalletd6"),
            ("org.kde.kwalletd5", "/modules/kwalletd5"),
        ):
            try:
                enabled = self._call(service, path, "isEnabled", None, "(b)")[0]
                if not enabled:
                    continue
                wallet = self._call(service, path, "localWallet", None, "(s)")[0]
                self.service = service
                self.path = path
                self.wallet = str(wallet)
                try:
                    is_open = bool(
                        self._call(
                            service,
                            path,
                            "isOpen",
                            GLib.Variant("(s)", (wallet,)),
                            "(b)",
                        )[0]
                    )
                except Exception:
                    is_open = False
                    if self.background:
                        self.status = "wallet-state-unknown"
                        self.locked = True
                        self.message = "KWallet lock state could not be checked during background refresh"
                        return
                if not is_open and self.background:
                    self.status = "wallet-locked"
                    self.locked = True
                    self.message = "KWallet is locked; background refresh skipped credential access"
                    return
                handle = self._call(
                    service,
                    path,
                    "open",
                    GLib.Variant("(sxs)", (wallet, 0, APP_ID)),
                    "(i)",
                )[0]
                if handle >= 0:
                    self.handle = int(handle)
                    self.status = "ok"
                    self.locked = False
                    self.message = "KWallet opened"
                    return
            except Exception:
                continue
        self.status = "unavailable"
        self.message = "No enabled KWallet service responded"

    def _call(self, service: str, path: str, method: str, args: Any, result_type: str) -> tuple[Any, ...]:
        assert self._bus is not None
        assert self._gio is not None
        assert self._glib is not None
        return self._bus.call_sync(
            service,
            path,
            "org.kde.KWallet",
            method,
            args,
            self._glib.VariantType.new(result_type),
            self._gio.DBusCallFlags.NONE,
            self.timeout_ms,
            None,
        ).unpack()

    def _wallet_call(self, method: str, signature: str, values: tuple[Any, ...], result_type: str) -> tuple[Any, ...]:
        assert self._glib is not None
        return self._call(
            self.service,
            self.path,
            method,
            self._glib.Variant(signature, values),
            result_type,
        )

    def folders(self) -> list[str]:
        if not self.available:
            return []
        try:
            folders = self._wallet_call("folderList", "(is)", (self.handle, APP_ID), "(as)")[0]
            return sorted(set(str(folder) for folder in folders))
        except Exception:
            return []

    def entries(self, folder: str) -> list[str]:
        if not self.available:
            return []
        try:
            entries = self._wallet_call("entryList", "(iss)", (self.handle, folder, APP_ID), "(as)")[0]
            return sorted(set(str(entry) for entry in entries))
        except Exception:
            return []

    def read_password(self, folder: str, key: str) -> str | None:
        if not self.available:
            return None
        try:
            value = self._wallet_call(
                "readPassword",
                "(isss)",
                (self.handle, folder, key, APP_ID),
                "(s)",
            )[0]
            return str(value) if value else None
        except Exception:
            return None

    def safe_storage_passwords(self) -> dict[str, list[bytes]]:
        found: dict[str, list[bytes]] = {
            "chrome": [],
            "chromium": [],
            "brave": [],
            "antigravity": [],
            "electron": [],
            "generic": [],
        }
        folder_map = {
            "Chrome Keys": "chrome",
            "Chromium Keys": "chromium",
            "Brave Keys": "brave",
            "Antigravity Keys": "antigravity",
            "ai-usage-monitor Keys": "antigravity",
            # The Antigravity Gemini browser profile (~/.gemini/antigravity-browser-profile)
            # is an Electron app; its cookie key is "Electron Safe Storage", not an
            # app-named folder. Mapping it explicitly (rather than leaning on the generic
            # " Keys" catch-all) keeps it working if that fallback is ever tightened.
            "Electron Keys": "electron",
        }
        for folder in self.folders():
            family = folder_map.get(folder, "generic" if folder.endswith(" Keys") else "")
            if not family:
                continue
            for entry in self.entries(folder):
                if "Safe Storage" not in entry:
                    continue
                password = self.read_password(folder, entry)
                if not password:
                    continue
                encoded = password.encode("utf-8")
                if encoded not in found[family]:
                    found[family].append(encoded)
                if encoded not in found["generic"]:
                    found["generic"].append(encoded)
        return found
