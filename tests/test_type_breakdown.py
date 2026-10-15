"""Cached vs uncached usage, per window (the summaries' ``typeBreakdown``).

Every cost summary splits its Today, 7-day and 30-day totals into uncached input, output,
cache reads and cache writes, as tokens and as dollars. The split must add up to the
window's total for every provider, under each provider's own usage shape: Anthropic's
cache counts are additive to input, OpenAI's / xAI's / Gemini's cached tokens are a
subset of it, and the Antigravity ledger bills its cache reads per entry.
"""
import datetime as dt
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "io.github.dlansama.tallybar" / "contents" / "code"))

import accounting  # noqa: E402
from providers import antigravity as agmod  # noqa: E402
from providers import cost as costmod  # noqa: E402

_NOW = dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.timezone.utc)
# Distinct rates per lane, so a token billed in the wrong lane changes the dollars.
_PRICES = {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_write_1h": 6.0,
           "cache_read": 0.3}
# One request today, one three days ago (7-day window), one 20 days ago (30-day only).
_DAYS = (20, 3, 0)
_WINDOWS = (("today", "tokensToday", "costToday"), ("7d", "tokens7d", "cost7d"),
            ("30d", "tokens30d", "cost30d"))


def _assert_split_adds_up(summary):
    split = summary["typeBreakdown"]
    for window, tokens_key, cost_key in _WINDOWS:
        rows = split[window]
        assert set(rows) >= set(accounting.USAGE_TYPES), (window, rows)
        assert sum(r["tokens"] for r in rows.values()) == summary[tokens_key], (window, rows)
        assert sum(r["cost"] for r in rows.values()) == pytest.approx(summary[cost_key], abs=1e-5), (window, rows)


def _row(summary, window, kind):
    r = summary["typeBreakdown"][window][kind]
    return r["tokens"], r["cost"]


# ---------------------------------------------------------------------------
# One request, split
# ---------------------------------------------------------------------------

def test_usage_split_anthropic_keeps_reads_and_writes_apart():
    usage = {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 5000,
             "cache_creation_input_tokens": 400,
             "cache_creation": {"ephemeral_1h_input_tokens": 100, "ephemeral_5m_input_tokens": 300}}
    with patch("accounting.model_pricing", return_value=_PRICES):
        s = accounting.usage_split(usage, "claude-sonnet-4-5")
        assert s.cost == accounting.usage_cost_usd(usage, "claude-sonnet-4-5")
    assert s.tokens == 6600
    assert s.type_tokens == (1000, 200, 5000, 400)
    # 1h writes bill at cache_write_1h, 5m writes at cache_write — both are "cacheWrite".
    assert s.type_costs == pytest.approx((0.003, 0.003, 0.0015, (100 * 6.0 + 300 * 3.75) / 1e6))
    assert sum(s.type_costs) == pytest.approx(s.cost)


def test_usage_split_openai_peels_cached_out_of_input():
    # Codex and Grok: cached ⊂ input, reasoning ⊂ output.
    usage = {"input_tokens": 10000, "cached_input_tokens": 4000, "output_tokens": 1000,
             "reasoning_output_tokens": 200, "total_tokens": 11000}
    with patch("accounting.model_pricing", return_value=_PRICES):
        s = accounting.usage_split(usage, "gpt-5")
        assert s.cost == accounting.usage_cost_usd(usage, "gpt-5")
    assert s.tokens == 11000
    assert s.type_tokens == (6000, 1000, 4000, 0)
    assert s.type_costs == pytest.approx((0.018, 0.015, 0.0012, 0.0))


def test_usage_split_gemini_adds_thoughts_and_tool():
    # Gemini: cached ⊂ input; thoughts bill as output and the tool-use prompt as input.
    usage = {"input": 10000, "output": 800, "cached": 3000, "thoughts": 200, "tool": 500,
             "total": 11500}
    with patch("accounting.model_pricing", return_value=_PRICES):
        s = accounting.usage_split(usage, "gemini-3.5-flash")
        assert s.cost == accounting.usage_cost_usd(usage, "gemini-3.5-flash")
    assert s.type_tokens == (7500, 1000, 3000, 0)
    assert sum(s.type_tokens) == s.tokens == 11500
    # The tool-use prompt bills at the input rate, thoughts at the output rate.
    assert s.type_costs == pytest.approx((7500 * 3.0 / 1e6, 1000 * 15.0 / 1e6, 3000 * 0.3 / 1e6, 0.0))


