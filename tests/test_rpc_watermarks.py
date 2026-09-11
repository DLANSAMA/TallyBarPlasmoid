"""Tests for incremental Antigravity RPC harvest (rpcWatermarks + deadline).

Covers:
  1. Unchanged cascade skipped (mark re-confirmed, absent from usage)
  2. New / changed cascade queried, mark set
  3. Failed metadata call doesn't advance the mark
  4. Deadline partial abort
  5. Missing / empty lastModifiedTime always queried
  6. Ledger integration round-trip (marks persisted + fed back as `known`)
  7. .bak recovery preserves rpcWatermarks
  8. Prune: entry-less stale mark dropped; entry-bearing mark kept with LS down
  9. No write churn when marks identical
 10. Old ledger (missing / wrong-typed rpcWatermarks) loads fine
"""

import datetime as dt
import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

CODE_DIR = Path(__file__).parent.parent / "io.github.dlansama.tallybar" / "contents" / "code"
import sys
sys.path.insert(0, str(CODE_DIR))

from providers import antigravity as agmod  # noqa: E402
from providers import cost as costmod       # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LMT_A = "2026-06-07T10:00:00.000000000Z"
_LMT_B = "2026-06-07T11:30:00.000000000Z"


def _make_rpc_stubs(summaries: dict, metadata_payloads: dict | None = None,
                    calls: list | None = None):
    """Return a fake `_post_local_rpc` that serves deterministic responses.

    ``summaries``        – {cascadeId: lastModifiedTime} for GetAllCascadeTrajectories.
    ``metadata_payloads`` – {cascadeId: list-of-gen-dicts} for GetCascadeTrajectoryGeneratorMetadata.
    ``calls``            – if given, each RPC URL+cascadeId is appended here so the test
                           can assert which cascades were queried.
    """
    if metadata_payloads is None:
        metadata_payloads = {}
    if calls is None:
        calls = []

    traj_response = {
        "trajectorySummaries": {
            cid: {"lastModifiedTime": lmt} for cid, lmt in summaries.items()
        }
    }

    def _fake_post(url, token, metadata, per_call):
        if "GetAllCascadeTrajectories" in url:
            return 200, traj_response
        if "GetAvailableModels" in url:
            return 200, {"response": {"models": {}}}  # display-name lookup; not tracked in `calls`
        # GetCascadeTrajectoryGeneratorMetadata
        cid = metadata.get("cascadeId", "")
        calls.append(cid)
        if cid not in metadata_payloads:
            return 404, {}
        gens = metadata_payloads[cid]
        return 200, {"generatorMetadata": gens}

    return _fake_post


def _gen(step_index: int, tokens: int = 100) -> dict:
    """Minimal well-formed generatorMetadata entry."""
    return {
        "stepIndices": [step_index],
        "chatModel": {
            "usage": {
                "inputTokens": tokens,
                "cacheReadTokens": 0,
                "outputTokens": tokens // 2,
                "model": "MODEL_PLACEHOLDER_M133",
                "apiProvider": "API_PROVIDER_GOOGLE_GEMINI",
            },
            "chatStartMetadata": {"createdAt": ""},
        },
    }


def _fake_processes(pid=9999, token="tok", scheme="http"):
    return [(pid, token, scheme)]


def _fake_ports(pid):
    return [12345], {}


# ---------------------------------------------------------------------------
# Test 1: unchanged cascade skipped
# ---------------------------------------------------------------------------

def test_unchanged_cascade_skipped():
    cid = "aaaa-unchanged"
    calls = []
    fake_post = _make_rpc_stubs(
        summaries={cid: _LMT_A},
        metadata_payloads={cid: [_gen(0)]},
        calls=calls,
    )
    with patch.object(agmod, "find_antigravity_processes", return_value=_fake_processes()), \
         patch.object(agmod, "antigravity_ports", side_effect=_fake_ports), \
         patch.object(agmod, "_post_local_rpc", side_effect=fake_post):
        usage, marks = agmod.collect_antigravity_rpc_usage(3.0, known={cid: _LMT_A})

    # No metadata call issued for the unchanged cascade.
    assert cid not in calls
    # Cascade absent from usage (was skipped).
    assert cid not in usage
    # Watermark re-confirmed.
    assert marks.get(cid) == _LMT_A


