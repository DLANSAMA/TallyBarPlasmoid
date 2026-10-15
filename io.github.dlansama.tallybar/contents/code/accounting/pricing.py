from __future__ import annotations

import sys
from typing import Any, NamedTuple

from pricing_data import get_pricing as _get_pricing
from .formatting import compact_token_count

DEFAULT_MODEL_FOR_PROVIDER = {
    "claude": "claude-sonnet-4",
    "codex":  "gpt-5",
    "gemini": "gemini-2.5-pro",
    "grok":   "grok-build",
}


def model_pricing(model: str | None) -> dict[str, float]:
    """Look up per-MTok pricing for a model (case-insensitive substring match).

    Pricing data is fetched from the LiteLLM community-maintained catalog,
    cached locally for 24h, and falls back to embedded defaults when offline.
    See pricing_data.py for the fetch/cache/fallback chain.
    """
    return _get_pricing(model)


def _get_pricing_fn():
    """Support test patching on accounting.model_pricing transparently."""
    acct = sys.modules.get("accounting")
    if acct is not None and hasattr(acct, "model_pricing"):
        return acct.model_pricing
    return model_pricing


def _provider_family_for_model(model: str | None) -> str | None:
    """Infer the provider family (claude/codex/gemini/grok) from a model-name string,
    so an unrecognised-but-non-empty model can fall back to its family's default
    pricing instead of billing $0."""
    m = (model or "").lower()
    if not m:
        return None
    if any(t in m for t in ("claude", "opus", "sonnet", "haiku", "anthropic")):
        return "claude"
    if "gemini" in m:
        return "gemini"
    if any(t in m for t in ("gpt", "codex", "openai", "davinci")) or \
            (len(m) >= 2 and m[0] == "o" and m[1].isdigit()):
        return "codex"
    if any(t in m for t in ("grok", "xai")):
        return "grok"
    return None


def _family_default_pricing(model: str | None) -> dict[str, float]:
    family = _provider_family_for_model(model)
    if family is None:
        return {}
    return _get_pricing_fn()(DEFAULT_MODEL_FOR_PROVIDER[family])


def has_catalog_price(model: str | None) -> bool:
    """True when ``model`` resolves to a price of its own (live catalog or embedded
    fallback). False means usage_cost_usd bills it at its family's default rate instead."""
    return bool(_get_pricing_fn()(model))


def prompt_tier_prices(prices: dict[str, float], prompt_tokens: float) -> dict[str, float]:
    """``prices`` switched to its prompt-length tier when ``prompt_tokens`` is OVER the
    entry's ``long_context_threshold`` (pricing_data._long_context_tier); ``prices`` itself
    otherwise, including a prompt of exactly the threshold.

    Every rate moves to its ``long_`` value, output included: the tier prices the whole
    request (Claude Haiku 5.5 bills 5x past 100K). The switched rates go into a NEW dict —
    ``prices`` is the memoized catalog entry and must never be mutated.

    ``prompt_tokens`` must be ONE request's prompt. A total summed over a day, a model or
    a session can cross the threshold when no request did (or hide one that did), so a
    caller holding only such a total can't pick a tier and bills it at the base rates."""
    threshold = prices.get("long_context_threshold")
    if not threshold or prompt_tokens <= threshold:
        return prices
    return {key: prices.get("long_" + key, rate) for key, rate in prices.items()
            if not key.startswith("long_")}


# The usage types every reader splits one request into, in display order:
#   input       the UNCACHED prompt: fresh input, plus Gemini's tool-use prompt
#   output      every output token, reasoning/thinking included
#   cacheRead   prompt tokens served from cache: Anthropic's cache_read_input_tokens, and
#               the cached subset of input that OpenAI (Codex), xAI (Grok) and Gemini report
#   cacheWrite  Anthropic's cache creation, 5-minute and 1-hour writes together. Only
#               Anthropic reports writes; everyone else's are 0.
# The four token counts add up to the request's volume and the four costs to
# usage_cost_usd. A provider total that is larger than its parts (a Codex record carrying
# only total_tokens) leaves the excess unsplit: see usage_split.
USAGE_TYPES = ("input", "output", "cacheRead", "cacheWrite")


