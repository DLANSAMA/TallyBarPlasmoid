"""Unit tests for proto_wire protobuf parsing and hardening."""
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "io.github.dlansama.tallybar" / "contents" / "code"))

from proto_wire import (
    _dominant_enum,
    _pb_fields,
    _pb_find_usage,
    _pb_generations,
    _pb_read_varint,
)


def test_pb_read_varint_valid():
    assert _pb_read_varint(b"\x00", 0) == (0, 1)
    assert _pb_read_varint(b"\x01", 0) == (1, 1)
    assert _pb_read_varint(b"\x7F", 0) == (127, 1)
    assert _pb_read_varint(b"\x80\x01", 0) == (128, 2)
    assert _pb_read_varint(b"\x96\x01", 0) == (150, 2)
    assert _pb_read_varint(b"\xff\xff\xff\xff\x07", 0) == (0x7FFFFFFF, 5)


def test_pb_read_varint_eof_raises_indexerror():
    with pytest.raises(IndexError, match="Unexpected EOF"):
        _pb_read_varint(b"\x80", 0)
    with pytest.raises(IndexError, match="Unexpected EOF"):
        _pb_read_varint(b"", 0)


def test_pb_read_varint_overflow_raises_valueerror():
    # 10 bytes with high bit set (70 bits of shift > 64 bits)
    malformed = b"\x80" * 12
    with pytest.raises(ValueError, match="exceeds 64 bits"):
        _pb_read_varint(malformed, 0)


def test_dominant_enum():
    assert _dominant_enum([]) == 0
    records = [
        {1: 1016, 2: 100, 3: 50, 5: 10},   # total 160
        {1: 1020, 2: 500, 3: 100, 5: 50},  # total 650 -> dominant
        {1: 1036, 2: 50, 3: 10, 5: 5},     # total 65
    ]
    assert _dominant_enum(records) == 1020


def test_pb_fields_soft_failure():
    # Malformed length exceeding buffer length
    malformed_chunk = b"\x12\x50\x01\x02"  # tag (2, length-delimited), length 80, only 2 bytes follow
    varints, subs = _pb_fields(malformed_chunk)
    assert subs == []


def test_pb_find_usage_depth_limit():
    empty_res = _pb_find_usage(b"\x00", depth=9)
    assert empty_res == []


def test_pb_generations_empty():
    assert _pb_generations(b"") == []
    # Arbitrary non-generation bytes
    assert _pb_generations(b"\x08\x01\x10\x02") == []