# ---------------------------------------------------------------------------
# Test 2: new / changed cascade queried
# ---------------------------------------------------------------------------

def test_new_cascade_queried():
    cid_new = "bbbb-new"
    cid_changed = "cccc-changed"
    calls = []
    fake_post = _make_rpc_stubs(
        summaries={cid_new: _LMT_A, cid_changed: _LMT_B},
        metadata_payloads={cid_new: [_gen(1)], cid_changed: [_gen(2)]},
        calls=calls,
    )
    # Known has cid_changed at an old LMT.
    known = {cid_changed: "2026-06-06T09:00:00.000000000Z"}
    with patch.object(agmod, "find_antigravity_processes", return_value=_fake_processes()), \
         patch.object(agmod, "antigravity_ports", side_effect=_fake_ports), \
         patch.object(agmod, "_post_local_rpc", side_effect=fake_post):
        usage, marks = agmod.collect_antigravity_rpc_usage(3.0, known=known)

    assert cid_new in calls
    assert cid_changed in calls
    assert cid_new in usage
    assert cid_changed in usage
    assert marks.get(cid_new) == _LMT_A
    assert marks.get(cid_changed) == _LMT_B


# ---------------------------------------------------------------------------
# Test 3: failed metadata call doesn't advance watermark
# ---------------------------------------------------------------------------

def test_failed_metadata_does_not_advance_mark():
    cid = "dddd-fail"
    calls = []

    def _fail_metadata(url, token, metadata, per_call):
        if "GetAllCascadeTrajectories" in url:
            return 200, {"trajectorySummaries": {cid: {"lastModifiedTime": _LMT_A}}}
        calls.append(metadata.get("cascadeId", ""))
        return 500, {}  # server error

    with patch.object(agmod, "find_antigravity_processes", return_value=_fake_processes()), \
         patch.object(agmod, "antigravity_ports", side_effect=_fake_ports), \
         patch.object(agmod, "_post_local_rpc", side_effect=_fail_metadata):
        usage, marks = agmod.collect_antigravity_rpc_usage(3.0, known={})

    assert cid in calls          # call was attempted
    assert cid not in usage      # no usage — server returned 500
    assert cid not in marks      # mark NOT advanced — will be retried next refresh


def test_exception_during_metadata_does_not_advance_mark():
    cid = "eeee-exc"

    def _exc_metadata(url, token, metadata, per_call):
        if "GetAllCascadeTrajectories" in url:
            return 200, {"trajectorySummaries": {cid: {"lastModifiedTime": _LMT_A}}}
        raise OSError("connection refused")

    with patch.object(agmod, "find_antigravity_processes", return_value=_fake_processes()), \
         patch.object(agmod, "antigravity_ports", side_effect=_fake_ports), \
         patch.object(agmod, "_post_local_rpc", side_effect=_exc_metadata):
        usage, marks = agmod.collect_antigravity_rpc_usage(3.0, known={})

    assert cid not in usage
    assert cid not in marks


# ---------------------------------------------------------------------------
# Test 4: deadline partial abort
# ---------------------------------------------------------------------------

def test_deadline_pre_list_returns_empty():
    """Deadline already expired before the list call → returns ({}. {})."""
    with patch.object(agmod, "find_antigravity_processes", return_value=_fake_processes()), \
         patch.object(agmod, "antigravity_ports", side_effect=_fake_ports):
        usage, marks = agmod.collect_antigravity_rpc_usage(
            3.0, known={}, deadline=time.time() - 1.0
        )

    assert usage == {}
    assert marks == {}


