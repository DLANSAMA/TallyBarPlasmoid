from pathlib import Path
import sys
from unittest.mock import patch, MagicMock
import ctypes

import pytest

# Adjust sys.path to find backend modules
sys.path.insert(0, str(Path(__file__).parent.parent / "io.github.dlansama.tallybar" / "contents" / "code"))

from crypto import OpenSslEvp, KWalletClient, CryptoError

def test_openssl_evp_aes_128_cbc_mock():
    with patch("ctypes.CDLL") as mock_cdll_class:
        mock_lib = MagicMock()
        mock_cdll_class.return_value = mock_lib
        
        mock_lib.EVP_CIPHER_CTX_new.return_value = 9999
        mock_lib.EVP_DecryptInit_ex.return_value = 1
        
        def mock_decrypt_update(ctx, out, out_len, in_buf, in_len):
            ctypes.memmove(out, in_buf, in_len)
            out_len._obj.value = in_len
            return 1
        mock_lib.EVP_DecryptUpdate.side_effect = mock_decrypt_update
        
        def mock_decrypt_final(ctx, out, out_len):
            out_len._obj.value = 0
            return 1
        mock_lib.EVP_DecryptFinal_ex.side_effect = mock_decrypt_final
        
        mock_lib.EVP_aes_128_cbc.return_value = 1111

        evp = OpenSslEvp()
        
        key = b"0" * 16
        iv = b"0" * 16
        ciphertext = b"hello world"
        
        decrypted = evp.decrypt_aes_128_cbc(key, iv, ciphertext)
        assert decrypted.startswith(b"hello world")
        mock_lib.EVP_aes_128_cbc.assert_called_once()

def test_openssl_evp_aes_256_gcm_mock():
    with patch("ctypes.CDLL") as mock_cdll_class:
        mock_lib = MagicMock()
        mock_cdll_class.return_value = mock_lib
        
        mock_lib.EVP_CIPHER_CTX_new.return_value = 9999
        mock_lib.EVP_DecryptInit_ex.return_value = 1
        mock_lib.EVP_CIPHER_CTX_ctrl.return_value = 1
        
        def mock_decrypt_update(ctx, out, out_len, in_buf, in_len):
            ctypes.memmove(out, in_buf, in_len)
            out_len._obj.value = in_len
            return 1
        mock_lib.EVP_DecryptUpdate.side_effect = mock_decrypt_update
        
        def mock_decrypt_final(ctx, out, out_len):
            out_len._obj.value = 0
            return 1
        mock_lib.EVP_DecryptFinal_ex.side_effect = mock_decrypt_final
        
        mock_lib.EVP_aes_256_gcm.return_value = 2222

        evp = OpenSslEvp()
        
        key = b"0" * 32
        nonce = b"0" * 12
        ciphertext = b"hello world"
        tag = b"0" * 16
        
        decrypted = evp.decrypt_aes_256_gcm(key, nonce, ciphertext, tag)
        assert decrypted.startswith(b"hello world")
        mock_lib.EVP_aes_256_gcm.assert_called_once()

def test_kwallet_client_not_available():
    with patch.dict("sys.modules", {"gi": None}):
        client = KWalletClient()
        assert client.available is False
        assert client.status == "unavailable"

