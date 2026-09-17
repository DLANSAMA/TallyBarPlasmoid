"""Antigravity CLI (agy) token capture via statusLine push + fold-in to the cost summary."""

import datetime as dt
import io
import json
import sys
from pathlib import Path

import pytest

CODE_DIR = Path(__file__).parent.parent / "io.github.dlansama.tallybar" / "contents" / "code"
CLI_DIR = Path(__file__).parent.parent / "integrations" / "antigravity_cli"
sys.path.insert(0, str(CODE_DIR))
sys.path.insert(0, str(CLI_DIR))

from providers import cost as costmod  # noqa: E402
from providers import antigravity as agmod  # noqa: E402
import cli_statusline_capture as cap  # noqa: E402


@pytest.fixture(autouse=True)
def _no_live_rpc(monkeypatch):
    # Unit tests must never reach a live Antigravity language server. Default the RPC source to
    # empty; tests exercising the RPC fold override this with their own fake payload.
    monkeypatch.setattr(agmod, "collect_antigravity_rpc_usage", lambda *a, **k: ({}, {}))

# A real statusLine payload (captured live from agy) — the confirmed schema.
REAL_PAYLOAD = {
    "session_id": "cafe1234-0000-0000-0000-0000000000ce",
    "model": {"id": "Gemini 3.5 Flash (High)"},
    "plan_tier": "Google AI Ultra",
    "context_window": {
        "total_input_tokens": 61794,
        "total_output_tokens": 4798,
        "used_percentage": 5.89,
        "current_usage": {
            "input_tokens": 2321,
            "output_tokens": 2213,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 48848,
        },
    },
}


def test_extract_real_schema():
    sid, agg = cap._extract(REAL_PAYLOAD)
    assert sid == "cafe1234-0000-0000-0000-0000000000ce"
    # u = total_input(61794) - cache_read(48848) = 12946 ; c = 48848 ; o = total_output(4798)
    assert agg == {"u": 12946, "c": 48848, "o": 4798, "t": 0, "x": 0}


@pytest.fixture
def cli_paths(tmp_path, monkeypatch):
    usage = tmp_path / "antigravity_cli_usage.json"
    monkeypatch.setattr(cap, "USAGE_PATH", usage)
    monkeypatch.setattr(cap, "_LOCK_PATH", tmp_path / "antigravity_cli_usage.lock")
    monkeypatch.setattr(cap, "_RAW_DUMP", tmp_path / "raw.json")
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CLI_USAGE_PATH", usage)
    return usage


def _run_capture(monkeypatch, payload):
    monkeypatch.setattr(cap._sys, "stdin", io.StringIO(json.dumps(payload)))
    cap.main()


def test_capture_writes_ledger(cli_paths, monkeypatch):
    _run_capture(monkeypatch, REAL_PAYLOAD)
    data = json.loads(cli_paths.read_text())
    assert list(data["entries"]) == ["cli:cafe1234-0000-0000-0000-0000000000ce"]
    e = data["entries"]["cli:cafe1234-0000-0000-0000-0000000000ce"]
    assert (e["u"], e["c"], e["o"]) == (12946, 48848, 4798)
    assert e["model"] == "Gemini 3.5 Flash (High)"  # captured for per-model pricing


def test_normalize_model_name():
    assert costmod._normalize_model_name("Gemini 3.5 Flash (High)") == "gemini-3.5-flash"
    assert costmod._normalize_model_name("Gemini 3.1 Pro (High)") == "gemini-3.1-pro"


def test_cli_priced_per_model_not_pro_default(cli_paths, monkeypatch, tmp_path):
    """A Flash CLI session must cost less than the same tokens at the Pro default rate."""
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CONVERSATION_DIRS", ())
    monkeypatch.setattr(costmod, "ANTIGRAVITY_LEDGER_PATH", tmp_path / "ide_ledger.json")
    today = dt.date.today().isoformat()
    toks = {"u": 100000, "c": 0, "o": 50000, "t": 0, "x": 0}

    def cost_with(entry):
        cli_paths.write_text(json.dumps({"trackingStarted": today, "entries": {"cli:s": {"d": today, **entry}}}))
        s = costmod.antigravity_ledger_cost_summary()
        return next(b for b in s["weeklyTokenUsage"] if b["date"] == today)["cost"]

    flash_cost = cost_with({"model": "Gemini 3.5 Flash (High)", **toks})
    pro_cost = cost_with(toks)  # no model -> Gemini 3.1 Pro default
    # Claude Opus uses hyphen version key (claude-opus-4-6); the dot->hyphen fallback must find it,
    # so Opus prices ABOVE Pro (not silently falling back to the Pro default).
    opus_cost = cost_with({"model": "Claude Opus 4.6 (Thinking)", **toks})
    # GPT-OSS has no catalog price -> our embedded estimate (cheap), must NOT fall back to Pro.
    oss_cost = cost_with({"model": "GPT-OSS 120B", **toks})
    # Gemini 3.6 Flash: no LiteLLM key yet -> the _EXTRA_MODEL_PRICES estimate
    # ($1.50/$7.50, output cheaper than 3.5 Flash's $9), must NOT fall back to Pro.
    flash36_cost = cost_with({"model": "Gemini 3.6 Flash (High)", **toks})
    assert 0 < oss_cost < flash36_cost < flash_cost < pro_cost < opus_cost


def test_gemini_36_enums_resolve(monkeypatch, tmp_path):
    """me-only ledger entries for the 3.6 Flash effort trio must resolve (display
    AND pricing) via the static enum table, not fall to the Pro default."""
    for enum in (1264, 1265, 1266):
        name = costmod._MODEL_ENUM_NAMES[enum]
        assert name.startswith("Gemini 3.6 Flash")
        assert costmod._normalize_model_name(name) == "gemini-3.6-flash"
    assert "gemini-3.6-flash" in costmod._EXTRA_MODEL_PRICES


def test_capture_is_idempotent_overwrite(cli_paths, monkeypatch):
    _run_capture(monkeypatch, REAL_PAYLOAD)
    bigger = json.loads(json.dumps(REAL_PAYLOAD))
    bigger["context_window"]["total_input_tokens"] = 90000
    bigger["context_window"]["total_output_tokens"] = 9000
    _run_capture(monkeypatch, bigger)
    data = json.loads(cli_paths.read_text())
    assert len(data["entries"]) == 1  # same session -> overwritten, not summed
    e = data["entries"]["cli:cafe1234-0000-0000-0000-0000000000ce"]
    assert e["u"] == 90000 - 48848 and e["o"] == 9000


def test_summary_includes_cli_tokens(cli_paths, monkeypatch, tmp_path):
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CONVERSATION_DIRS", ())  # no IDE data
    monkeypatch.setattr(costmod, "ANTIGRAVITY_LEDGER_PATH", tmp_path / "ide_ledger.json")
    _run_capture(monkeypatch, REAL_PAYLOAD)

    summary = costmod.antigravity_ledger_cost_summary()
    assert summary is not None
    today = dt.date.today().isoformat()
    bucket = next(b for b in summary["weeklyTokenUsage"] if b["date"] == today)
    # Token volume = total processed = uncached input + cached re-read + output (the
    # industry-standard total; cached is billed at the discounted cache-read rate in the
    # cost and shown as its own lane in the breakdown).
    assert bucket["tokens"] == 12946 + 48848 + 4798  # 66592


def test_cache_counted_in_volume_and_billed(monkeypatch, tmp_path):
    # Cached re-reads are part of the standard token VOLUME AND are billed in the cost
    # (at the cache-read rate). More cache -> more tokens AND more cost.
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CONVERSATION_DIRS", ())
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CLI_USAGE_PATH", tmp_path / "cli.json")
    ledger = tmp_path / "ide_ledger.json"
    monkeypatch.setattr(costmod, "ANTIGRAVITY_LEDGER_PATH", ledger)
    monkeypatch.setattr(costmod, "model_pricing",
                        lambda *a, **k: {"input": 10.0, "output": 10.0, "cache_read": 10.0})
    today = dt.date.today().isoformat()

    def vol_and_cost(c):
        ledger.write_text(json.dumps({"trackingStarted": today, "entries": {
            "conv:0": {"d": today, "me": 1133, "u": 100000, "c": c, "o": 50000, "t": 0, "x": 0}}}))
        b = next(x for x in costmod.antigravity_ledger_cost_summary()["weeklyTokenUsage"]
                 if x["date"] == today)
        return b["tokens"], b["cost"]

    tok0, cost0 = vol_and_cost(0)
    tok1, cost1 = vol_and_cost(900000)
    assert tok1 == tok0 + 900000   # cached IS counted in the standard total volume
    assert cost1 > cost0           # and billed (at the cache-read rate)


def test_relaxed_gate_shows_cli_when_ide_not_running(cli_paths, monkeypatch, tmp_path):
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CONVERSATION_DIRS", ())
    monkeypatch.setattr(costmod, "ANTIGRAVITY_LEDGER_PATH", tmp_path / "ide_ledger.json")
    _run_capture(monkeypatch, REAL_PAYLOAD)

    providers = {"antigravity": {"label": "Antigravity", "status": "not-running", "limits": []}}
    costmod.apply_local_cost_summaries(providers)
    assert "costSummary" in providers["antigravity"]


def test_pb_find_usage_is_model_agnostic():
    # usage record: field1=model enum 1133 (Flash), field2=100 input, field3=50 output, field6=24 marker
    blob = bytes([0x08, 0xED, 0x08, 0x10, 0x64, 0x18, 0x32, 0x30, 0x18])
    found = costmod._pb_find_usage(blob)
    assert found and found[0].get(1) == 1133 and found[0].get(2) == 100 and found[0].get(6) == 24
    # the OLD gate (field1 must == 1016) would have rejected this Flash record
    assert found[0].get(1) != 1016