def test_deadline_partial_harvest():
    """Deadline expires after the first metadata call → prefix harvested + marked; rest unmarked."""
    cid1 = "aaaa-first"
    cid2 = "bbbb-second"

    # Monotonically-increasing fake time: before the metadata call the clock is well before the
    # deadline; after the first metadata call the clock jumps past it so cid2 is not queried.
    call_count = {"n": 0}
    deadline = 100.0  # absolute fake-clock value
    clock = [90.0]    # starts well before deadline

    def _mock_time():
        # Return current clock value; advance by 1 on each call so multiple checks progress.
        v = clock[0]
        clock[0] += 1
        return v

    def _timed_post(url, token, metadata, per_call):
        if "GetAllCascadeTrajectories" in url:
            return 200, {"trajectorySummaries": {
                cid1: {"lastModifiedTime": _LMT_A},
                cid2: {"lastModifiedTime": _LMT_B},
            }}
        if "GetAvailableModels" in url:
            return 200, {"response": {"models": {}}}  # display-name lookup, not a metadata call
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Advance clock past deadline so the next cascade-loop iteration aborts.
            clock[0] = deadline + 10
            return 200, {"generatorMetadata": [_gen(1)]}
        return 200, {"generatorMetadata": [_gen(2)]}

    with patch.object(agmod, "find_antigravity_processes", return_value=_fake_processes()), \
         patch.object(agmod, "antigravity_ports", side_effect=_fake_ports), \
         patch.object(agmod, "_post_local_rpc", side_effect=_timed_post), \
         patch.object(agmod.time, "time", side_effect=_mock_time):

        usage, marks = agmod.collect_antigravity_rpc_usage(
            3.0, known={}, deadline=deadline
        )

    # cid1 was harvested and marked; cid2 should have been aborted by the deadline.
    assert cid1 in usage
    assert cid1 in marks
    assert cid2 not in marks


# ---------------------------------------------------------------------------
# Test 5: missing / empty lastModifiedTime always queried
# ---------------------------------------------------------------------------

def test_missing_lmt_always_queried():
    cid = "ffff-nolmt"
    calls = []

    def _post(url, token, metadata, per_call):
        if "GetAllCascadeTrajectories" in url:
            # summary without a lastModifiedTime key
            return 200, {"trajectorySummaries": {cid: {}}}
        calls.append(metadata.get("cascadeId", ""))
        return 200, {"generatorMetadata": [_gen(0)]}

    # Even if 'known' has a mark, the empty LMT must still trigger a query.
    known = {cid: ""}
    with patch.object(agmod, "find_antigravity_processes", return_value=_fake_processes()), \
         patch.object(agmod, "antigravity_ports", side_effect=_fake_ports), \
         patch.object(agmod, "_post_local_rpc", side_effect=_post):
        usage, marks = agmod.collect_antigravity_rpc_usage(3.0, known=known)

    assert cid in calls
    assert cid in usage
    # Mark set to empty-string — cascade will be re-queried next refresh (can't tell if changed).
    assert marks.get(cid) == ""


# ---------------------------------------------------------------------------
# Test 6: ledger integration round-trip (marks persisted + fed back as `known`)
# ---------------------------------------------------------------------------

