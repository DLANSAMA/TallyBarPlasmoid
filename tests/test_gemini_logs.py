"""Gemini CLI session transcripts: .json documents and .jsonl append logs."""
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "io.github.dlansama.tallybar" / "contents" / "code"))

import accounting
from accounting import log_parsers as lp

_NOW = dt.datetime(2026, 6, 9, 12, 0, tzinfo=dt.timezone.utc).astimezone()


def _msg(msg_id, tokens=1000, extra=None):
    msg = {"id": msg_id, "timestamp": _NOW.isoformat(), "type": "gemini", "content": "",
           "tokens": {"input": tokens, "output": 0, "cached": 0, "thoughts": 0, "tool": 0,
                      "total": tokens},
           "model": "gemini-2.5-pro"}
    msg.update(extra or {})
    return msg


def _jsonl(path, messages):
    lines = [{"sessionId": "s", "kind": "main", "startTime": _NOW.isoformat()},
             {"id": "u", "timestamp": _NOW.isoformat(), "type": "user", "content": "hi"},
             {"$set": {"lastUpdated": _NOW.isoformat()}}]
    for m in messages:
        lines += [m, {"$set": {"lastUpdated": _NOW.isoformat()}}]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")


def test_jsonl_sessions_are_scanned_and_a_re_logged_message_counts_once(tmp_path):
    chats = tmp_path / "tmp" / "proj" / "chats"
    _jsonl(chats / "session-2026-06-09T10-00-aaaa.jsonl",
           [_msg("m1"), _msg("m1", extra={"toolCalls": []}), _msg("m2", 500)])
    summary = accounting.local_gemini_token_summary(gemini_dir=tmp_path, now=_NOW, cache_dir=None)
    assert summary["tokens30d"] == 1500


def test_a_message_copied_into_a_second_file_counts_once(tmp_path):
    chats = tmp_path / "tmp" / "proj" / "chats"
    _jsonl(chats / "session-2026-06-09T10-00-aaaa.jsonl", [_msg("m1")])
    _jsonl(chats / "session-2026-06-09T11-00-bbbb.jsonl", [_msg("m1"), _msg("m3", 200)])
    summary = accounting.local_gemini_token_summary(gemini_dir=tmp_path, now=_NOW, cache_dir=None)
    assert summary["tokens30d"] == 1200


def test_json_sessions_still_count_and_antigravity_trees_stay_pruned(tmp_path):
    chats = tmp_path / "tmp" / "proj" / "chats"
    chats.mkdir(parents=True)
    (chats / "session-a.json").write_text(json.dumps({"messages": [_msg("j1", 300)]}))
    _jsonl(tmp_path / "antigravity-cli" / "chats" / "session-x.jsonl", [_msg("a1", 9_000_000)])
    files = sorted(p.name for p in lp._gemini_session_files(tmp_path))
    assert files == ["session-a.json"]
    summary = accounting.local_gemini_token_summary(gemini_dir=tmp_path, now=_NOW, cache_dir=None)
    assert summary["tokens30d"] == 300
