import pytest
import datetime as dt
import json
import os
import time
from pathlib import Path
import sys
from unittest.mock import patch

# Adjust sys.path to find backend modules
sys.path.insert(0, str(Path(__file__).parent.parent / "io.github.dlansama.tallybar" / "contents" / "code"))

import accounting

def test_compact_token_count():
    assert accounting.compact_token_count(0) == "0"
    assert accounting.compact_token_count(150) == "150"
    assert accounting.compact_token_count(1000) == "1K"
    assert accounting.compact_token_count(15400) == "15K"
    assert accounting.compact_token_count(99999) == "100K"
    assert accounting.compact_token_count(1200000) == "1.2M"
    assert accounting.compact_token_count(15400000) == "15.4M"
    assert accounting.compact_token_count(1500000000) == "1.5B"


def test_compact_token_count_fixed_point_at_1e12_does_not_recurse():
    """999_500_000_000 rounds to n=1000 at the "B" tier, the largest unit — there's no
    bigger tier to carry into, so the old code recursed into compact_token_count(1e12),
    which round-trips to the SAME value (a true numeric fixed point) and blew the stack
    (RecursionError). Must format directly against "B" instead."""
    assert accounting.compact_token_count(999_500_000_000) == "1000B"
    assert accounting.compact_token_count(10**12) == "1000B"
    # A comfortably larger value in the same regime must not recurse/crash either.
    assert accounting.compact_token_count(5_000_000_000_000) == "5000B"

def test_exact_usd():
    assert accounting.exact_usd(0.0) == "$0.00"
    assert accounting.exact_usd(123.456) == "$123.46"
    assert accounting.exact_usd(1234.56) == "$1,234.56"
    assert accounting.exact_usd(-10.0) == "$0.00"

def test_compact_usd():
    assert accounting.compact_usd(0.0) == "$0.00"
    assert accounting.compact_usd(0.0015) == "$0.0015"
    assert accounting.compact_usd(0.15) == "$0.15"
    assert accounting.compact_usd(5.5) == "$5.50"
    assert accounting.compact_usd(150.0) == "$150"
    assert accounting.compact_usd(1234.56) == "$1,235"
    assert accounting.compact_usd(-5.0) == "$0.00"

def test_usage_cost_usd_anthropic():
    with patch("accounting.model_pricing") as mock_pricing:
        mock_pricing.return_value = {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30}
        usage = {
            "input_tokens": 100000,
            "output_tokens": 5000,
            "cache_read_input_tokens": 50000,
            "cache_creation_input_tokens": 20000
        }
        cost = accounting.usage_cost_usd(usage, "claude-sonnet-4")
        # Anthropic reports input_tokens, cache_creation_input_tokens and
        # cache_read_input_tokens as SEPARATE, additive buckets (input_tokens
        # already EXCLUDES the cache tokens), so each is billed at its own rate:
        #   100000*3.00 + 5000*15.00 + 20000*3.75 + 50000*0.30 = 465000 -> $0.465.
        # (Subtracting cache_creation off input_tokens — the old behavior — under-billed.)
        assert pytest.approx(cost, 1e-6) == 0.465

def test_usage_cost_usd_anthropic_1h_cache_write():
    # Anthropic bills 1-HOUR cache writes at 2x input ($10/MTok for Opus 4.8) and
    # 5-minute writes at 1.25x ($6.25). The flat cache_creation_input_tokens count is
    # split by the nested cache_creation.ephemeral_1h/5m breakdown and each portion
    # billed at its own rate. Billing ALL of it at the 5m rate (the old behavior)
    # under-billed cache-heavy Claude use by ~8.7% on real data.
    with patch("accounting.model_pricing") as mock_pricing:
        mock_pricing.return_value = {"input": 5.00, "output": 25.00,
                                     "cache_write": 6.25, "cache_write_1h": 10.00, "cache_read": 0.50}
        usage = {
            "input_tokens": 10000, "output_tokens": 2000,
            "cache_read_input_tokens": 40000,
            "cache_creation_input_tokens": 30000,
            "cache_creation": {"ephemeral_1h_input_tokens": 18000,
                               "ephemeral_5m_input_tokens": 12000},
        }
        cost = accounting.usage_cost_usd(usage, "claude-opus-4-8")
        # 10000*5 + 2000*25 + 18000*10 + 12000*6.25 + 40000*0.50
        #  = 50000 + 50000 + 180000 + 75000 + 20000 = 375000 -> $0.375.
        # Old (all 30000 @ 6.25): 50000+50000+187500+20000 = 307500 -> $0.3075.
        assert pytest.approx(cost, 1e-6) == 0.375

def test_usage_cost_usd_cache_write_flat_without_breakdown():
    # No cache_creation breakdown (older logs / non-Anthropic) -> all cache-creation
    # billed at the flat 5m rate. Guards that the 1h split is inert without the field.
    with patch("accounting.model_pricing") as mock_pricing:
        mock_pricing.return_value = {"input": 5.00, "output": 25.00,
                                     "cache_write": 6.25, "cache_write_1h": 10.00, "cache_read": 0.50}
        usage = {"input_tokens": 10000, "output_tokens": 2000,
                 "cache_creation_input_tokens": 30000}
        cost = accounting.usage_cost_usd(usage, "claude-opus-4-8")
        # 10000*5 + 2000*25 + 30000*6.25 = 50000 + 50000 + 187500 = 287500 -> $0.2875
        assert pytest.approx(cost, 1e-6) == 0.2875

def test_usage_cost_usd_1h_breakdown_clamped_to_cache_create():
    # A malformed breakdown whose 1h count exceeds cache_creation_input_tokens must
    # NOT over-bill: the 1h portion is clamped to cache_create, the remainder is 0.
    with patch("accounting.model_pricing") as mock_pricing:
        mock_pricing.return_value = {"input": 5.00, "output": 25.00,
                                     "cache_write": 6.25, "cache_write_1h": 10.00, "cache_read": 0.50}
        usage = {"input_tokens": 0, "output_tokens": 0,
                 "cache_creation_input_tokens": 1000,
                 "cache_creation": {"ephemeral_1h_input_tokens": 999999}}
        cost = accounting.usage_cost_usd(usage, "claude-opus-4-8")
        # all 1000 (clamped) @ 10.00 -> $0.01, never 999999 @ 10.00.
        assert pytest.approx(cost, 1e-6) == 0.01

def test_usage_cost_usd_1h_breakdown_without_flat_field_is_derived():
    # Defensive: a record carrying ONLY the nested breakdown (no flat
    # cache_creation_input_tokens) must still bill — the volume is derived from
    # ephemeral_1h + ephemeral_5m so the cache-write tokens aren't silently dropped to $0.
    with patch("accounting.model_pricing") as mock_pricing:
        mock_pricing.return_value = {"input": 5.00, "output": 25.00,
                                     "cache_write": 6.25, "cache_write_1h": 10.00, "cache_read": 0.50}
        usage = {"input_tokens": 0, "output_tokens": 0,
                 "cache_creation": {"ephemeral_1h_input_tokens": 18000,
                                    "ephemeral_5m_input_tokens": 12000}}
        cost = accounting.usage_cost_usd(usage, "claude-opus-4-8")
        # 18000 @ 10.00 (1h) + 12000 @ 6.25 (5m) = 180000 + 75000 = 255000 -> $0.255.
        # Without the fallback this would be $0 (cache_create == 0).
        assert pytest.approx(cost, 1e-6) == 0.255

def test_usage_cost_usd_openai():
    with patch("accounting.model_pricing") as mock_pricing:
        mock_pricing.return_value = {"input": 2.50, "output": 15.00, "cache_read": 0.25}
        usage = {
            "input_tokens": 100000,
            "cached_input_tokens": 40000,
            "output_tokens": 5000
        }
        cost = accounting.usage_cost_usd(usage, "gpt-5")
        assert pytest.approx(cost, 1e-6) == 0.235