def test_kwallet_client_mock_dbus():
    mock_gio = MagicMock()
    mock_glib = MagicMock()
    mock_bus = MagicMock()
    
    mock_gio.bus_get_sync.return_value = mock_bus
    
    call_sync_returns = [
        (True,),                         # isEnabled
        ("kdewallet",),                  # localWallet
        (True,),                         # isOpen
        (42,),                           # open (returns handle)
        (["Chrome Keys", "Brave Keys"],), # folders
        ([],),                           # entries for Brave Keys
        (["Chrome Safe Storage"],),      # entries for Chrome Keys
        ("mysecretpassword",)            # readPassword for Chrome Safe Storage
    ]
    
    mock_bus.call_sync.return_value = MagicMock(unpack=lambda: call_sync_returns.pop(0) if call_sync_returns else (None,))
    
    with patch.dict("sys.modules", {"gi": MagicMock(), "gi.repository": MagicMock(Gio=mock_gio, GLib=mock_glib)}):
        client = KWalletClient()
        assert client.available is True
        assert client.handle == 42
        assert client.wallet == "kdewallet"
        assert client.status == "ok"
        assert client.locked is False
        
        folders = client.folders()
        assert folders == ["Brave Keys", "Chrome Keys"]
        
        call_sync_returns.clear()
        call_sync_returns.extend([
            (["Chrome Keys", "Brave Keys"],), # folders()
            ([],),                           # entries("Brave Keys")
            (["Chrome Safe Storage"],),      # entries("Chrome Keys")
            ("mysecretpassword",)            # readPassword()
        ])
        
        passwords = client.safe_storage_passwords()
        assert b"mysecretpassword" in passwords["chrome"]
        assert b"mysecretpassword" in passwords["generic"]


# Background refresh must never trigger a GUI wallet-unlock prompt: a
# locked wallet (isOpen -> False) reports wallet-locked and skips open() entirely.
def test_kwallet_background_locked_skips_open():
    mock_gio = MagicMock()
    mock_glib = MagicMock()
    mock_bus = MagicMock()

    mock_gio.bus_get_sync.return_value = mock_bus

    call_sync_returns = [
        (True,),          # isEnabled
        ("kdewallet",),   # localWallet
        (False,),         # isOpen -> wallet is locked
        # No further calls expected: open() must NOT be invoked in background.
    ]

    mock_bus.call_sync.return_value = MagicMock(
        unpack=lambda: call_sync_returns.pop(0) if call_sync_returns else (None,)
    )

    with patch.dict("sys.modules", {"gi": MagicMock(), "gi.repository": MagicMock(Gio=mock_gio, GLib=mock_glib)}):
        client = KWalletClient(background=True)
        assert client.status == "wallet-locked"
        assert client.locked is True
        assert client.available is False  # handle never set, so open() never succeeded
        # Exactly three D-Bus calls: isEnabled, localWallet, isOpen — no open().
        assert mock_bus.call_sync.call_count == 3
        called_methods = [c.args[3] for c in mock_bus.call_sync.call_args_list]
        assert "open" not in called_methods
        assert called_methods == ["isEnabled", "localWallet", "isOpen"]


# A FOREGROUND run (background=False, the "Unlock KWallet" path) must NOT
# short-circuit on a locked wallet — it proceeds to open(), which is exactly what pops
# the native KWallet unlock dialog. (The widget always passes --background; this path is
# reached only via main.qml's deliberate foreground refresh.)
def test_kwallet_foreground_locked_attempts_open():
    mock_gio = MagicMock()
    mock_glib = MagicMock()
    mock_bus = MagicMock()

    mock_gio.bus_get_sync.return_value = mock_bus

    call_sync_returns = [
        (True,),          # isEnabled
        ("kdewallet",),   # localWallet
        (False,),         # isOpen -> locked, but foreground does NOT skip
        (7,),             # open -> handle 7 (user unlocked the dialog)
    ]

    mock_bus.call_sync.return_value = MagicMock(
        unpack=lambda: call_sync_returns.pop(0) if call_sync_returns else (None,)
    )

    with patch.dict("sys.modules", {"gi": MagicMock(), "gi.repository": MagicMock(Gio=mock_gio, GLib=mock_glib)}):
        client = KWalletClient(background=False)
        assert client.status == "ok"
        assert client.available is True       # open() succeeded -> handle set
        called_methods = [c.args[3] for c in mock_bus.call_sync.call_args_list]
        assert called_methods == ["isEnabled", "localWallet", "isOpen", "open"]