def test_usage_split_prices_every_type_at_the_request_prompt_tier():
    # Past the threshold every rate moves to its long_ value, cache reads included, and the
    # prompt that picks the tier counts the cache reads.
    tiered = {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write": 1.25,
              "long_context_threshold": 100_000, "long_input": 5.0, "long_output": 25.0,
              "long_cache_read": 0.5, "long_cache_write": 6.25}
    long_prompt = {"input_tokens": 1000, "output_tokens": 100, "cache_read_input_tokens": 150_000,
                   "cache_creation_input_tokens": 2000}
    short_prompt = {"input_tokens": 1000, "output_tokens": 100, "cache_read_input_tokens": 50_000,
                    "cache_creation_input_tokens": 2000}
    with patch("accounting.model_pricing", return_value=tiered):
        long_split = accounting.usage_split(long_prompt, "claude-haiku-5-5")
        short_split = accounting.usage_split(short_prompt, "claude-haiku-5-5")
        assert long_split.cost == accounting.usage_cost_usd(long_prompt, "claude-haiku-5-5")
    assert long_split.type_costs == pytest.approx((0.005, 0.0025, 0.075, 0.0125))
    assert short_split.type_costs == pytest.approx((0.001, 0.0005, 0.005, 0.0025))


def test_nested_cache_writes_bill_but_do_not_count_as_tokens():
    # A record with only the nested 5m/1h breakdown and no top-level cache-write count bills
    # those writes, but the token readers count the top-level field alone, as they always
    # have: the parse caches keep or drop a record by usage_token_total, so counting them
    # would change what a cache holds without a parse-version bump.
    usage = {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 0,
             "cache_creation": {"ephemeral_1h_input_tokens": 400}}
    with patch("accounting.model_pricing", return_value=_PRICES):
        s = accounting.usage_split(usage, "claude-sonnet-4-5")
        assert s.cost == accounting.usage_cost_usd(usage, "claude-sonnet-4-5")
    assert s.tokens == accounting.usage_token_total(usage) == 15
    assert s.type_tokens == (10, 5, 0, 0)
    assert s.type_costs[3] == pytest.approx(400 * 6.0 / 1e6)
    assert accounting.usage_token_total({"cache_creation": {"ephemeral_5m_input_tokens": 1000}}) == 0


def test_request_cost_adds_its_lanes_in_order():
    # Python 3.12's sum() compensates float rounding; the cost has always been added lane by
    # lane, and this record is one where the two differ in the last bit.
    prices = {"input": 0.1, "output": 0.5, "cache_read": 0.01}
    usage = {"input_tokens": 12000, "cached_input_tokens": 7, "output_tokens": 150000}
    with patch("accounting.model_pricing", return_value=prices):
        cost = accounting.usage_cost_usd(usage, "claude-haiku-5-5")
        split = accounting.usage_split(usage, "claude-haiku-5-5")
    assert cost == split.cost == (11993 * 0.1 + 150000 * 0.5 + 7 * 0.01) / 1e6


def test_usage_split_keeps_a_bare_total_unsplit():
    # A Codex event carrying only total_tokens has no type to put its tokens in.
    with patch("accounting.model_pricing", return_value=_PRICES):
        s = accounting.usage_split({"total_tokens": 500}, "gpt-5")
    assert (s.tokens, s.cost, s.type_tokens) == (500, 0.0, (0, 0, 0, 0))
    acc = accounting.empty_type_split()
    accounting.type_split_add(acc, s)
    assert accounting.type_split_rows(acc)["unsplit"] == {"tokens": 500, "cost": 0.0}


def test_usage_split_of_nothing():
    assert accounting.usage_split(None, "gpt-5") == (0, 0.0, (0, 0, 0, 0), (0.0, 0.0, 0.0, 0.0))
    # No usage → no "unsplit" row; the four types are always there, at zero.
    assert accounting.type_split_rows(accounting.empty_type_split()) == {
        k: {"tokens": 0, "cost": 0.0} for k in accounting.USAGE_TYPES}


# ---------------------------------------------------------------------------
# Every provider: each window's split adds up to that window's total
# ---------------------------------------------------------------------------