def test_usage_token_total_and_breakdown():
    usage = {
        "input_tokens": 100000,
        "cached_input_tokens": 40000,
        "output_tokens": 5000,
        "thoughts": 1000,
        "cache_creation_input_tokens": 10000,
        "cache_read_input_tokens": 20000
    }
    uncached, output, cached = accounting.usage_token_breakdown(usage)
    assert uncached == 60000   # input_tokens(100000) - cached subset(40000)
    assert output == 6000      # Gemini "thoughts" are SEPARATE from output: 5000 + 1000
    assert cached == 70000     # cached_input_tokens(40000) + cache_creation(10000) + cache_read(20000)

    total = accounting.usage_token_total(usage)
    assert total == 136000     # 60000 + 6000 + 70000 (no explicit total key)

def test_usage_cost_and_breakdown_with_tool_tokens():
    with patch("accounting.model_pricing") as mock_pricing:
        mock_pricing.return_value = {
            "input": 2.00,
            "output": 10.00,
            "cache_read": 0.20
        }
        usage = {
            "input_tokens": 100000,
            "cached_input_tokens": 30000,
            "output_tokens": 4000,
            "tool": 5000,
            "thoughts": 1000
        }
        uncached, output, cached = accounting.usage_token_breakdown(usage)
        assert uncached == 75000   # (input 100000 - cached 30000) + tool 5000
        assert output == 5000      # output 4000 + separate thoughts 1000
        assert cached == 30000

        cost = accounting.usage_cost_usd(usage, "mock-model")
        # Gemini: tool tokens are SEPARATE (additive, input-rate) and thoughts are
        # SEPARATE (additive, output-rate) — neither is a subset of input/output:
        #   70000*2.00 + (4000+1000)*10.00 + 5000*2.00 + 30000*0.20 = 206000 -> $0.206.
        assert pytest.approx(cost, 1e-6) == 0.206

        total = accounting.usage_token_total(usage)
        assert total == 110000     # 75000 + 5000 + 30000

def test_usage_token_total_and_breakdown_helper_matches_separate_calls():
    # The combined helper must be behaviour-identical to calling usage_token_total()
    # and usage_token_breakdown() separately, INCLUDING the case where an explicit
    # total field is present and does NOT equal the breakdown sum (OpenAI-style records
    # carry total_tokens where cached ⊂ prompt, so the sum can legitimately differ).
    explicit_mismatch = {
        "total_tokens": 99999,   # deliberately inconsistent with the breakdown below
        "input_tokens": 10000,
        "output_tokens": 2000,
    }
    total, breakdown = accounting.usage_token_total_and_breakdown(explicit_mismatch)
    assert total == 99999                      # explicit field wins over the breakdown sum
    assert breakdown == (10000, 2000, 0)
    assert total == accounting.usage_token_total(explicit_mismatch)
    assert breakdown == accounting.usage_token_breakdown(explicit_mismatch)

    # No explicit total (Anthropic-style) -> total falls back to the breakdown sum.
    no_total = {"input_tokens": 100000, "output_tokens": 5000, "cache_read_input_tokens": 20000}
    total, breakdown = accounting.usage_token_total_and_breakdown(no_total)
    assert breakdown == (100000, 5000, 20000)
    assert total == 125000                     # 100000 + 5000 + 20000
    assert total == accounting.usage_token_total(no_total)
    assert breakdown == accounting.usage_token_breakdown(no_total)

    # Non-dict degrades to (0, (0, 0, 0)), matching the individual functions.
    assert accounting.usage_token_total_and_breakdown(None) == (0, (0, 0, 0))
    assert accounting.usage_token_total(None) == 0
    assert accounting.usage_token_breakdown(None) == (0, 0, 0)


def test_empty_weekly_and_monthly_buckets():
    current = dt.datetime(2026, 5, 27, 12, 0, 0, tzinfo=dt.timezone.utc)
    weekly = accounting.empty_weekly_token_buckets(current)
    assert len(weekly) == 7
    assert "2026-05-27" in weekly
    assert weekly["2026-05-27"]["day"] == "Wed"

    monthly = accounting.empty_monthly_token_buckets(current)
    assert len(monthly) == 42
    assert monthly["2026-05-01"]["inMonth"] is True
    assert monthly["2026-06-01"]["inMonth"] is False

def test_codex_credit_balance():
    rate_limits = {
        "credits": {
            "balance": 1500.0,
            "currency": "credits"
        }
    }
    res = accounting.codex_credit_balance(rate_limits)
    assert res is not None
    assert res["label"] == "Credits"
    assert res["amount"] == 1500.0
    assert res["currency"] == "credits"
    assert "Credits: 2K available" in res["detail"]

def test_antigravity_user_tier():
    data = {
        "userStatus": {
            "userTier": {
                "name": "Google AI Ultra Premium",
                "id": "g1-ultra-tier"
            }
        }
    }
    assert accounting.antigravity_user_tier(data) == "Google AI Ultra Premium"
    assert accounting.antigravity_user_tier({"userStatus": {"userTier": {"id": "g1-ultra-tier"}}}) == "Google AI Ultra"
    assert accounting.antigravity_user_tier({"userStatus": {"userTier": {"id": "g1-pro-tier"}}}) == "Google AI Pro"
    assert accounting.antigravity_user_tier(None) == ""

def test_antigravity_credit_state():
    data = {
        "userStatus": {
            "planStatus": {
                "availablePromptCredits": 50000,
                "availableFlowCredits": 10000,
                "planInfo": {
                    "monthlyPromptCredits": 100000,
                    "monthlyFlowCredits": 20000
                }
            }
        }
    }
    balance, limit = accounting.antigravity_credit_state(data)
    assert balance is not None
    assert balance["amount"] == 60000
    assert limit is not None
    assert limit["percent"] == 50.0


def test_antigravity_credit_state_orphan_available_excluded():
    # flow_limit=None → flow pool excluded from limit math; only prompt pool contributes.
    # prompt_avail=500, prompt_limit=50000 → used=49500, pct≈99.0%.
    # flow_avail=100 is NOT netted against prompt_limit (that would give 98.8%).
    data = {
        "userStatus": {
            "planStatus": {
                "availablePromptCredits": 500,
                "availableFlowCredits": 100,
                "planInfo": {
                    "monthlyPromptCredits": 50000,
                    # monthlyFlowCredits intentionally absent → flow_limit=None
                }
            }
        }
    }
    balance, limit = accounting.antigravity_credit_state(data)
    assert balance is not None
    # Balance shows all available credits for display purposes.
    assert balance["amount"] == 600  # 500 + 100
    assert limit is not None
    assert limit["percent"] == pytest.approx(99.0, rel=1e-3)   # 49500/50000
    assert limit["used"] == pytest.approx(49500.0, rel=1e-6)
    assert limit["limit"] == pytest.approx(50000.0, rel=1e-6)


def test_antigravity_credit_state_both_pools_complete():
    # Both pools have valid available + limit — blended math unchanged.
    data = {
        "userStatus": {
            "planStatus": {
                "availablePromptCredits": 50000,
                "availableFlowCredits": 10000,
                "planInfo": {
                    "monthlyPromptCredits": 100000,
                    "monthlyFlowCredits": 20000,
                }
            }
        }
    }
    balance, limit = accounting.antigravity_credit_state(data)
    assert balance is not None
    assert balance["amount"] == 60000
    assert limit is not None
    assert limit["percent"] == pytest.approx(50.0, rel=1e-3)   # 60000 used of 120000


def test_enrich_ui_formatting():
    providers = {
        "claude": {
            "limits": [
                {
                    "label": "Session",
                    "percent": 45.54,
                    "detailText": "Raw detail"
                },
                {
                    "label": "Credits",
                    "unit": "USD",
                    "used": 10.0,
                    "limit": 100.0,
                    "percent": 10.0
                }
            ],
            "creditBalance": {
                "amount": 25.0,
                "currency": "USD",
                "detail": "Credits: $ 25.00 available"
            }
        }
    }
    accounting.enrich_ui_formatting(providers)
    
    claude = providers["claude"]
    assert claude["formattedExtraUsageDetail"] == "Credits: $ 25.00 available"
    
    limits = claude["limits"]
    assert limits[0]["isExtraUsage"] is False
    assert limits[0]["formattedUsedText"] == "46% used"
    
    assert limits[1]["isExtraUsage"] is True
    assert limits[1]["formattedUsedText"] == "10% used"