def test_desktop_entry_priced_by_model_enum(monkeypatch, tmp_path):
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CONVERSATION_DIRS", ())
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CLI_USAGE_PATH", tmp_path / "cli.json")
    ledger = tmp_path / "ide_ledger.json"
    monkeypatch.setattr(costmod, "ANTIGRAVITY_LEDGER_PATH", ledger)
    today = dt.date.today().isoformat()
    toks = {"u": 100000, "c": 0, "o": 50000, "t": 0, "x": 0}

    def cost_with(enum):
        ledger.write_text(json.dumps({"trackingStarted": today, "entries": {"x:0": {"d": today, "me": enum, **toks}}}))
        s = costmod.antigravity_ledger_cost_summary()
        return next(b for b in s["weeklyTokenUsage"] if b["date"] == today)["cost"]

    flash = cost_with(1133)   # -> gemini-3.5-flash $0.5/$3
    pro = cost_with(1016)     # -> gemini-3-pro $2/$12
    assert 0 < flash < pro


def test_learned_model_name_heals_unmapped_enum_to_real_name(monkeypatch, tmp_path):
    """A model enum GetAvailableModels reports live (even with zero usage this run) gets
    remembered in the ledger's learnedModelNames, healing ANY entry with that enum --
    including a disk-scan entry from an entirely separate run -- to its real picker name
    instead of the generic "Model M<N>" placeholder. The learned name must also survive a
    later run where Antigravity has closed and the RPC returns nothing at all."""
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CONVERSATION_DIRS", ())
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CLI_USAGE_PATH", tmp_path / "cli.json")
    ledger_path = tmp_path / "ide_ledger.json"
    monkeypatch.setattr(costmod, "ANTIGRAVITY_LEDGER_PATH", ledger_path)
    today = dt.date.today().isoformat()

    # Seed a disk-scan-style entry (me only, no model string) for an enum not in
    # _MODEL_ENUM_NAMES -- simulating a prior scan that couldn't name it.
    assert 1084 not in costmod._MODEL_ENUM_NAMES
    ledger_path.write_text(json.dumps({
        "trackingStarted": today,
        "entries": {"disk-stem:0": {"d": today, "u": 100000, "c": 0, "o": 50000, "me": 1084}},
    }))

    def _fake_rpc_with_learned(timeout, known=None, deadline=None, learned_names=None, **_kw):
        if learned_names is not None:
            learned_names["MODEL_PLACEHOLDER_M84"] = "Gemini 4.0 Ultra"
        return {}, {}  # zero NEW usage this run -- the model was only seen via GetAvailableModels

    monkeypatch.setattr(agmod, "collect_antigravity_rpc_usage", _fake_rpc_with_learned)

    summary = costmod.antigravity_ledger_cost_summary()
    labels = {row["model"] for row in summary["modelBreakdown"]}
    assert "Gemini 4.0 Ultra" in labels
    assert "Model M84" not in labels

    saved = json.loads(ledger_path.read_text())
    assert saved["learnedModelNames"].get("1084") == "Gemini 4.0 Ultra"

    # Antigravity has since closed -- the RPC returns nothing at all -- but the persisted
    # learned name must still heal the entry, not just during the run it was captured.
    monkeypatch.setattr(agmod, "collect_antigravity_rpc_usage", lambda *a, **k: ({}, {}))
    summary2 = costmod.antigravity_ledger_cost_summary()
    labels2 = {row["model"] for row in summary2["modelBreakdown"]}
    assert "Gemini 4.0 Ultra" in labels2


def test_unmapped_enum_shows_self_documenting_placeholder_not_other(monkeypatch, tmp_path):
    # A model placeholder Antigravity has started using ahead of a _MODEL_ENUM_NAMES
    # update (enum = 1000 + N, matching MODEL_PLACEHOLDER_M<N> everywhere else in this
    # codebase) must read as "Model M84" in the cost breakdown, not silently collapse
    # into an unidentifiable "Other" bucket.
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CONVERSATION_DIRS", ())
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CLI_USAGE_PATH", tmp_path / "cli.json")
    ledger = tmp_path / "ide_ledger.json"
    monkeypatch.setattr(costmod, "ANTIGRAVITY_LEDGER_PATH", ledger)
    today = dt.date.today().isoformat()
    toks = {"u": 100000, "c": 0, "o": 50000, "t": 0, "x": 0}
    assert 1084 not in costmod._MODEL_ENUM_NAMES  # sanity: this test is about an UNMAPPED enum
    ledger.write_text(json.dumps({"trackingStarted": today, "entries": {"x:0": {"d": today, "me": 1084, **toks}}}))
    summary = costmod.antigravity_ledger_cost_summary()
    labels = {row["model"] for row in summary["modelBreakdown"]}
    assert "Model M84" in labels
    assert "Other" not in labels and "Unknown" not in labels


def test_no_summary_without_tokens(cli_paths, monkeypatch, tmp_path):
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CONVERSATION_DIRS", ())
    monkeypatch.setattr(costmod, "ANTIGRAVITY_LEDGER_PATH", tmp_path / "ide_ledger.json")
    providers = {"antigravity": {"label": "Antigravity", "status": "ok", "limits": []}}
    costmod.apply_local_cost_summaries(providers)
    assert "costSummary" not in providers["antigravity"]  # subscription price NOT shown


def test_google_ai_tier_propagates_to_gemini_when_ok():
    """Only Antigravity's GetUserStatus exposes the exact Google AI tier name; Gemini
    and Antigravity share one subscription, so apply_cost_summaries (via
    google_ai_subscription_tier) must copy it onto providers['gemini']['tier'] whenever
    Gemini's own fetch came back 'ok'."""
    providers = {
        "antigravity": {"label": "Antigravity", "status": "ok", "tier": "Google AI Ultra", "limits": []},
        "gemini": {"label": "Gemini", "status": "ok", "limits": []},
    }
    costmod.apply_cost_summaries(providers, {})
    assert providers["gemini"]["tier"] == "Google AI Ultra"


def test_google_ai_tier_not_propagated_when_gemini_not_ok():
    # Gemini's own fetch failed this run (e.g. unauthorized) -- don't stamp a tier onto
    # a provider entry whose data isn't actually live.
    providers = {
        "antigravity": {"label": "Antigravity", "status": "ok", "tier": "Google AI Ultra", "limits": []},
        "gemini": {"label": "Gemini", "status": "unauthorized", "limits": []},
    }
    costmod.apply_cost_summaries(providers, {})
    assert "tier" not in providers["gemini"]


def test_google_ai_tier_not_propagated_when_tier_not_google_ai():
    # google_ai_subscription_tier only recognises tiers that literally start with
    # "google ai" -- an unrelated/blank Antigravity tier must not leak onto Gemini.
    providers = {
        "antigravity": {"label": "Antigravity", "status": "ok", "tier": "Free", "limits": []},
        "gemini": {"label": "Gemini", "status": "ok", "limits": []},
    }
    costmod.apply_cost_summaries(providers, {})
    assert "tier" not in providers["gemini"]


def test_rpc_model_pricing_key():
    # MODEL_PLACEHOLDER_M<N> resolves via enum (1000+N) to the EXACT picker display name
    # (verified live against GetAvailableModels 2026-06-09); apiProvider is the fallback family.
    assert costmod._rpc_model_pricing_key("MODEL_PLACEHOLDER_M133", "API_PROVIDER_GOOGLE_GEMINI") == "Gemini 3.5 Flash"
    assert costmod._rpc_model_pricing_key("MODEL_PLACEHOLDER_M132", "API_PROVIDER_GOOGLE_GEMINI") == "Gemini 3.5 Flash (High)"
    assert costmod._rpc_model_pricing_key("MODEL_PLACEHOLDER_M26", "API_PROVIDER_ANTHROPIC_VERTEX") == "Claude Opus 4.6 (Thinking)"
    # M16 is Gemini 3.1 Pro (High) — there is NO "Gemini 3 Pro" in the picker.
    assert costmod._rpc_model_pricing_key("MODEL_PLACEHOLDER_M16", "API_PROVIDER_GOOGLE_GEMINI") == "Gemini 3.1 Pro (High)"
    # Unknown placeholder -> provider-family fallback (never silently Gemini for a Claude model)
    assert costmod._rpc_model_pricing_key("MODEL_PLACEHOLDER_M999", "API_PROVIDER_ANTHROPIC_VERTEX") == "claude-opus-4-6"
    assert costmod._rpc_model_pricing_key("", "API_PROVIDER_GOOGLE_GEMINI") == "gemini-3.1-pro"


def test_rpc_local_date():
    # Unparseable / empty -> "" so the caller falls back to today.
    assert agmod._rpc_local_date("") == ""
    assert agmod._rpc_local_date("not-a-date") == ""
    # Nanosecond precision + trailing Z must not crash (fromisoformat rejects >6 frac digits);
    # the result is a real local YYYY-MM-DD within one tz-offset day of the UTC instant
    # (this is the regression guard for the "always fell back to today" createdAt bug).
    out = agmod._rpc_local_date("2026-05-28T12:00:00.137443809Z")
    assert len(out) == 10 and out.count("-") == 2
    assert out in {"2026-05-27", "2026-05-28", "2026-05-29"}
    # A UTC instant just past midnight localises to the correct *local* day (not naive [:10]).
    near_midnight = agmod._rpc_local_date("2026-05-30T00:30:48.001487532Z")
    assert near_midnight in {"2026-05-29", "2026-05-30"}


def test_output_not_double_counted(monkeypatch, tmp_path):
    """`o` is total output; `t`/`x` (thinking/response) are subsets — billing them again would
    double-count. An entry with t/x set must cost (and total tokens) exactly as if t=x=0."""
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CONVERSATION_DIRS", ())
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CLI_USAGE_PATH", tmp_path / "cli.json")
    ledger = tmp_path / "ide_ledger.json"
    monkeypatch.setattr(costmod, "ANTIGRAVITY_LEDGER_PATH", ledger)
    today = dt.date.today().isoformat()

    def summarize(entry):
        ledger.write_text(json.dumps({"trackingStarted": today, "entries": {"x#0": {"d": today, **entry}}}))
        s = costmod.antigravity_ledger_cost_summary()
        b = next(b for b in s["weeklyTokenUsage"] if b["date"] == today)
        return b["cost"], b["tokens"]

    base = {"u": 1000, "c": 0, "o": 300, "model": "gemini-3.5-flash"}
    with_subsets = summarize({**base, "t": 200, "x": 100})   # o == t + x
    without = summarize({**base, "t": 0, "x": 0})
    assert with_subsets == without            # t/x are NOT billed/counted on top of o
    assert without[1] == 1000 + 0 + 300       # tokens = u + c + o


