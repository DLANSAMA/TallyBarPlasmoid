"""Model pricing and usage cost calculation."""
from __future__ import annotations

import sys
from typing import Any

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
            (len(m) >= 2 and m[0] == "o" and m[1].isdigit()):  # o3 / o4-mini / o1
        return "codex"
    if any(t in m for t in ("grok", "xai")):
        return "grok"
    return None


def _family_default_pricing(model: str | None) -> dict[str, float]:
    """Pricing for the family-default model of `model`'s inferred provider, or {}."""
    family = _provider_family_for_model(model)
    if family is None:
        return {}
    return _get_pricing_fn()(DEFAULT_MODEL_FOR_PROVIDER[family])


def usage_cost_usd(usage: Any, model: str | None) -> float:
    """Compute USD cost for one assistant message's usage dict.

    Recognises Anthropic-style (input_tokens/cache_creation/cache_read/output_tokens)
    and OpenAI/Gemini-style (input_tokens/cached_input_tokens/output_tokens/reasoning_output_tokens)
    breakdowns. Reasoning tokens are billed as output. Cache-read tokens are billed at the
    discounted cache-read rate; cache-creation/write at the higher write rate (Anthropic only).
    """
    if not isinstance(usage, dict):
        return 0.0
    prices = _get_pricing_fn()(model)
    if not prices and model:
        # A non-empty model the catalog doesn't know (a brand-new model that shipped
        # before LiteLLM catalogued it) would otherwise bill $0 while tokens still
        # count. Fall back to its provider family's default pricing instead.
        prices = _family_default_pricing(model)
    if not prices:
        return 0.0

    def get(*keys: str) -> int:
        for k in keys:
            v = usage.get(k)
            if isinstance(v, (int, float)) and v > 0:
                return int(v)
        return 0

    input_tokens   = get("input_tokens", "inputTokens", "promptTokenCount", "input")
    output_tokens  = get("output_tokens", "outputTokens", "candidatesTokenCount", "output")
    tool           = get("tool", "tools", "toolTokens", "toolUsePromptTokenCount", "tool_use_prompt_token_count")
    cache_create   = get("cache_creation_input_tokens", "cacheCreationInputTokens")
    cache_read_separate = get("cache_read_input_tokens", "cacheReadInputTokens")
    cached_within_input = get("cached_input_tokens", "cachedInputTokens",
                              "cachedContentTokenCount", "cached")
    thoughts_separate = get("thoughtsTokenCount", "thoughts")

    uncached_input  = max(0, input_tokens - cached_within_input)
    billable_output = output_tokens + thoughts_separate
    cache_read_rate = prices.get("cache_read", prices.get("input", 0.0))
    cache_write_rate = prices.get("cache_write", prices.get("input", 0.0))

    cache_write_1h_rate = prices.get("cache_write_1h", cache_write_rate)
    cache_create_1h = 0
    cc_detail = usage.get("cache_creation")
    if isinstance(cc_detail, dict):
        eph_1h = cc_detail.get("ephemeral_1h_input_tokens")
        eph_1h = int(eph_1h) if isinstance(eph_1h, (int, float)) and eph_1h > 0 else 0
        if cache_create == 0:
            eph_5m = cc_detail.get("ephemeral_5m_input_tokens")
            eph_5m = int(eph_5m) if isinstance(eph_5m, (int, float)) and eph_5m > 0 else 0
            cache_create = eph_1h + eph_5m
        cache_create_1h = min(eph_1h, cache_create)

    cost = 0.0
    cost += uncached_input            * prices.get("input", 0.0)
    cost += billable_output           * prices.get("output", 0.0)
    cost += tool                      * prices.get("input", 0.0)
    cost += cache_create_1h           * cache_write_1h_rate
    cost += (cache_create - cache_create_1h) * cache_write_rate
    cost += cache_read_separate       * cache_read_rate
    cost += cached_within_input       * cache_read_rate
    return cost / 1_000_000.0


def _explicit_total(usage: Any) -> int | None:
    """The record's own total-token field, if it carries a positive one."""
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
    # fallback: reconstruct total using breakdown elements
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
    lanes add up to the cost."""
    if not isinstance(usage, dict):
        return (0, 0, 0)

    def get(*keys: str) -> int:
        for k in keys:
            v = usage.get(k)
            if isinstance(v, (int, float)) and v > 0:
                return int(v)
        return 0

    input_tokens = get("input_tokens", "inputTokens", "promptTokenCount", "input")
    output_tokens = get("output_tokens", "outputTokens", "candidatesTokenCount", "output")
    thoughts_separate = get("thoughtsTokenCount", "thoughts")
    tool = get("tool", "tools", "toolTokens", "toolUsePromptTokenCount", "tool_use_prompt_token_count")
    cache_create = get("cache_creation_input_tokens", "cacheCreationInputTokens")
    cache_read_separate = get("cache_read_input_tokens", "cacheReadInputTokens")
    cached_within_input = get("cached_input_tokens", "cachedInputTokens",
                              "cachedContentTokenCount", "cached")
    uncached_input = max(0, input_tokens - cached_within_input) + tool
    cached_total = cache_create + cache_read_separate + cached_within_input
    return (uncached_input, output_tokens + thoughts_separate, cached_total)


def cost_breakdown_line(month_in: int, month_out: int, month_cached: int) -> str:
    parts = []
    if month_in > 0:
        parts.append(f"Input {compact_token_count(month_in)}")
    if month_out > 0:
        parts.append(f"Output {compact_token_count(month_out)}")
    if month_cached > 0:
        parts.append(f"Cached {compact_token_count(month_cached)}")
    return " · ".join(parts)
