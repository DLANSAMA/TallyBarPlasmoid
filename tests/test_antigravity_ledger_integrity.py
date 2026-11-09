"""Antigravity token-ledger integrity: dating, double counts, re-ingest, pricing, isolation.

Drives the real ledger updater against on-disk SQLite trajectory DBs and the real protobuf
parser; only the language-server RPC is faked.
"""

import datetime as dt
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

CODE_DIR = Path(__file__).parent.parent / "io.github.dlansama.tallybar" / "contents" / "code"
sys.path.insert(0, str(CODE_DIR))

import accounting  # noqa: E402
from accounting import LogScanIncomplete  # noqa: E402
from proto_wire import _pb_read_varint  # noqa: E402
from providers import antigravity as agmod  # noqa: E402
from providers import cost as costmod  # noqa: E402
from providers import grok as grokmod  # noqa: E402

_FIXED_NOW = dt.datetime(2026, 6, 9, 12, 0, 0, tzinfo=dt.timezone.utc)


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _field(fn: int, val: int) -> bytes:
    return _varint(fn << 3) + _varint(val)


def _ld(fn: int, payload: bytes) -> bytes:
    return _varint(fn << 3 | 2) + _varint(len(payload)) + payload


def _usage(marker: int, *, model=1132, u=200, o=80, c=30) -> bytes:
    return _field(1, model) + _field(2, u) + _field(3, o) + _field(5, c) + _field(6, marker)


def _gen_blob(marker: int, secs: int, **usage) -> bytes:
    """A gen_metadata row: one generation with its usage at field 4 and a Timestamp at 9.4."""
    return _ld(1, _ld(4, _usage(marker, **usage)) + _ld(9, _ld(4, _field(1, secs))))


def _make_db(path, *, steps=None, gen=None):
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, metadata BLOB)")
    con.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB, size INTEGER)")
    if steps:
        con.executemany("INSERT INTO steps (idx, metadata) VALUES (?, ?)", steps)
    if gen:
        con.executemany("INSERT INTO gen_metadata (idx, data, size) VALUES (?, ?, ?)",
                        [(i, b, len(b)) for (i, b) in gen])
    con.commit()
    con.close()


def _wire(monkeypatch, tmp_path, *conv_dirs, harvest=None):
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CONVERSATION_DIRS", tuple(conv_dirs))
    monkeypatch.setattr(costmod, "ANTIGRAVITY_LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CLI_USAGE_PATH", tmp_path / "cli.json")
    monkeypatch.setattr(agmod, "collect_antigravity_rpc_usage",
                        harvest or (lambda *a, **k: agmod.RpcHarvest()))


def _set_mtime(path, isodate):
    ts = dt.datetime.fromisoformat(isodate + "T12:00:00").timestamp()
    os.utime(path, (ts, ts))


def _noon(isodate: str) -> int:
    return int(dt.datetime.fromisoformat(isodate + "T12:00:00").timestamp())


def _write_ledger(tmp_path, **ledger):
    ledger.setdefault("trackingStarted", "2026-05-01")
    (tmp_path / "ledger.json").write_text(json.dumps(ledger), encoding="utf-8")


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


# ---------------------------------------------------------------------------
# A corrupt timestamp skips its generation; a failing provider keeps the others
# ---------------------------------------------------------------------------

def test_out_of_range_generation_timestamp_skips_only_that_generation(monkeypatch, tmp_path):
    conv = tmp_path / "convs"
    conv.mkdir()
    good = _noon("2026-06-08")
    _make_db(conv / "cas.db", gen=[(0, _gen_blob(26, good, u=100)),
                                   (1, _gen_blob(26, good * 1000, u=999))])  # milliseconds
    _wire(monkeypatch, tmp_path, conv)
    res = costmod.update_antigravity_token_ledger(now=_FIXED_NOW)
    assert {k: e["d"] for k, e in res["entries"].items()} == {"cas@0.0": "2026-06-08"}


def _stub_summaries(monkeypatch, tmp_path, antigravity):
    monkeypatch.setattr(costmod, "COST_ARCHIVE_PATH", tmp_path / "cost_archive.json")
    monkeypatch.setattr(grokmod, "grok_billing_period", lambda: None)
    for name in ("codex", "claude", "gemini", "grok"):
        monkeypatch.setattr(accounting, f"local_{name}_token_summary",
                            lambda name=name, **_k: {"source": name, "monthlyTokenUsage": []})
    monkeypatch.setattr(costmod, "antigravity_ledger_cost_summary", antigravity)