def _fake_rpc(payload):
    return lambda *a, **k: (payload, {})


def test_rpc_usage_folds_with_per_model_pricing(monkeypatch, tmp_path):
    """Desktop/IDE generations harvested from the language-server RPC land in the ledger, each
    priced by its real model (Gemini Flash vs Claude Opus), keyed `<cascadeId>#<step>`."""
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CONVERSATION_DIRS", ())
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CLI_USAGE_PATH", tmp_path / "cli.json")
    monkeypatch.setattr(costmod, "ANTIGRAVITY_LEDGER_PATH", tmp_path / "ide_ledger.json")
    today = dt.date.today().isoformat()
    cid = "cafe1234-0000-0000-0000-000000000001"
    monkeypatch.setattr(agmod, "collect_antigravity_rpc_usage", _fake_rpc({
        cid: [
            {"stepKey": 4, "u": 1000, "c": 0, "o": 200,
             "model_placeholder": "MODEL_PLACEHOLDER_M133", "api_provider": "API_PROVIDER_GOOGLE_GEMINI", "date": today},
            {"stepKey": 6, "u": 1000, "c": 0, "o": 200,
             "model_placeholder": "MODEL_PLACEHOLDER_M26", "api_provider": "API_PROVIDER_ANTHROPIC_VERTEX", "date": today},
        ]
    }))
    res = costmod.update_antigravity_token_ledger()
    assert res["entries"][f"{cid}#4"]["model"] == "Gemini 3.5 Flash"
    assert res["entries"][f"{cid}#6"]["model"] == "Claude Opus 4.6 (Thinking)"
    # Identical tokens, different model -> Claude Opus must cost more than Gemini Flash.
    flash_c = costmod.model_pricing("gemini-3.5-flash")
    opus_c = costmod.model_pricing("claude-opus-4-6")
    assert opus_c["output"] > flash_c["output"] > 0


def test_rpc_supersedes_legacy_db_entries(monkeypatch, tmp_path):
    """When the RPC covers a conversation, its stale on-disk-sourced entries are dropped so the two
    sources never double-count the same conversation."""
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CONVERSATION_DIRS", ())
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CLI_USAGE_PATH", tmp_path / "cli.json")
    ledger = tmp_path / "ide_ledger.json"
    monkeypatch.setattr(costmod, "ANTIGRAVITY_LEDGER_PATH", ledger)
    today = dt.date.today().isoformat()
    cid = "cafe1234-0000-0000-0000-000000000002"
    # Seed legacy DB-sourced entries for this conversation (old steps.metadata format).
    ledger.write_text(json.dumps({"trackingStarted": today, "entries": {
        f"{cid}:3": {"d": today, "u": 5, "c": 0, "o": 5, "t": 0, "x": 0, "me": 1133},
        f"{cid}:5": {"d": today, "u": 7, "c": 0, "o": 7, "t": 0, "x": 0, "me": 1133},
        "other:1": {"d": today, "u": 9, "c": 0, "o": 9, "t": 0, "x": 0, "me": 1016},
    }}))
    monkeypatch.setattr(agmod, "collect_antigravity_rpc_usage", _fake_rpc({
        cid: [{"stepKey": 4, "u": 1000, "c": 0, "o": 200,
               "model_placeholder": "MODEL_PLACEHOLDER_M26", "api_provider": "API_PROVIDER_ANTHROPIC_VERTEX", "date": today}]
    }))
    res = costmod.update_antigravity_token_ledger()
    keys = set(res["entries"])
    assert f"{cid}:3" not in keys and f"{cid}:5" not in keys   # legacy purged
    assert f"{cid}#4" in keys                                  # RPC entry added
    assert "other:1" in keys                                   # unrelated conversation untouched


def test_update_antigravity_token_ledger_pruning(monkeypatch, tmp_path):
    # Mock conversation dirs so no DB files are scanned
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CONVERSATION_DIRS", ())
    ledger_path = tmp_path / "ide_ledger.json"
    monkeypatch.setattr(costmod, "ANTIGRAVITY_LEDGER_PATH", ledger_path)
    
    # Save a ledger with some old entries and some recent entries
    current_date = dt.date.today()
    old_date = (current_date - dt.timedelta(days=36)).isoformat()
    recent_date = (current_date - dt.timedelta(days=34)).isoformat()
    
    ledger_data = {
        "trackingStarted": old_date,
        "entries": {
            "old:1": {"d": old_date, "u": 100, "c": 0, "o": 50, "t": 0, "x": 0},
            "recent:1": {"d": recent_date, "u": 200, "c": 0, "o": 100, "t": 0, "x": 0}
        }
    }
    ledger_path.write_text(json.dumps(ledger_data))
    
    # Run the ledger update (or ledger_cost_summary which triggers update)
    res = costmod.update_antigravity_token_ledger()
    
    # Verify old entry is pruned, recent entry is kept
    assert "old:1" not in res["entries"]
    assert "recent:1" in res["entries"]
    
    # Verify file is updated on disk
    saved_data = json.loads(ledger_path.read_text())
    assert "old:1" not in saved_data["entries"]
    assert "recent:1" in saved_data["entries"]


def test_update_antigravity_token_ledger_multi_generation_aggregation(monkeypatch, tmp_path):
    from unittest.mock import patch, MagicMock
    mock_found = [
        {2: 100, 5: 10, 3: 50, 9: 5, 10: 2, 1: 1016},
        {2: 200, 5: 20, 3: 100, 9: 10, 10: 4, 1: 1016}
    ]
    with patch("providers.cost._pb_find_usage", return_value=mock_found), \
         patch("providers.cost.sqlite3.connect") as mock_connect, \
         patch("providers.cost._load_antigravity_ledger", return_value={"trackingStarted": None, "entries": {}}), \
         patch("providers.cost._save_antigravity_ledger") as mock_save:
        
        mock_con = MagicMock()
        
        def mock_execute(query, *args):
            cursor = MagicMock()
            if "PRAGMA" in query:
                cursor.__iter__.return_value = []
                cursor.fetchall.return_value = []
            elif "SELECT idx FROM steps" in query:
                cursor.__iter__.return_value = [(1,)]
                cursor.fetchall.return_value = [(1,)]
            elif "SELECT idx, metadata" in query:
                cursor.__iter__.return_value = [(1, b"mockblob")]
                cursor.fetchall.return_value = [(1, b"mockblob")]
            return cursor
            
        mock_con.execute.side_effect = mock_execute
        mock_connect.return_value = mock_con
        
        # Mock ANTIGRAVITY_CONVERSATION_DIRS to have at least one directory with a DB file
        db_dir = tmp_path / "convs"
        db_dir.mkdir()
        (db_dir / "test.db").write_text("")
        monkeypatch.setattr(costmod, "ANTIGRAVITY_CONVERSATION_DIRS", (db_dir,))
        
        res = costmod.update_antigravity_token_ledger()
        
        # Verify the sum of the elements in the entries
        entry = res["entries"]["test:1"]
        assert entry["u"] == 300 # 100 + 200
        assert entry["c"] == 30 # 10 + 20
        assert entry["o"] == 150 # 50 + 100
        assert entry["t"] == 15 # 5 + 10
        assert entry["x"] == 6 # 2 + 4
        assert entry["me"] == 1016

        mock_save.assert_called_once()


# --- field6==24 usage blob bytes (mirrors test_pb_find_usage_is_model_agnostic) -----------------
# field1=1133 (Flash enum), field2=100 (input), field3=50 (output), field6=24 (usage marker).
_USAGE_BLOB_F6_24 = bytes([0x08, 0xED, 0x08, 0x10, 0x64, 0x18, 0x32, 0x30, 0x18])


def _make_steps_db(path, indices, blob=_USAGE_BLOB_F6_24):
    """Write a REAL plaintext SQLite trajectory DB with a steps(idx, metadata) table.

    Each given index gets a row holding a genuine field6==24 usage blob, so the scan
    path runs end-to-end against real sqlite3 + real _pb_find_usage (no mocks)."""
    import sqlite3 as _sql
    con = _sql.connect(str(path))
    con.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, metadata BLOB)")
    con.executemany("INSERT INTO steps (idx, metadata) VALUES (?, ?)",
                    [(i, blob) for i in indices])
    con.commit()
    con.close()


def test_real_temp_db_two_phase_scan_short_circuits(monkeypatch, tmp_path):
    """A real on-disk steps DB is parsed exactly once: the first update captures the
    usage entry, and a SECOND update must NOT re-run _pb_find_usage on the already-seen blob
    (the `key in entries` short-circuit that keeps the SQLite read lock window minimal and avoids
    re-parsing every step's protobuf on every widget poll)."""
    from providers import cost as cm
    conv_dir = tmp_path / "convs"
    conv_dir.mkdir()
    _make_steps_db(conv_dir / "mydb.db", [0])
    ledger = tmp_path / "ledger.json"
    monkeypatch.setattr(cm, "ANTIGRAVITY_CONVERSATION_DIRS", (conv_dir,))
    monkeypatch.setattr(cm, "ANTIGRAVITY_LEDGER_PATH", ledger)
    monkeypatch.setattr(cm, "ANTIGRAVITY_CLI_USAGE_PATH", tmp_path / "cli.json")
    # RPC already empty via the autouse fixture; keep it explicit for clarity.
    monkeypatch.setattr(agmod, "collect_antigravity_rpc_usage", lambda *a, **k: ({}, {}))

    # Spy on _pb_find_usage WITHOUT replacing its behaviour (first scan still really parses).
    real_pb = cm._pb_find_usage
    calls = {"n": 0}

    def spy(buf, *a, **k):
        calls["n"] += 1
        return real_pb(buf, *a, **k)
    monkeypatch.setattr(cm, "_pb_find_usage", spy)

    res1 = costmod.update_antigravity_token_ledger()
    assert "mydb:0" in res1["entries"]             # first scan captured the real blob
    assert calls["n"] == 1                          # parsed exactly once
    entry = res1["entries"]["mydb:0"]
    assert (entry["u"], entry["o"], entry["me"]) == (100, 50, 1133)

    seen = calls["n"]
    res2 = costmod.update_antigravity_token_ledger()
    assert "mydb:0" in res2["entries"]
    assert calls["n"] == seen                        # already-seen short-circuit: NO re-parse


