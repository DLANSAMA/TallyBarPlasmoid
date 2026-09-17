"""Codex JSON-RPC subprocess reader robustness.

Drives JsonRpcChild's stdout/stderr readers against a fake process stream so the
two hardening fixes are regression-guarded:

- When the stdout buffer is force-truncated past 16MB, a blind tail-slice
  can cut mid-message and leave an undecodable partial that raw_decode fails on
  forever (the RPC then never resolves). The reader must realign to the next
  newline boundary and recover the following valid message.
- The stderr reader must drain in fixed chunks (not StreamReader.readline,
  which dies on a >64KiB no-newline burst), keeping the tail bounded.
"""

import asyncio
import sys
from pathlib import Path

import pytest

CODE_DIR = Path(__file__).parent.parent / "io.github.dlansama.tallybar" / "contents" / "code"
sys.path.insert(0, str(CODE_DIR))

from providers.codex import JsonRpcChild  # noqa: E402


class _FakeReader:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read(self, n=-1):
        return self._chunks.pop(0) if self._chunks else b""

    async def readline(self):
        return b""


class _FakeProc:
    def __init__(self, stdout_chunks, stderr_chunks):
        self.stdout = _FakeReader(stdout_chunks)
        self.stderr = _FakeReader(stderr_chunks)
        self.stdin = None


@pytest.mark.asyncio
async def test_stdout_recovers_after_buffer_truncation():
    # 17MB of newline-less garbage trips the 16MB cap; the realign-to-newline must
    # discard it so the *next* valid message still resolves its pending future.
    garbage = b"x" * 17_000_000
    valid = b'{"id": 1, "result": {"ok": true}}\n'
    child = JsonRpcChild(_FakeProc([garbage, valid], []))
    fut = asyncio.get_running_loop().create_future()
    child.pending[1] = fut  # set before the loop runs the reader task (no await yet)

    result = await asyncio.wait_for(fut, timeout=2.0)  # would hang pre-fix
    assert result["result"]["ok"] is True
    await child.stdout_task


@pytest.mark.asyncio
async def test_stderr_chunked_drain_is_bounded_and_survives_long_line():
    # A 200KB burst without a newline must not kill the reader; subsequent lines
    # still land, and the diagnostic tail stays capped at 8.
    proc = _FakeProc([], [b"a" * 200000, b"\nfirst\nsecond\n"])
    child = JsonRpcChild(proc)
    await asyncio.wait_for(child.stderr_task, timeout=2.0)

    assert "second" in child.stderr_tail
    assert len(child.stderr_tail) <= 8
    await child.stdout_task
