"""Antigravity token-ledger integrity: dating, double counts, re-ingest, pricing, isolation.

Drives the real ledger updater against on-disk SQLite trajectory DBs and the real protobuf
parser; only the language-server RPC is faked.
"""

import sys
from pathlib import Path

import pytest

CODE_DIR = Path(__file__).parent.parent / "io.github.dlansama.tallybar" / "contents" / "code"
sys.path.insert(0, str(CODE_DIR))

from proto_wire import _pb_read_varint  # noqa: E402


# ---------------------------------------------------------------------------
# Varints
# ---------------------------------------------------------------------------

def test_varint_accepts_the_largest_uint64():
    assert _pb_read_varint(b"\xff" * 9 + b"\x01", 0) == (2**64 - 1, 10)


@pytest.mark.parametrize("raw", [
    b"\xff" * 9 + b"\x02",          # bit 64 set
    b"\xff" * 9 + b"\x7f",          # up to 2**70 - 1
    b"\x80" * 9 + b"\x81\x00",      # continuation on the tenth byte
])
def test_varint_wider_than_64_bits_is_rejected(raw):
    with pytest.raises(ValueError):
        _pb_read_varint(raw, 0)
