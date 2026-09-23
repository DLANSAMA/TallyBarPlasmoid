"""Fetchable model pricing data with disk cache and embedded fallback.

Fetches per-token pricing from the LiteLLM community-maintained model catalog
(https://github.com/BerriAI/litellm) and caches it locally. Falls back to
embedded defaults if the network is unavailable and no cache exists.

All prices are stored as USD per million tokens (MTok) to match the convention
used throughout the rest of the codebase.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

SSL_AVAILABLE = True
try:
    import ssl
    import urllib.request
except ImportError as e:
    SSL_AVAILABLE = False
    print(f"Warning: SSL/networking unavailable ({e}). Cost-tracking features may degrade gracefully.", file=sys.stderr)
from typing import Any


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LITELLM_PRICING_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)

from io_helpers import atomic_write_text, to_daemon_thread

PRICING_CACHE_PATH = Path.home() / ".tallybar" / "pricing_cache.json"
PRICING_CACHE_TTL = 86400.0  # 24 hours — model pricing changes rarely

# Only ingest chat models from these direct-API providers (skip Bedrock/Vertex
# duplicates which carry the same prices but with provider-prefixed keys). NOTE:
# these direct-API providers can ALSO ship provider-prefixed raw model_ids of their
# own (e.g. "gemini/gemini-3.5-flash", "xai/grok-4" — only Anthropic/OpenAI keys are
# reliably bare); _litellm_to_tallybar registers both the raw key and the
# un-prefixed bare name so a query for either form resolves to the live price.
_RELEVANT_PROVIDERS = frozenset(("anthropic", "openai", "gemini", "xai"))


# ---------------------------------------------------------------------------
# Embedded fallback — a snapshot of per-MTok pricing that ships with the code.
# This is ONLY used when both the network fetch and the local cache are
# unavailable (e.g., first run offline). It is intentionally NOT the source of
# truth — the fetched data from LiteLLM is.
# ---------------------------------------------------------------------------

_FALLBACK_PRICING: tuple[tuple[str, dict[str, float]], ...] = (
    # Anthropic Claude. cache_write is the 5-minute write rate (1.25x input);
    # cache_write_1h is the 1-hour write rate (2x input) — usage_cost_usd splits
    # cache_creation by the on-disk ephemeral_1h/5m breakdown and bills each portion
    # at its own rate. (LiteLLM is the source of truth; this ships for offline use.)
    ("claude-fable-5",       {"input": 10.00, "output": 50.00, "cache_write": 12.50, "cache_write_1h": 20.00, "cache_read": 1.00}),
    ("claude-opus-4-8",      {"input":  5.00, "output": 25.00, "cache_write":  6.25, "cache_write_1h": 10.00, "cache_read": 0.50}),
    ("claude-opus-4-7",      {"input":  5.00, "output": 25.00, "cache_write":  6.25, "cache_write_1h": 10.00, "cache_read": 0.50}),
    ("claude-opus-4-6",      {"input":  5.00, "output": 25.00, "cache_write":  6.25, "cache_write_1h": 10.00, "cache_read": 0.50}),
    ("claude-opus-4-5",      {"input":  5.00, "output": 25.00, "cache_write":  6.25, "cache_write_1h": 10.00, "cache_read": 0.50}),
    ("claude-opus-4-1",      {"input": 15.00, "output": 75.00, "cache_write": 18.75, "cache_write_1h": 30.00, "cache_read": 1.50}),
    ("claude-sonnet-5",      {"input":  3.00, "output": 15.00, "cache_write":  3.75, "cache_write_1h":  6.00, "cache_read": 0.30}),
    ("claude-sonnet-4-6",    {"input":  3.00, "output": 15.00, "cache_write":  3.75, "cache_write_1h":  6.00, "cache_read": 0.30}),
    ("claude-sonnet-4-5",    {"input":  3.00, "output": 15.00, "cache_write":  3.75, "cache_write_1h":  6.00, "cache_read": 0.30}),
    ("claude-haiku-4-5",     {"input":  1.00, "output":  5.00, "cache_write":  1.25, "cache_write_1h":  2.00, "cache_read": 0.10}),
    ("claude-3-5-sonnet",    {"input":  3.00, "output": 15.00, "cache_write":  3.75, "cache_write_1h":  6.00, "cache_read": 0.30}),
    ("claude-3-5-haiku",     {"input":  0.80, "output":  4.00, "cache_write":  1.00, "cache_write_1h":  1.60, "cache_read": 0.08}),
    ("claude-3-opus",        {"input": 15.00, "output": 75.00, "cache_write": 18.75, "cache_write_1h": 30.00, "cache_read": 1.50}),
    ("claude-opus-4",        {"input":  5.00, "output": 25.00, "cache_write":  6.25, "cache_write_1h": 10.00, "cache_read": 0.50}),
    ("claude-sonnet-4",      {"input":  3.00, "output": 15.00, "cache_write":  3.75, "cache_write_1h":  6.00, "cache_read": 0.30}),
    ("claude-haiku-4",       {"input":  1.00, "output":  5.00, "cache_write":  1.25, "cache_write_1h":  2.00, "cache_read": 0.10}),
    # OpenAI / Codex
    ("gpt-5.5",              {"input":  5.00, "output": 30.00, "cache_read":  0.50}),
    ("gpt-5.4-mini",         {"input":  0.75, "output":  4.50, "cache_read":  0.075}),
    ("gpt-5.4",              {"input":  2.50, "output": 15.00, "cache_read":  0.25}),
    ("gpt-5-mini",           {"input":  0.75, "output":  4.50, "cache_read":  0.075}),
    ("gpt-5-nano",           {"input":  0.05, "output":  0.40, "cache_read":  0.005}),
    ("gpt-5",                {"input":  2.50, "output": 15.00, "cache_read":  0.25}),
    ("o4-mini",              {"input":  1.10, "output":  4.40, "cache_read":  0.275}),
    ("o3-mini",              {"input":  1.10, "output":  4.40, "cache_read":  0.55}),
    ("o3",                   {"input":  2.00, "output":  8.00, "cache_read":  0.50}),
    ("gpt-4.1-mini",         {"input":  0.40, "output":  1.60, "cache_read":  0.10}),
    ("gpt-4.1-nano",         {"input":  0.10, "output":  0.40, "cache_read":  0.025}),
    ("gpt-4.1",              {"input":  2.00, "output":  8.00, "cache_read":  0.50}),
    ("gpt-4o-mini",          {"input":  0.15, "output":  0.60, "cache_read":  0.075}),
    ("gpt-4o",               {"input":  2.50, "output": 10.00, "cache_read":  1.25}),
    # Google Gemini
    ("gemini-3.1-pro",       {"input":  2.00, "output": 12.00, "cache_read":  0.20}),
    ("gemini-3.5-flash",     {"input":  0.30, "output":  2.50, "cache_read":  0.075}),
    ("gemini-2.5-pro",       {"input":  1.25, "output": 10.00, "cache_read":  0.31}),
    ("gemini-2.5-flash-lite",{"input":  0.10, "output":  0.40, "cache_read":  0.025}),
    ("gemini-2.5-flash",     {"input":  0.30, "output":  2.50, "cache_read":  0.075}),
    ("gemini-1.5-pro",       {"input":  1.25, "output":  5.00, "cache_read":  0.31}),
    ("gemini-1.5-flash",     {"input":  0.075, "output": 0.30, "cache_read":  0.019}),
    # xAI / Grok. The CLI's grok-build / grok-composer are subscription/credit models
    # proxied through cli-chat-proxy.grok.com, so the "Cost (if pay-per-use)" figure
    # prices them at xAI's published coding-model API rate — grok-build-0.1 /
    # grok-code-fast-1 ($1.00/$2.00 per MTok, $0.20 cache-read; docs.x.ai/docs/models,
    # 2026-09-11, and the same figures LiteLLM carries for both keys). Real grok-N
    # models price from LiteLLM when online; these rows are the offline fallback and
    # MUST match the live catalog: a machine with no pricing cache yet (first run, CI)
    # bills straight off this table.
    # _grok_pricing_model() normalizes grok-build → grok-build-0.1 so both keys needed.
    ("grok-build-0.1",            {"input":  1.00, "output":  2.00, "cache_read":  0.20}),
    ("grok-build",                {"input":  1.00, "output":  2.00, "cache_read":  0.20}),
    ("grok-composer-2.5-fast",    {"input":  1.00, "output":  2.00, "cache_read":  0.20}),
    ("grok-composer",             {"input":  1.00, "output":  2.00, "cache_read":  0.20}),
    ("grok-code-fast-1",          {"input":  1.00, "output":  2.00, "cache_read":  0.20}),
    ("grok-code-fast",            {"input":  1.00, "output":  2.00, "cache_read":  0.20}),
    # Named Grok API models (pay-per-use rates from xAI price sheet).
    # grok-4.6 MUST have its own exact key: get_pricing family-matches
    # ``"grok-4" in "grok-4.6"``, so a missing row inherits grok-4's $3/$15
    # instead of the published 4.6 short-context rate ($2 / $6 / $0.50 cache).
    # LiteLLM has lagged this launch; the supplement keeps the live catalog
    # authoritative once it grows a grok-4.6 key.
    ("grok-4.6",                  {"input":  2.00, "output":  6.00, "cache_read":  0.50}),
    ("grok-4.5",                  {"input":  3.00, "output": 15.00, "cache_read":  0.75}),
    ("grok-4.20",                 {"input":  3.00, "output": 15.00, "cache_read":  0.75}),
    ("grok-4.3",                  {"input":  3.00, "output": 15.00, "cache_read":  0.75}),
    ("grok-4",                    {"input":  3.00, "output": 15.00, "cache_read":  0.75}),
    ("grok-3",                    {"input":  3.00, "output": 15.00, "cache_read":  0.75}),
)


# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

# The active pricing table: list of (pattern, {input, output, ...}) tuples.
# Populated on first call to get_pricing(), then refreshed in the background.
_active_pricing: list[tuple[str, dict[str, float]]] | None = None
_last_fetch_time: float = 0.0
# Resolution memo: get_pricing is called once per usage record by the local-log cost
# summarizers, so an un-memoized lookup is O(records × catalog) substring scans per
# refresh. Keyed by lowercased model name; MUST be cleared wherever _active_pricing is
# (re)built, or a stale catalog's resolution survives a refresh.
_resolve_memo: dict[str, dict[str, float]] = {}
# Bumped (under _pricing_lock) every time _active_pricing is (re)built. get_pricing scans
# the catalog OUTSIDE the lock — it runs once per usage record — and only memoizes its
# answer if the generation it scanned is still current.
_catalog_generation = 0
# Guards every mutation of _active_pricing / _resolve_memo / _last_fetch_time /
# _catalog_generation. They race across real OS threads: a daemon thread runs
# compute_local_cost_summaries -> get_pricing while the event-loop thread finishes an
# awaited refresh_pricing. A resolution computed against the pre-refresh catalog must not
# be memoized after refresh_pricing's clear() (it would serve a stale price for the rest of
# the run) — the generation check in get_pricing's memo write is what prevents that; the
# lock alone did not, because the scan and memo write used to happen outside it.
# → tests/test_pricing_data.py::test_memo_write_dropped_when_catalog_refreshed_mid_scan
_pricing_lock = threading.Lock()


# ---------------------------------------------------------------------------
# LiteLLM → TallyBar price format converter
# ---------------------------------------------------------------------------

def _litellm_to_tallybar(raw: dict[str, Any]) -> list[tuple[str, dict[str, float]]]:
    """Convert LiteLLM's per-token pricing dict into the TallyBar per-MTok
    tuple format, filtering to only the direct-API providers we care about."""
    results: list[tuple[str, dict[str, float]]] = []
    for model_id, info in raw.items():
        if model_id == "sample_spec":
            continue
        if not isinstance(info, dict):
            continue
        provider = info.get("litellm_provider", "")
        if provider not in _RELEVANT_PROVIDERS:
            continue
        if info.get("mode") != "chat":
            continue
        input_cpt = info.get("input_cost_per_token")
        output_cpt = info.get("output_cost_per_token")
        if not isinstance(input_cpt, (int, float)) or not isinstance(output_cpt, (int, float)):
            continue

        prices: dict[str, float] = {
            "input": float(input_cpt) * 1_000_000,
            "output": float(output_cpt) * 1_000_000,
        }
        cache_read = info.get("cache_read_input_token_cost")
        if isinstance(cache_read, (int, float)) and cache_read > 0:
            prices["cache_read"] = float(cache_read) * 1_000_000
        cache_write = info.get("cache_creation_input_token_cost")
        if isinstance(cache_write, (int, float)) and cache_write > 0:
            prices["cache_write"] = float(cache_write) * 1_000_000
        # Anthropic prices 1-HOUR cache writes higher than the default 5-minute
        # write (2x vs 1.25x base input). LiteLLM exposes the 1h rate as a separate
        # field; capture it so usage_cost_usd can bill the 1h portion of
        # cache_creation correctly. (OpenAI/Gemini have no 1h-write tier, so the
        # field is absent and cache_write_1h simply stays unset.)
        cache_write_1h = info.get("cache_creation_input_token_cost_above_1hr")
        if isinstance(cache_write_1h, (int, float)) and cache_write_1h > 0:
            prices["cache_write_1h"] = float(cache_write_1h) * 1_000_000
        elif provider == "anthropic":
            # LiteLLM doesn't always populate the 1h-write field (e.g. claude-sonnet-4-5
            # currently omits it). Anthropic's published 1h cache-write rate is a flat
            # 2x input for EVERY Claude model, so synthesize it from input rather than
            # silently letting usage_cost_usd fall back to the 5m cache_write rate — that
            # undercounts 1h-cached Claude usage by ~8.7% (the exact bug the 1h split fixed,
            # re-opened for any model the live catalog hasn't filled in). Matches the
            # embedded fallback table, which hardcodes cache_write_1h = 2x input.
            prices["cache_write_1h"] = prices["input"] * 2.0
        if provider == "anthropic" and "cache_write" not in prices:
            # Anthropic's published 5-minute cache-write rate is 1.25x input for every
            # Claude model; synthesize it when the live catalog omits the field.
            prices["cache_write"] = prices["input"] * 1.25

        results.append((model_id, prices))
        # LiteLLM keys some direct-API providers (gemini, xai) with a "provider/"
        # prefix (e.g. "gemini/gemini-3.5-flash", "xai/grok-4") while others
        # (anthropic, openai) are bare. get_pricing's exact-match rule is keyed on
        # the exact queried name, which is always the bare form elsewhere in this
        # codebase — without also registering the bare name, a prefixed live entry
        # can NEVER win the exact-match check for "gemini-3.5-flash", so the
        # embedded _FALLBACK_PRICING row (appended by _with_fallback_supplement,
        # which only excludes fallback keys already present verbatim) wins instead
        # and permanently shadows the live rate. Register both forms so a query for
        # either the raw prefixed id or the bare name resolves to the same live price.
        if "/" in model_id:
            bare_name = model_id.split("/", 1)[1]
            if bare_name and bare_name != model_id:
                results.append((bare_name, prices))
    return results


# ---------------------------------------------------------------------------
# Fetch and cache
# ---------------------------------------------------------------------------

def _fetch_litellm_pricing(timeout: float = 10.0) -> dict[str, Any] | None:
    """Download the LiteLLM model catalog JSON. Returns None on any failure."""
    if not SSL_AVAILABLE:
        return None
    try:
        request = urllib.request.Request(
            LITELLM_PRICING_URL,
            headers={"Accept": "application/json", "User-Agent": "tallybar-plasmoid/1.0"},
        )
        # Use default SSL for public GitHub raw content
        context = ssl.create_default_context()
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            if response.status != 200:
                return None
            return json.loads(response.read(20_000_000).decode("utf-8", errors="replace"))
    except Exception:
        return None


def _load_cache() -> list[tuple[str, dict[str, float]]] | None:
    """Load the cached pricing from disk. Returns None if missing/corrupt/expired."""
    try:
        data = json.loads(PRICING_CACHE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        fetched_at = float(data.get("fetchedAt", 0))
        if time.time() - fetched_at > PRICING_CACHE_TTL:
            return None  # Expired, but we'll still use it as a fallback below
        entries = data.get("pricing")
        if not isinstance(entries, list):
            return None
        # Require the price value to be a dict too: a corrupt/truncated entry whose
        # value isn't a price map would otherwise flow into get_pricing and crash
        # every cost caller (usage_cost_usd / _prices_for). Drop bad entries instead.
        return [(str(e[0]), e[1]) for e in entries
                if isinstance(e, list) and len(e) == 2 and isinstance(e[1], dict)]
    except Exception:
        return None


def _load_cache_stale() -> list[tuple[str, dict[str, float]]] | None:
    """Load cached pricing regardless of TTL — better stale than nothing."""
    try:
        data = json.loads(PRICING_CACHE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        entries = data.get("pricing")
        if not isinstance(entries, list):
            return None
        return [(str(e[0]), e[1]) for e in entries
                if isinstance(e, list) and len(e) == 2 and isinstance(e[1], dict)]
    except Exception:
        return None


def _save_cache(pricing: list[tuple[str, dict[str, float]]]) -> None:
    """Persist pricing to disk as JSON via the shared io_helpers atomic-0600 recipe
    (unique mkstemp, fsync, replace, dir-fsync). Best-effort: the cache is
    regenerable, so any write failure is swallowed."""
    try:
        payload = json.dumps({"fetchedAt": time.time(), "pricing": pricing})
        atomic_write_text(PRICING_CACHE_PATH, payload)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _with_fallback_supplement(entries: list[tuple[str, dict[str, float]]]) -> list[tuple[str, dict[str, float]]]:
    """Append embedded-fallback rows for models the live catalog doesn't have an
    exact key for. LiteLLM can lag a model launch by days (e.g. claude-fable-5);
    without the supplement, a brand-new model misses the catalog entirely and
    usage_cost_usd bills it at the provider-family DEFAULT rate (claude-sonnet-4
    for Claude — a ~70% undercount for Fable 5). Appending (not prepending) keeps
    the live catalog authoritative: exact keys win outright, and family matching
    picks the longest pattern regardless of position."""
    known = {key for key, _ in entries}
    supplement = [(key, prices) for key, prices in _FALLBACK_PRICING if key not in known]
    return list(entries) + supplement


async def refresh_pricing(timeout: float = 10.0) -> bool:
    """Fetch latest pricing from LiteLLM and update the in-memory + disk cache.
    Returns True if successful, False if the fetch failed (stale data retained)."""
    global _active_pricing, _last_fetch_time, _catalog_generation

    raw = await to_daemon_thread(_fetch_litellm_pricing, timeout)
    if raw is None:
        return False

    converted = _litellm_to_tallybar(raw)
    if not converted:
        return False

    with _pricing_lock:
        _active_pricing = _with_fallback_supplement(converted)
        _resolve_memo.clear()
        _catalog_generation += 1
        _last_fetch_time = time.time()
    _save_cache(converted)  # disk cache stays pure fetched data
    return True


def get_pricing(model: str | None) -> dict[str, float]:
    """Look up per-MTok pricing for a model name (case-insensitive substring
    match, same semantics as the original MODEL_PRICING_USD_PER_MTOK).

    ONLY does fast in-memory or stale disk-cache reads, and never calls
    refresh_pricing synchronously."""
    global _active_pricing, _last_fetch_time, _catalog_generation

    # Bootstrap: load pricing data if not yet in memory
    if _active_pricing is None:
        with _pricing_lock:
            # Re-check inside the lock: another thread may have already bootstrapped
            # while we were waiting to acquire it (double-checked locking).
            if _active_pricing is None:
                _resolve_memo.clear()
                _catalog_generation += 1
                # Try disk cache first (fast, no network)
                cached = _load_cache()
                if cached:
                    _active_pricing = _with_fallback_supplement(cached)
                    _last_fetch_time = time.time()
                else:
                    # Try stale cache (better than nothing, no network)
                    stale = _load_cache_stale()
                    if stale:
                        _active_pricing = _with_fallback_supplement(stale)
                        try:
                            _last_fetch_time = os.path.getmtime(PRICING_CACHE_PATH)
                        except Exception:
                            _last_fetch_time = 0.0
                    else:
                        # Last resort: embedded fallback
                        _active_pricing = list(_FALLBACK_PRICING)
                        _last_fetch_time = 0.0

    # Lookup: prefer an EXACT (case-insensitive) key, then the most specific
    # FAMILY match (a catalog key that is a substring of the queried name — e.g.
    # "gpt-5" for "gpt-5-codex"), and only then the looser direction (the query is
    # a substring of a longer catalog key). Returning the FIRST hit in either
    # direction (the old behaviour) let a short canonical name like "gpt-5" resolve
    # to a longer earlier-listed key ("gpt-5.5") and mis-price by ~2x; exact-first
    # plus longest-pattern fixes that while preserving the useful family matching
    # (still guarded by test_pricing_data.py's bidirectional-match test).
    name = (model or "").lower()
    if not name:
        return {}
    hit = _resolve_memo.get(name)  # dict.get is atomic under the GIL; no lock on the hot path
    if hit is not None:
        return hit
    with _pricing_lock:
        catalog = _active_pricing or []
        generation = _catalog_generation
    result: dict[str, float] = {}
    family = None        # pattern in name  (catalog key ⊂ query) — most specific family
    family_len = -1
    loose = None         # name in pattern  (query ⊂ longer catalog key) — last resort
    loose_len = -1
    for pattern, prices in catalog:
        if pattern == name:
            result = prices                    # exact key wins outright
            break
        if pattern in name:
            if len(pattern) > family_len:
                family, family_len = prices, len(pattern)
        elif name in pattern:
            if len(pattern) > loose_len:
                loose, loose_len = prices, len(pattern)
    else:
        if family is not None:
            result = family
        elif loose is not None:
            result = loose
    with _pricing_lock:
        if generation == _catalog_generation:  # catalog not rebuilt while we scanned
            _resolve_memo[name] = result
    return result


def invalidate_cache() -> None:
    """Clear in-memory pricing so the next get_pricing() call re-bootstraps."""
    global _active_pricing, _last_fetch_time, _catalog_generation
    with _pricing_lock:
        _active_pricing = None
        _last_fetch_time = 0.0
        _resolve_memo.clear()
        _catalog_generation += 1
