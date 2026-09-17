"""pricing_data.get_pricing bootstrap precedence (fresh cache > stale > embedded
fallback) and the bidirectional substring match. A regression here mis-prices every
cost line, so it's worth guarding even though failures degrade gracefully."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

CODE_DIR = Path(__file__).parent.parent / "io.github.dlansama.tallybar" / "contents" / "code"
sys.path.insert(0, str(CODE_DIR))

import pricing_data  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_pricing():
    # get_pricing caches into a module global; clear it before AND after so a fake
    # injected here can never leak into the accounting tests (and vice-versa).
    pricing_data.invalidate_cache()
    yield
    pricing_data.invalidate_cache()


def test_fresh_cache_wins_and_substring_match_is_bidirectional():
    fake = [("test-model-x", {"input": 1.0, "output": 2.0})]
    with patch.object(pricing_data, "_load_cache", return_value=fake):
        assert pricing_data.get_pricing("test-model-x") == {"input": 1.0, "output": 2.0}
        assert pricing_data.get_pricing("test-model-x-2026") == {"input": 1.0, "output": 2.0}  # pattern in name
        assert pricing_data.get_pricing("test") == {"input": 1.0, "output": 2.0}               # name in pattern
        assert pricing_data.get_pricing("totally-unrelated") == {}                              # no match -> {}


def test_stale_used_when_no_fresh_cache():
    fake_stale = [("stale-model", {"input": 9.9})]
    with patch.object(pricing_data, "_load_cache", return_value=None), \
         patch.object(pricing_data, "_load_cache_stale", return_value=fake_stale):
        assert pricing_data.get_pricing("stale-model") == {"input": 9.9}
        # Stale entries lead; embedded fallback rows are appended as a supplement.
        assert pricing_data._active_pricing[: len(fake_stale)] == fake_stale


def test_fallback_supplements_models_missing_from_live_catalog():
    """A brand-new model LiteLLM hasn't catalogued yet (e.g. claude-fable-5,
    claude-sonnet-5) must resolve to its embedded-fallback prices, not the
    provider-family default — while live-catalog entries stay authoritative for
    models present in both."""
    live = [("claude-opus-4-6", {"input": 5.55, "output": 25.55})]  # deliberately != fallback
    with patch.object(pricing_data, "_load_cache", return_value=live):
        fallback = dict(pricing_data._FALLBACK_PRICING)
        assert pricing_data.get_pricing("claude-fable-5") == fallback["claude-fable-5"]
        assert pricing_data.get_pricing("claude-sonnet-5") == fallback["claude-sonnet-5"]
        # Exact live key wins over the embedded row for the same model
        assert pricing_data.get_pricing("claude-opus-4-6") == {"input": 5.55, "output": 25.55}


def test_embedded_exact_match_claude_sonnet_5_not_sonnet_4():
    """claude-sonnet-5 must resolve to its own row (3.0/15.0), not fall through to a
    differently-versioned sonnet-4-x key via substring matching."""
    with patch.object(pricing_data, "_load_cache", return_value=None), \
         patch.object(pricing_data, "_load_cache_stale", return_value=None):
        prices = pricing_data.get_pricing("claude-sonnet-5")
        assert prices["input"] == 3.0
        assert prices["output"] == 15.0


def test_embedded_exact_match_grok_46_not_grok_4():
    """grok-4.6 must not inherit grok-4 via ``"grok-4" in "grok-4.6"``."""
    with patch.object(pricing_data, "_load_cache", return_value=None), \
         patch.object(pricing_data, "_load_cache_stale", return_value=None):
        fallback = dict(pricing_data._FALLBACK_PRICING)
        prices = pricing_data.get_pricing("grok-4.6")
        assert prices == fallback["grok-4.6"]
        assert prices != fallback["grok-4"]
        assert prices != fallback["grok-4.5"]
        assert prices["input"] == 2.0
        assert prices["output"] == 6.0
        assert prices["cache_read"] == 0.5


def test_grok_46_fallback_when_live_catalog_lacks_the_key():
    """LiteLLM currently ships grok-4.5 / grok-4 but not grok-4.6. The
    supplement must still exact-match 4.6 rather than family-match grok-4."""
    live = [
        ("grok-4.5", {"input": 2.0, "output": 6.0, "cache_read": 0.5}),
        ("grok-4", {"input": 3.0, "output": 15.0, "cache_read": 0.75}),
    ]
    with patch.object(pricing_data, "_load_cache", return_value=live):
        fallback = dict(pricing_data._FALLBACK_PRICING)
        prices = pricing_data.get_pricing("grok-4.6")
        assert prices == fallback["grok-4.6"]
        assert prices != fallback["grok-4"]
        # Live 4.5 stays authoritative for 4.5 queries.
        assert pricing_data.get_pricing("grok-4.5") == live[0][1]


def test_embedded_fallback_when_no_cache_at_all():
    with patch.object(pricing_data, "_load_cache", return_value=None), \
         patch.object(pricing_data, "_load_cache_stale", return_value=None):
        pricing_data.get_pricing("anything")
        assert pricing_data._active_pricing == list(pricing_data._FALLBACK_PRICING)
        assert len(pricing_data._FALLBACK_PRICING) > 0          # embedded defaults exist


# ---------------------------------------------------------------------------
# Exact-match-first against the EMBEDDED fallback table. With no cache,
# the embedded _FALLBACK_PRICING is active. The old "first substring hit wins"
# behaviour let a short canonical name ("gpt-5") resolve to a longer EARLIER-listed
# key ("gpt-5.5") and overbill ~2x (5.0/30.0 vs the correct 2.5/15.0). exact-first
# fixes that. These guard the embedded table specifically (the autouse
# _reset_pricing fixture keeps the injected fallback from leaking).
# ---------------------------------------------------------------------------

def test_embedded_exact_match_gpt5_not_gpt55():
    """gpt-5 must resolve to the gpt-5 row (2.5/15.0), NOT gpt-5.5 (5.0/30.0) — the 2x-overbill bug."""
    with patch.object(pricing_data, "_load_cache", return_value=None), \
         patch.object(pricing_data, "_load_cache_stale", return_value=None):
        prices = pricing_data.get_pricing("gpt-5")
        assert prices["input"] == 2.5
        assert prices["output"] == 15.0


def test_embedded_exact_match_o3_not_o3_mini():
    """o3 must hit the o3 row (2.0/8.0), not the longer o3-mini key (1.1/4.4)."""
    with patch.object(pricing_data, "_load_cache", return_value=None), \
         patch.object(pricing_data, "_load_cache_stale", return_value=None):
        prices = pricing_data.get_pricing("o3")
        assert prices["input"] == 2.0
        assert prices["output"] == 8.0


def test_embedded_exact_match_gemini_flash_not_lite():
    """gemini-2.5-flash must hit the flash row (0.30/2.50), not flash-lite (0.10/0.40)."""
    with patch.object(pricing_data, "_load_cache", return_value=None), \
         patch.object(pricing_data, "_load_cache_stale", return_value=None):
        prices = pricing_data.get_pricing("gemini-2.5-flash")
        assert prices["input"] == 0.30
        assert prices["output"] == 2.50


def test_embedded_family_match_gpt5_codex():
    """gpt-5-codex has no exact row, so it must fall to the longest 'pattern in name'
    family key (gpt-5 → 2.5/15.0), not a shorter unrelated key."""
    with patch.object(pricing_data, "_load_cache", return_value=None), \
         patch.object(pricing_data, "_load_cache_stale", return_value=None):
        prices = pricing_data.get_pricing("gpt-5-codex")
        assert prices["input"] == 2.5
        assert prices["output"] == 15.0


def test_empty_and_none_model_return_empty_dict():
    """Empty string and None must short-circuit to {} (no spurious substring match)."""
    with patch.object(pricing_data, "_load_cache", return_value=None), \
         patch.object(pricing_data, "_load_cache_stale", return_value=None):
        assert pricing_data.get_pricing("") == {}
        assert pricing_data.get_pricing(None) == {}


# ---------------------------------------------------------------------------
# 1-hour cache-write rate. Anthropic prices 1h cache writes (2x input) higher than
# the default 5-minute write (1.25x input); LiteLLM exposes the 1h rate in a
# separate field. The converter must ingest it (per-MTok) so usage_cost_usd can
# bill the 1h portion of cache_creation correctly. Dropping it under-bills cache-
# heavy Claude use by ~8.7% on real data.
# ---------------------------------------------------------------------------

def test_litellm_converts_1h_cache_write_rate():
    raw = {
        "claude-opus-4-8": {
            "litellm_provider": "anthropic", "mode": "chat",
            "input_cost_per_token": 5e-06, "output_cost_per_token": 2.5e-05,
            "cache_read_input_token_cost": 5e-07,
            "cache_creation_input_token_cost": 6.25e-06,
            "cache_creation_input_token_cost_above_1hr": 1e-05,
        }
    }
    out = dict(pricing_data._litellm_to_tallybar(raw))
    prices = out["claude-opus-4-8"]
    assert prices["input"] == 5.0
    assert prices["cache_write"] == 6.25           # 5-minute write (1.25x)
    assert prices["cache_write_1h"] == 10.0        # 1-hour write (2x), per-MTok


def test_litellm_no_1h_field_leaves_cache_write_1h_unset():
    raw = {
        "gpt-5": {
            "litellm_provider": "openai", "mode": "chat",
            "input_cost_per_token": 2.5e-06, "output_cost_per_token": 1.5e-05,
            "cache_read_input_token_cost": 2.5e-07,
        }
    }
    out = dict(pricing_data._litellm_to_tallybar(raw))
    assert "cache_write_1h" not in out["gpt-5"]     # OpenAI has no 1h-write tier


def test_litellm_synthesizes_5m_cache_write_when_absent():
    """A catalog entry with only input/output gets cache_write=1.25x AND cache_write_1h=2x
    synthesized so usage_cost_usd can bill both write tiers even when LiteLLM omits them."""
    raw = {
        "claude-new-model": {
            "litellm_provider": "anthropic", "mode": "chat",
            "input_cost_per_token": 4e-06, "output_cost_per_token": 1.6e-05,
        }
    }
    out = dict(pricing_data._litellm_to_tallybar(raw))
    prices = out["claude-new-model"]
    assert prices["input"] == pytest.approx(4.0)
    assert prices["cache_write"] == pytest.approx(5.0)       # 1.25x input
    assert prices["cache_write_1h"] == pytest.approx(8.0)    # 2x input


def test_embedded_fallback_anthropic_has_1h_write_at_2x_input():
    """Every embedded Anthropic entry must carry cache_write_1h == 2x input, so the
    1h premium still applies when fully offline."""
    for pattern, prices in pricing_data._FALLBACK_PRICING:
        if not pattern.startswith("claude"):
            continue
        assert "cache_write_1h" in prices, pattern
        assert prices["cache_write_1h"] == pytest.approx(2.0 * prices["input"]), pattern


# ---------------------------------------------------------------------------
# [CRITICAL] Provider-prefixed raw LiteLLM model_ids (gemini/*, xai/*) must
# resolve against their OWN live-fetched price, not the embedded
# _FALLBACK_PRICING row for the same bare name. Before the fix, _litellm_to_
# tallybar stored the prefixed id verbatim, so get_pricing's exact-match rule
# could never match the bare-named query and always fell through to the
# fallback row appended by _with_fallback_supplement (whose "already known"
# exclusion was also keyed on the prefixed string, so it never excluded the
# bare fallback key either). This silently under/over-billed every Gemini and
# xAI usage record.
# ---------------------------------------------------------------------------

def test_litellm_prefixed_model_id_resolves_to_live_price_not_fallback():
    raw = {
        "gemini/gemini-3.5-flash": {
            "litellm_provider": "gemini", "mode": "chat",
            "input_cost_per_token": 1.5e-06, "output_cost_per_token": 9.0e-06,
            "cache_read_input_token_cost": 1.5e-07,
        },
        "xai/grok-4": {
            "litellm_provider": "xai", "mode": "chat",
            "input_cost_per_token": 4.0e-06, "output_cost_per_token": 20.0e-06,
        },
    }
    converted = pricing_data._litellm_to_tallybar(raw)
    active = pricing_data._with_fallback_supplement(converted)
    with patch.object(pricing_data, "_load_cache", return_value=active):
        # Bare (un-prefixed) query form -> live rate, not the embedded fallback.
        gemini_prices = pricing_data.get_pricing("gemini-3.5-flash")
        assert gemini_prices["input"] == pytest.approx(1.5)
        assert gemini_prices["output"] == pytest.approx(9.0)
        assert gemini_prices["cache_read"] == pytest.approx(0.15)
        fallback = dict(pricing_data._FALLBACK_PRICING)
        assert gemini_prices != fallback["gemini-3.5-flash"]

        # Prefixed raw-id query form resolves to the SAME live rate.
        assert pricing_data.get_pricing("gemini/gemini-3.5-flash") == gemini_prices

        grok_prices = pricing_data.get_pricing("grok-4")
        assert grok_prices["input"] == pytest.approx(4.0)
        assert grok_prices["output"] == pytest.approx(20.0)
        assert grok_prices != fallback["grok-4"]
        assert pricing_data.get_pricing("xai/grok-4") == grok_prices


def test_get_pricing_memoizes_resolution_until_invalidated():
    # get_pricing runs once per usage record, so resolution is memoized per model name.
    fake = [("memo-model", {"input": 1.0})]
    with patch.object(pricing_data, "_load_cache", return_value=fake):
        assert pricing_data.get_pricing("memo-model-variant") == {"input": 1.0}
    # Poke the catalog away: a memoized name must keep resolving without a rescan...
    pricing_data._active_pricing = []
    assert pricing_data.get_pricing("memo-model-variant") == {"input": 1.0}
    # ...and invalidate_cache must clear the memo along with the catalog (a stale memo
    # would otherwise survive a pricing refresh).
    pricing_data.invalidate_cache()
    with patch.object(pricing_data, "_load_cache", return_value=[]), \
         patch.object(pricing_data, "_load_cache_stale", return_value=None):
        assert pricing_data.get_pricing("memo-model-variant") == {}


# --- real _load_cache TTL boundary + refresh_pricing fetch chain -----------
# The existing tests patch _load_cache/_load_cache_stale. These drive the REAL disk
# functions and the fetch→convert→cache path so a TTL or fetch-failure regression is caught.

import json as _json  # noqa: E402
import time as _time  # noqa: E402
import pytest  # noqa: E402


def _write_cache(path, fetched_at, pricing):
    path.write_text(_json.dumps({"fetchedAt": fetched_at, "pricing": pricing}), encoding="utf-8")


def test_load_cache_ttl_boundary(tmp_path, monkeypatch):
    cache = tmp_path / "pricing_cache.json"
    monkeypatch.setattr(pricing_data, "PRICING_CACHE_PATH", cache)
    # Fresh (just written): _load_cache returns the entries.
    _write_cache(cache, _time.time(), [["fresh-model", {"input": 1.0}]])
    assert pricing_data._load_cache() == [("fresh-model", {"input": 1.0})]
    # Older than the 24h TTL: _load_cache returns None, but _load_cache_stale still serves it.
    _write_cache(cache, _time.time() - pricing_data.PRICING_CACHE_TTL - 10, [["old-model", {"input": 2.0}]])
    assert pricing_data._load_cache() is None
    assert pricing_data._load_cache_stale() == [("old-model", {"input": 2.0})]


@pytest.mark.asyncio
async def test_refresh_pricing_fetch_failure_returns_false(monkeypatch):
    # A network blip (fetch -> None) must return False WITHOUT raising or clobbering
    # whatever pricing is already active.
    pricing_data._active_pricing = [("keep-me", {"input": 9.0})]
    monkeypatch.setattr(pricing_data, "_fetch_litellm_pricing", lambda timeout=10.0: None)
    ok = await pricing_data.refresh_pricing(timeout=0.5)
    assert ok is False
    assert pricing_data._active_pricing == [("keep-me", {"input": 9.0})]


@pytest.mark.asyncio
async def test_refresh_pricing_success_updates_and_clears_memo(tmp_path, monkeypatch):
    monkeypatch.setattr(pricing_data, "PRICING_CACHE_PATH", tmp_path / "pricing_cache.json")
    # Poison the resolution memo to prove refresh clears it.
    pricing_data._resolve_memo["claude-neo"] = {"input": 0.0}
    raw = {"claude-neo": {"litellm_provider": "anthropic", "mode": "chat",
                          "input_cost_per_token": 3e-6, "output_cost_per_token": 15e-6}}
    monkeypatch.setattr(pricing_data, "_fetch_litellm_pricing", lambda timeout=10.0: raw)
    ok = await pricing_data.refresh_pricing(timeout=0.5)
    assert ok is True
    assert pricing_data.get_pricing("claude-neo")["input"] == 3.0   # $/MTok, memo rebuilt
    assert (tmp_path / "pricing_cache.json").is_file()              # disk cache written