def test_one_failing_provider_keeps_the_others(monkeypatch, tmp_path):
    def broken(**_k):
        raise ValueError("year 58000 is out of range")

    _stub_summaries(monkeypatch, tmp_path, broken)
    summaries = costmod.compute_local_cost_summaries()
    assert summaries["antigravity"] is None
    assert summaries["_errors"] == {"antigravity": "ValueError: year 58000 is out of range"}
    assert [summaries[p]["source"] for p in ("codex", "claude", "gemini", "grok")] == \
        ["codex", "claude", "gemini", "grok"]


@pytest.mark.parametrize("exc", [TimeoutError("cut"), LogScanIncomplete("cut")])
def test_a_deadline_cut_still_aborts_the_whole_scan(monkeypatch, tmp_path, exc):
    def cut(**_k):
        raise exc

    _stub_summaries(monkeypatch, tmp_path, cut)
    with pytest.raises(type(exc)):
        costmod.compute_local_cost_summaries()


def test_error_map_is_neither_a_provider_nor_an_archived_month(monkeypatch, tmp_path):
    monkeypatch.setattr(costmod, "COST_ARCHIVE_PATH", tmp_path / "cost_archive.json")
    kept = {"cost": 1.5, "tokens": 10}
    (tmp_path / "cost_archive.json").write_text(json.dumps(
        {"version": 1, "months": {"2026-06": {"antigravity": kept}}}), encoding="utf-8")
    summaries = {"codex": {"monthlyTokenUsage": []}, "antigravity": None,
                 "_errors": {"antigravity": "ValueError: boom"}}
    costmod.update_cost_archive(summaries, now=_FIXED_NOW)
    month = json.loads((tmp_path / "cost_archive.json").read_text())["months"]["2026-06"]
    assert "_errors" not in month
    assert month["antigravity"] == kept
    providers: dict = {}
    costmod.apply_cost_summaries(providers, summaries)
    assert "_errors" not in providers


# ---------------------------------------------------------------------------
# RPC generations with no time of their own
# ---------------------------------------------------------------------------

_LMT = "2026-06-02T14:00:00.123456789Z"


def _stub_language_server(monkeypatch, summaries, generations, calls):
    """A live language server listing ``summaries`` ({cid: lastModifiedTime}); every
    cascade's metadata is ``generations`` without createdAt, and the step lookup fails."""
    monkeypatch.setattr(agmod, "find_antigravity_processes", lambda: [(1, "tok", "http")])
    monkeypatch.setattr(agmod, "antigravity_ports", lambda pid: ([1234], ""))

    def post(url, token, body, timeout):
        method = url.rsplit("/", 1)[1]
        calls.append(method)
        if method == "GetAllCascadeTrajectories":
            return 200, {"trajectorySummaries": {c: {"lastModifiedTime": t} for c, t in summaries.items()}}
        if method == "GetAvailableModels":
            return 200, {"response": {"models": {}}}
        if method == "GetCascadeTrajectoryGeneratorMetadata":
            return 200, {"generatorMetadata": generations}
        raise OSError("reply too large")

    monkeypatch.setattr(agmod, "_post_local_rpc", post)


def _undated_gen(step: int, tokens: int = 1000) -> dict:
    return {"stepIndices": [step], "chatModel": {"usage": {
        "model": "MODEL_PLACEHOLDER_M132", "apiProvider": "API_PROVIDER_GOOGLE_GEMINI",
        "inputTokens": str(tokens), "outputTokens": "10", "cacheReadTokens": "0"}}}


def test_undated_generation_takes_its_cascade_day(monkeypatch, tmp_path):
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CONVERSATION_DIRS", ())
    monkeypatch.setattr(costmod, "ANTIGRAVITY_LEDGER_PATH", tmp_path / "ledger.json")
    calls: list = []
    _stub_language_server(monkeypatch, {"old": _LMT}, [_undated_gen(0), _undated_gen(4)], calls)
    res = costmod.update_antigravity_token_ledger(now=_FIXED_NOW)
    want = agmod._rpc_local_date(_LMT)
    assert {k: (e["d"], e.get("h")) for k, e in res["entries"].items()} == \
        {"old#0": (want, None), "old#4": (want, None)}