def test_deadline_break_abandons_scan_early(monkeypatch, tmp_path):
    """A deadline already in the past makes update_antigravity_token_ledger abandon the
    on-disk scan before capturing anything (the OUTER conversation-dir guard breaks immediately),
    returning cleanly with no exception — so a huge conversation set can't hard-hang the snapshot."""
    import time
    from providers import cost as cm
    conv_dir = tmp_path / "convs"
    conv_dir.mkdir()
    _make_steps_db(conv_dir / "mydb.db", list(range(10)))  # several unseen indices
    ledger = tmp_path / "ledger.json"
    monkeypatch.setattr(cm, "ANTIGRAVITY_CONVERSATION_DIRS", (conv_dir,))
    monkeypatch.setattr(cm, "ANTIGRAVITY_LEDGER_PATH", ledger)
    monkeypatch.setattr(cm, "ANTIGRAVITY_CLI_USAGE_PATH", tmp_path / "cli.json")
    monkeypatch.setattr(agmod, "collect_antigravity_rpc_usage", lambda *a, **k: ({}, {}))

    res = costmod.update_antigravity_token_ledger(deadline=time.time() - 100)
    # The scan never ran (deadline spent at the top of the dir loop) -> nothing captured.
    assert res["entries"] == {}


def test_deadline_break_skips_remaining_db_connects(monkeypatch, tmp_path):
    """Pins the INNER per-DB pre-connect guard specifically: once the deadline trips
    mid-scan, the remaining DBs must be abandoned WITHOUT paying a connect + index query each.

    The previous test uses an already-expired deadline, so the outer conv-dir guard fires first and
    it would still pass even if the per-DB guard were deleted. Here the clock is still BEFORE the
    deadline when the conv-dir loop is entered (outer guard passes), then a connect-spy advances it
    PAST the deadline the moment the first DB opens — so the inner guard must skip the second DB.
    If the per-DB guard is removed, the second DB gets connected and this fails."""
    import time
    import sqlite3
    from providers import cost as cm
    conv_dir = tmp_path / "convs"
    conv_dir.mkdir()
    _make_steps_db(conv_dir / "db1.db", list(range(5)))
    _make_steps_db(conv_dir / "db2.db", list(range(5)))
    monkeypatch.setattr(cm, "ANTIGRAVITY_CONVERSATION_DIRS", (conv_dir,))
    monkeypatch.setattr(cm, "ANTIGRAVITY_LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(cm, "ANTIGRAVITY_CLI_USAGE_PATH", tmp_path / "cli.json")
    monkeypatch.setattr(agmod, "collect_antigravity_rpc_usage", lambda *a, **k: ({}, {}))

    base = 1_000_000.0
    clock = {"t": base}
    deadline = base + 5.0  # still in the future when the conv-dir loop is entered
    # update_antigravity_token_ledger does `import time` then calls time.time(); patch the module fn.
    monkeypatch.setattr(time, "time", lambda: clock["t"])

    real_connect = sqlite3.connect
    connected_dbs = set()

    def spy_connect(dsn, *a, **k):
        connected_dbs.add(str(dsn).split("file:", 1)[-1].split("?", 1)[0])
        clock["t"] = base + 100.0  # first DB opened -> spend the budget so the per-DB guard trips next
        return real_connect(dsn, *a, **k)

    monkeypatch.setattr(sqlite3, "connect", spy_connect)

    costmod.update_antigravity_token_ledger(deadline=deadline)
    # Exactly one DB was opened; the per-DB guard skipped the other before connecting it.
    assert len(connected_dbs) == 1


def test_hourly_residual_fold(monkeypatch, tmp_path):
    """hourlyTokenUsage buckets today's usage by local hour: an entry carrying a valid
    "h" lands in that hour, while a today entry with NO "h" (disk first-seen / CLI / pre-hour RPC)
    is folded into the CURRENT local hour — so the Day bars always sum to the Today total."""
    from providers import cost as cm
    monkeypatch.setattr(cm, "ANTIGRAVITY_CONVERSATION_DIRS", ())
    monkeypatch.setattr(cm, "ANTIGRAVITY_CLI_USAGE_PATH", tmp_path / "cli.json")
    ledger = tmp_path / "ledger.json"
    monkeypatch.setattr(cm, "ANTIGRAVITY_LEDGER_PATH", ledger)
    monkeypatch.setattr(agmod, "collect_antigravity_rpc_usage", lambda *a, **k: ({}, {}))
    monkeypatch.setattr(costmod, "model_pricing",
                        lambda *a, **k: {"input": 10.0, "output": 10.0, "cache_read": 10.0})

    today = dt.date.today().isoformat()
    hnow = dt.datetime.now().astimezone().hour
    hfixed = 3 if hnow != 3 else 4            # a fixed hour distinct from "now"
    ledger.write_text(json.dumps({"trackingStarted": today, "entries": {
        # h-bearing: tokens = u(1000) + c(0) + o(200) = 1200, stamped to hfixed
        "x#0": {"d": today, "u": 1000, "c": 0, "o": 200, "model": "Gemini 3.5 Flash (High)", "h": hfixed},
        # hour-LESS: tokens = u(500) + c(0) + o(100) = 600, must land in the current hour
        "x#1": {"d": today, "u": 500, "c": 0, "o": 100, "model": "Gemini 3.5 Flash (High)"},
    }}))

    summary = costmod.antigravity_ledger_cost_summary()
    by_hour = {b["hour"]: b for b in summary["hourlyTokenUsage"]}
    assert by_hour[hfixed]["tokens"] == 1200            # h-bearing tokens landed in their hour
    assert by_hour[hnow]["tokens"] == 600               # hour-less residual folded into "now"
    today_total = 1200 + 600
    assert sum(b["tokens"] for b in summary["hourlyTokenUsage"]) == today_total  # Day sums to Today
    # Per-bucket model attribution rides along — including through the hour-less fold.
    # Display names are FAMILY-grouped ("(High)" collapses away — _model_family).
    assert by_hour[hfixed]["models"][0]["model"] == "Gemini 3.5 Flash"
    assert by_hour[hfixed]["models"][0]["tokens"] == 1200
    assert by_hour[hnow]["models"][0]["tokens"] == 600
    day_bucket = next(b for b in summary["weeklyTokenUsage"] if b["date"] == today)
    assert day_bucket["models"][0] == {"model": "Gemini 3.5 Flash",
                                       "cost": day_bucket["cost"], "tokens": today_total}


def test_enum_map_heals_fallback_model_retroactively(monkeypatch, tmp_path):
    """An RPC entry captured while its placeholder was UNKNOWN stores the family-fallback
    model string (e.g. gemini-3-pro) plus the raw enum in "me". Once the enum is mapped
    in _MODEL_ENUM_NAMES, the summary re-attributes the entry WITHOUT a re-query — the
    enum map outranks the stored string (real case: M132 = Gemini 3.5 Flash, 2026-06-09)."""
    from providers import cost as cm
    monkeypatch.setattr(cm, "ANTIGRAVITY_CONVERSATION_DIRS", ())
    monkeypatch.setattr(cm, "ANTIGRAVITY_CLI_USAGE_PATH", tmp_path / "cli.json")
    ledger = tmp_path / "ledger.json"
    monkeypatch.setattr(cm, "ANTIGRAVITY_LEDGER_PATH", ledger)
    monkeypatch.setattr(agmod, "collect_antigravity_rpc_usage", lambda *a, **k: ({}, {}))

    today = dt.date.today().isoformat()
    ledger.write_text(json.dumps({"trackingStarted": today, "entries": {
        # Stale fallback attribution, raw enum preserved: must read as 3.5 Flash now.
        "x#0": {"d": today, "u": 1000, "c": 0, "o": 200, "model": "gemini-3-pro", "me": 1132},
        # CLI display-name entry (no "me"): untouched by the enum-first order.
        "y#0": {"d": today, "u": 500, "c": 0, "o": 100, "model": "Gemini 3.1 Pro (High)"},
    }}))

    summary = costmod.antigravity_ledger_cost_summary()
    names = {r["model"] for r in summary["modelBreakdown"]}
    # Breakdown rows are family-grouped: enum 1132 reads "Gemini 3.5 Flash" (healed
    # from the stale gemini-3-pro string), the CLI entry reads "Gemini 3.1 Pro".
    assert "Gemini 3.5 Flash" in names
    assert "Gemini 3.1 Pro" in names
    assert "gemini-3-pro" not in names


def test_model_family_grouping():
    """The breakdown collapses effort/thinking variants into display families:
    Gemini keeps version+tier, Claude and GPT are single buckets. Pricing is
    untouched (it resolves the exact name before grouping)."""
    fam = costmod._model_family
    assert fam("Gemini 3.1 Pro (High)") == "Gemini 3.1 Pro"
    assert fam("Gemini 3.1 Pro (Low)") == "Gemini 3.1 Pro"
    assert fam("Gemini 3.5 Flash (Medium)") == "Gemini 3.5 Flash"
    assert fam("Gemini 3.5 Flash") == "Gemini 3.5 Flash"
    assert fam("Gemini 3 Flash") == "Gemini 3 Flash"
    assert fam("Gemini 3.1 Flash Lite") == "Gemini 3.1 Flash Lite"
    # Hyphenated pricing-key fallbacks read like the picker family
    assert fam("gemini-3.1-pro") == "Gemini 3.1 Pro"
    assert fam("gemini-3.5-flash") == "Gemini 3.5 Flash"
    # Claude / GPT collapse to one bucket each, whatever the variant
    assert fam("Claude Opus 4.6 (Thinking)") == "Claude"
    assert fam("Claude Sonnet 4.6 (Thinking)") == "Claude"
    assert fam("claude-opus-4-6") == "Claude"
    assert fam("gpt-oss") == "GPT"
    assert fam("GPT-5.2 (High)") == "GPT"
    # Unknown / empty stay the QML "Other" sentinel
    assert fam("Unknown") == "Unknown"
    assert fam("") == "Unknown"
    assert fam(None) == "Unknown"


def test_legacy_model_strings_migrated_on_update(monkeypatch, tmp_path):
    """Pre-2026-06-09 ledgers stored normalized pricing keys ("gemini-3-pro" was a mislabel
    of M16 = Gemini 3.1 Pro (High)). update_antigravity_token_ledger normalizes them to the
    exact picker names under its flock, so old ledgers / .bak restores converge safely."""
    from providers import cost as cm
    monkeypatch.setattr(cm, "ANTIGRAVITY_CONVERSATION_DIRS", ())
    monkeypatch.setattr(cm, "ANTIGRAVITY_CLI_USAGE_PATH", tmp_path / "cli.json")
    ledger = tmp_path / "ledger.json"
    monkeypatch.setattr(cm, "ANTIGRAVITY_LEDGER_PATH", ledger)
    monkeypatch.setattr(agmod, "collect_antigravity_rpc_usage", lambda *a, **k: ({}, {}))

    today = dt.date.today().isoformat()
    ledger.write_text(json.dumps({"trackingStarted": today, "entries": {
        "x#0": {"d": today, "u": 10, "c": 0, "o": 5, "model": "gemini-3-pro"},
        "x#1": {"d": today, "u": 10, "c": 0, "o": 5, "model": "claude-opus-4-6"},
        "x#2": {"d": today, "u": 10, "c": 0, "o": 5, "model": "Gemini 3.5 Flash (High)"},
    }}))

    res = costmod.update_antigravity_token_ledger()
    assert res["entries"]["x#0"]["model"] == "Gemini 3.1 Pro (High)"
    assert res["entries"]["x#1"]["model"] == "Claude Opus 4.6 (Thinking)"
    assert res["entries"]["x#2"]["model"] == "Gemini 3.5 Flash (High)"  # already exact — untouched
    # The migration persisted (survives a reload), not just the returned dict.
    assert json.loads(ledger.read_text())["entries"]["x#0"]["model"] == "Gemini 3.1 Pro (High)"


def test_pb_find_usage_accepts_both_markers_rejects_missing_input(monkeypatch):
    """The disk gate accepts BOTH usage markers — field6==24 (pre-2.0) and field6==26 (the
    Antigravity-2.0 marker flip) — provided token field 2 is present. Gating on 24 alone silently
    dropped every 2.0-format generation (measured ~45-80% of IDE usage). A field6==24 record MISSING
    token field 2 is still rejected so non-usage submessages don't slip into the ledger as garbage."""
    # field1=1133, field2=100, field3=50, field6=24 (pre-2.0 marker) -> accepted.
    found_v1 = costmod._pb_find_usage(_USAGE_BLOB_F6_24)
    assert len(found_v1) == 1 and found_v1[0].get(2) == 100
    # Otherwise-identical but the field6 value byte 0x18(24) -> 0x1A(26): the 2.0 marker -> accepted.
    v2_blob = bytes([0x08, 0xED, 0x08, 0x10, 0x64, 0x18, 0x32, 0x30, 0x1A])
    found_v2 = costmod._pb_find_usage(v2_blob)
    assert len(found_v2) == 1 and found_v2[0].get(6) == 26 and found_v2[0].get(2) == 100
    # field6==24 but field 2 (input tokens) dropped -> `2 in varints` fails the gate.
    no_input_blob = bytes([0x08, 0xED, 0x08, 0x18, 0x32, 0x30, 0x18])
    assert costmod._pb_find_usage(no_input_blob) == []


def test_rpc_local_hour(monkeypatch):
    """_rpc_local_hour mirrors _rpc_local_date: empty/garbage -> None; a nanosecond UTC
    createdAt yields an int hour in 0..23; localisation IS applied (not naive UTC-hour slicing) —
    a just-past-midnight UTC instant must read as its local hour, not 0."""
    assert agmod._rpc_local_hour("") is None
    assert agmod._rpc_local_hour("not-a-date") is None

    h = agmod._rpc_local_hour("2026-05-28T12:00:00.137443809Z")
    assert isinstance(h, int) and 0 <= h <= 23          # ns precision parsed without crashing

    # The expected hour computed the same way the impl does (ns->us trim, UTC->local). If the impl
    # naively sliced UTC it would read 0 for this 00:30Z stamp; assert it matches the LOCAL hour.
    import re as _re
    iso = "2026-05-30T00:30:48.001487532Z"
    norm = _re.sub(r"(\.\d{6})\d+", r"\1", iso[:-1] + "+00:00")
    expected = dt.datetime.fromisoformat(norm).astimezone().hour
    assert agmod._rpc_local_hour(iso) == expected
    # Sanity: in any tz with a non-zero UTC offset this differs from the naive UTC hour (0),
    # proving localisation. (Skip the strict inequality only in the pathological UTC case.)
    if dt.datetime.now(dt.timezone.utc).astimezone().utcoffset() != dt.timedelta(0):
        assert agmod._rpc_local_hour(iso) != 0


def test_cli_statusline_superseded_by_ledger_coverage(monkeypatch, tmp_path):
    """A statusLine 'cli:<id>' entry is the session's CUMULATIVE total. Once the ledger
    covers the same session per-generation (trajectory DB scanned -> '<id>:<n>' keys, or
    live agy RPC -> '<id>#<n>'), the summary merge must drop the cli: entry or the whole
    session counts twice. Sessions without ledger coverage (the encrypted pre-2026-06-02
    .pb era) keep their statusLine entry — it is the only record of them."""
    from providers import cost as cm
    monkeypatch.setattr(cm, "ANTIGRAVITY_CONVERSATION_DIRS", ())
    cli_file = tmp_path / "cli.json"
    monkeypatch.setattr(cm, "ANTIGRAVITY_CLI_USAGE_PATH", cli_file)
    ledger = tmp_path / "ledger.json"
    monkeypatch.setattr(cm, "ANTIGRAVITY_LEDGER_PATH", ledger)

    today = dt.date.today().isoformat()
    ledger.write_text(json.dumps({"trackingStarted": today, "entries": {
        "S:0": {"d": today, "u": 100, "c": 0, "o": 50, "model": "Gemini 3.5 Flash"},
    }}))
    cli_file.write_text(json.dumps({"trackingStarted": today, "entries": {
        # Same session as the ledger's S:0 -> must be dropped (else double-count)
        "cli:S": {"d": today, "u": 999, "c": 0, "o": 999, "model": "Gemini 3.5 Flash (High)"},
        # No ledger coverage -> kept
        "cli:T": {"d": today, "u": 10, "c": 0, "o": 5, "model": "Gemini 3.1 Pro (High)"},
    }}))

    summary = costmod.antigravity_ledger_cost_summary()
    assert summary["tokensToday"] == (100 + 50) + (10 + 5)
    names = {r["model"] for r in summary["modelBreakdown"]}
    assert names == {"Gemini 3.5 Flash", "Gemini 3.1 Pro"}


def test_agy_cmdline_discovered_tokenless():
    # The agy CLI serves the language-server RPC in-process with NO CSRF token; its
    # cmdline is just "agy" (which the language-server gate rejects).
    assert agmod._parse_agy_cmdline(["agy"]) == ("", "http")
    assert agmod._parse_agy_cmdline(["/usr/local/bin/agy", "--continue"]) == ("", "http")
    assert agmod._parse_agy_cmdline(["agy-helper"]) is None
    assert agmod._parse_agy_cmdline([]) is None
    assert agmod._parse_language_server_cmdline("agy", ["agy"]) is None


def test_local_rpc_csrf_header_omitted_when_tokenless(monkeypatch):
    """An empty token means 'agy in-process server' — the CSRF header must be omitted
    (sending an empty value is what we're avoiding); a real token must still be sent."""
    seen = {}

    class _Resp:
        status = 200
        def read(self, n=-1):
            return b"{}"
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None, context=None):
        seen["req"] = req
        return _Resp()

    monkeypatch.setattr(agmod.urllib.request, "urlopen", fake_urlopen)
    # urllib capitalizes stored header names: "X-Codeium-Csrf-Token" -> "X-codeium-csrf-token"
    agmod._post_local_rpc("http://127.0.0.1:1/x", "", {}, 1.0)
    assert not seen["req"].has_header("X-codeium-csrf-token")
    agmod._post_local_rpc("http://127.0.0.1:1/x", "tok", {}, 1.0)
    assert seen["req"].get_header("X-codeium-csrf-token") == "tok"
    agmod.post_local_json("http://127.0.0.1:1/x", "", 1.0)
    assert not seen["req"].has_header("X-codeium-csrf-token")
    agmod.post_local_json("http://127.0.0.1:1/x", "tok2", 1.0)
    assert seen["req"].get_header("X-codeium-csrf-token") == "tok2"