def test_ledger_marks_round_trip(monkeypatch, tmp_path):
    """Watermarks land in the saved ledger JSON; a second run feeds them back as `known`."""
    monkeypatch.setattr(costmod, "ANTIGRAVITY_LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CONVERSATION_DIRS", ())
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CLI_USAGE_PATH", tmp_path / "cli.json")

    today = dt.date.today().isoformat()
    cid = "gggg-round-trip"
    lmt = "2026-06-07T08:00:00.000000000Z"

    received_known = {}

    def _fake_rpc(timeout, known=None, deadline=None, **_kwargs):
        received_known.update(known or {})
        return (
            {cid: [{"stepKey": "0", "u": 10, "c": 0, "o": 5,
                    "model_placeholder": "MODEL_PLACEHOLDER_M133",
                    "api_provider": "API_PROVIDER_GOOGLE_GEMINI",
                    "date": today, "hour": 9}]},
            {cid: lmt},
        )

    monkeypatch.setattr(agmod, "collect_antigravity_rpc_usage", _fake_rpc)

    # First run — no prior watermarks.
    costmod.update_antigravity_token_ledger()
    saved = json.loads((tmp_path / "ledger.json").read_text())
    assert "rpcWatermarks" in saved
    assert saved["rpcWatermarks"].get(cid) == lmt

    # Second run — watermark should arrive in `known`.
    received_known.clear()
    costmod.update_antigravity_token_ledger()
    assert received_known.get(cid) == lmt


# ---------------------------------------------------------------------------
# Test 7: .bak recovery preserves rpcWatermarks
# ---------------------------------------------------------------------------

def test_bak_recovery_preserves_watermarks(monkeypatch, tmp_path):
    ledger_path = tmp_path / "ledger.json"
    monkeypatch.setattr(costmod, "ANTIGRAVITY_LEDGER_PATH", ledger_path)

    today = dt.date.today().isoformat()
    cid = "hhhh-bak"
    lmt = "2026-06-07T09:00:00.000000000Z"
    good = {
        "trackingStarted": today,
        "entries": {f"{cid}#0": {"d": today, "u": 1, "c": 0, "o": 1}},
        "rpcWatermarks": {cid: lmt},
    }

    # Write good data to .bak, corrupt the main file.
    bak = ledger_path.with_name(ledger_path.name + ".bak")
    bak.write_text(json.dumps(good), encoding="utf-8")
    ledger_path.write_text("NOT JSON", encoding="utf-8")

    loaded = costmod._load_antigravity_ledger()
    assert loaded.get("rpcWatermarks", {}).get(cid) == lmt


# ---------------------------------------------------------------------------
# Test 8: prune rules
# ---------------------------------------------------------------------------

def test_stale_mark_without_entries_is_pruned(monkeypatch, tmp_path):
    """A mark for a cascade with no persisted entries and absent from live_marks is dropped."""
    monkeypatch.setattr(costmod, "ANTIGRAVITY_LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CONVERSATION_DIRS", ())
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CLI_USAGE_PATH", tmp_path / "cli.json")

    stale_cid = "iiii-stale"
    today = dt.date.today().isoformat()
    # Pre-seed a ledger with a watermark but NO entries for stale_cid.
    (tmp_path / "ledger.json").write_text(json.dumps({
        "trackingStarted": today,
        "entries": {},
        "rpcWatermarks": {stale_cid: _LMT_A},
    }), encoding="utf-8")

    # RPC returns empty (LS not running or cascade unloaded).
    monkeypatch.setattr(agmod, "collect_antigravity_rpc_usage", lambda *a, **k: ({}, {}))

    costmod.update_antigravity_token_ledger()

    saved = json.loads((tmp_path / "ledger.json").read_text())
    assert stale_cid not in saved.get("rpcWatermarks", {})


def test_entry_bearing_mark_survives_ls_down(monkeypatch, tmp_path):
    """A mark for a cascade WITH persisted entries is kept even when the LS is down."""
    monkeypatch.setattr(costmod, "ANTIGRAVITY_LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CONVERSATION_DIRS", ())
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CLI_USAGE_PATH", tmp_path / "cli.json")

    cid = "jjjj-has-entries"
    today = dt.date.today().isoformat()
    (tmp_path / "ledger.json").write_text(json.dumps({
        "trackingStarted": today,
        "entries": {f"{cid}#0": {"d": today, "u": 5, "c": 0, "o": 2, "model": "Gemini 3.5 Flash (High)"}},
        "rpcWatermarks": {cid: _LMT_A},
    }), encoding="utf-8")

    # LS down — RPC returns nothing.
    monkeypatch.setattr(agmod, "collect_antigravity_rpc_usage", lambda *a, **k: ({}, {}))

    costmod.update_antigravity_token_ledger()

    saved = json.loads((tmp_path / "ledger.json").read_text())
    assert saved.get("rpcWatermarks", {}).get(cid) == _LMT_A


