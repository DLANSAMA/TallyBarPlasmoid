"""Local-log readers survive bytes that are not UTF-8."""
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "io.github.dlansama.tallybar" / "contents" / "code"))

import accounting
from accounting import log_parsers as lp

_NOW = dt.datetime(2026, 6, 9, 12, 0, tzinfo=dt.timezone.utc).astimezone()


def _grok_line(ts, sid, prompt):
    return json.dumps({"ts": ts.isoformat(), "msg": "shell.turn.inference_done", "sid": sid,
                       "ctx": {"prompt_tokens": prompt, "cached_prompt_tokens": 0,
                               "completion_tokens": 10, "loop_index": prompt}})


def test_codex_rollout_with_an_invalid_byte_keeps_its_other_lines(tmp_path):
    usage = {"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100}
    good = json.dumps({"timestamp": _NOW.isoformat(), "payload": {"type": "token_count", "info": {
        "last_token_usage": usage, "total_token_usage": usage}}})
    bad = b'{"timestamp": "x", "text": "caf\xe9"}\n'
    f = tmp_path / "rollout.jsonl"
    f.write_bytes(bad + good.encode() + b"\n")
    assert len(lp._parse_codex_file(f)) == 1
    summary = accounting.local_codex_token_summary(sessions_dir=tmp_path, now=_NOW, cache_dir=None)
    assert summary is not None and "1K tok" in summary["today"]


def test_grok_log_with_an_invalid_byte_keeps_its_other_lines(tmp_path):
    f = tmp_path / "unified.jsonl"
    f.write_bytes(b'{"msg": "noise \xff shell.turn.inference_done"}\n'
                  + _grok_line(_NOW, "s1", 500).encode() + b"\n")
    assert len(lp._parse_grok_file(f)) == 1


def test_gemini_session_with_an_invalid_byte_still_parses(tmp_path):
    session = json.dumps({"messages": [{
        "type": "gemini", "timestamp": _NOW.isoformat(), "model": "gemini-2.5-pro",
        "content": "@@", "tokens": {"input": 10, "output": 5, "total": 15}}]})
    f = tmp_path / "session-a.json"
    f.write_bytes(session.replace("@@", "\x00").encode().replace(b"\x00", b"\xff"))
    assert len(lp._parse_gemini_file(f)) == 1
