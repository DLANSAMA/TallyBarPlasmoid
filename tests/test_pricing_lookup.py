"""Price lookup: usage-date intro/list rates, same-model vs sibling matches, OpenAI
"responses" rows, offline fallback rows."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "io.github.dlansama.tallybar" / "contents" / "code"))

import accounting  # noqa: E402
import pricing_data  # noqa: E402


@pytest.fixture(autouse=True)
def offline_fallback_catalog(monkeypatch, tmp_path):
    """Every test bills off the embedded table (no pricing cache on disk)."""
    monkeypatch.setattr(pricing_data, "PRICING_CACHE_PATH", tmp_path / "missing" / "pricing_cache.json")
    pricing_data.invalidate_cache()
    yield
    pricing_data.invalidate_cache()


def _install(entries):
    with pricing_data._pricing_lock:
        pricing_data._install_catalog(list(entries), 0.0)


# --- usage-date intro / list rates -------------------------------------------------

_ARGON_INTRO = {"input": 2.00, "output": 10.00, "cache_read": 0.10}
_ARGON_LIST = {"input": 4.00, "output": 20.00, "cache_read": 0.20}


@pytest.mark.parametrize("first, second", [("2026-11-30", "2026-12-01"), ("2026-12-01", "2026-11-30")])
def test_usage_date_picks_the_intro_or_list_rate(first, second):
    """The two sides of a switch share no memo entry: whichever is asked first, each gets
    its own rate, and a later lookup of either is unchanged."""
    expected = {"2026-11-30": _ARGON_INTRO, "2026-12-01": _ARGON_LIST}
    for day in (first, second, first):
        assert pricing_data.get_pricing("gemini-4-argon", day) == expected[day], day
    assert pricing_data.get_pricing("gemini-3.8-flash", "2026-12-31")["input"] == 0.75
    assert pricing_data.get_pricing("gemini-3.8-flash", "2027-01-01")["input"] == 1.50
    # a variant name reaches the scheduled row, so it switches with it
    assert pricing_data.get_pricing("gemini-4-argon-preview", "2026-12-01") == _ARGON_LIST


def test_usage_date_none_or_unreadable_means_today(monkeypatch):
    monkeypatch.setattr(pricing_data, "_today", lambda: "2026-12-15")
    assert pricing_data.get_pricing("gemini-4-argon") == _ARGON_LIST
    assert pricing_data.get_pricing("gemini-4-argon", "not-a-date") == _ARGON_LIST
    assert pricing_data.get_pricing("gemini-4-argon", "2026-11-01") == _ARGON_INTRO
    assert pricing_data.get_pricing("gemini-4-argon", "2026-11-01T23:59:59+00:00") == _ARGON_INTRO
    monkeypatch.setattr(pricing_data, "_today", lambda: "2026-11-15")
    assert pricing_data.get_pricing("gemini-4-argon") == _ARGON_INTRO


def test_a_catalog_row_that_already_moved_is_left_alone_on_both_sides():
    _install([("gemini-4-argon", {"input": 3.00, "output": 15.00, "cache_read": 0.30})])
    for day in ("2026-11-01", "2026-12-01", "2030-01-01"):
        assert pricing_data.get_pricing("gemini-4-argon", day)["input"] == 3.00, day


def test_usage_cost_and_split_bill_the_rate_of_the_usage_day():
    usage = {"input_tokens": 1_000_000}
    for day, input_rate in (("2026-11-30", 2.00), ("2026-12-01", 4.00)):
        assert accounting.usage_cost_usd(usage, "gemini-4-argon", day) == pytest.approx(input_rate)
        split = accounting.usage_split(usage, "gemini-4-argon", day)
        assert split.cost == pytest.approx(input_rate)
        assert split.type_costs[0] == pytest.approx(input_rate)
    flash = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    assert accounting.usage_cost_usd(flash, "gemini-3.6-flash", "2026-12-31") == pytest.approx(0.75 + 3.75)
    assert accounting.usage_cost_usd(flash, "gemini-3.6-flash", "2027-01-01") == pytest.approx(1.50 + 7.50)


def test_usage_cost_without_a_date_is_todays_rate(monkeypatch):
    monkeypatch.setattr(pricing_data, "_today", lambda: "2027-02-01")
    assert accounting.usage_cost_usd({"input_tokens": 1_000_000}, "gemini-4-argon") == pytest.approx(4.00)
    assert accounting.usage_split({"input_tokens": 1_000_000}, "gemini-4-argon").cost == pytest.approx(4.00)


# --- same-model vs sibling matches --------------------------------------------------

def test_a_sibling_match_is_not_a_catalog_price():
    """A name that only reached a shorter sibling is billed at that sibling's rate but is
    not priced: the same-model variants (date, [1m], -latest, -preview) are."""
    _install([
        ("claude-fable-5", {"input": 10.0, "output": 50.0, "cache_read": 1.0}),
        ("claude-opus-4", {"input": 15.0, "output": 75.0}),
        ("gpt-5", {"input": 1.25, "output": 10.0}),
        ("gemini-2.5-pro", {"input": 1.25, "output": 10.0}),
        ("gemini/gemini-2.5-flash", {"input": 0.3, "output": 2.5}),
        ("gemini-2.5-flash", {"input": 0.3, "output": 2.5}),
        ("grok-4", {"input": 3.0, "output": 15.0}),
    ])
    sibling = ["claude-fable-5-2", "claude-opus-4-1", "gpt-5.3-codex", "gpt-5-mini", "gemini-2.5-pro-vision",
               "gemini-9-pro", "gemini-2.5-flash-lite", "grok-4.6", "totally-unknown"]
    for model in sibling:
        assert not accounting.pricing.has_catalog_price(model), model
    # still billed at the shorter row, as before
    assert pricing_data.get_pricing("claude-fable-5-2")["cache_read"] == 1.0
    assert pricing_data.get_pricing("gpt-5.3-codex")["input"] == 1.25
    same = ["claude-fable-5", "claude-fable-5[1m]", "claude-fable-5-20261101", "Claude-Opus-4-20250514",
            "gpt-5-2025-08-07", "gpt-5-latest", "gemini-2.5-pro-preview-06-05", "gemini-2.5-pro-preview",
            "gemini-2.5-flash", "gemini/gemini-2.5-flash", "grok-4"]
    for model in same:
        assert accounting.pricing.has_catalog_price(model), model
    assert not accounting.pricing.has_catalog_price(None)
    assert not accounting.pricing.has_catalog_price("")


def test_a_key_that_is_a_dated_variant_of_the_name_is_the_same_model():
    _install([("gpt-5.1-2025-11-13", {"input": 1.25, "output": 10.0}),
              ("gemini-3-flash-preview", {"input": 0.5, "output": 3.0})])
    assert accounting.pricing.has_catalog_price("gpt-5.1")
    assert accounting.pricing.has_catalog_price("gemini-3-flash")
    assert not accounting.pricing.has_catalog_price("gpt-5")


def test_same_model_row_beats_a_longer_sibling_row():
    _install([("claude-opus-4", {"input": 15.0, "output": 75.0}),
              ("claude-opus-4-20250514-thinking", {"input": 1.0, "output": 1.0})])
    match = pricing_data.resolve_pricing("claude-opus-4-20250514")
    assert match.same_model and match.prices["input"] == 15.0


# --- offline fallback rows ----------------------------------------------------------

_PUBLISHED = {
    # Anthropic pricing page: claude-fable-5-2 / mythos-5-2 $8 in, $10 / $16 writes, $0.20 read, $40 out.
    "claude-fable-5-2": {"input": 8.0, "output": 40.0, "cache_write": 10.0, "cache_write_1h": 16.0, "cache_read": 0.20},
    "claude-mythos-5-2": {"input": 8.0, "output": 40.0, "cache_write": 10.0, "cache_write_1h": 16.0, "cache_read": 0.20},
    # The original Opus 4 bills like Opus 4.1.
    "claude-opus-4-20250514": {"input": 15.0, "output": 75.0, "cache_write": 18.75, "cache_write_1h": 30.0, "cache_read": 1.5},
    "claude-opus-4-0": {"input": 15.0, "output": 75.0, "cache_write": 18.75, "cache_write_1h": 30.0, "cache_read": 1.5},
    "claude-opus-4-1": {"input": 15.0, "output": 75.0, "cache_write": 18.75, "cache_write_1h": 30.0, "cache_read": 1.5},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0, "cache_write": 6.25, "cache_write_1h": 10.0, "cache_read": 0.5},
    "gpt-5.2": {"input": 1.75, "output": 14.0, "cache_read": 0.175},
    "gpt-5.3": {"input": 1.75, "output": 14.0, "cache_read": 0.175},
    "gpt-5.3-codex": {"input": 1.75, "output": 14.0, "cache_read": 0.175},
    "gpt-5.1-codex-mini": {"input": 0.25, "output": 2.0, "cache_read": 0.025},
    "gpt-5-codex": {"input": 1.25, "output": 10.0, "cache_read": 0.125},
    "gpt-5.4-nano": {"input": 0.20, "output": 1.25, "cache_read": 0.02},
    "gpt-5.5-cyber": {"input": 12.5, "output": 75.0, "cache_read": 1.25},
    "gpt-5.2-pro": {"input": 21.0, "output": 168.0},
    "gemini-3-flash-preview": {"input": 0.5, "output": 3.0, "cache_read": 0.05},
}


@pytest.mark.parametrize("model", sorted(_PUBLISHED))
def test_offline_fallback_has_an_exact_row(model):
    assert pricing_data.get_pricing(model) == _PUBLISHED[model], model
    assert accounting.pricing.has_catalog_price(model), model


def test_offline_fallback_gpt_56_pro_and_cyber_rows_carry_the_catalog_tiers():
    gpt56 = pricing_data.get_pricing("gpt-5.6")
    assert gpt56["input"] == 4.0 and gpt56["long_context_threshold"] == 272_000 and gpt56["long_input"] == 8.0
    assert pricing_data.get_pricing("gpt-5.6-cyber")["long_output"] == 112.5
    pro = pricing_data.get_pricing("gpt-5.5-pro")
    assert (pro["input"], pro["long_input"], pro["long_output"]) == (30.0, 60.0, 270.0)
    assert not accounting.pricing.has_catalog_price("gpt-5.7")


# --- LiteLLM ingestion --------------------------------------------------------------

def test_litellm_ingests_responses_mode_models():
    """LiteLLM lists the codex, codex-mini and pro models as mode "responses"."""
    raw = {
        "gpt-5.3-codex": {"litellm_provider": "openai", "mode": "responses",
                          "input_cost_per_token": 1.75e-6, "output_cost_per_token": 1.4e-5,
                          "cache_read_input_token_cost": 1.75e-7},
        "gpt-5.5-pro": {"litellm_provider": "openai", "mode": "responses",
                        "input_cost_per_token": 3e-5, "output_cost_per_token": 1.8e-4},
        "text-embedding-3-small": {"litellm_provider": "openai", "mode": "embedding",
                                   "input_cost_per_token": 2e-8, "output_cost_per_token": 0},
        "sora-2-pro": {"litellm_provider": "openai", "mode": "video_generation"},
        "bedrock-gpt": {"litellm_provider": "bedrock", "mode": "responses",
                        "input_cost_per_token": 1e-6, "output_cost_per_token": 1e-6},
    }
    out = dict(pricing_data._litellm_to_tallybar(raw))
    assert out["gpt-5.3-codex"] == pytest.approx({"input": 1.75, "output": 14.0, "cache_read": 0.175})
    assert out["gpt-5.5-pro"]["input"] == pytest.approx(30.0)
    assert set(out) == {"gpt-5.3-codex", "gpt-5.5-pro"}


def test_a_zero_priced_lowest_tier_does_not_hide_a_higher_valid_one():
    info = {"litellm_provider": "gemini", "mode": "chat",
            "input_cost_per_token": 1e-6, "output_cost_per_token": 5e-6,
            "input_cost_per_token_above_128k_tokens": 0.0,
            "output_cost_per_token_above_128k_tokens": 0.0,
            "input_cost_per_token_above_200k_tokens": 2e-6,
            "output_cost_per_token_above_200k_tokens": 8e-6}
    tier = pricing_data._long_context_tier(info, {"input": 1.0, "output": 5.0}, "gemini")
    assert tier == {"long_context_threshold": 200_000.0, "long_input": 2.0, "long_output": 8.0}
    info.pop("input_cost_per_token_above_200k_tokens")
    assert pricing_data._long_context_tier(info, {"input": 1.0, "output": 5.0}, "gemini") == {}


@pytest.mark.parametrize("model,inp,out", [
    ("claude-opus-4-20250514", 15.0, 75.0), ("claude-opus-4-0", 15.0, 75.0),
    ("claude-opus-4-1-20250805", 15.0, 75.0), ("claude-opus-4-5-20251101", 5.0, 25.0),
    ("claude-opus-4-8", 5.0, 25.0), ("claude-opus-4-9", 5.0, 25.0),
    ("claude-opus-4.6", 5.0, 25.0), ("claude-opus-4", 5.0, 25.0),
])
def test_offline_opus_4_family_rates(model, inp, out):
    from pricing_data import _FALLBACK_PRICING, _scan_catalog
    _, prices, _ = _scan_catalog(list(_FALLBACK_PRICING), model)
    assert (prices["input"], prices["output"]) == (inp, out)
    if inp == 5.0:
        assert (prices["cache_write"], prices["cache_write_1h"], prices["cache_read"]) == (6.25, 10.0, 0.5)