# ---------------------------------------------------------------------------
# Test 9: no write churn when marks are identical
# ---------------------------------------------------------------------------

def test_no_write_churn_when_marks_identical(monkeypatch, tmp_path):
    """When watermarks and entries are unchanged, the ledger file is NOT re-written."""
    ledger_path = tmp_path / "ledger.json"
    monkeypatch.setattr(costmod, "ANTIGRAVITY_LEDGER_PATH", ledger_path)
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CONVERSATION_DIRS", ())
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CLI_USAGE_PATH", tmp_path / "cli.json")

    cid = "kkkk-no-churn"
    today = dt.date.today().isoformat()
    ledger_content = {
        "trackingStarted": today,
        "entries": {f"{cid}#0": {"d": today, "u": 10, "c": 0, "o": 5, "model": "Gemini 3.5 Flash (High)"}},
        "rpcWatermarks": {cid: _LMT_A},
    }
    ledger_path.write_text(json.dumps(ledger_content), encoding="utf-8")
    mtime_before = ledger_path.stat().st_mtime_ns

    # RPC re-confirms the same watermark (unchanged cascade).
    monkeypatch.setattr(agmod, "collect_antigravity_rpc_usage",
                        lambda *a, **k: ({}, {cid: _LMT_A}))

    costmod.update_antigravity_token_ledger()

    # File must not have been re-written (mtime unchanged).
    mtime_after = ledger_path.stat().st_mtime_ns
    assert mtime_before == mtime_after, "Ledger was unnecessarily re-written despite no changes"


# ---------------------------------------------------------------------------
# Test 10: old ledger without / with wrong-typed rpcWatermarks loads fine
# ---------------------------------------------------------------------------

def test_old_ledger_without_watermarks(monkeypatch, tmp_path):
    monkeypatch.setattr(costmod, "ANTIGRAVITY_LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CONVERSATION_DIRS", ())
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CLI_USAGE_PATH", tmp_path / "cli.json")

    today = dt.date.today().isoformat()
    # Old-format ledger with no rpcWatermarks key.
    (tmp_path / "ledger.json").write_text(json.dumps({
        "trackingStarted": today,
        "entries": {},
    }), encoding="utf-8")

    received_known = {}

    def _fake_rpc(timeout, known=None, deadline=None, **_kwargs):
        received_known.update(known or {})
        return {}, {}

    monkeypatch.setattr(agmod, "collect_antigravity_rpc_usage", _fake_rpc)
    costmod.update_antigravity_token_ledger()

    # `known` was passed as empty dict — no KeyError / crash.
    assert received_known == {}


@pytest.mark.parametrize("bad_value", [None, 42, "not-a-dict", []])
def test_wrong_typed_watermarks_treated_as_empty(monkeypatch, tmp_path, bad_value):
    monkeypatch.setattr(costmod, "ANTIGRAVITY_LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CONVERSATION_DIRS", ())
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CLI_USAGE_PATH", tmp_path / "cli.json")

    today = dt.date.today().isoformat()
    (tmp_path / "ledger.json").write_text(json.dumps({
        "trackingStarted": today,
        "entries": {},
        "rpcWatermarks": bad_value,
    }), encoding="utf-8")

    received_known = {}

    def _fake_rpc(timeout, known=None, deadline=None, **_kwargs):
        received_known.update(known or {})
        return {}, {}

    monkeypatch.setattr(agmod, "collect_antigravity_rpc_usage", _fake_rpc)
    costmod.update_antigravity_token_ledger()  # must not raise

    assert received_known == {}