class UsageSplit(NamedTuple):
    """One request's usage, whole and by type (USAGE_TYPES order)."""
    tokens: int
    cost: float
    type_tokens: tuple[int, int, int, int]
    type_costs: tuple[float, float, float, float]


def _usage_int(usage: dict[str, Any], *keys: str) -> int:
    for k in keys:
        v = usage.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    return 0


def _usage_counts(usage: dict[str, Any]) -> dict[str, int]:
    """Every count the token and cost readers use, read ONCE from one usage dict."""
    input_tokens   = _usage_int(usage, "input_tokens", "inputTokens", "promptTokenCount", "input")
    output_tokens  = _usage_int(usage, "output_tokens", "outputTokens", "candidatesTokenCount", "output")
    tool           = _usage_int(usage, "tool", "tools", "toolTokens", "toolUsePromptTokenCount",
                                "tool_use_prompt_token_count")
    cache_create   = _usage_int(usage, "cache_creation_input_tokens", "cacheCreationInputTokens")
    cache_read_separate = _usage_int(usage, "cache_read_input_tokens", "cacheReadInputTokens")
    cached_within_input = _usage_int(usage, "cached_input_tokens", "cachedInputTokens",
                                     "cachedContentTokenCount", "cached")
    thoughts_separate = _usage_int(usage, "thoughtsTokenCount", "thoughts")

    # Token counts take cache writes from the top-level field only; the nested fallback
    # below is billed but never counted. The parse caches' keep/drop filter
    # (usage_token_total > 0) depends on it.
    cache_create_tokens = cache_create
    cache_create_1h = 0
    cc_detail = usage.get("cache_creation")
    if isinstance(cc_detail, dict):
        eph_1h = _usage_int(cc_detail, "ephemeral_1h_input_tokens")
        if cache_create == 0:
            cache_create = eph_1h + _usage_int(cc_detail, "ephemeral_5m_input_tokens")
        cache_create_1h = min(eph_1h, cache_create)

    return {
        "input": input_tokens,
        "uncached_input": max(0, input_tokens - cached_within_input),
        "output": output_tokens + thoughts_separate,
        "tool": tool,
        "cache_create": cache_create,
        "cache_create_tokens": cache_create_tokens,
        "cache_create_1h": cache_create_1h,
        "cache_read_separate": cache_read_separate,
        "cached_within_input": cached_within_input,
    }


def _cost_terms(n: dict[str, int], model: str | None) -> list[tuple[int, float]]:
    """``(USAGE_TYPES index, cost x 1e6)`` per billed lane of one request, in the order
    usage_cost_usd has always summed them (so its total stays bit-identical). Empty when
    the model has no price."""
    prices = _get_pricing_fn()(model)
    if not prices and model:
        prices = _family_default_pricing(model)
    if not prices:
        return []

    # Prompt-length pricing (prompt_tier_prices): this ONE request's prompt is everything
    # it sent the model — fresh input + tool-use prompt + cache writes (breakdown-derived
    # too) + cache reads. Every provider's usage shape reduces to that sum:
    #   Anthropic (Claude): input_tokens, cache writes and cache reads are separate,
    #     additive counts.
    #   OpenAI (Codex) and xAI (Grok): input_tokens / prompt_tokens already include the
    #     cached tokens (cached_within_input), which are not added again.
    #   Gemini: `input` includes `cached`; the tool-use prompt adds on top.
    # Cached tokens count toward the prompt for every provider, Gemini and xAI included
    # (their pricing pages don't say): they are part of the prompt size each API reports.
    prices = prompt_tier_prices(prices, n["input"] + n["tool"] + n["cache_create"]
                                + n["cache_read_separate"])

    input_rate = prices.get("input", 0.0)
    cache_read_rate = prices.get("cache_read", input_rate)
    cache_write_rate = prices.get("cache_write", input_rate)
    cache_write_1h_rate = prices.get("cache_write_1h", cache_write_rate)
    return [
        (0, n["uncached_input"] * input_rate),
        (1, n["output"] * prices.get("output", 0.0)),
        (0, n["tool"] * input_rate),
        (3, n["cache_create_1h"] * cache_write_1h_rate),
        (3, (n["cache_create"] - n["cache_create_1h"]) * cache_write_rate),
        (2, n["cache_read_separate"] * cache_read_rate),
        (2, n["cached_within_input"] * cache_read_rate),
    ]


