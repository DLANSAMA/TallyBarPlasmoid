"""Every summarizer bills a record at the rate of its own local day: usage from before an
intro rate ends keeps it, usage on or after the list date gets the list rate."""
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "io.github.dlansama.tallybar" / "contents" / "code"))

import accounting  # noqa: E402
import pricing_data  # noqa: E402
from providers import cost as costmod  # noqa: E402
from providers import antigravity as agmod  # noqa: E402

_MODEL = "gemini-4-argon"      # $2 per MTok input through 2026-11-30, $4 from 2026-12-01


@pytest.fixture(autouse=True)
def _after_the_switch(monkeypatch, tmp_path):
    monkeypatch.setattr(pricing_data, "PRICING_CACHE_PATH", tmp_path / "missing" / "pricing_cache.json")
    monkeypatch.setattr(pricing_data, "_today", lambda: "2026-12-15")
    pricing_data.invalidate_cache()
    yield
    pricing_data.invalidate_cache()


def _ts(day):
    return dt.datetime.fromisoformat(day + "T12:00:00").astimezone().isoformat()


def test_codex_prices_each_record_by_its_day(tmp_path):
    def tc(day, loop):
        u = {"input_tokens": 1_000_000, "output_tokens": 0, "total_tokens": 1_000_000 + loop}
        return json.dumps({"timestamp": _ts(day), "payload": {"model": _MODEL, "info": {
            "last_token_usage": u, "total_token_usage": u}}})
    (tmp_path / "r.jsonl").write_text(tc("2026-11-30", 1) + "\n" + tc("2026-12-01", 2) + "\n")
    summary = accounting.local_codex_token_summary(sessions_dir=tmp_path, now=dt.datetime(2026, 12, 2, 12).astimezone(),
                                                   cache_dir=None)
    by_day = {d["date"]: d["cost"] for d in summary["weeklyTokenUsage"]}
    assert by_day["2026-11-30"] == pytest.approx(2.0)
    assert by_day["2026-12-01"] == pytest.approx(4.0)


def test_claude_prices_each_record_by_its_day(tmp_path):
    def line(req, day):
        return json.dumps({"type": "assistant", "timestamp": _ts(day), "requestId": req,
                           "message": {"model": _MODEL,
                                       "usage": {"input_tokens": 1_000_000, "output_tokens": 0}}})
    (tmp_path / "s.jsonl").write_text(line("a", "2026-11-30") + "\n" + line("b", "2026-12-01") + "\n")
    summary = accounting.local_claude_token_summary(
        projects_dir=tmp_path, now=dt.datetime(2026, 12, 2, 12).astimezone(), cache_dir=None)
    by_day = {d["date"]: d["cost"] for d in summary["weeklyTokenUsage"]}
    assert by_day["2026-11-30"] == pytest.approx(2.0)
    assert by_day["2026-12-01"] == pytest.approx(4.0)


def test_gemini_prices_each_record_by_its_day(tmp_path):
    def msg(i, day):
        return {"id": i, "type": "gemini", "timestamp": _ts(day), "model": _MODEL,
                "tokens": {"input": 1_000_000, "output": 0, "total": 1_000_000}}
    (tmp_path / "session-a.json").write_text(json.dumps(
        {"messages": [msg("a", "2026-11-30"), msg("b", "2026-12-01")]}))
    summary = accounting.local_gemini_token_summary(
        gemini_dir=tmp_path, now=dt.datetime(2026, 12, 2, 12).astimezone(), cache_dir=None)
    by_day = {d["date"]: d["cost"] for d in summary["weeklyTokenUsage"]}
    assert by_day["2026-11-30"] == pytest.approx(2.0)
    assert by_day["2026-12-01"] == pytest.approx(4.0)


def test_grok_prices_each_record_by_its_day(tmp_path, monkeypatch):
    # No scheduled grok model exists; schedule one so the grok path is observable.
    monkeypatch.setitem(pricing_data.LIST_RATE_FROM, "grok-build-0.1",
                        ("2026-12-01", {"input": 4.00, "output": 20.00, "cache_read": 0.20}))
    monkeypatch.setitem(pricing_data._INTRO_INPUT, "grok-build-0.1",
                        pricing_data.get_pricing("grok-build-0.1", "2026-11-30")["input"])
    pricing_data.invalidate_cache()

    def done(day, loop):
        return json.dumps({"ts": _ts(day), "sid": "s", "msg": "shell.turn.inference_done",
                           "ctx": {"loop_index": loop, "prompt_tokens": 100_000,
                                   "cached_prompt_tokens": 0, "completion_tokens": 0}})
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "unified.jsonl").write_text(done("2026-11-30", 0) + "\n" + done("2026-12-01", 1) + "\n")
    summary = accounting.local_grok_token_summary(
        logs_dir=logs, now=dt.datetime(2026, 12, 2, 12).astimezone(), cache_dir=None)
    by_day = {d["date"]: d["cost"] for d in summary["weeklyTokenUsage"]}
    assert by_day["2026-12-01"] / by_day["2026-11-30"] == pytest.approx(
        4.00 / pricing_data.get_pricing("grok-build-0.1", "2026-11-30")["input"])


def test_antigravity_ledger_prices_each_entry_by_its_day(tmp_path, monkeypatch):
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CONVERSATION_DIRS", ())
    monkeypatch.setattr(costmod, "ANTIGRAVITY_LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CLI_USAGE_PATH", tmp_path / "cli.json")
    monkeypatch.setattr(agmod, "collect_antigravity_rpc_usage", lambda *a, **k: agmod.RpcHarvest())
    entries = {f"x#{i}": {"d": day, "h": 1, "u": 1_000_000, "c": 0, "o": 0, "model": _MODEL}
               for i, day in enumerate(("2026-11-30", "2026-12-01"))}
    (tmp_path / "ledger.json").write_text(json.dumps({"trackingStarted": "2026-05-01", "entries": entries}))
    summary = costmod.antigravity_ledger_cost_summary(now=dt.datetime(2026, 12, 2, 12).astimezone())
    by_day = {d["date"]: d["cost"] for d in summary["weeklyTokenUsage"]}
    assert by_day["2026-11-30"] == pytest.approx(2.0)
    assert by_day["2026-12-01"] == pytest.approx(4.0)