def test_cascade_dated_from_its_watermark_is_not_requeried(monkeypatch, tmp_path):
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CONVERSATION_DIRS", ())
    monkeypatch.setattr(costmod, "ANTIGRAVITY_LEDGER_PATH", tmp_path / "ledger.json")
    calls: list = []
    _stub_language_server(monkeypatch, {"old": _LMT}, [_undated_gen(0)], calls)
    costmod.update_antigravity_token_ledger(now=_FIXED_NOW)
    assert "GetCascadeTrajectoryGeneratorMetadata" in calls
    before = (tmp_path / "ledger.json").stat().st_mtime_ns
    calls.clear()
    costmod.update_antigravity_token_ledger(now=_FIXED_NOW)
    assert "GetCascadeTrajectoryGeneratorMetadata" not in calls
    assert "GetCascadeTrajectorySteps" not in calls
    assert (tmp_path / "ledger.json").stat().st_mtime_ns == before


def test_harvest_day_entries_move_to_their_watermark_day(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)   # language server down
    mark_day = agmod._rpc_local_date(_LMT)
    _write_ledger(tmp_path, rpcWatermarks={"old": _LMT, "live": _LMT}, entries={
        "old#0": {"d": "2026-06-09", "u": 5, "c": 0, "o": 1, "me": 1132},     # harvest day
        "old#1": {"d": "2026-06-01", "u": 5, "c": 0, "o": 1, "me": 1132},     # already earlier
        "live#0": {"d": "2026-06-09", "h": 8, "u": 5, "c": 0, "o": 1, "me": 1132},  # real time
    })
    res = costmod.update_antigravity_token_ledger(now=_FIXED_NOW)
    assert {k: e["d"] for k, e in res["entries"].items()} == \
        {"old#0": mark_day, "old#1": "2026-06-01", "live#0": "2026-06-09"}
    before = (tmp_path / "ledger.json").stat().st_mtime_ns
    costmod.update_antigravity_token_ledger(now=_FIXED_NOW)
    assert (tmp_path / "ledger.json").stat().st_mtime_ns == before


def test_repaired_cascade_is_queried_once_then_left_alone(monkeypatch, tmp_path):
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CONVERSATION_DIRS", ())
    monkeypatch.setattr(costmod, "ANTIGRAVITY_LEDGER_PATH", tmp_path / "ledger.json")
    _write_ledger(tmp_path, rpcWatermarks={"old": _LMT}, entries={
        "old#0": {"d": "2026-06-09", "u": 1000, "c": 0, "o": 10, "me": 1132}})
    calls: list = []
    _stub_language_server(monkeypatch, {"old": _LMT}, [_undated_gen(0)], calls)
    costmod.update_antigravity_token_ledger(now=_FIXED_NOW)
    assert calls.count("GetCascadeTrajectoryGeneratorMetadata") == 1
    calls.clear()
    res = costmod.update_antigravity_token_ledger(now=_FIXED_NOW)
    assert "GetCascadeTrajectoryGeneratorMetadata" not in calls
    assert res["entries"]["old#0"]["d"] == agmod._rpc_local_date(_LMT)


# ---------------------------------------------------------------------------
# The 30-day window
# ---------------------------------------------------------------------------

def test_last_30_days_is_thirty_calendar_dates(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    now = dt.datetime(2026, 6, 9, 0, 30).astimezone()
    first = (now.date() - dt.timedelta(days=29)).isoformat()
    before = (now.date() - dt.timedelta(days=30)).isoformat()
    _write_ledger(tmp_path, entries={
        "in#0": {"d": first, "h": 1, "u": 1000, "c": 0, "o": 0, "me": 1132},
        "out#0": {"d": before, "h": 23, "u": 7, "c": 0, "o": 0, "me": 1132},
    })
    summary = costmod.antigravity_ledger_cost_summary(now=now)
    assert summary["tokens30d"] == 1000


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("enum,row,older", [(1402, "claude-opus-5-5", "claude-opus-5"),
                                            (1405, "claude-sonnet-5-5", "claude-sonnet-5")])
def test_claude_5_5_prices_from_its_own_row(monkeypatch, tmp_path, enum, row, older):
    _wire(monkeypatch, tmp_path)
    today = _FIXED_NOW.astimezone().date().isoformat()
    u, c, o = 1000, 10_000, 1000
    _write_ledger(tmp_path, entries={"x#0": {"d": today, "h": 10, "u": u, "c": c, "o": o, "me": enum}})

    def cost(p):
        return round((u * p["input"] + c * p["cache_read"] + o * p["output"]) / 1e6, 6)

    exact, stale = accounting.model_pricing(row), accounting.model_pricing(older)
    assert cost(exact) != cost(stale)
    assert costmod.antigravity_ledger_cost_summary(now=_FIXED_NOW)["costToday"] == cost(exact)