def _terms_cost(terms: list[tuple[int, float]]) -> float:
    """The terms' total (x 1e6), added one by one in their order. Not sum(): from Python
    3.12 it compensates float rounding, which moves some totals by an ulp off what
    usage_cost_usd has always returned."""
    total = 0.0
    for _, c in terms:
        total += c
    return total


def _type_tokens(n: dict[str, int]) -> tuple[int, int, int, int]:
    return (n["uncached_input"] + n["tool"], n["output"],
            n["cache_read_separate"] + n["cached_within_input"], n["cache_create_tokens"])


def usage_cost_usd(usage: Any, model: str | None) -> float:
    """Compute USD cost for one assistant message's usage dict.

    Recognises Anthropic-style (input_tokens/cache_creation/cache_read/output_tokens)
    and OpenAI/Gemini-style (input_tokens/cached_input_tokens/output_tokens/reasoning_output_tokens)
    breakdowns. Reasoning tokens are billed as output. Cache-read tokens are billed at the
    discounted cache-read rate; cache-creation/write at the higher write rate (Anthropic only).
    """
    if not isinstance(usage, dict):
        return 0.0
    return _terms_cost(_cost_terms(_usage_counts(usage), model)) / 1_000_000.0


def usage_split(usage: Any, model: str | None) -> UsageSplit:
    """One request's total tokens and cost, and both split by USAGE_TYPES — what the
    per-window summaries accumulate. ``cost`` equals usage_cost_usd and ``type_costs``
    add up to it; ``tokens`` equals usage_token_total.

    ``type_tokens`` add up to ``tokens`` unless the record's own total field is larger
    than its parts (observed: Codex events carrying only total_tokens); the summaries
    report that excess as unsplit rather than guess its type."""
    if not isinstance(usage, dict):
        return UsageSplit(0, 0.0, (0, 0, 0, 0), (0.0, 0.0, 0.0, 0.0))
    n = _usage_counts(usage)
    terms = _cost_terms(n, model)
    type_costs = [0.0, 0.0, 0.0, 0.0]
    for idx, c in terms:
        type_costs[idx] += c
    type_tokens = _type_tokens(n)
    total = _explicit_total(usage)
    if total is None:
        total = sum(type_tokens)
    return UsageSplit(
        total,
        _terms_cost(terms) / 1_000_000.0,
        type_tokens,
        (type_costs[0] / 1_000_000.0, type_costs[1] / 1_000_000.0,
         type_costs[2] / 1_000_000.0, type_costs[3] / 1_000_000.0),
    )


