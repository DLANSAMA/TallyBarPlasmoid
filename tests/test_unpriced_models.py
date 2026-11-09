"""Every provider's cost summary names the models it could only bill at a sibling's or
the family default's rate, so diagnostics.unpriced_models can report them."""
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "io.github.dlansama.tallybar" / "contents" / "code"))

import accounting  # noqa: E402
from providers import antigravity as agmod  # noqa: E402
from providers import cost as costmod  # noqa: E402

_NOW = dt.datetime.now(dt.timezone.utc).astimezone().replace(hour=12, minute=0, second=0, microsecond=0)


def test_codex_names_an_unpriced_model(tmp_path):
    def tc(model, n):
        u = {"input_tokens": 1000, "output_tokens": 10, "total_tokens": 1010}
        return [json.dumps({"timestamp": _NOW.isoformat(), "payload": {"model": model}}),
                json.dumps({"timestamp": _NOW.isoformat(), "payload": {"info": {
                    "last_token_usage": u, "total_token_usage": {**u, "total_tokens": 1010 + n}}}})]
    (tmp_path / "r.jsonl").write_text("\n".join(tc("gpt-5.3-codex-zz", 1) + tc("gpt-5-codex", 2)) + "\n")
    summary = accounting.local_codex_token_summary(sessions_dir=tmp_path, now=_NOW, cache_dir=None)
    assert summary["unpricedModels"] == ["gpt-5.3-codex-zz"]


def test_gemini_names_an_unpriced_model(tmp_path):
    def msg(i, model):
        return {"id": i, "type": "gemini", "timestamp": _NOW.isoformat(), "model": model,
                "tokens": {"input": 100, "output": 5, "total": 105}}
    (tmp_path / "session-a.json").write_text(json.dumps(
        {"messages": [msg("a", "gemini-9.9-hyper"), msg("b", "gemini-2.5-pro")]}))
    summary = accounting.local_gemini_token_summary(gemini_dir=tmp_path, now=_NOW, cache_dir=None)
    assert summary["unpricedModels"] == ["gemini-9.9-hyper"]


def test_grok_names_an_unpriced_model(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    lines = [json.dumps({"ts": _NOW.isoformat(), "msg": "model changed", "sid": "s",
                         "ctx": {"model": "grok-oddball"}}),
             json.dumps({"ts": _NOW.isoformat(), "sid": "s", "msg": "shell.turn.inference_done",
                         "ctx": {"loop_index": 0, "prompt_tokens": 1000,
                                 "cached_prompt_tokens": 0, "completion_tokens": 5}})]
    (logs / "unified.jsonl").write_text("\n".join(lines) + "\n")
    summary = accounting.local_grok_token_summary(logs_dir=logs, now=_NOW, cache_dir=None)
    assert summary["unpricedModels"]
    assert all(m.startswith("grok") for m in summary["unpricedModels"])


def test_priced_models_are_not_named(tmp_path):
    (tmp_path / "session-a.json").write_text(json.dumps({"messages": [
        {"id": "a", "type": "gemini", "timestamp": _NOW.isoformat(), "model": "gemini-2.5-pro",
         "tokens": {"input": 100, "output": 5, "total": 105}}]}))
    summary = accounting.local_gemini_token_summary(gemini_dir=tmp_path, now=_NOW, cache_dir=None)
    assert "unpricedModels" not in summary


def test_antigravity_names_an_unpriced_model(tmp_path, monkeypatch):
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CONVERSATION_DIRS", ())
    monkeypatch.setattr(costmod, "ANTIGRAVITY_LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CLI_USAGE_PATH", tmp_path / "cli.json")
    monkeypatch.setattr(agmod, "collect_antigravity_rpc_usage", lambda *a, **k: agmod.RpcHarvest())
    day = _NOW.date().isoformat()
    entries = {"x#0": {"d": day, "h": 1, "u": 1000, "c": 0, "o": 5, "model": "gemini-9.9-hyper"},
               "x#1": {"d": day, "h": 1, "u": 1000, "c": 0, "o": 5, "model": "gemini-2.5-pro"}}
    (tmp_path / "ledger.json").write_text(json.dumps({"trackingStarted": "2026-05-01", "entries": entries}))
    summary = costmod.antigravity_ledger_cost_summary(now=_NOW)
    assert summary["unpricedModels"] == ["gemini-9.9-hyper"]
