"""Claude advisor sub-inferences: a message that consulted an advisor model reports that
usage in ``usage.iterations`` as ``advisor_message`` entries, outside the top-level usage,
and each is billed at the advisor model's own rates."""
import datetime as dt
import json
from unittest.mock import patch

import pytest

import accounting
from accounting import log_parsers as lp

_NOW = dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.timezone.utc)
_EXECUTOR = "claude-executor-1"
_ADVISOR = "claude-advisor-1"
_PRICES = {
    _EXECUTOR: {"input": 4.0, "output": 20.0, "cache_write": 5.0, "cache_write_1h": 8.0,
                "cache_read": 0.4, "long_context_threshold": 100_000, "long_input": 8.0,
                "long_output": 40.0, "long_cache_read": 0.8, "long_cache_write": 10.0,
                "long_cache_write_1h": 16.0},
    _ADVISOR: {"input": 10.0, "output": 50.0, "cache_write": 12.5, "cache_write_1h": 20.0,
               "cache_read": 1.0, "long_context_threshold": 100_000, "long_input": 30.0,
               "long_output": 150.0, "long_cache_read": 3.0, "long_cache_write": 37.5,
               "long_cache_write_1h": 60.0},
}
# One executor turn: $0.4 + $0.2 = $0.6 for 110,000 tokens.
_TURN = {"input_tokens": 100_000, "output_tokens": 10_000}
_TURN_COST, _TURN_TOKENS = 0.6, 110_000


def _pricing(model, usage_date=None):
    return _PRICES.get(model, {})