def test_heal_never_downgrades_stored_name_when_lookup_fails(monkeypatch, tmp_path):
    """Re-queried cascade with an UNMAPPED enum while GetAvailableModels failed this
    refresh (model_display=""): the previously-captured exact picker name must survive
    — never downgrade to the family fallback. A MISSING stored name is still filled."""
    from providers import cost as cm
    monkeypatch.setattr(cm, "ANTIGRAVITY_CONVERSATION_DIRS", ())
    monkeypatch.setattr(cm, "ANTIGRAVITY_CLI_USAGE_PATH", tmp_path / "cli.json")
    ledger = tmp_path / "ledger.json"
    monkeypatch.setattr(cm, "ANTIGRAVITY_LEDGER_PATH", ledger)

    today = dt.date.today().isoformat()
    cid = "c1"
    ledger.write_text(json.dumps({"trackingStarted": today, "entries": {
        f"{cid}#0": {"d": today, "u": 10, "c": 0, "o": 5,
                     "model": "Gemini 4 Ultra (High)", "me": 1999},
        f"{cid}#1": {"d": today, "u": 10, "c": 0, "o": 5, "me": 1999},  # no stored name
    }}))

    def rec(step):
        return {"stepKey": step, "u": 10, "c": 0, "o": 5,
                "model_placeholder": "MODEL_PLACEHOLDER_M999",
                "api_provider": "API_PROVIDER_GOOGLE_GEMINI",
                "model_display": "",   # lookup failed / id retired from the picker
                "date": today, "hour": None}

    monkeypatch.setattr(agmod, "collect_antigravity_rpc_usage",
                        lambda *a, **k: ({cid: [rec("0"), rec("1")]}, {cid: "lmt"}))
    res = costmod.update_antigravity_token_ledger()
    assert res["entries"][f"{cid}#0"]["model"] == "Gemini 4 Ultra (High)"  # preserved
    assert res["entries"][f"{cid}#1"]["model"] == "gemini-3.1-pro"         # filled


