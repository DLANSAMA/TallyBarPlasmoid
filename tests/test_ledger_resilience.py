"""Antigravity token-ledger resilience: .bak recovery, .corrupt quarantine, flock.

Guards the 2026-05-29 history-collapse incident — if `_load_antigravity_ledger` ever falls
through to an EMPTY ledger on a transient read error, the next scan re-stamps the whole
history onto "today" (the disk path dates by first-seen) and the per-day graph flattens
into one bar. CLAUDE.md forbids "simplifying" the loader back to a bare try/except -> empty.

These tests drive the REAL loader/saver/lock (not mocks), so a regression that removed the
.bak recovery or the flock would fail here instead of passing silently.
"""

import json
import sys
from pathlib import Path

CODE_DIR = Path(__file__).parent.parent / "io.github.dlansama.tallybar" / "contents" / "code"
sys.path.insert(0, str(CODE_DIR))

from providers import cost as costmod  # noqa: E402
from providers import antigravity as agmod  # noqa: E402


def _point_at(monkeypatch, tmp_path):
    led = tmp_path / "antigravity_token_ledger.json"
    monkeypatch.setattr(costmod, "ANTIGRAVITY_LEDGER_PATH", led)
    return led


def test_save_mirrors_main_to_bak(monkeypatch, tmp_path):
    led = _point_at(monkeypatch, tmp_path)
    data = {"trackingStarted": "2026-05-01",
            "entries": {"a#0": {"d": "2026-05-01", "u": 1, "c": 2, "o": 3}}}
    costmod._save_antigravity_ledger(data)
    bak = led.with_name(led.name + ".bak")
    assert led.exists() and bak.exists()
    assert led.read_text() == bak.read_text()          # byte-identical last-good mirror
    assert json.loads(led.read_text()) == data


def test_load_recovers_from_bak_and_quarantines_corrupt_main(monkeypatch, tmp_path):
    led = _point_at(monkeypatch, tmp_path)
    good = {"trackingStarted": "2026-05-01", "entries": {"a#0": {"d": "2026-05-01"}}}
    led.write_text("{ this is not valid json", encoding="utf-8")          # corrupt main
    led.with_name(led.name + ".bak").write_text(json.dumps(good), encoding="utf-8")

    out = costmod._load_antigravity_ledger()
    assert out == good                                                    # recovered from .bak
    assert led.with_name(led.name + ".corrupt").exists()                  # main quarantined
    assert not led.exists()                                               # main moved aside


def test_load_recovers_when_main_missing(monkeypatch, tmp_path):
    led = _point_at(monkeypatch, tmp_path)
    good = {"trackingStarted": "2026-05-02", "entries": {}}
    led.with_name(led.name + ".bak").write_text(json.dumps(good), encoding="utf-8")
    assert costmod._load_antigravity_ledger() == good                     # main absent -> .bak


def test_load_returns_empty_only_when_both_lost(monkeypatch, tmp_path):
    # The ONLY path to an empty ledger: main AND .bak both unreadable. Must not raise.
    led = _point_at(monkeypatch, tmp_path)
    led.write_text("garbage", encoding="utf-8")
    led.with_name(led.name + ".bak").write_text("also garbage", encoding="utf-8")
    assert costmod._load_antigravity_ledger() == {"trackingStarted": None, "entries": {}}


def test_load_rejects_wrong_shape(monkeypatch, tmp_path):
    # Valid JSON but not a {entries: dict} ledger -> treated as corrupt, recover from .bak.
    led = _point_at(monkeypatch, tmp_path)
    good = {"trackingStarted": "2026-05-03", "entries": {"x:0": {"d": "2026-05-03"}}}
    led.write_text(json.dumps([1, 2, 3]), encoding="utf-8")               # a list, not a dict
    led.with_name(led.name + ".bak").write_text(json.dumps(good), encoding="utf-8")
    assert costmod._load_antigravity_ledger() == good


