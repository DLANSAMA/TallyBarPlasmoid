"""Claude transcripts outside ~/.claude/projects (desktop Cowork / agent sessions, the
other standard config dirs) feed the same summary and parse-cache store."""
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "io.github.dlansama.tallybar" / "contents" / "code"))

import pytest

import accounting
from accounting import log_parsers as lp

_NOW = dt.datetime.now(dt.timezone.utc).astimezone().replace(hour=12, minute=0, second=0, microsecond=0)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


def _line(req, tokens, session="s"):
    return json.dumps({"type": "assistant", "timestamp": _NOW.isoformat(), "requestId": req,
                       "uuid": f"{session}-{req}",
                       "message": {"model": "claude-sonnet-4", "usage": {
                           "input_tokens": tokens, "output_tokens": 0}}}) + "\n"


def _write(path, *lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines), encoding="utf-8")


def _cowork(home):
    return home / ".config" / "Claude" / "local-agent-mode-sessions" / "org" / "user"


def _tokens():
    return accounting.local_claude_token_summary(now=_NOW, cache_dir=None)["tokens30d"]


def test_cowork_layouts_are_counted_and_other_jsonl_is_not():
    home = Path.home()
    base = _cowork(home)
    _write(base / "local_abc" / ".claude" / "projects" / "proj" / "a.jsonl", _line("r1", 1000))
    _write(base / "agent" / "local_ditto_x" / ".claude" / "projects" / "session" / "b.jsonl",
           _line("r2", 200))
    _write(base / "local_abc" / "audit.jsonl", _line("r3", 9_000_000))
    _write(base / "skills-plugin" / "u" / ".claude" / "projects" / "p" / "c.jsonl", _line("r4", 7_000_000))
    _write(home / ".claude" / "projects" / "main" / "m.jsonl", _line("r5", 30))
    assert _tokens() == 1230


def test_a_transcript_present_in_two_roots_counts_once():
    home = Path.home()
    _write(home / ".claude" / "projects" / "p" / "m.jsonl", _line("same", 500))
    _write(_cowork(home) / "local_a" / ".claude" / "projects" / "p" / "copy.jsonl", _line("same", 500))
    assert _tokens() == 500


def test_missing_roots_are_fine_and_other_config_dirs_are_read(monkeypatch, tmp_path):
    assert accounting.local_claude_token_summary(now=_NOW, cache_dir=None) is None
    cfg = tmp_path / "cfg"
    _write(cfg / "projects" / "p" / "a.jsonl", _line("r1", 40))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    xdg = tmp_path / "xdg"
    _write(xdg / "claude" / "projects" / "p" / "b.jsonl", _line("r2", 2))
    _write(xdg / "Claude" / "local-agent-mode-sessions" / "o" / "local_a" / ".claude" / "projects" / "p" / "c.jsonl", _line("r3", 300))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    assert _tokens() == 342


def test_new_roots_do_not_reparse_the_primary_cache(tmp_path):
    home = Path.home()
    primary = home / ".claude" / "projects"
    _write(primary / "p" / "m.jsonl", _line("r1", 10))
    cache = tmp_path / "cache" / "claude_logs.json"
    cache.parent.mkdir()
    lp._cached_claude_states(primary, cache)
    new = _cowork(home) / "local_a" / ".claude" / "projects" / "p" / "c.jsonl"
    _write(new, _line("r2", 20))
    parsed = []
    real = lp._parse_claude_file
    lp._parse_claude_file = lambda path, *a, **k: (parsed.append(path), real(path, *a, **k))[1]
    try:
        extra = lp._claude_extra_roots(primary)
        out = lp._cached_claude_states(primary, cache, extra_roots=extra)
    finally:
        lp._parse_claude_file = real
    assert parsed == [new] and len(out) == 2
    # A root that disappears leaves the store.
    new.unlink()
    out = lp._cached_claude_states(primary, cache, extra_roots=())
    assert len(out) == 1


def test_a_subagent_file_in_a_new_root_shares_its_sessions_shard():
    store = lp._ClaudeLogStore(Path("/x/claude_logs.json"), Path("/h/.claude/projects"),
                               (Path("/c/.claude/projects"),))
    main = store._shard_of("/c/.claude/projects/proj/sess.jsonl")
    sub = store._shard_of("/c/.claude/projects/proj/sess/subagents/a.jsonl")
    assert main == sub
