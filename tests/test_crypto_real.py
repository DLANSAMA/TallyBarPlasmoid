"""Real-libcrypto AES tests for crypto.OpenSslEvp (complements the mocked test_crypto.py).

These exercise the ACTUAL EVP calling convention against libcrypto, so a regression in the
ctypes argtypes/restype setup or the GCM SET_IVLEN/SET_TAG ctrl sequence fails here instead
of passing silently against mocks. Skips cleanly where openssl/libcrypto isn't available.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

CODE_DIR = Path(__file__).parent.parent / "io.github.dlansama.tallybar" / "contents" / "code"
sys.path.insert(0, str(CODE_DIR))

from crypto import OpenSslEvp, CryptoError  # noqa: E402

_OPENSSL = shutil.which("openssl")


@pytest.fixture
def evp():
    e = OpenSslEvp()
    if not e.available:
        pytest.skip("libcrypto unavailable")
    return e


@pytest.mark.skipif(_OPENSSL is None, reason="openssl CLI not available")
def test_decrypt_aes_128_cbc_round_trip(evp):
    # Generate a real AES-128-CBC ciphertext (PKCS7-padded, the Chromium safe-storage
    # shape) with the openssl CLI, then decrypt it through the real EVP path.
    key = os.urandom(16)
    iv = os.urandom(16)
    plaintext = b"chromium-safe-storage-cookie-secret!"   # not a 16-byte multiple -> needs padding
    ct = subprocess.run(
        [_OPENSSL, "enc", "-aes-128-cbc", "-K", key.hex(), "-iv", iv.hex()],
        input=plaintext, capture_output=True, check=True,
    ).stdout
    assert evp.decrypt_aes_128_cbc(key, iv, ct) == plaintext


def test_decrypt_aes_256_gcm_auth_failure_raises(evp):
    # Random ciphertext+tag must fail the GCM tag check — exercises the real EVP path
    # (SET_IVLEN + SET_TAG ctrl) AND the "AES-GCM authentication failed" branch.
    with pytest.raises(CryptoError):
        evp.decrypt_aes_256_gcm(os.urandom(32), os.urandom(12), os.urandom(32), os.urandom(16))


def test_decrypt_unavailable_raises():
    # available=False short-circuits to CryptoError before touching libcrypto.
    e = OpenSslEvp.__new__(OpenSslEvp)
    e.available = False
    with pytest.raises(CryptoError):
        e.decrypt_aes_128_cbc(b"k" * 16, b"i" * 16, b"x" * 16)
