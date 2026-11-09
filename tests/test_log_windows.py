"""The 'last 30 days' window is 30 local calendar dates including today."""
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "io.github.dlansama.tallybar" / "contents" / "code"))

import accounting

_NOW = dt.datetime(2026, 6, 20, 12, 0).astimezone()


def _at(days_back, hour):
    day = _NOW.date() - dt.timedelta(days=days_back)
    return dt.datetime.combine(day, dt.time(hour)).astimezone()


def _check(summary):
    assert summary["tokens30d"] == 1000      # day 29 back is in, day 30 back is out
    assert [r["tokens"] for r in summary["modelBreakdown"]] == [1000]


def test_claude_window(tmp_path):
    def line(req, when, tokens):
        return json.dumps({"type": "assistant", "timestamp": when.isoformat(), "requestId": req,
                           "message": {"model": "claude-sonnet-4",
                                       "usage": {"input_tokens": tokens, "output_tokens": 0}}})
    (tmp_path / "s.jsonl").write_text(
        line("in", _at(29, 0), 1000) + "\n" + line("out", _at(30, 23), 7) + "\n")
    _check(accounting.local_claude_token_summary(projects_dir=tmp_path, now=_NOW, cache_dir=None))


def test_codex_window(tmp_path):
    def tc(when, tokens):
        u = {"input_tokens": tokens, "output_tokens": 0, "total_tokens": tokens}
        return json.dumps({"timestamp": when.isoformat(), "payload": {"info": {
            "last_token_usage": u, "total_token_usage": {**u, "total_tokens": tokens + when.day}}}})
    (tmp_path / "r.jsonl").write_text(tc(_at(29, 0), 1000) + "\n" + tc(_at(30, 23), 7) + "\n")
    _check(accounting.local_codex_token_summary(sessions_dir=tmp_path, now=_NOW, cache_dir=None))


def test_grok_window(tmp_path):
    def done(when, loop, tokens):
        return json.dumps({"ts": when.isoformat(), "sid": "s", "msg": "shell.turn.inference_done",
                           "ctx": {"loop_index": loop, "prompt_tokens": tokens,
                                   "cached_prompt_tokens": 0, "completion_tokens": 0}})
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "unified.jsonl").write_text(done(_at(29, 0), 0, 1000) + "\n" + done(_at(30, 23), 1, 7) + "\n")
    _check(accounting.local_grok_token_summary(logs_dir=logs, now=_NOW, cache_dir=None))


def test_gemini_window(tmp_path):
    def msg(i, when, tokens):
        return {"id": i, "type": "gemini", "timestamp": when.isoformat(), "model": "gemini-2.5-pro",
                "tokens": {"input": tokens, "output": 0, "total": tokens}}
    (tmp_path / "session-a.json").write_text(json.dumps(
        {"messages": [msg("a", _at(29, 0), 1000), msg("b", _at(30, 23), 7)]}))
    _check(accounting.local_gemini_token_summary(gemini_dir=tmp_path, now=_NOW, cache_dir=None))