# Every usage key the readers actually look at: _usage_counts (behind usage_cost_usd,
# usage_split and usage_token_breakdown) and _explicit_total. Anything else in a provider's usage dict is
# metadata the math never touches (Anthropic ships service_tier, inference_geo,
# server_tool_use, iterations, speed and output_tokens_details on EVERY assistant message;
# OpenAI ships reasoning_output_tokens, which is deliberately not added — it is already
# inside output_tokens).
#
# This set lives here, beside its consumers, so that adding a key to usage_cost_usd without
# adding it here is a one-file mistake rather than a silent cross-module drift. The
# round-trip is pinned by tests/test_accounting.py::test_slim_usage_*.
_USAGE_SCALAR_FIELDS = frozenset({
    "input_tokens", "inputTokens", "promptTokenCount", "input",
    "output_tokens", "outputTokens", "candidatesTokenCount", "output",
    "tool", "tools", "toolTokens", "toolUsePromptTokenCount", "tool_use_prompt_token_count",
    "cache_creation_input_tokens", "cacheCreationInputTokens",
    "cache_read_input_tokens", "cacheReadInputTokens",
    "cached_input_tokens", "cachedInputTokens", "cachedContentTokenCount", "cached",
    "thoughtsTokenCount", "thoughts",
    "total_tokens", "totalTokens", "totalTokenCount", "total",
})
# usage_cost_usd reads the Anthropic 5m/1h split out of this nested dict.
_USAGE_CACHE_CREATION_FIELDS = frozenset({
    "ephemeral_1h_input_tokens", "ephemeral_5m_input_tokens",
})


def slim_usage(usage: Any) -> Any:
    """Project a provider usage dict onto only the fields the token/cost math reads.

    The parse caches under ~/.tallybar/cache/ store one record per assistant message for
    ALL time, and the summarizer re-applies its window on every refresh. Keeping the raw
    usage dict meant ~55% of a 52 MB claude_logs.json was metadata that is loaded, decoded
    into Python objects and re-serialised on every 5-minute refresh, and never read.

    Every consumer treats a missing key and a zero/negative key identically (each uses a
    ``v > 0`` guard), so non-positive values are dropped rather than stored. Non-dict input
    is returned unchanged.
    """
    if not isinstance(usage, dict):
        return usage
    out: dict[str, Any] = {
        k: v for k, v in usage.items()
        if k in _USAGE_SCALAR_FIELDS and isinstance(v, (int, float)) and v > 0
    }
    detail = usage.get("cache_creation")
    if isinstance(detail, dict):
        kept = {
            k: v for k, v in detail.items()
            if k in _USAGE_CACHE_CREATION_FIELDS and isinstance(v, (int, float)) and v > 0
        }
        if kept:
            out["cache_creation"] = kept
    return out


def _explicit_total(usage: Any) -> int | None:
    if not isinstance(usage, dict):
        return None
    for key in ("total_tokens", "totalTokens", "totalTokenCount", "total"):
        value = usage.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    return None


def usage_token_total(usage: Any) -> int:
    total = _explicit_total(usage)
    if total is not None:
        return total
    in_val, out_val, cached_val = usage_token_breakdown(usage)
    return in_val + out_val + cached_val


def usage_token_total_and_breakdown(usage: Any) -> tuple[int, tuple[int, int, int]]:
    """``(total, (uncached_input, output, cached))`` computing the breakdown ONCE."""
    breakdown = usage_token_breakdown(usage)
    total = _explicit_total(usage)
    if total is None:
        total = breakdown[0] + breakdown[1] + breakdown[2]
    return total, breakdown


def usage_token_breakdown(usage: Any) -> tuple[int, int, int]:
    """(billable input, output incl. reasoning, cached) token counts for one
    message — mirrors how usage_cost_usd categorises tokens so the breakdown
    lanes add up to the cost. "cached" is cache reads + cache writes together;
    usage_split keeps the two apart."""
    if not isinstance(usage, dict):
        return (0, 0, 0)
    uncached_input, output, cache_read, cache_write = _type_tokens(_usage_counts(usage))
    return (uncached_input, output, cache_read + cache_write)


def cost_breakdown_line(month_in: int, month_out: int, month_cached: int) -> str:
    parts = []
    if month_in > 0:
        parts.append(f"Input {compact_token_count(month_in)}")
    if month_out > 0:
        parts.append(f"Output {compact_token_count(month_out)}")
    if month_cached > 0:
        parts.append(f"Cached {compact_token_count(month_cached)}")
    return " · ".join(parts)