# ---------------------------------------------------------------------------
# Local per-provider token-summary readers (TEST-4). These exercise the real
# file-discovery / parsing / dedup / bucketing paths AND the corrected cost math
# end-to-end. Pricing is patched to a flat $10/MTok so each expected dollar value
# is a regression guard for a specific per-provider accounting rule.
# ---------------------------------------------------------------------------

_FLAT_PRICES = {"input": 10.0, "output": 10.0, "cache_write": 10.0, "cache_read": 10.0}
_NOW = dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.timezone.utc)


def test_local_claude_token_summary(tmp_path):
    # Anthropic shape: input_tokens EXCLUDES the cache buckets (they are additive).
    rec = {
        "type": "assistant",
        "timestamp": _NOW.isoformat(),
        "requestId": "r1",
        "message": {"model": "claude-sonnet-4", "usage": {
            "input_tokens": 100000, "output_tokens": 5000,
            "cache_creation_input_tokens": 20000, "cache_read_input_tokens": 50000}},
    }
    proj = tmp_path / "proj"
    proj.mkdir()
    # The SAME requestId twice must be de-duplicated (counted once).
    (proj / "session.jsonl").write_text(json.dumps(rec) + "\n" + json.dumps(rec) + "\n", encoding="utf-8")

    with patch("accounting.model_pricing", return_value=_FLAT_PRICES):
        summary = accounting.local_claude_token_summary(projects_dir=tmp_path, now=_NOW)

    assert summary is not None
    assert summary["source"] == "local-claude-logs"
    # (100000 + 5000 + 20000 + 50000) * 10 / 1e6 = $1.75. With the old bug
    # (cache_creation subtracted from input_tokens) it would be $1.55.
    assert "Today: $1.75" in summary["today"]
    assert "175K tok" in summary["today"]
    # The single record is today, so "Last 7 days" (sum of the daily buckets) matches it.
    assert "Last 7 days: $1.75" in summary["last7Days"]
    assert "175K tok" in summary["last7Days"]


def test_local_claude_summary_uses_final_streamed_output(tmp_path):
    # Claude streams one jsonl line per content block: same requestId, CONSTANT
    # input/cache, but output_tokens grows from a partial first value to the final
    # (stop_reason-bearing) line. The summary must bill the FINAL (largest) output,
    # not the first-seen partial — else output (and volume) is undercounted ~20%.
    base_usage = {"input_tokens": 100000, "cache_creation_input_tokens": 20000,
                  "cache_read_input_tokens": 50000}
    partial = {"type": "assistant", "timestamp": _NOW.isoformat(), "requestId": "r1",
               "message": {"model": "claude-sonnet-4",
                           "usage": {**base_usage, "output_tokens": 5}}}
    final = {"type": "assistant", "timestamp": _NOW.isoformat(), "requestId": "r1",
             "stop_reason": "end_turn",
             "message": {"model": "claude-sonnet-4",
                         "usage": {**base_usage, "output_tokens": 5000}}}
    proj = tmp_path / "proj"
    proj.mkdir()
    # Partial line FIRST, final line second (real streaming order).
    (proj / "session.jsonl").write_text(
        json.dumps(partial) + "\n" + json.dumps(final) + "\n", encoding="utf-8")

    with patch("accounting.model_pricing", return_value=_FLAT_PRICES):
        summary = accounting.local_claude_token_summary(projects_dir=tmp_path, now=_NOW)

    assert summary is not None
    # Final output 5000 billed (input/cache counted once):
    #   (100000 + 5000 + 20000 + 50000) * 10 / 1e6 = $1.75 ; 175K tok.
    # First-wins (the old bug) would bill output 5 -> $1.70 and 170K tok.
    assert "Today: $1.75" in summary["today"]
    assert "175K tok" in summary["today"]