def test_ledger_family_resolution_is_memoized(monkeypatch, tmp_path):
    """antigravity_ledger_cost_summary resolves each distinct (me, model) pair's display
    family ONCE, not once per entry — there are only a handful of pairs across tens of
    thousands of entries. Guards the per-call memo against a regression to per-entry work."""
    from providers import cost as cm
    monkeypatch.setattr(cm, "ANTIGRAVITY_CONVERSATION_DIRS", ())
    monkeypatch.setattr(cm, "ANTIGRAVITY_CLI_USAGE_PATH", tmp_path / "cli.json")
    ledger = tmp_path / "ledger.json"
    monkeypatch.setattr(cm, "ANTIGRAVITY_LEDGER_PATH", ledger)

    today = dt.date.today().isoformat()
    # 3 distinct model enums, 20 entries each = 60 entries but only 3 (me, model) pairs.
    entries = {}
    for enum in (1016, 1026, 1132):
        for i in range(20):
            entries[f"stem{enum}:{i}"] = {"d": today, "me": enum, "u": 1000, "c": 0, "o": 500}
    ledger.write_text(json.dumps({"trackingStarted": today, "entries": entries}))

    calls = []
    real_resolve = cm._resolve_model_display

    def counting(e, learned=None):
        calls.append((e.get("me"), e.get("model")))
        return real_resolve(e, learned=learned)

    monkeypatch.setattr(cm, "_resolve_model_display", counting)
    summary = costmod.antigravity_ledger_cost_summary()
    assert summary is not None
    # Resolver invoked exactly once per distinct pair, not once per entry.
    assert len(calls) == 3
    assert len(set(calls)) == 3


# ---------------------------------------------------------------------------
# gen_metadata protobuf encoder + embedded-timestamp disk scan (main-tree unique)
# ---------------------------------------------------------------------------

def _pb_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _pb_tag(field: int, wt: int) -> bytes:
    return _pb_varint((field << 3) | wt)


def _pb_vfield(field: int, value: int) -> bytes:
    return _pb_tag(field, 0) + _pb_varint(value)


def _pb_lfield(field: int, payload: bytes) -> bytes:
    return _pb_tag(field, 2) + _pb_varint(len(payload)) + payload


def _encode_generation(u, c, o, me, secs, marker=24):
    """A single generation: field4=usage (field6 in {24,26} gated), field9.field4=Timestamp(field1=secs).

    ``marker`` selects the usage-record marker — 24 (pre-2.0) or 26 (Antigravity-2.0 flip); both
    must be captured. Also embeds the DUPLICATE usage record at field17.2 (byte-identical to field4)
    — the impl must read field4 ONLY and never sum both, exactly as the real DBs store it."""
    usage = (_pb_vfield(1, me) + _pb_vfield(2, u) + _pb_vfield(3, o)
             + _pb_vfield(5, c) + _pb_vfield(6, marker))
    timestamp = _pb_lfield(4, _pb_vfield(1, secs))  # field9.field4 = google.protobuf.Timestamp
    f17 = _pb_lfield(2, usage)  # the duplicate copy under field17.2
    return (_pb_lfield(4, usage) + _pb_lfield(9, timestamp) + _pb_lfield(17, f17))


def _encode_gen_metadata_blob(generations):
    """root wrapping one-or-more generations under field 1."""
    return b"".join(_pb_lfield(1, _encode_generation(**g)) for g in generations)


def _make_gen_metadata_db(path, blobs):
    """Write a REAL gen_metadata(idx INTEGER PRIMARY KEY, data BLOB) SQLite DB."""
    import sqlite3 as _sql
    con = _sql.connect(str(path))
    con.execute("DROP TABLE IF EXISTS gen_metadata")
    con.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB)")
    con.executemany("INSERT INTO gen_metadata (idx, data) VALUES (?, ?)",
                    list(enumerate(blobs)))
    con.commit()
    con.close()


def test_pb_generations_counts_each_generation_once_dated_by_timestamp():
    """A gen_metadata blob with two generations decodes to two records, each dated by its OWN
    embedded timestamp — NOT doubled (the field17.2 duplicate must be ignored)."""
    secs_a = int(dt.datetime(2026, 6, 1, 10, 0, 0).timestamp())
    secs_b = int(dt.datetime(2026, 6, 2, 11, 0, 0).timestamp())
    blob = _encode_gen_metadata_blob([
        {"u": 100, "c": 10, "o": 50, "me": 1133, "secs": secs_a},
        {"u": 200, "c": 20, "o": 80, "me": 1016, "secs": secs_b},
    ])
    gens = costmod._pb_generations(blob)
    assert len(gens) == 2  # each counted ONCE, not 4 (the field17.2 duplicate is ignored)
    assert gens[0] == {"u": 100, "c": 10, "o": 50, "me": 1133, "secs": secs_a}
    assert gens[1] == {"u": 200, "c": 20, "o": 80, "me": 1016, "secs": secs_b}