def _claude(root):
    proj = root / "proj"
    proj.mkdir()
    recs = [{"type": "assistant", "timestamp": (_NOW - dt.timedelta(days=d)).isoformat(),
             "requestId": f"r{i}", "message": {"model": "claude-sonnet-4-5", "usage": {
                 "input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 5000,
                 "cache_creation_input_tokens": 400,
                 "cache_creation": {"ephemeral_1h_input_tokens": 100,
                                    "ephemeral_5m_input_tokens": 300}}}}
            for i, d in enumerate(_DAYS)]
    (proj / "s.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    return accounting.local_claude_token_summary(projects_dir=root, now=_NOW)


def _codex(root):
    for i, d in enumerate(_DAYS):
        ts = _NOW - dt.timedelta(days=d)
        day_dir = root / ts.strftime("%Y/%m/%d")
        day_dir.mkdir(parents=True)
        lines = [json.dumps({"timestamp": ts.isoformat(), "payload": {"model": "gpt-5-codex"}}),
                 json.dumps({"timestamp": ts.isoformat(), "payload": {"info": {"last_token_usage": {
                     "input_tokens": 10000, "cached_input_tokens": 4000, "output_tokens": 1000,
                     "reasoning_output_tokens": 200, "total_tokens": 11000}}}})]
        if d == 0:  # plus a bare-total event today: its tokens count, untyped
            lines.append(json.dumps({"timestamp": ts.isoformat(), "payload": {"info": {
                "last_token_usage": {"total_tokens": 500}}}}))
        (day_dir / f"rollout-{i}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return accounting.local_codex_token_summary(sessions_dir=root, now=_NOW)


def _gemini(root):
    msgs = [{"type": "gemini", "timestamp": (_NOW - dt.timedelta(days=d)).isoformat(),
             "model": "gemini-3.5-flash",
             "tokens": {"input": 10000, "output": 800, "cached": 3000, "thoughts": 200,
                        "tool": 500, "total": 11500}}
            for d in _DAYS]
    (root / "session-test.json").write_text(json.dumps({"messages": msgs}), encoding="utf-8")
    return accounting.local_gemini_token_summary(gemini_dir=root, now=_NOW)


def _grok_line(ts, sid, prompt, cached, completion, reasoning=0):
    return json.dumps({"ts": ts.isoformat(), "msg": "shell.turn.inference_done", "sid": sid,
                       "ctx": {"prompt_tokens": prompt, "cached_prompt_tokens": cached,
                               "completion_tokens": completion, "reasoning_tokens": reasoning}})


def _grok(root):
    logs = root / "logs"
    logs.mkdir()
    lines = [_grok_line(_NOW - dt.timedelta(days=d), f"s{i}", 10000, 4000, 1000, 200)
             for i, d in enumerate(_DAYS)]
    (logs / "unified.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return accounting.local_grok_token_summary(logs_dir=logs, sessions_dir=root / "sessions", now=_NOW)


@pytest.mark.parametrize("build", [_claude, _codex, _gemini, _grok],
                         ids=["claude", "codex", "gemini", "grok"])
def test_type_split_adds_up_in_every_window(tmp_path, build):
    with patch("accounting.model_pricing", return_value=_PRICES):
        summary = build(tmp_path)
    assert summary is not None
    _assert_split_adds_up(summary)


def test_claude_window_split_values(tmp_path):
    with patch("accounting.model_pricing", return_value=_PRICES):
        s = _claude(tmp_path)
    # One request per window step: today 1, 7 days 2, 30 days 3.
    for window, n in (("today", 1), ("7d", 2), ("30d", 3)):
        assert _row(s, window, "input") == (1000 * n, pytest.approx(0.003 * n))
        assert _row(s, window, "output") == (200 * n, pytest.approx(0.003 * n))
        assert _row(s, window, "cacheRead") == (5000 * n, pytest.approx(0.0015 * n))
        assert _row(s, window, "cacheWrite") == (400 * n, pytest.approx(0.001725 * n))
    # The 30-day text line still lumps reads and writes into "Cached".
    assert s["breakdown"] == "Input 3K · Output 600 · Cached 16K"


def test_codex_split_reports_a_bare_total_as_unsplit(tmp_path):
    with patch("accounting.model_pricing", return_value=_PRICES):
        s = _codex(tmp_path)
    assert s["tokensToday"] == 11500
    assert _row(s, "today", "input") == (6000, pytest.approx(0.018))
    assert _row(s, "today", "cacheRead") == (4000, pytest.approx(0.0012))
    assert _row(s, "today", "cacheWrite") == (0, 0.0)
    assert _row(s, "today", "unsplit") == (500, 0.0)
    assert _row(s, "30d", "unsplit") == (500, 0.0)


def test_grok_split_includes_archived_days(tmp_path):
    """Days that rotated out of unified.jsonl come from grok_archive.json. One archived
    before the split existed (in/out/cached and one cost) splits its tokens exactly and
    shares its cost out by grok-build's rates; one archived since carries its split."""
    logs = Path.home() / ".grok" / "logs"
    logs.mkdir(parents=True)
    (logs / "unified.jsonl").write_text(
        _grok_line(_NOW, "s0", 10000, 4000, 1000, 200) + "\n", encoding="utf-8")
    legacy_day = (_NOW - dt.timedelta(days=20)).date().isoformat()
    typed_day = (_NOW - dt.timedelta(days=3)).date().isoformat()
    typed = accounting.empty_type_split()
    typed["input"] = {"tokens": 600, "cost": 0.0018}
    typed["output"] = {"tokens": 100, "cost": 0.0015}
    typed["cacheRead"] = {"tokens": 400, "cost": 0.00012}
    archive = {"version": 1, "days": {
        legacy_day: {"tokens": 11000, "cost": 0.0342, "in": 6000, "out": 1000, "cached": 4000,
                     "models": {"Grok Build": {"cost": 0.0342, "tokens": 11000}}},
        typed_day: {"tokens": 1100, "cost": 0.00342, "in": 600, "out": 100, "cached": 400,
                    "types": typed, "models": {"Grok Build": {"cost": 0.00342, "tokens": 1100}}},
    }}
    accounting.GROK_ARCHIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
    accounting.GROK_ARCHIVE_PATH.write_text(json.dumps(archive), encoding="utf-8")

    with patch("accounting.model_pricing", return_value=_PRICES):
        s = accounting.local_grok_token_summary(now=_NOW)

    _assert_split_adds_up(s)
    assert s["tokens30d"] == 11000 + 1100 + 11000
    assert s["cost30d"] == pytest.approx(0.0342 * 2 + 0.00342)
    # 7 days = today's live request + the typed archive day, as archived.
    assert _row(s, "7d", "input") == (6000 + 600, pytest.approx(0.018 + 0.0018))
    assert _row(s, "7d", "cacheRead") == (4000 + 400, pytest.approx(0.0012 + 0.00012))
    # The legacy day's $0.0342 is exactly its tokens at the rates, so the share-out
    # lands on the per-request figures.
    assert _row(s, "30d", "output") == (2100, pytest.approx(0.015 * 2 + 0.0015))
    # Today's live record was banked with its split, for when it rotates out too.
    banked = json.loads(accounting.GROK_ARCHIVE_PATH.read_text())["days"][_NOW.date().isoformat()]
    assert banked["types"]["cacheRead"]["tokens"] == 4000


# ---------------------------------------------------------------------------
# Antigravity: the token ledger (u / c / o per entry; no cache writes)
# ---------------------------------------------------------------------------

def test_antigravity_split_adds_up_in_every_window(tmp_path, monkeypatch):
    today = dt.date.today()
    entries = {}
    for i, d in enumerate(_DAYS):
        day = (today - dt.timedelta(days=d)).isoformat()
        entries[f"conv:{i}"] = {"d": day, "me": 1016, "u": 500, "c": 10_000, "o": 100}
        entries[f"conv@{i}"] = {"d": day, "me": 1016, "u": 300, "c": 20_000, "o": 80}
    # A statusLine session total (cumulative, base rates) splits the same way.
    entries["cli:session-a"] = {"d": today.isoformat(), "model": "Gemini 3.5 Flash (High)",
                                "u": 200, "c": 3_000, "o": 50}
    (tmp_path / "ledger.json").write_text(json.dumps({
        "trackingStarted": today.isoformat(), "entries": entries}))
    # Never reach a live Antigravity language server from a unit test.
    monkeypatch.setattr(agmod, "collect_antigravity_rpc_usage", lambda *a, **k: ({}, {}))
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CONVERSATION_DIRS", ())
    monkeypatch.setattr(costmod, "ANTIGRAVITY_LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CLI_USAGE_PATH", tmp_path / "cli.json")
    prices = {"input": 2.0, "output": 12.0, "cache_read": 0.2}
    monkeypatch.setattr(costmod, "model_pricing", lambda *a, **k: dict(prices))

    s = costmod.antigravity_ledger_cost_summary()

    _assert_split_adds_up(s)
    # Today: two ledger entries + the statusLine session. Cache reads billed per entry.
    assert _row(s, "today", "input") == (500 + 300 + 200, pytest.approx(1000 * 2.0 / 1e6))
    assert _row(s, "today", "cacheRead") == (33_000, pytest.approx(33_000 * 0.2 / 1e6))
    assert _row(s, "today", "output") == (230, pytest.approx(230 * 12.0 / 1e6))
    assert _row(s, "30d", "cacheWrite") == (0, 0.0)
    assert _row(s, "30d", "cacheRead")[0] == 3 * 30_000 + 3_000