def test_local_codex_token_summary(tmp_path):
    # OpenAI/Codex shape: cached_input_tokens ⊂ input_tokens, reasoning ⊂ output_tokens.
    lines = [
        json.dumps({"timestamp": _NOW.isoformat(), "payload": {"model": "gpt-5-codex"}}),
        json.dumps({"timestamp": _NOW.isoformat(), "payload": {"info": {"last_token_usage": {
            "input_tokens": 100000, "cached_input_tokens": 40000,
            "output_tokens": 10000, "reasoning_output_tokens": 2000, "total_tokens": 110000}}}}),
    ]
    sess = tmp_path / "2026" / "05" / "28"
    sess.mkdir(parents=True)
    (sess / "rollout.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    with patch("accounting.model_pricing", return_value=_FLAT_PRICES):
        summary = accounting.local_codex_token_summary(sessions_dir=tmp_path, now=_NOW)

    assert summary is not None
    assert summary["source"] == "local-codex-logs"
    # (60000 uncached + 10000 output + 40000 cached) * 10 / 1e6 = $1.10. reasoning is
    # a subset of output (not added on top); cached is peeled out of input. If reasoning
    # were double-counted it would be $1.12.
    assert "Today: $1.10" in summary["today"]
    assert "110K tok" in summary["today"]


def test_local_gemini_token_summary(tmp_path):
    # Gemini short shape: thoughts + tool are SEPARATE (additive); cached ⊂ input.
    session = {"messages": [{
        "type": "gemini",
        "timestamp": _NOW.isoformat(),
        "model": "gemini-3.5-flash",
        "tokens": {"input": 100000, "output": 4000, "cached": 30000,
                   "thoughts": 1000, "tool": 5000, "total": 110000},
    }]}
    (tmp_path / "session-test.json").write_text(json.dumps(session), encoding="utf-8")

    with patch("accounting.model_pricing", return_value=_FLAT_PRICES):
        summary = accounting.local_gemini_token_summary(gemini_dir=tmp_path, now=_NOW)

    assert summary is not None
    assert summary["source"] == "local-gemini-logs"
    # (70000 uncached input + (4000+1000) output + 5000 tool + 30000 cached) * 10 / 1e6
    # = $1.10. thoughts billed as output (not subtracted/clamped); tool added at input
    # rate. With the old "output - thoughts" clamp it would be $1.09.
    assert "Today: $1.10" in summary["today"]
    assert "110K tok" in summary["today"]


def test_local_token_summary_returns_none_when_empty(tmp_path):
    # No matching files / no usage -> None (so the widget shows no cost row).
    assert accounting.local_claude_token_summary(projects_dir=tmp_path, now=_NOW) is None
    assert accounting.local_codex_token_summary(sessions_dir=tmp_path, now=_NOW) is None
    assert accounting.local_gemini_token_summary(gemini_dir=tmp_path, now=_NOW) is None
    # Grok: a missing logs dir -> None even though a sessions dir exists.
    (tmp_path / "sessions").mkdir()
    assert accounting.local_grok_token_summary(
        logs_dir=tmp_path / "logs", sessions_dir=tmp_path / "sessions", now=_NOW) is None


def _grok_done(ts, sid, prompt, cached, completion, reasoning=0):
    """One ~/.grok/logs/unified.jsonl per-turn usage line."""
    return json.dumps({"ts": ts.isoformat(), "msg": "shell.turn.inference_done", "sid": sid,
                       "ctx": {"prompt_tokens": prompt, "cached_prompt_tokens": cached,
                               "completion_tokens": completion, "reasoning_tokens": reasoning}})


def _grok_session(sessions_dir, sid, model):
    """A ~/.grok/sessions/<urlenc-cwd>/<sid>/summary.json naming the session's model."""
    sdir = sessions_dir / "%2Fhome%2Fproj" / sid
    sdir.mkdir(parents=True)
    (sdir / "summary.json").write_text(
        json.dumps({"info": {"id": sid}, "current_model_id": model}), encoding="utf-8")


def test_local_grok_token_summary(tmp_path):
    # Grok Build CLI usage is OpenAI-shaped: cached_prompt_tokens ⊂ prompt_tokens,
    # reasoning_tokens ⊂ completion_tokens. Same billing math as the Codex case.
    logs = tmp_path / "logs"
    logs.mkdir()
    sessions = tmp_path / "sessions"
    _grok_session(sessions, "S1", "grok-composer-2.5-fast")
    lines = [
        # Noise: non-inference_done lines (even ones carrying token-like fields) are ignored.
        json.dumps({"ts": _NOW.isoformat(), "msg": "AuthManager::new", "ctx": {}}),
        json.dumps({"ts": _NOW.isoformat(), "msg": "shell.turn.first_token",
                    "ctx": {"prompt_tokens": 999999}}),
        _grok_done(_NOW, "S1", 100000, 40000, 10000, 2000),
    ]
    (logs / "unified.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    with patch("accounting.model_pricing", return_value=_FLAT_PRICES):
        summary = accounting.local_grok_token_summary(
            logs_dir=logs, sessions_dir=sessions, now=_NOW)

    assert summary is not None
    assert summary["source"] == "local-grok-logs"
    # (60000 uncached + 10000 output + 40000 cached) * 10 / 1e6 = $1.10. reasoning is a
    # subset of completion (not added); cached is peeled out of prompt. The noise lines'
    # 999999 prompt_tokens must NOT leak in (would blow the total far past 110K).
    assert "Today: $1.10" in summary["today"]
    assert "110K tok" in summary["today"]
    # Model resolved from summary.json and mapped to display name via _display_grok_model.
    models = [r["model"] for r in summary["modelBreakdown"]]
    assert "Composer 2.5" in models  # "grok-composer-2.5-fast" maps to "Composer 2.5"


def test_local_grok_summary_defaults_unmapped_sid_to_grok_build(tmp_path):
    # A turn whose sid has no summary.json falls back to the provider default model.
    logs = tmp_path / "logs"
    logs.mkdir()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (logs / "unified.jsonl").write_text(
        _grok_done(_NOW, "UNKNOWN", 100000, 40000, 10000, 2000) + "\n", encoding="utf-8")

    with patch("accounting.model_pricing", return_value=_FLAT_PRICES):
        summary = accounting.local_grok_token_summary(
            logs_dir=logs, sessions_dir=sessions, now=_NOW)

    assert summary is not None
    # No summary.json -> falls back to grok-build default, mapped to display name.
    assert "Grok Build" in [r["model"] for r in summary["modelBreakdown"]]


def test_parse_cache_grok_transparent(tmp_path):
    # The single-file unified.jsonl source must be cache-transparent: cold, warm, and
    # uncached parses all agree (model resolved out-of-band each time).
    logs = tmp_path / "logs"
    logs.mkdir()
    sessions = tmp_path / "sessions"
    _grok_session(sessions, "S1", "grok-build")
    (logs / "unified.jsonl").write_text(
        _grok_done(_NOW, "S1", 100000, 40000, 10000, 2000) + "\n", encoding="utf-8")
    cache = tmp_path / "cache"

    with patch("accounting.model_pricing", return_value=_FLAT_PRICES):
        fresh = accounting.local_grok_token_summary(
            logs_dir=logs, sessions_dir=sessions, now=_NOW, cache_dir=None)
        cold = accounting.local_grok_token_summary(
            logs_dir=logs, sessions_dir=sessions, now=_NOW, cache_dir=cache)
        warm = accounting.local_grok_token_summary(
            logs_dir=logs, sessions_dir=sessions, now=_NOW, cache_dir=cache)

    assert fresh is not None
    assert fresh == cold == warm
    assert "Today: $1.10" in warm["today"]


def test_parse_cache_gemini_transparent(tmp_path):
    # Gemini's walker prunes antigravity* subtrees; the cache must be transparent AND
    # keep honouring the prune (a session file inside an antigravity dir stays invisible).
    root = tmp_path / "gemini"
    (root / "chats").mkdir(parents=True)
    ag = root / "antigravity-cli" / "conversations"
    ag.mkdir(parents=True)
    session = {"messages": [{
        "type": "gemini", "timestamp": _NOW.isoformat(), "model": "gemini-3.5-flash",
        "tokens": {"input": 100000, "output": 4000, "cached": 30000,
                   "thoughts": 1000, "tool": 5000, "total": 110000},
    }]}
    (root / "chats" / "session-a.json").write_text(json.dumps(session), encoding="utf-8")
    (ag / "session-hidden.json").write_text(json.dumps(session), encoding="utf-8")  # pruned
    cache = tmp_path / "cache"

    with patch("accounting.model_pricing", return_value=_FLAT_PRICES):
        fresh = accounting.local_gemini_token_summary(gemini_dir=root, now=_NOW, cache_dir=None)
        cold = accounting.local_gemini_token_summary(gemini_dir=root, now=_NOW, cache_dir=cache)
        warm = accounting.local_gemini_token_summary(gemini_dir=root, now=_NOW, cache_dir=cache)

    assert fresh is not None
    assert fresh == cold == warm
    assert "Today: $1.10" in warm["today"]   # the pruned duplicate would make it $2.20
    assert (cache / "gemini_logs.json").is_file()


# ---------------------------------------------------------------------------
# Per-file parse cache: the local-log summarizers reuse a (path,mtime,size)-keyed
# extract cache so a 5-minute refresh doesn't re-json.loads ~1 GB of unchanged
# logs. These guard that the cache is TRANSPARENT (same result as a fresh parse),
# correctly INVALIDATES on change/grow/delete, and SELF-HEALS a corrupt/bumped
# cache — i.e. it can never silently desync the displayed cost from the logs.
# ---------------------------------------------------------------------------

def _claude_rec(req, out, ts=None, model="claude-sonnet-4"):
    return {"type": "assistant", "timestamp": (ts or _NOW).isoformat(), "requestId": req,
            "message": {"model": model, "usage": {
                "input_tokens": 100000, "output_tokens": out,
                "cache_creation_input_tokens": 20000, "cache_read_input_tokens": 50000}}}


def test_parse_cache_is_transparent_and_warm(tmp_path):
    # A cached run must equal a no-cache run, and a second (warm) run must equal the
    # first (cold) — the cache changes speed, never the numbers.
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.jsonl").write_text(json.dumps(_claude_rec("r1", 5000)) + "\n", encoding="utf-8")
    (proj / "b.jsonl").write_text(json.dumps(_claude_rec("r2", 3000)) + "\n", encoding="utf-8")
    cache = tmp_path / "cache"

    with patch("accounting.model_pricing", return_value=_FLAT_PRICES):
        fresh = accounting.local_claude_token_summary(projects_dir=proj, now=_NOW, cache_dir=None)
        cold = accounting.local_claude_token_summary(projects_dir=proj, now=_NOW, cache_dir=cache)
        warm = accounting.local_claude_token_summary(projects_dir=proj, now=_NOW, cache_dir=cache)

    assert fresh is not None
    assert fresh == cold == warm
    # The cache file was actually written (so the warm path read it, not re-parsed).
    assert (cache / "claude_logs.json").is_file()


def test_parse_cache_invalidates_on_file_growth(tmp_path):
    # Appending a NEW request to a file (changing mtime+size) must be picked up on the
    # next run even though that file is already in the cache — else new usage is invisible.
    proj = tmp_path / "proj"
    proj.mkdir()
    f = proj / "s.jsonl"
    f.write_text(json.dumps(_claude_rec("r1", 5000)) + "\n", encoding="utf-8")
    cache = tmp_path / "cache"

    with patch("accounting.model_pricing", return_value=_FLAT_PRICES):
        first = accounting.local_claude_token_summary(projects_dir=proj, now=_NOW, cache_dir=cache)
        # Append a second request. Bump mtime explicitly so the test can't lose to
        # same-second-write granularity (size also changes, which alone invalidates).
        with f.open("a", encoding="utf-8") as h:
            h.write(json.dumps(_claude_rec("r2", 7000)) + "\n")
        st = f.stat()
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        second = accounting.local_claude_token_summary(projects_dir=proj, now=_NOW, cache_dir=cache)
        # Ground truth: a no-cache parse of the grown file.
        truth = accounting.local_claude_token_summary(projects_dir=proj, now=_NOW, cache_dir=None)

    assert second == truth
    assert second != first   # the new request changed the totals


def test_parse_cache_prunes_deleted_files(tmp_path):
    # Removing a file must drop its usage from the next run (and from the cache).
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "keep.jsonl").write_text(json.dumps(_claude_rec("r1", 5000)) + "\n", encoding="utf-8")
    gone = proj / "gone.jsonl"
    gone.write_text(json.dumps(_claude_rec("r2", 9000)) + "\n", encoding="utf-8")
    cache = tmp_path / "cache"

    with patch("accounting.model_pricing", return_value=_FLAT_PRICES):
        accounting.local_claude_token_summary(projects_dir=proj, now=_NOW, cache_dir=cache)
        gone.unlink()
        after = accounting.local_claude_token_summary(projects_dir=proj, now=_NOW, cache_dir=cache)
        truth = accounting.local_claude_token_summary(projects_dir=proj, now=_NOW, cache_dir=None)

    assert after == truth
    # The pruned path no longer appears in the persisted cache.
    persisted = json.loads((cache / "claude_logs.json").read_text(encoding="utf-8"))
    assert not any(p.endswith("gone.jsonl") for p in persisted["files"])


def test_parse_cache_rebuilds_on_corruption_and_version_bump(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "s.jsonl").write_text(json.dumps(_claude_rec("r1", 5000)) + "\n", encoding="utf-8")
    cache = tmp_path / "cache"
    cache.mkdir()
    cache_file = cache / "claude_logs.json"

    with patch("accounting.model_pricing", return_value=_FLAT_PRICES):
        truth = accounting.local_claude_token_summary(projects_dir=proj, now=_NOW, cache_dir=None)

        # (a) Garbage cache -> ignored, full reparse, correct result, cache rewritten valid.
        cache_file.write_text("}{ not json", encoding="utf-8")
        with patch("accounting.model_pricing", return_value=_FLAT_PRICES):
            assert accounting.local_claude_token_summary(projects_dir=proj, now=_NOW, cache_dir=cache) == truth
        json.loads(cache_file.read_text(encoding="utf-8"))   # now valid again

        # (b) Stale parser version -> treated as empty, full reparse.
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        data["version"] = data["version"] + 999
        cache_file.write_text(json.dumps(data), encoding="utf-8")
        with patch("accounting.model_pricing", return_value=_FLAT_PRICES):
            assert accounting.local_claude_token_summary(projects_dir=proj, now=_NOW, cache_dir=cache) == truth


def test_parse_cache_codex_transparent(tmp_path):
    # Same transparency guarantee for the Codex summarizer (whose extractor also tracks
    # the active model per file — cached records must carry the resolved model).
    sess = tmp_path / "2026" / "05" / "28"
    sess.mkdir(parents=True)
    lines = [
        json.dumps({"timestamp": _NOW.isoformat(), "payload": {"model": "gpt-5-codex"}}),
        json.dumps({"timestamp": _NOW.isoformat(), "payload": {"info": {"last_token_usage": {
            "input_tokens": 100000, "cached_input_tokens": 40000,
            "output_tokens": 10000, "reasoning_output_tokens": 2000, "total_tokens": 110000}}}}),
    ]
    (sess / "rollout.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    cache = tmp_path / "cache"

    with patch("accounting.model_pricing", return_value=_FLAT_PRICES):
        fresh = accounting.local_codex_token_summary(sessions_dir=tmp_path, now=_NOW, cache_dir=None)
        cold = accounting.local_codex_token_summary(sessions_dir=tmp_path, now=_NOW, cache_dir=cache)
        warm = accounting.local_codex_token_summary(sessions_dir=tmp_path, now=_NOW, cache_dir=cache)

    assert fresh is not None
    assert fresh == cold == warm
    assert "Today: $1.10" in warm["today"]   # model resolved from cache, priced correctly


def test_local_summary_deadline_stops_walk_without_crashing(tmp_path):
    # A deadline already in the past must stop the file walk before it does any I/O (not
    # raise, not hang) -- a slow cold parse mustn't burn the whole cost-scan budget. And
    # the cut-short walk must NOT persist a partial file map into the on-disk parse cache
    # (the cache must stay a pure function of a COMPLETE walk's file bytes, never a
    # partial/inconsistent snapshot masquerading as a full one).
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.jsonl").write_text(json.dumps(_claude_rec("r1", 5000)) + "\n", encoding="utf-8")
    cache = tmp_path / "cache"

    with patch("accounting.model_pricing", return_value=_FLAT_PRICES):
        expired = accounting.local_claude_token_summary(
            projects_dir=proj, now=_NOW, cache_dir=cache, deadline=time.time() - 1000)
    assert expired is None                                  # no time to parse anything
    assert not (cache / "claude_logs.json").exists()         # incomplete walk -> no cache write

    # A deadline comfortably in the future must behave exactly like no deadline at all.
    with patch("accounting.model_pricing", return_value=_FLAT_PRICES):
        complete = accounting.local_claude_token_summary(
            projects_dir=proj, now=_NOW, cache_dir=cache, deadline=time.time() + 1000)
        truth = accounting.local_claude_token_summary(projects_dir=proj, now=_NOW, cache_dir=None)
    assert complete == truth
    assert (cache / "claude_logs.json").is_file()             # complete walk -> cache written


def test_local_summary_deadline_threads_through_gemini_walker(tmp_path):
    # Gemini's summarizer passes BOTH walker= and deadline= to _cached_log_records --
    # guard that the two kwargs coexist correctly (an expired deadline still short-circuits
    # the walker-driven walk cleanly) and that an ample deadline is fully transparent.
    session = {"messages": [{
        "type": "gemini", "timestamp": _NOW.isoformat(), "model": "gemini-3.5-flash",
        "tokens": {"input": 100000, "output": 4000, "cached": 30000,
                   "thoughts": 1000, "tool": 5000, "total": 110000},
    }]}
    (tmp_path / "session-a.json").write_text(json.dumps(session), encoding="utf-8")
    cache = tmp_path / "cache"

    with patch("accounting.model_pricing", return_value=_FLAT_PRICES):
        expired = accounting.local_gemini_token_summary(
            gemini_dir=tmp_path, now=_NOW, cache_dir=cache, deadline=time.time() - 1000)
    assert expired is None
    assert not (cache / "gemini_logs.json").exists()

    with patch("accounting.model_pricing", return_value=_FLAT_PRICES):
        complete = accounting.local_gemini_token_summary(
            gemini_dir=tmp_path, now=_NOW, cache_dir=cache, deadline=time.time() + 1000)
    assert complete is not None
    assert "Today: $1.10" in complete["today"]
    assert (cache / "gemini_logs.json").is_file()


def test_empty_hourly_buckets():
    current = dt.datetime(2026, 5, 27, 9, 30, 0, tzinfo=dt.timezone.utc)
    hourly = accounting.empty_hourly_token_buckets(current)
    assert len(hourly) == 24
    # 12-hour clock labels with a/p suffix (axis tags for the Day view).
    assert hourly[0]["label"] == "12a"
    assert hourly[11]["label"] == "11a"
    assert hourly[12]["label"] == "12p"
    assert hourly[23]["label"] == "11p"
    assert all(b["tokens"] == 0 and b["cost"] == 0.0 for b in hourly.values())

    serialized = accounting.hourly_token_usage(hourly)
    assert len(serialized) == 24
    assert serialized[0] == {"hour": 0, "label": "12a", "tokens": 0, "cost": 0.0, "models": []}


def test_bucket_models_breakdown_serialized():
    # Each day/hour/month bucket carries its own per-model attribution (top-4 by cost),
    # rendered by the cost popout's pinned click-tooltip.
    current = dt.datetime(2026, 5, 27, 9, 30, 0, tzinfo=dt.timezone.utc)
    daily = accounting.empty_weekly_token_buckets(current)
    key = current.date().isoformat()
    accounting.bucket_add(daily[key], 1000, 0.50, "claude-opus-4-6")
    accounting.bucket_add(daily[key], 200, 0.10, "claude-opus-4-6")
    accounting.bucket_add(daily[key], 300, 0.90, "gemini-3-pro")
    for extra in ("m3", "m4", "m5"):  # 5 distinct models -> top-4 cap drops the cheapest
        accounting.bucket_add(daily[key], 10, 0.01, extra)

    bucket = next(b for b in accounting.weekly_token_usage(daily) if b["date"] == key)
    assert bucket["tokens"] == 1530
    rows = bucket["models"]
    assert len(rows) == 4  # capped
    assert rows[0] == {"model": "gemini-3-pro", "cost": 0.9, "tokens": 300}  # sorted by cost desc
    assert rows[1] == {"model": "claude-opus-4-6", "cost": 0.6, "tokens": 1200}  # merged per model
    # Untouched buckets serialize an empty list, not a missing key.
    other = next(b for b in accounting.weekly_token_usage(daily) if b["date"] != key)
    assert other["models"] == []


def test_local_summary_includes_hourly(tmp_path):
    # Today's usage lands in exactly one local-hour bucket, and the per-hour totals
    # reconcile with the day's total (the Day view's bars sum to "Today").
    rec = {
        "type": "assistant",
        "timestamp": _NOW.isoformat(),
        "requestId": "r1",
        "message": {"model": "claude-sonnet-4", "usage": {
            "input_tokens": 100000, "output_tokens": 5000,
            "cache_creation_input_tokens": 20000, "cache_read_input_tokens": 50000}},
    }
    (tmp_path / "session.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")

    with patch("accounting.model_pricing", return_value=_FLAT_PRICES):
        summary = accounting.local_claude_token_summary(projects_dir=tmp_path, now=_NOW)

    assert summary is not None
    hourly = summary["hourlyTokenUsage"]
    assert len(hourly) == 24
    # parse_timestamp localises, so bucket by the LOCAL hour of _NOW (tz-robust).
    expected_hour = _NOW.astimezone().hour
    assert hourly[expected_hour]["tokens"] == 175000
    assert sum(b["tokens"] for b in hourly) == 175000
    assert sum(1 for b in hourly if b["tokens"] > 0) == 1


# ---------------------------------------------------------------------------
# [AUDIT-5] codex_credit_balance selects the first PRESENT key, not first truthy
# ---------------------------------------------------------------------------

def test_codex_credit_balance_zero_balance_not_dropped():
    """A real exhausted balance of 0.0 must survive — the old `or` chain treated
    0.0 as falsy and skipped to a later field (or None)."""
    res = accounting.codex_credit_balance(
        {"credits": {"balance": 0.0, "currency": "credits"}}
    )
    assert res is not None
    assert res["amount"] == 0.0
    assert res["label"] == "Credits"
    assert res["currency"] == "credits"


def test_codex_credit_balance_no_numeric_key_returns_none():
    """When none of balance/remaining/available/amount is present, there is no
    numeric balance to report, so the helper returns None (not a 0-amount row)."""
    res = accounting.codex_credit_balance({"credits": {"currency": "credits"}})
    assert res is None


# ---------------------------------------------------------------------------
# [AUDIT-6] provider-family fallback pricing for unknown-but-non-empty models
# ---------------------------------------------------------------------------

def test_provider_family_for_model_mapping():
    """An unrecognised-but-non-empty model name maps to its provider family so
    usage_cost_usd can fall back to that family's default pricing instead of $0;
    a truly unknown string maps to None (no family → no fallback)."""
    assert accounting._provider_family_for_model("claude-opus-4-6") == "claude"
    assert accounting._provider_family_for_model("gpt-6-turbo") == "codex"
    assert accounting._provider_family_for_model("o3-pro") == "codex"
    assert accounting._provider_family_for_model("gemini-9-ultra") == "gemini"
    assert accounting._provider_family_for_model("grok-5") == "grok"
    assert accounting._provider_family_for_model("xai-supreme-1") == "grok"
    assert accounting._provider_family_for_model("totally-unknown") is None


def test_usage_cost_usd_unknown_grok_model_uses_family_fallback():
    """An uncatalogued Grok model (e.g. a future grok-5 shipped before LiteLLM/the
    embedded fallback catalogue it) must still bill via the grok family default
    (DEFAULT_MODEL_FOR_PROVIDER["grok"] = "grok-build") — not silently $0."""
    def _fake_pricing(model):
        if model == "grok-5":
            return {}
        return accounting._get_pricing(model)

    with patch("accounting.model_pricing", side_effect=_fake_pricing):
        cost = accounting.usage_cost_usd(
            {"input_tokens": 1_000_000, "output_tokens": 1_000_000}, "grok-5"
        )
    assert cost > 0


def test_usage_cost_usd_unknown_model_uses_family_fallback():
    """A non-empty model the pricing catalog doesn't know (e.g. a brand-new gpt-*)
    must still bill via its family default (gpt-5) — not silently $0 while tokens count."""
    # Empty pricing for the unknown model forces the family fallback path; the
    # gpt-5 family-default lookup is left live so the magnitude assertion is robust.
    def _fake_pricing(model):
        if model == "gpt-6-turbo":
            return {}
        return accounting._get_pricing(model)

    with patch("accounting.model_pricing", side_effect=_fake_pricing):
        cost = accounting.usage_cost_usd(
            {"input_tokens": 1_000_000, "output_tokens": 1_000_000}, "gpt-6-turbo"
        )
    assert cost > 0


def test_usage_cost_usd_none_model_is_zero():
    """A None model has no family to infer, so the fallback can't fire — cost is
    exactly 0.0 (no pricing → no billing)."""
    cost = accounting.usage_cost_usd({"input_tokens": 1_000_000}, None)
    assert cost == 0.0


# ---------------------------------------------------------------------------
# Grok billing-week window (main-tree unique)
# ---------------------------------------------------------------------------

def test_local_grok_billing_week_window(tmp_path):
    """Billing-week window uses exact timestamps — a record on the start DAY but BEFORE the
    start TIME must be EXCLUDED; one inside the window is INCLUDED; one at/after week_end is EXCLUDED.
    A call without week args must not produce billingWeekTokens/billingWeekCost."""
    # Billing period: Fri 2026-07-04 13:20:44 UTC -> Fri 2026-07-11 13:20:44 UTC
    week_start = dt.datetime(2026, 7, 4, 13, 20, 44, tzinfo=dt.timezone.utc)
    week_end   = dt.datetime(2026, 7, 11, 13, 20, 44, tzinfo=dt.timezone.utc)
    now_inside = dt.datetime(2026, 7, 10, 12, 0, 0, tzinfo=dt.timezone.utc)

    def _grok_line(ts: dt.datetime, loop: int, prompt: int = 10000, completion: int = 1000,
                   sid: str = "s1") -> str:
        return json.dumps({
            "ts": ts.isoformat(),
            "sid": sid,
            "msg": "shell.turn.inference_done",
            "ctx": {
                "loop_index": loop,
                "prompt_tokens": prompt,
                "cached_prompt_tokens": 0,
                "completion_tokens": completion,
                "reasoning_tokens": 0,
            },
        })

    before_start = dt.datetime(2026, 7, 4, 10, 0, 0, tzinfo=dt.timezone.utc)
    inside       = dt.datetime(2026, 7, 7, 12, 0, 0, tzinfo=dt.timezone.utc)
    at_end       = dt.datetime(2026, 7, 11, 13, 20, 44, tzinfo=dt.timezone.utc)

    lines = [
        _grok_line(before_start, loop=0, prompt=5000, completion=500, sid="s1"),
        _grok_line(inside,       loop=1, prompt=10000, completion=1000, sid="s2"),
        _grok_line(at_end,       loop=2, prompt=3000, completion=300, sid="s3"),
    ]
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "unified.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    with patch("accounting.model_pricing", return_value=_FLAT_PRICES):
        summary_with = accounting.local_grok_token_summary(
            logs_dir=logs, now=now_inside,
            week_start=week_start, week_end=week_end,
        )
        summary_without = accounting.local_grok_token_summary(
            logs_dir=logs, now=now_inside,
        )

    assert summary_with is not None
    assert summary_with["billingWeekTokens"] == 11000
    assert summary_with["billingWeekCost"] > 0

    assert summary_without is not None
    assert "billingWeekTokens" not in summary_without
    assert "billingWeekCost" not in summary_without


def test_enrich_ui_formatting_grok_billing_week_detail(tmp_path):
    """enrich_ui_formatting injects 'This week: N tokens · $ X.XX' onto the Weekly limit."""
    week_tokens = 110000
    week_cost   = 3.21

    providers = {
        "grok": {
            "limits": [{"label": "Weekly", "percent": 91.0, "unit": "percent"}],
            "costSummary": {
                "billingWeekTokens": week_tokens,
                "billingWeekCost": week_cost,
            },
        }
    }
    accounting.enrich_ui_formatting(providers)
    limit = providers["grok"]["limits"][0]
    expected_tokens_str = accounting.compact_token_count(week_tokens)
    assert limit["formattedDetailText"] == f"This week: {expected_tokens_str} tokens · $ {week_cost:.2f}"

    providers2 = {
        "grok": {
            "limits": [{"label": "Weekly", "percent": 50.0, "unit": "percent"}],
            "costSummary": {"billingWeekTokens": 0, "billingWeekCost": 0.0},
        }
    }
    accounting.enrich_ui_formatting(providers2)
    assert providers2["grok"]["limits"][0]["formattedDetailText"] == ""

    providers3 = {
        "grok": {
            "limits": [{"label": "Weekly", "percent": 50.0, "unit": "percent"}],
            "costSummary": {},
        }
    }
    accounting.enrich_ui_formatting(providers3)
    assert providers3["grok"]["limits"][0]["formattedDetailText"] == ""

    providers4 = {
        "grok": {
            "limits": [{"label": "Weekly", "percent": 50.0, "unit": "percent"}],
        }
    }
    accounting.enrich_ui_formatting(providers4)
    assert providers4["grok"]["limits"][0]["formattedDetailText"] == ""

    providers5 = {
        "grok": {
            "limits": [{"label": "Plan", "percent": 91.0, "unit": "percent"}],
            "costSummary": {"billingWeekTokens": week_tokens, "billingWeekCost": week_cost},
        }
    }
    accounting.enrich_ui_formatting(providers5)
    assert providers5["grok"]["limits"][0]["formattedDetailText"] == ""


def test_local_grok_keeps_composer_and_45_distinct(tmp_path):
    """composer and grok-4.5 must both appear as short labels — not merged into build."""
    lines = [
        json.dumps({"ts": _NOW.isoformat(), "sid": "s1", "msg": "model changed",
                    "ctx": {"model": "grok-4.5"}}),
        json.dumps({"ts": _NOW.isoformat(), "sid": "s1", "msg": "shell.turn.inference_done",
                    "ctx": {"loop_index": 0, "prompt_tokens": 1000, "cached_prompt_tokens": 0,
                            "completion_tokens": 100, "reasoning_tokens": 90}}),
        json.dumps({"ts": _NOW.isoformat(), "sid": "s1", "msg": "backend_search: model switch",
                    "ctx": {"new_model": "grok-composer-2.5-fast"}}),
        json.dumps({"ts": _NOW.isoformat(), "sid": "s1", "msg": "shell.turn.inference_done",
                    "ctx": {"loop_index": 1, "prompt_tokens": 2000, "cached_prompt_tokens": 500,
                            "completion_tokens": 50, "reasoning_tokens": 40}}),
    ]
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "unified.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    with patch("accounting.model_pricing", return_value=_FLAT_PRICES):
        summary = accounting.local_grok_token_summary(logs_dir=logs, now=_NOW)

    assert summary is not None
    models = {row["model"] for row in (summary.get("modelBreakdown") or [])}
    assert "Grok 4.5" in models
    assert "Composer 2.5" in models
    assert "grok-build-0.1" not in models
    assert "grok-composer-2.5-fast" not in models


def test_local_grok_fills_model_from_session_summary(tmp_path):
    """First turns before any model-changed line still resolve via summary.json."""
    home = tmp_path
    logs = home / "logs"
    sessions = home / "sessions" / "proj" / "sess-orphan"
    logs.mkdir()
    sessions.mkdir(parents=True)
    (logs / "unified.jsonl").write_text(
        json.dumps({
            "ts": _NOW.isoformat(),
            "sid": "sess-orphan",
            "msg": "shell.turn.inference_done",
            "ctx": {"loop_index": 0, "prompt_tokens": 500, "cached_prompt_tokens": 0,
                    "completion_tokens": 20, "reasoning_tokens": 10},
        }) + "\n",
        encoding="utf-8",
    )
    (sessions / "summary.json").write_text(json.dumps({
        "info": {"id": "sess-orphan"},
        "current_model_id": "grok-composer-2.5-fast",
    }), encoding="utf-8")

    with patch("accounting.model_pricing", return_value=_FLAT_PRICES):
        summary = accounting.local_grok_token_summary(logs_dir=logs, now=_NOW)

    assert summary is not None
    models = {row["model"] for row in (summary.get("modelBreakdown") or [])}
    assert "Composer 2.5" in models


def test_display_grok_model_short_labels():
    assert accounting._display_grok_model("grok-4.6") == "Grok 4.6"
    assert accounting._display_grok_model("grok-4.5") == "Grok 4.5"
    assert accounting._display_grok_model("grok-composer-2.5-fast") == "Composer 2.5"
    assert accounting._display_grok_model("grok-build") == "Grok Build"
    assert accounting._display_grok_model("grok-build-0.1") == "Grok Build"
    assert accounting._display_grok_model(None) == "Grok Build"
    assert accounting._grok_pricing_model("grok-4.6") == "grok-4.6"
    assert accounting._grok_pricing_model("Grok 4.6") == "grok-4.6"


def test_local_grok_keeps_45_and_46_distinct(tmp_path):
    """grok-4.6 must not collapse into the grok-4.5 breakdown bucket."""
    lines = [
        json.dumps({"ts": _NOW.isoformat(), "sid": "s45", "msg": "model changed",
                    "ctx": {"model": "grok-4.5"}}),
        json.dumps({"ts": _NOW.isoformat(), "sid": "s45", "msg": "shell.turn.inference_done",
                    "ctx": {"loop_index": 0, "prompt_tokens": 1000, "cached_prompt_tokens": 0,
                            "completion_tokens": 100, "reasoning_tokens": 90}}),
        json.dumps({"ts": _NOW.isoformat(), "sid": "s46", "msg": "model changed",
                    "ctx": {"model": "grok-4.6"}}),
        json.dumps({"ts": _NOW.isoformat(), "sid": "s46", "msg": "shell.turn.inference_done",
                    "ctx": {"loop_index": 0, "prompt_tokens": 2000, "cached_prompt_tokens": 0,
                            "completion_tokens": 200, "reasoning_tokens": 80}}),
    ]
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "unified.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    with patch("accounting.model_pricing", return_value=_FLAT_PRICES):
        summary = accounting.local_grok_token_summary(logs_dir=logs, now=_NOW)

    assert summary is not None
    models = {row["model"] for row in (summary.get("modelBreakdown") or [])}
    assert "Grok 4.6" in models
    assert "Grok 4.5" in models


def test_grok_cli_pricing_maps_bare_versions_to_the_build_row():
    """CLI usage is build-line usage even when the on-disk model id doesn't say so.

    `grok usage <id>` reports primaryModelId as "grok-4.6-build", but unified.jsonl
    and summary.json both record the bare "grok-4.6". The bare string misses
    _grok_pricing_model's "build" branch, so CLI turns were priced on xAI's API row.
    """
    # Bare versions -> build row (the fix).
    assert accounting._grok_cli_pricing_model("grok-4.6") == "grok-build-0.1"
    assert accounting._grok_cli_pricing_model("Grok 4.6") == "grok-build-0.1"
    assert accounting._grok_cli_pricing_model("grok-4.5") == "grok-build-0.1"

    # Already-build and composer ids are untouched.
    assert accounting._grok_cli_pricing_model("grok-4.6-build") == "grok-build-0.1"
    assert accounting._grok_cli_pricing_model("grok-build-0.1") == "grok-build-0.1"
    assert accounting._grok_cli_pricing_model("grok-composer-2.5-fast") == "grok-composer-2.5-fast"
    assert accounting._grok_cli_pricing_model("Composer 2.5") == "grok-composer-2.5-fast"

    # The SHARED mapper must keep the API row for non-CLI callers — the CLI rule
    # lives only in the CLI-scoped helper.
    assert accounting._grok_pricing_model("grok-4.6") == "grok-4.6"
    assert accounting._grok_pricing_model("grok-4.5") == "grok-4.5"


def test_grok_cli_cost_uses_the_build_row_not_the_api_row(monkeypatch):
    """End-to-end: a cache-heavy CLI turn must bill at build rates.

    Published xAI rates (docs.x.ai/docs/models, 2026-09-11):
      grok-4.6       $2.00 in / $6.00 out / $0.50 cache_read per MTok
      grok-build-0.1 $1.00 in / $2.00 out / $0.20 cache_read per MTok

    Pinned to the embedded fallback table so the result does not depend on
    whether this machine has a LiteLLM pricing cache on disk (CI never does):
    the fallback rows are what a first run bills from, so they are what must
    carry the published figures.
    """
    import pricing_data
    monkeypatch.setattr(pricing_data, "_active_pricing", list(pricing_data._FALLBACK_PRICING))
    monkeypatch.setattr(pricing_data, "_resolve_memo", {})
    usage = {
        "input_tokens": 12_815_698,      # includes the cached subset
        "cached_input_tokens": 12_086_656,
        "output_tokens": 81_977,
    }
    api_row = accounting.usage_cost_usd(usage, "grok-4.6")
    cli_row = accounting.usage_cost_usd(usage, accounting._grok_cli_pricing_model("grok-4.6"))

    # Sanity-check the API row against hand arithmetic so a pricing-table change
    # is caught here rather than silently shifting the ratio.
    uncached = (12_815_698 - 12_086_656) / 1e6
    assert api_row == pytest.approx(uncached * 2.0 + 12.086656 * 0.5 + 0.081977 * 6.0, rel=1e-6)

    assert cli_row < api_row
    assert cli_row == pytest.approx(uncached * 1.0 + 12.086656 * 0.2 + 0.081977 * 2.0, rel=1e-6)
    # ~2.4x on this cache-dominated shape; pin the magnitude, not the exact float.
    assert 2.3 < api_row / cli_row < 2.5


def test_codex_lane_label_follows_window_length_not_position():
    """A free account's primary lane is a 30-day window, not a 5-hour session.

    account/rateLimits/read returns lanes positionally, but the durations vary by
    plan. Labelling by position alone rendered a free account's 43200-minute
    primary as "Session - Resets in 29d 23h" (verified against a live
    planType:"free" response, 2026-09-11).
    """
    plus = {
        "primary": {"usedPercent": 68, "windowDurationMins": 300, "resetsAt": 0},
        "secondary": {"usedPercent": 70, "windowDurationMins": 10080, "resetsAt": 0},
    }
    assert [r["label"] for r in accounting.codex_rate_limit_rows(plus)] == ["Session", "Weekly"]

    free = {"primary": {"usedPercent": 0, "windowDurationMins": 43200, "resetsAt": 0},
            "secondary": None}
    assert [r["label"] for r in accounting.codex_rate_limit_rows(free)] == ["Plan"]

    # No duration reported -> keep the positional default, so previously-correct
    # payloads are untouched.
    bare = {"primary": {"usedPercent": 5, "resetsAt": 0},
            "secondary": {"usedPercent": 9, "resetsAt": 0}}
    assert [r["label"] for r in accounting.codex_rate_limit_rows(bare)] == ["Session", "Weekly"]


def test_codex_plan_window_is_not_flagged_as_extra_usage():
    """The 30-day lane must be labelled "Plan", never "Monthly".

    enrich_ui_formatting flags any label containing monthly/credits/extra/on-demand
    as isExtraUsage and renders it as a separate on-demand lane. A plan window is
    not on-demand usage, so the word choice is load-bearing.
    """
    free = {"primary": {"usedPercent": 42, "windowDurationMins": 43200, "resetsAt": 0}}
    rows = accounting.codex_rate_limit_rows(free)
    assert rows and rows[0]["label"] == "Plan"

    providers = {"codex": {"label": "Codex", "status": "ok", "limits": rows}}
    accounting.enrich_ui_formatting(providers)
    assert providers["codex"]["limits"][0]["isExtraUsage"] is False


def test_per_model_weekly_lane_gets_the_same_pace_line_as_weekly():
    """A per-model cap (Anthropic's seven_day_<model>) is a weekly WINDOW lane.

    parse_claude_usage discovers these from the payload, so the label is the model
    name ("Fable"), not "Weekly". The label-only predicate matched neither
    session nor weekly, so the lane skipped the polished pace line AND escaped the
    left/right blanking — leaking internal phrasing ("3% in deficit") to the UI,
    which reads as *behind* when it means ahead. Verified against a live
    subscriber account with per-model caps.
    """
    def lane(label, pct, pace):
        return {"label": label, "percent": pct, "unit": "%", "windowMinutes": 10080,
                "resetAt": "2026-09-17T20:00:00+00:00", "pacePercent": pace,
                "detailLeftText": "3% in deficit", "detailRightText": "Runs out in 4d 14h"}

    providers = {"claude": {"label": "Claude", "status": "ok", "limits": [
        lane("Weekly", 17.0, 12.5),
        lane("Fable", 16.0, 12.5),
    ]}}
    accounting.enrich_ui_formatting(providers)
    weekly, fable = providers["claude"]["limits"]

    # Both render the polished line; neither leaks the raw internal phrasing.
    assert weekly["formattedDetailText"].startswith("Pace: ")
    assert fable["formattedDetailText"].startswith("Pace: ")
    assert "in deficit" not in fable["formattedDetailText"]
    assert fable["formattedDetailLeft"] == ""
    assert fable["formattedDetailRight"] == ""


def test_window_classification_bands():
    """Session/weekly bands are window-derived, with the label kept as a fallback."""
    assert accounting._is_session_window({"windowMinutes": 300}) is True
    assert accounting._is_weekly_window({"windowMinutes": 10080}) is True

    # Codex's 30-day plan window is neither — it must not inherit weekly's pace line.
    plan = {"label": "Plan", "windowMinutes": 43200}
    assert accounting._is_session_window(plan) is False
    assert accounting._is_weekly_window(plan) is False

    # Lanes with no reported window still classify by label (Grok's weekly relies
    # on this for its billing-week detail).
    assert accounting._is_weekly_window({"label": "Weekly"}) is True
    assert accounting._is_session_window({"label": "Session"}) is True

    # A money lane with no window is neither.
    assert accounting._is_weekly_window({"label": "Monthly", "unit": "USD"}) is False