def test_disk_scan_dates_by_embedded_timestamp_not_today(monkeypatch, tmp_path):
    """A gen_metadata generation is dated by its EMBEDDED timestamp, never 'today'."""
    from providers import cost as cm
    conv_dir = tmp_path / "convs"
    conv_dir.mkdir()
    old = dt.date.today() - dt.timedelta(days=5)
    secs = int(dt.datetime(old.year, old.month, old.day, 9, 0, 0).timestamp())
    _make_gen_metadata_db(conv_dir / "mydb.db",
                          [_encode_gen_metadata_blob([{"u": 100, "c": 0, "o": 50, "me": 1133, "secs": secs}])])
    monkeypatch.setattr(cm, "ANTIGRAVITY_CONVERSATION_DIRS", (conv_dir,))
    monkeypatch.setattr(cm, "ANTIGRAVITY_LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(cm, "ANTIGRAVITY_CLI_USAGE_PATH", tmp_path / "cli.json")
    monkeypatch.setattr(agmod, "collect_antigravity_rpc_usage", lambda *a, **k: ({}, {}))

    res = costmod.update_antigravity_token_ledger()
    key = "mydb@0.0"
    assert key in res["entries"]
    e = res["entries"][key]
    assert e["d"] == old.isoformat()
    assert (e["u"], e["c"], e["o"], e["me"]) == (100, 0, 50, 1133)


def test_disk_scan_counts_generation_once_not_doubled(monkeypatch, tmp_path):
    """End-to-end: a real gen_metadata DB with one generation produces exactly one ledger entry
    with the un-doubled tokens (the duplicate field17.2 usage record must not be summed)."""
    from providers import cost as cm
    conv_dir = tmp_path / "convs"
    conv_dir.mkdir()
    secs = int(dt.datetime.now().timestamp())
    _make_gen_metadata_db(conv_dir / "c.db",
                          [_encode_gen_metadata_blob([{"u": 1000, "c": 0, "o": 300, "me": 1133, "secs": secs}])])
    monkeypatch.setattr(cm, "ANTIGRAVITY_CONVERSATION_DIRS", (conv_dir,))
    monkeypatch.setattr(cm, "ANTIGRAVITY_LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(cm, "ANTIGRAVITY_CLI_USAGE_PATH", tmp_path / "cli.json")
    monkeypatch.setattr(agmod, "collect_antigravity_rpc_usage", lambda *a, **k: ({}, {}))

    res = costmod.update_antigravity_token_ledger()
    conv_keys = [k for k in res["entries"] if k.startswith("c@")]
    assert conv_keys == ["c@0.0"]
    assert res["entries"]["c@0.0"]["u"] == 1000
    assert res["entries"]["c@0.0"]["o"] == 300


def test_disk_scan_rescans_changed_db_skips_unchanged(monkeypatch, tmp_path):
    """A DB whose mtime changed is re-scanned; an unchanged DB is skipped."""
    import os
    import time as _time
    from providers import cost as cm
    conv_dir = tmp_path / "convs"
    conv_dir.mkdir()
    db = conv_dir / "grow.db"
    secs0 = int(dt.datetime.now().timestamp())
    _make_gen_metadata_db(db, [_encode_gen_metadata_blob([{"u": 100, "c": 0, "o": 50, "me": 1133, "secs": secs0}])])
    # Use a fixed mtime within the 35-day scan window (2 days ago) so the DB is not treated
    # as a stale-backlog and skipped.  The exact value is stable across run-1 and run-2 so the
    # content signature matches and run-2 is memoised (the skip-on-unchanged contract).
    mtime_stable = _time.time() - 2 * 86400
    mtime_changed = _time.time() - 1 * 86400
    os.utime(db, (mtime_stable, mtime_stable))
    monkeypatch.setattr(cm, "ANTIGRAVITY_CONVERSATION_DIRS", (conv_dir,))
    ledger = tmp_path / "ledger.json"
    monkeypatch.setattr(cm, "ANTIGRAVITY_LEDGER_PATH", ledger)
    monkeypatch.setattr(cm, "ANTIGRAVITY_CLI_USAGE_PATH", tmp_path / "cli.json")
    monkeypatch.setattr(agmod, "collect_antigravity_rpc_usage", lambda *a, **k: ({}, {}))

    real_pb = cm._pb_generations
    calls = {"n": 0}

    def spy(buf):
        calls["n"] += 1
        return real_pb(buf)
    monkeypatch.setattr(cm, "_pb_generations", spy)

    res1 = costmod.update_antigravity_token_ledger()
    assert "grow@0.0" in res1["entries"]
    first = calls["n"]
    assert first == 1

    res2 = costmod.update_antigravity_token_ledger()
    assert calls["n"] == first
    assert "grow@0.0" in res2["entries"]

    secs1 = int(dt.datetime.now().timestamp())
    _make_gen_metadata_db(db, [
        _encode_gen_metadata_blob([{"u": 100, "c": 0, "o": 50, "me": 1133, "secs": secs0}]),
        _encode_gen_metadata_blob([{"u": 999, "c": 0, "o": 5, "me": 1016, "secs": secs1}]),
    ])
    os.utime(db, (mtime_changed, mtime_changed))
    res3 = costmod.update_antigravity_token_ledger()
    assert calls["n"] > first
    assert "grow@0.0" in res3["entries"] and "grow@1.0" in res3["entries"]
    assert res3["entries"]["grow@1.0"]["u"] == 999


def test_disk_dating_migration_resets_db_scanned(monkeypatch, tmp_path):
    """A ledger missing diskDatingVersion has its dbScanned cache cleared, triggering re-scan."""
    from providers import cost as cm
    import os, time as _time
    conv_dir = tmp_path / "convs"
    conv_dir.mkdir()
    secs = int(dt.datetime.now().timestamp())
    db = conv_dir / "mig.db"
    _make_gen_metadata_db(db, [_encode_gen_metadata_blob([{"u": 7, "c": 0, "o": 3, "me": 1133, "secs": secs}])])
    # Set mtime within the 35-day scan window so the DB is not treated as a stale-backlog.
    # The pre-seeded dbScanned value uses a different format so the sig never matches and the
    # DB is always re-scanned; the migration additionally clears the memo map.
    recent_mtime = _time.time() - 3 * 86400
    os.utime(db, (recent_mtime, recent_mtime))
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({
        "trackingStarted": dt.date.today().isoformat(),
        "entries": {},
        "dbScanned": {"mig": "stale-sig-placeholder"},
    }))
    monkeypatch.setattr(cm, "ANTIGRAVITY_CONVERSATION_DIRS", (conv_dir,))
    monkeypatch.setattr(cm, "ANTIGRAVITY_LEDGER_PATH", ledger)
    monkeypatch.setattr(cm, "ANTIGRAVITY_CLI_USAGE_PATH", tmp_path / "cli.json")
    monkeypatch.setattr(agmod, "collect_antigravity_rpc_usage", lambda *a, **k: ({}, {}))

    res = costmod.update_antigravity_token_ledger()
    assert res["diskDatingVersion"] == cm.ANTIGRAVITY_DISK_DATING_VERSION
    assert "mig@0.0" in res["entries"]


def test_pb_generations_captures_2_0_marker_26(tmp_path, monkeypatch):
    """End-to-end: a gen_metadata DB with Antigravity-2.0 usage marker (field6==26) is captured."""
    from providers import cost as cm
    secs = int(dt.datetime.now().timestamp())
    blob = _encode_gen_metadata_blob([
        {"u": 100, "c": 0, "o": 50, "me": 1133, "secs": secs, "marker": 24},
        {"u": 200, "c": 0, "o": 70, "me": 1026, "secs": secs, "marker": 26},
    ])
    gens = costmod._pb_generations(blob)
    assert len(gens) == 2
    assert {g["u"] for g in gens} == {100, 200}
    assert {g["o"] for g in gens} == {50, 70}

    conv_dir = tmp_path / "convs"
    conv_dir.mkdir()
    _make_gen_metadata_db(conv_dir / "conv26.db", [blob])
    monkeypatch.setattr(cm, "ANTIGRAVITY_CONVERSATION_DIRS", (conv_dir,))
    monkeypatch.setattr(cm, "ANTIGRAVITY_LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(cm, "ANTIGRAVITY_CLI_USAGE_PATH", tmp_path / "cli.json")
    monkeypatch.setattr(agmod, "collect_antigravity_rpc_usage", lambda *a, **k: ({}, {}))
    res = costmod.update_antigravity_token_ledger()
    folded = {k: v for k, v in res["entries"].items() if k.startswith("conv26@")}
    assert len(folded) == 2
    assert sum(v["u"] for v in folded.values()) == 300
    assert sum(v["o"] for v in folded.values()) == 120


def test_antigravity_summary_burn_rate_uses_unbiased_denominator(tmp_path, monkeypatch):
    """antigravity_ledger_cost_summary must use the same trailing_week_days()
    denominator as token_summary — not the old hard-coded 7.0."""
    import accounting
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CONVERSATION_DIRS", ())
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CLI_USAGE_PATH", tmp_path / "cli.json")
    ledger = tmp_path / "ledger.json"
    monkeypatch.setattr(costmod, "ANTIGRAVITY_LEDGER_PATH", ledger)
    # Write 7 daily entries with $7/day so week_cost == 49 — same fixture as the
    # token_summary burn-rate test.
    today = dt.date.today()
    entries = {}
    for i in range(7):
        d = (today - dt.timedelta(days=6 - i)).isoformat()
        entries[f"conv:{i}"] = {"d": d, "me": 1016, "u": 100, "c": 0, "o": 50, "t": 0, "x": 0, "#": True}
    monkeypatch.setattr(costmod, "model_pricing",
                        lambda *a, **k: {"input": 0.0, "output": 0.0, "cache_read": 0.0})

    # Build a ledger where each day has $7 of precomputed cost rather than raw tokens
    # (easier: patch trailing_week_days and verify it is consulted).
    called_with = []

    def fake_twd():
        v = accounting.trailing_week_days.__wrapped__() if hasattr(accounting.trailing_week_days, "__wrapped__") else accounting.trailing_week_days()
        called_with.append(v)
        return v

    monkeypatch.setattr(costmod, "trailing_week_days", fake_twd)
    ledger.write_text(json.dumps({"trackingStarted": today.isoformat(), "entries": entries}))
    s = costmod.antigravity_ledger_cost_summary()
    # The function must have called trailing_week_days (not a hard-coded 7.0).
    assert called_with, "trailing_week_days was never called — cost.py still uses hard-coded 7.0"
    # The reported burn rate must match week_cost / trailing_week_days().
    expected_denom = accounting.trailing_week_days()
    week_cost = s["cost7d"]
    assert s["burnRatePerDay"] == pytest.approx(week_cost / expected_denom, rel=1e-3)
    assert s["projectedMonthlyCost"] == pytest.approx(s["burnRatePerDay"] * 30.0, rel=1e-3)


# ---------------------------------------------------------------------------
# Task 1: Cache-read de-inflation (per-conversation max, not cumulative sum)
# ---------------------------------------------------------------------------

def _write_ledger(tmp_path, entries: dict) -> None:
    import json as _json
    ledger = tmp_path / "ledger.json"
    ledger.write_text(_json.dumps({
        "trackingStarted": dt.date.today().isoformat(),
        "entries": entries,
    }))


def _cost_summary(tmp_path, monkeypatch) -> dict:
    """Run antigravity_ledger_cost_summary with no live RPC and no conv dirs."""
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CONVERSATION_DIRS", ())
    monkeypatch.setattr(costmod, "ANTIGRAVITY_LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CLI_USAGE_PATH", tmp_path / "cli.json")
    # Use zero pricing so cost==0 and we can assert on token counts cleanly.
    monkeypatch.setattr(costmod, "model_pricing",
                        lambda *a, **k: {"input": 0.0, "output": 0.0, "cache_read": 0.0})
    return costmod.antigravity_ledger_cost_summary()


def test_cache_read_not_inflated_across_steps(tmp_path, monkeypatch):
    """A conversation with monotonically growing per-step cache reads must report
    the MAX single-step value (peak context size), NOT the sum across all steps.

    Scenario: 3-step conversation, steps read 10K/20K/30K cache tokens.
    Sum would be 60K; correct de-inflated total is 30K (the max/final step).
    """
    today = dt.date.today().isoformat()
    entries = {
        "conv:0": {"d": today, "me": 1016, "u": 500, "c": 10_000, "o": 100},
        "conv:1": {"d": today, "me": 1016, "u": 300, "c": 20_000, "o": 80},
        "conv:2": {"d": today, "me": 1016, "u": 400, "c": 30_000, "o": 90},
    }
    _write_ledger(tmp_path, entries)
    s = _cost_summary(tmp_path, monkeypatch)
    # tokensToday should be: u_total + max_c + o_total = (500+300+400) + 30000 + (100+80+90)
    expected_tok = (500 + 300 + 400) + 30_000 + (100 + 80 + 90)
    assert s["tokensToday"] == expected_tok, (
        f"Expected {expected_tok} (de-inflated max-c), got {s['tokensToday']} "
        f"(diff={s['tokensToday'] - expected_tok} would indicate sum-inflation)"
    )
    # Confirm the inflated (sum) value is distinct from the correct value, so the
    # assertion above is non-trivial.
    inflated_tok = (500 + 300 + 400) + 60_000 + (100 + 80 + 90)
    assert s["tokensToday"] != inflated_tok, "tokensToday equals inflated sum — de-inflation not applied"


def test_cache_read_single_step_unchanged(tmp_path, monkeypatch):
    """A single-entry conversation (one step) is unaffected — its c value is both
    max and sum, so the canonical key == the only key and eff_c == c."""
    today = dt.date.today().isoformat()
    entries = {"conv:0": {"d": today, "me": 1016, "u": 1000, "c": 65_000, "o": 200}}
    _write_ledger(tmp_path, entries)
    s = _cost_summary(tmp_path, monkeypatch)
    assert s["tokensToday"] == 1000 + 65_000 + 200


def test_cache_read_independent_conversations_unaffected(tmp_path, monkeypatch):
    """Two different conversations must NOT have their cache reads merged — each
    takes its own max independently."""
    today = dt.date.today().isoformat()
    entries = {
        # Conversation A: 2 steps, max c = 20K
        "convA:0": {"d": today, "me": 1016, "u": 100, "c": 10_000, "o": 50},
        "convA:1": {"d": today, "me": 1016, "u": 100, "c": 20_000, "o": 50},
        # Conversation B: 1 step, c = 5K
        "convB:0": {"d": today, "me": 1016, "u": 200, "c": 5_000, "o": 80},
    }
    _write_ledger(tmp_path, entries)
    s = _cost_summary(tmp_path, monkeypatch)
    # Expected: (100+100+200) + (20000+5000) + (50+50+80) = 400 + 25000 + 180 = 25580
    assert s["tokensToday"] == 400 + 25_000 + 180


def test_cache_read_rpc_entries_unaffected(tmp_path, monkeypatch):
    """RPC '#'-keyed entries are per-generation records; each is an independent
    API call with its own cache-read (NOT a cumulative re-read of growing context).
    De-inflation must NOT combine '#' entries across the same cascade — each is its
    own singleton stem and keeps its full 'c' value."""
    today = dt.date.today().isoformat()
    entries = {
        "conv#gen0": {"d": today, "me": 1026, "u": 1000, "c": 500, "o": 200},
        "conv#gen1": {"d": today, "me": 1026, "u": 800, "c": 400, "o": 150},
    }
    _write_ledger(tmp_path, entries)
    s = _cost_summary(tmp_path, monkeypatch)
    # Each '#' entry is its own singleton stem: both keep their full c values.
    # tokensToday = (1000+800) + (500+400) + (200+150) = 1800 + 900 + 350 = 3050
    assert s["tokensToday"] == 1800 + 900 + 350


def test_cache_read_cli_sessions_not_deflated_against_each_other(tmp_path, monkeypatch):
    """CLI statusLine entries (keyed 'cli:<sessionId>') are session-cumulative totals —
    each entry is already the single authoritative total for its session and must NOT be
    de-inflated against other CLI sessions sharing the 'cli:' prefix.

    Regression: _key_stem('cli:abc') previously split on ':' and returned 'cli', collapsing
    ALL CLI sessions into one stem; only the highest-c session's value survived and others
    were zeroed out — e.g. two sessions c=5000/c=3000 reported 5300 tokens instead of 8300.
    """
    today = dt.date.today().isoformat()
    entries = {
        "cli:session-aaa": {"d": today, "model": "Gemini 3.5 Flash (High)", "u": 100, "c": 5_000, "o": 50},
        "cli:session-bbb": {"d": today, "model": "Gemini 3.5 Flash (High)", "u": 200, "c": 3_000, "o": 80},
    }
    _write_ledger(tmp_path, entries)
    s = _cost_summary(tmp_path, monkeypatch)
    # Both sessions must keep their full c values (singletons, not cross-de-inflated).
    # tokensToday = (100+200) + (5000+3000) + (50+80) = 300 + 8000 + 130 = 8430
    expected = 300 + 8_000 + 130
    assert s["tokensToday"] == expected, (
        f"Expected {expected} (both CLI sessions' full c), got {s['tokensToday']} "
        f"(if ~5300+130=5430, de-inflation incorrectly merged CLI sessions)"
    )


def test_cache_read_at_keyed_gen_entries_are_singletons(tmp_path, monkeypatch):
    """gen_metadata '@'-keyed entries are per-generation independent cache-read
    snapshots (each is one discrete API call, not a cumulative context re-read
    accumulating across turns). They must be singletons in _key_stem so two
    '@' entries from the same cascade keep their own full 'c' values.

    Verified in _pb_generations: 'c' = protobuf field 5 from the generation's
    own usage record — it reflects that single generation's cache cost, not a
    rolling total across all prior generations in the conversation.
    """
    today = dt.date.today().isoformat()
    entries = {
        # Two gen_metadata entries from the same cascade: each is independent.
        "cas@0": {"d": today, "me": 1026, "u": 200, "c": 10_000, "o": 80},
        "cas@1": {"d": today, "me": 1026, "u": 150, "c":  8_000, "o": 60},
    }
    _write_ledger(tmp_path, entries)
    s = _cost_summary(tmp_path, monkeypatch)
    # Each '@' entry is its own singleton: both keep their full c values.
    # tokensToday = (200+150) + (10000+8000) + (80+60) = 350 + 18000 + 140 = 18490
    expected = 350 + 18_000 + 140
    assert s["tokensToday"] == expected, (
        f"Expected {expected} (both '@' entries' full c), got {s['tokensToday']} "
        f"(if ~10490, de-inflation incorrectly merged '@' gen_metadata entries)"
    )


def test_cache_read_colon_in_cascade_id_not_truncated(tmp_path, monkeypatch):
    """A cascade id containing ':' must not be truncated by _key_stem — splitting
    on the first ':' would merge 'abc:def:0' (stem 'abc') with 'abc:ghi:0' (also
    stem 'abc'), cross-de-inflating unrelated conversations.

    rsplit(":", 1) correctly recovers the full cascade id 'abc:def' from key
    'abc:def:0' (suffix is always a plain integer, never contains ':').
    """
    today = dt.date.today().isoformat()
    entries = {
        # Two distinct conversations whose IDs share an 'abc:' prefix.
        "abc:def:0": {"d": today, "me": 1016, "u": 100, "c": 5_000, "o": 50},
        "abc:def:1": {"d": today, "me": 1016, "u": 100, "c": 9_000, "o": 50},  # same cascade
        "abc:ghi:0": {"d": today, "me": 1016, "u": 200, "c": 3_000, "o": 80},  # different cascade
    }
    _write_ledger(tmp_path, entries)
    s = _cost_summary(tmp_path, monkeypatch)
    # Stem of 'abc:def:0'/'abc:def:1' = 'abc:def'; stem of 'abc:ghi:0' = 'abc:ghi'.
    # De-inflated: max_c(abc:def) = 9000, c(abc:ghi) = 3000 (singleton).
    # tokensToday = (100+100+200) + (9000+3000) + (50+50+80) = 400 + 12000 + 180 = 12580
    expected = 400 + 12_000 + 180
    # Wrong (split-on-first-':'): stem 'abc' for all three → max_c=9000, others zeroed
    # → (100+100+200) + 9000 + (50+50+80) = 400+9000+180 = 9580
    wrong = 400 + 9_000 + 180
    assert s["tokensToday"] != wrong, "rsplit not applied — cascade id truncated at first ':'"
    assert s["tokensToday"] == expected, (
        f"Expected {expected}, got {s['tokensToday']}"
    )


# ---------------------------------------------------------------------------
# Task 3: Enum mappings for 330 and 1050
# ---------------------------------------------------------------------------

def test_enum_330_is_mapped():
    """Enum 330 must map to a flash-lite-tier display name (not fall through to Unknown)."""
    name = costmod._MODEL_ENUM_NAMES.get(330)
    assert name is not None, "Enum 330 is unmapped — must resolve to a flash-lite tier display name"
    assert "flash" in name.lower() or "lite" in name.lower(), (
        f"Enum 330 mapped to '{name}' — expected a flash/lite tier name"
    )


def test_enum_1050_is_mapped():
    """Enum 1050 must map to a flash-lite-tier display name."""
    name = costmod._MODEL_ENUM_NAMES.get(1050)
    assert name is not None, "Enum 1050 is unmapped"
    assert "flash" in name.lower() or "lite" in name.lower()


def test_enum_330_resolve_model_display():
    """_resolve_model_display must return the enum-330 display name, not 'Unknown'."""
    entry = {"me": 330, "u": 100, "c": 0, "o": 50}
    name = costmod._resolve_model_display(entry)
    assert name != "Unknown", "330 fell through to Unknown; should resolve to flash-lite name"
    assert name == costmod._MODEL_ENUM_NAMES[330]


def test_enum_330_in_model_family_gemini_group():
    """Enum 330 should resolve to the Gemini family (for breakdown grouping)."""
    entry = {"me": 330, "u": 100, "c": 0, "o": 50}
    name = costmod._resolve_model_display(entry)
    family = costmod._model_family(name)
    assert "gemini" in family.lower() or "flash" in family.lower(), (
        f"Expected Gemini family for enum 330, got '{family}'"
    )


# ---------------------------------------------------------------------------
# Task 2: Marker-26 ingestion + dedup (focused tests, supplements test_genmetadata_scan.py)
# ---------------------------------------------------------------------------

def test_marker26_and_marker24_steps_disjoint_keys(tmp_path, monkeypatch):
    """Marker-26 entries from gen_metadata and marker-24 entries from steps.metadata
    must be keyed disjointly (<stem>@<idx> vs <stem>:<idx>) with no double-counting."""
    import sqlite3 as _sqlite3

    CODE_DIR_inner = Path(__file__).parent.parent / "io.github.dlansama.tallybar" / "contents" / "code"
    sys.path.insert(0, str(CODE_DIR_inner))
    from providers import cost as cm

    def _varint(n: int) -> bytes:
        out = bytearray()
        while True:
            b = n & 0x7F
            n >>= 7
            if n:
                out.append(b | 0x80)
            else:
                out.append(b)
                return bytes(out)

    def _field(fn: int, val: int) -> bytes:
        return _varint(fn << 3) + _varint(val)

    def _blob(marker: int, *, u=100, o=50, c=20, model=1016) -> bytes:
        return _field(1, model) + _field(2, u) + _field(3, o) + _field(5, c) + _field(6, marker)

    conv = tmp_path / "convs"
    conv.mkdir()
    db = conv / "dedup.db"
    con = _sqlite3.connect(str(db))
    con.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, metadata BLOB)")
    con.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB, size INTEGER)")
    # steps: marker-24 record for step 0
    con.execute("INSERT INTO steps VALUES (0, ?)", (_blob(24, u=100, o=50, c=20),))
    # gen_metadata: marker-24 duplicate (must be ignored) + NEW marker-26 record
    con.execute("INSERT INTO gen_metadata VALUES (0, ?, ?)",
                (_blob(24, u=100, o=50, c=20), 0))  # dup -> ignored
    con.execute("INSERT INTO gen_metadata VALUES (1, ?, ?)",
                (_blob(26, u=200, o=80, c=30, model=1026), 0))  # new -> must ingest
    con.commit()
    con.close()

    monkeypatch.setattr(cm, "ANTIGRAVITY_CONVERSATION_DIRS", (conv,))
    monkeypatch.setattr(cm, "ANTIGRAVITY_LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(cm, "ANTIGRAVITY_CLI_USAGE_PATH", tmp_path / "cli.json")
    monkeypatch.setattr(agmod, "collect_antigravity_rpc_usage", lambda *a, **k: ({}, {}))

    res = cm.update_antigravity_token_ledger()
    entries = res["entries"]
    # Exactly two entries: steps:0 and gen@1 — the gen-side marker-24 is skipped
    assert set(entries) == {"dedup:0", "dedup@1"}, (
        f"Expected {{dedup:0, dedup@1}}, got {set(entries)}"
    )
    assert entries["dedup:0"]["u"] == 100
    assert entries["dedup@1"]["u"] == 200
    assert entries["dedup@1"]["me"] == 1026