# If isOpen RAISES during a background refresh, we cannot know the lock
# state, so report wallet-state-unknown and likewise skip open() (no GUI prompt).
def test_kwallet_background_isopen_raises_state_unknown():
    mock_gio = MagicMock()
    mock_glib = MagicMock()
    mock_bus = MagicMock()

    mock_gio.bus_get_sync.return_value = mock_bus

    # isEnabled, localWallet succeed; isOpen (3rd call) raises.
    call_results = [(True,), ("kdewallet",)]

    def call_sync(*args, **kwargs):
        method = args[3]
        if method == "isOpen":
            raise RuntimeError("D-Bus isOpen blew up")
        return MagicMock(unpack=lambda: call_results.pop(0) if call_results else (None,))

    mock_bus.call_sync.side_effect = call_sync

    with patch.dict("sys.modules", {"gi": MagicMock(), "gi.repository": MagicMock(Gio=mock_gio, GLib=mock_glib)}):
        client = KWalletClient(background=True)
        assert client.status == "wallet-state-unknown"
        assert client.locked is True
        assert client.available is False
        # open() must not be attempted after isOpen failed in background.
        called_methods = [c.args[3] for c in mock_bus.call_sync.call_args_list]
        assert "open" not in called_methods
        assert called_methods == ["isEnabled", "localWallet", "isOpen"]


# Length validation happens BEFORE libcrypto so a short key/IV/nonce/tag
# can't make OpenSSL over-read adjacent heap — each bad length raises CryptoError.
def test_crypto_length_validation_rejects_bad_sizes():
    evp = OpenSslEvp()
    if not evp.available:
        pytest.skip("libcrypto unavailable")

    # AES-128-CBC: 15-byte key is rejected (needs exactly 16).
    with pytest.raises(CryptoError):
        evp.decrypt_aes_128_cbc(b"k" * 15, b"i" * 16, b"x" * 16)

    # AES-128-CBC: valid 16-byte key but 15-byte IV is rejected (needs exactly 16).
    with pytest.raises(CryptoError):
        evp.decrypt_aes_128_cbc(b"k" * 16, b"i" * 15, b"x" * 16)

    # AES-256-GCM: 31-byte key is rejected (needs exactly 32).
    with pytest.raises(CryptoError):
        evp.decrypt_aes_256_gcm(b"k" * 31, b"n" * 12, b"x" * 32, b"t" * 16)

    # AES-256-GCM: valid key+nonce+ct but 15-byte tag is rejected (needs exactly 16).
    with pytest.raises(CryptoError):
        evp.decrypt_aes_256_gcm(b"k" * 32, b"n" * 12, b"x" * 32, b"t" * 15)


def test_kwallet_electron_folder_maps_to_electron_bucket():
    # The Antigravity Gemini browser profile is an Electron app; its cookie key lives in
    # the "Electron Keys" folder. safe_storage_passwords must route it to a dedicated
    # 'electron' bucket (not silently into the generic catch-all), and still mirror to generic.
    mock_gio = MagicMock()
    mock_glib = MagicMock()
    mock_bus = MagicMock()
    mock_gio.bus_get_sync.return_value = mock_bus
    call_sync_returns = [
        (True,), ("kdewallet",), (True,), (42,),        # isEnabled/localWallet/isOpen/open
        (["Electron Keys"],),                            # folders()
        (["Electron Safe Storage"],),                    # entries("Electron Keys")
        ("electron-key",),                               # readPassword()
    ]
    mock_bus.call_sync.return_value = MagicMock(
        unpack=lambda: call_sync_returns.pop(0) if call_sync_returns else (None,))
    with patch.dict("sys.modules", {"gi": MagicMock(),
                                    "gi.repository": MagicMock(Gio=mock_gio, GLib=mock_glib)}):
        client = KWalletClient()
        passwords = client.safe_storage_passwords()
    assert b"electron-key" in passwords["electron"]
    assert b"electron-key" in passwords["generic"]