def test_update_acquires_and_releases_exclusive_lock(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path)
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CONVERSATION_DIRS", ())     # no disk scan
    monkeypatch.setattr(agmod, "collect_antigravity_rpc_usage", lambda *a, **k: ({}, {}))  # no RPC
    import fcntl
    calls = []
    real_flock = fcntl.flock

    def recording_flock(fd, op):
        calls.append(op)
        return real_flock(fd, op)

    monkeypatch.setattr(fcntl, "flock", recording_flock)
    costmod.update_antigravity_token_ledger()
    acquire = fcntl.LOCK_EX | fcntl.LOCK_NB                               # non-blocking acquire (ARCH-1)
    assert acquire in calls and fcntl.LOCK_UN in calls                    # acquired + released
    assert calls.index(acquire) < calls.index(fcntl.LOCK_UN)              # in that order
    assert fcntl.LOCK_EX not in calls                                     # never the blocking form


def test_ledger_update_skips_gracefully_when_lock_held(monkeypatch, tmp_path):
    # ARCH-1: if another process holds the ledger lock, the update must NOT block (it runs in an
    # asyncio.to_thread worker that an outer wait_for can't unblock). It returns the last-known
    # ledger read-only within the deadline budget instead of hanging.
    import fcntl
    import time
    led = _point_at(monkeypatch, tmp_path)
    seeded = {"trackingStarted": "2026-05-01",
              "entries": {"a#0": {"d": "2026-05-01", "u": 1, "c": 2, "o": 3}}}
    led.write_text(json.dumps(seeded), encoding="utf-8")
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CONVERSATION_DIRS", ())
    monkeypatch.setattr(agmod, "collect_antigravity_rpc_usage", lambda *a, **k: {})

    lock_path = led.with_suffix(".lock")
    holder = lock_path.open("a")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)                           # another process holds it
    try:
        start = time.time()
        out = costmod.update_antigravity_token_ledger(deadline=time.time() + 0.2)
        elapsed = time.time() - start
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()

    assert elapsed < 2.0                                                  # bounded, did NOT hang
    assert out == seeded                                                  # returned last-known ledger


def test_flock_with_timeout_false_when_held_true_when_free(tmp_path):
    import fcntl
    import time
    from io_helpers import flock_with_timeout
    lp = tmp_path / "x.lock"
    holder = lp.open("a")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
    other = lp.open("a")
    try:
        start = time.time()
        assert flock_with_timeout(other.fileno(), 0.15) is False         # contended -> False
        assert (time.time() - start) < 1.0                               # bounded wait, no hang
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()
    assert flock_with_timeout(other.fileno(), 0.0) is True               # free -> one attempt succeeds
    other.close()


import datetime as _dt  # noqa: E402

_FIXED_NOW = _dt.datetime(2026, 6, 2, 12, 0, tzinfo=_dt.timezone.utc)


def test_rpc_stems_reconstruct_with_rsplit(monkeypatch, tmp_path):
    # DATA-6: a persisted RPC key whose cascade id pathologically contains '#' must still map back
    # to the FULL cascade id (rsplit on the last '#'), so the matching disk DB stays deduped.
    led = _point_at(monkeypatch, tmp_path)
    weird = "ca#sc"                                                       # cascade id containing '#'
    led.write_text(json.dumps({"trackingStarted": "2026-06-01",
                               "entries": {f"{weird}#0": {"d": "2026-06-01", "u": 1, "c": 0, "o": 1,
                                                          "model": "gemini-3.1-pro"}}}),
                   encoding="utf-8")
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CONVERSATION_DIRS", ())
    monkeypatch.setattr(agmod, "collect_antigravity_rpc_usage", lambda *a, **k: {})

    # No exception, entry preserved; the rsplit stem would be "ca#sc" (full), not "ca" (split).
    out = costmod.update_antigravity_token_ledger(now=_FIXED_NOW)
    assert f"{weird}#0" in out["entries"]
    # The full key "ca#sc#0" rsplits to the full cascade id "ca#sc"; split() would give "ca".
    assert f"{weird}#0".rsplit("#", 1)[0] == weird
    assert f"{weird}#0".split("#", 1)[0] != weird                        # the old (buggy) form