def _line(msg_id, usage, advisor=(), model=_EXECUTOR, uuid="u", seconds=0):
    """One transcript line. ``advisor`` is ``(model, usage)`` per sub-inference; the
    executor's own usage is repeated as the ``message`` iteration, as the API does."""
    usage = dict(usage)
    if advisor:
        usage["iterations"] = [
            {"type": "message", **usage},
            *({"type": "advisor_message", **({"model": m} if m else {}), **u}
              for m, u in advisor)]
    stamp = (_NOW + dt.timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
    return json.dumps({"type": "assistant", "timestamp": stamp, "uuid": uuid,
                       "message": {"id": msg_id, "model": model, "usage": usage}})


def _transcript(tmp_path, lines, name="session.jsonl"):
    path = tmp_path / "projects" / "proj" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _summary(tmp_path, cache=None):
    with patch("accounting.pricing.model_pricing", side_effect=_pricing):
        return accounting.local_claude_token_summary(
            projects_dir=tmp_path / "projects", now=_NOW, cache_dir=cache)


def _models(summary):
    return {row["model"]: (row["tokens"], row["cost"]) for row in summary["modelBreakdown"]}


def test_advisor_tokens_bill_at_the_advisor_models_rates_cache_fields_included(tmp_path):
    advisor = {"input_tokens": 20_000, "output_tokens": 4_000,
               "cache_read_input_tokens": 50_000, "cache_creation_input_tokens": 8_000,
               "cache_creation": {"ephemeral_5m_input_tokens": 2_000,
                                  "ephemeral_1h_input_tokens": 6_000}}
    _transcript(tmp_path, [_line("msg_1", _TURN, [(_ADVISOR, advisor)])])
    summary = _summary(tmp_path)
    # input 0.2 + output 0.2 + 1h write 0.12 + 5m write 0.025 + cache read 0.05
    assert _models(summary) == {_EXECUTOR: (_TURN_TOKENS, _TURN_COST),
                                _ADVISOR: (82_000, pytest.approx(0.595))}
    assert summary["tokens30d"] == _TURN_TOKENS + 82_000
    assert summary["cost30d"] == pytest.approx(_TURN_COST + 0.595)
    assert summary["costToday"] == summary["cost7d"] == summary["cost30d"]


def test_the_executors_own_iterations_are_not_billed_twice(tmp_path):
    """The top-level usage already is the sum of the ``message`` iterations."""
    usage = {**_TURN, "iterations": [{"type": "message", "input_tokens": 60_000,
                                      "output_tokens": 4_000},
                                     {"type": "message", "input_tokens": 40_000,
                                      "output_tokens": 6_000}]}
    _transcript(tmp_path, [_line("msg_1", usage)])
    summary = _summary(tmp_path)
    assert _models(summary) == {_EXECUTOR: (_TURN_TOKENS, _TURN_COST)}
    assert "advisor" not in lp._parse_claude_file(tmp_path / "projects/proj/session.jsonl")[0]["u"]


def test_a_message_logged_on_several_lines_and_in_two_transcripts_counts_once(tmp_path):
    advisor = [(_ADVISOR, {"input_tokens": 30_000, "output_tokens": 2_000})]
    lines = [_line("msg_1", _TURN, advisor, uuid=f"u{i}", seconds=i) for i in range(3)]
    _transcript(tmp_path, lines)
    _transcript(tmp_path, lines[:2], name="resumed.jsonl")
    assert _models(_summary(tmp_path)) == {_EXECUTOR: (_TURN_TOKENS, _TURN_COST),
                                           _ADVISOR: (32_000, pytest.approx(0.4))}


def test_each_advisor_call_picks_its_own_prompt_tier(tmp_path):
    """Executor and advisor prompts of 60K each are two requests under the 100K tier, not
    one over it; an advisor call that is itself over the tier bills the long rates alone."""
    turn = {"input_tokens": 60_000, "output_tokens": 1_000}
    short = {"input_tokens": 60_000, "output_tokens": 1_000}
    long = {"input_tokens": 150_000, "output_tokens": 1_000}
    _transcript(tmp_path, [_line("msg_1", turn, [(_ADVISOR, short), (_ADVISOR, short)]),
                           _line("msg_2", turn, [(_ADVISOR, long)], uuid="v")])
    models = _models(_summary(tmp_path))
    assert models[_EXECUTOR] == (122_000, pytest.approx(2 * (0.24 + 0.02)))
    assert models[_ADVISOR] == (273_000, pytest.approx(2 * (0.6 + 0.05) + (4.5 + 0.15)))


def test_fast_mode_is_not_carried_onto_the_advisor_call(tmp_path):
    advisor = [(_ADVISOR, {"input_tokens": 30_000, "output_tokens": 2_000})]
    _transcript(tmp_path, [_line("msg_1", {**_TURN, "speed": "fast"}, advisor)])
    assert _models(_summary(tmp_path)) == {_EXECUTOR: (_TURN_TOKENS, 2 * _TURN_COST),
                                           _ADVISOR: (32_000, pytest.approx(0.4))}


def test_an_advisor_entry_naming_no_model_bills_at_the_messages_model(tmp_path):
    _transcript(tmp_path, [_line("msg_1", _TURN, [(None, dict(_TURN))])])
    assert _models(_summary(tmp_path)) == {_EXECUTOR: (2 * _TURN_TOKENS, 2 * _TURN_COST)}


def test_the_type_split_and_every_bucket_include_the_advisor(tmp_path):
    advisor = {"input_tokens": 20_000, "output_tokens": 4_000,
               "cache_read_input_tokens": 50_000, "cache_creation_input_tokens": 8_000}
    _transcript(tmp_path, [_line("msg_1", _TURN, [(_ADVISOR, advisor)])])
    summary = _summary(tmp_path)
    total_tokens, total_cost = _TURN_TOKENS + 82_000, _TURN_COST + 0.55
    for window in ("today", "7d", "30d"):
        split = summary["typeBreakdown"][window]
        assert {kind: split[kind]["tokens"] for kind in ("input", "output", "cacheRead",
                                                         "cacheWrite")} == {
            "input": 120_000, "output": 14_000, "cacheRead": 50_000, "cacheWrite": 8_000}
        assert sum(split[kind]["cost"] for kind in ("input", "output", "cacheRead",
                                                    "cacheWrite")) == pytest.approx(total_cost)
    for buckets in (summary["weeklyTokenUsage"], summary["monthlyTokenUsage"],
                    summary["hourlyTokenUsage"]):
        assert sum(b["tokens"] for b in buckets) == total_tokens
        assert sum(b["cost"] for b in buckets) == pytest.approx(total_cost)
        assert {m["model"] for b in buckets for m in b["models"]} == {_EXECUTOR, _ADVISOR}


def test_an_advisor_model_with_no_price_of_its_own_is_named(tmp_path):
    advisor = [("claude-zeta-9-9", {"input_tokens": 30_000, "output_tokens": 2_000})]
    _transcript(tmp_path, [_line("msg_1", _TURN, advisor, model="claude-opus-5-5")])
    summary = accounting.local_claude_token_summary(
        projects_dir=tmp_path / "projects", now=_NOW, cache_dir=None)
    assert summary["unpricedModels"] == ["claude-zeta-9-9"]


def test_the_cached_record_keeps_only_what_the_advisor_math_reads(tmp_path):
    advisor = {"input_tokens": 20_000, "output_tokens": 4_000, "cache_read_input_tokens": 0,
               "cache_creation_input_tokens": 6_000, "service_tier": "standard",
               "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                  "ephemeral_1h_input_tokens": 6_000}}
    empty = {"input_tokens": 0, "output_tokens": 0}
    path = _transcript(tmp_path, [_line("msg_1", _TURN, [(_ADVISOR, advisor), (_ADVISOR, empty)])])
    assert lp._parse_claude_file(path)[0]["u"] == {
        **_TURN, "advisor": [[_ADVISOR, {
            "input_tokens": 20_000, "output_tokens": 4_000, "cache_creation_input_tokens": 6_000,
            "cache_creation": {"ephemeral_1h_input_tokens": 6_000}}]]}


def test_a_summary_from_the_parse_cache_equals_a_fresh_one(tmp_path):
    advisor = [(_ADVISOR, {"input_tokens": 30_000, "output_tokens": 2_000,
                           "cache_read_input_tokens": 9_000})]
    path = _transcript(tmp_path, [_line("msg_1", _TURN, advisor)])
    fresh = _summary(tmp_path)
    cache = tmp_path / "cache"
    assert _summary(tmp_path, cache) == fresh
    with patch.object(lp, "_parse_claude_file", side_effect=AssertionError("reparsed")):
        assert _summary(tmp_path, cache) == fresh
    # A message appended later extends the cached state from the file's tail.
    with path.open("a", encoding="utf-8") as handle:
        handle.write(_line("msg_2", _TURN, advisor, uuid="v", seconds=5) + "\n")
    grown = _summary(tmp_path, cache)
    assert grown == _summary(tmp_path)
    assert _models(grown)[_ADVISOR][0] == 2 * 41_000


def test_merging_states_keeps_the_advisor_usage_of_the_winning_line(tmp_path):
    advisor = [(_ADVISOR, {"input_tokens": 30_000, "output_tokens": 2_000})]
    lines = [_line("msg_1", {"input_tokens": 100_000, "output_tokens": 1}, uuid="a"),
             _line("msg_1", _TURN, advisor, uuid="b", seconds=1),
             _line("msg_1", _TURN, advisor, uuid="c", seconds=2),
             _line("msg_2", _TURN, advisor, uuid="d", seconds=3),
             _line("msg_3", _TURN, uuid="e", seconds=4)]
    records = lp._parse_claude_file(_transcript(tmp_path, lines))
    whole = lp._claude_state(records)
    assert [bool(row[2].get("advisor")) for row in whole["g"].values()] == [True, True, False]
    for cut in range(len(records) + 1):
        merged = lp._merge_claude_state(lp._claude_state(records[:cut]),
                                        lp._claude_state(records[cut:]))
        assert merged == whole, cut
