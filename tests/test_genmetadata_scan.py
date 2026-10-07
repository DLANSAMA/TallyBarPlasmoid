"""Post-2.0 Antigravity ``gen_metadata`` disk-scan.

After Antigravity 2.0 the usage records moved out of ``steps.metadata`` (marker field6==24)
and into a ``gen_metadata`` table (marker field6==26); steps rows carry no usage. These tests
drive the REAL scan end-to-end against on-disk SQLite + the real protobuf parser (no mocks):
26-records ingested under disjoint ``<stem>@<idx>`` keys, the duplicate 24-records inside
gen_metadata ignored, steps entries untouched, RPC takeover purging ``@`` keys, and the
mtime backfill dating that keeps a first-run backlog out of "Today".
"""

import datetime as dt
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

CODE_DIR = Path(__file__).parent.parent / "io.github.dlansama.tallybar" / "contents" / "code"
sys.path.insert(0, str(CODE_DIR))

from providers import cost as costmod  # noqa: E402
from providers import antigravity as agmod  # noqa: E402

_FIXED_NOW = dt.datetime(2026, 6, 9, 12, 0, 0, tzinfo=dt.timezone.utc)


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
    return _varint(fn << 3) + _varint(val)  # wiretype 0 (varint) for all token fields


def _usage_blob(marker: int, *, model=1026, u=200, o=80, c=30) -> bytes:
    """A flat protobuf usage record: field1=model enum, 2=uncached input, 3=output,
    5=cached input, 6=marker (24 pre-2.0 / 26 post-2.0)."""
    return _field(1, model) + _field(2, u) + _field(3, o) + _field(5, c) + _field(6, marker)


def _make_db(path, *, steps=None, gen=None):
    """Real plaintext trajectory DB with steps(idx, metadata) and gen_metadata(idx, data, size)."""
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, metadata BLOB)")
    con.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB, size INTEGER)")
    if steps:
        con.executemany("INSERT INTO steps (idx, metadata) VALUES (?, ?)", steps)
    if gen:
        con.executemany("INSERT INTO gen_metadata (idx, data, size) VALUES (?, ?, ?)",
                        [(i, b, len(b)) for (i, b) in gen])
    con.commit()
    con.close()


def _wire(monkeypatch, tmp_path, conv_dir):
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CONVERSATION_DIRS", (conv_dir,))
    monkeypatch.setattr(costmod, "ANTIGRAVITY_LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(costmod, "ANTIGRAVITY_CLI_USAGE_PATH", tmp_path / "cli.json")
    monkeypatch.setattr(agmod, "collect_antigravity_rpc_usage", lambda *a, **k: ({}, {}))


def _set_mtime(path, isodate):
    ts = dt.datetime.fromisoformat(isodate + "T12:00:00").timestamp()
    os.utime(path, (ts, ts))


def test_genmetadata_26_records_ingested_under_at_keys(monkeypatch, tmp_path):
    conv = tmp_path / "convs"
    conv.mkdir()
    _make_db(conv / "cas.db", gen=[(0, _usage_blob(26, model=1026, u=200, o=80, c=30)),
                                   (1, _usage_blob(26, model=1133, u=10, o=5, c=0))])
    _set_mtime(conv / "cas.db", "2026-06-01")
    _wire(monkeypatch, tmp_path, conv)
    res = costmod.update_antigravity_token_ledger(now=_FIXED_NOW)
    e = res["entries"]
    assert "cas@0.0" in e and "cas@1.0" in e
    assert (e["cas@0.0"]["u"], e["cas@0.0"]["c"], e["cas@0.0"]["o"], e["cas@0.0"]["me"]) == (200, 30, 80, 1026)
    assert "t" not in e["cas@0.0"] and "x" not in e["cas@0.0"]   # t/x omitted by design


def test_genmetadata_24_records_ignored(monkeypatch, tmp_path):
    # A 24-marked record inside gen_metadata duplicates what the steps scan captures, so the
    # marker-26 gate must skip it. This DB has NO steps rows -> nothing should be ingested.
    conv = tmp_path / "convs"
    conv.mkdir()
    _make_db(conv / "old.db", gen=[(0, _usage_blob(24))])
    _wire(monkeypatch, tmp_path, conv)
    res = costmod.update_antigravity_token_ledger(now=_FIXED_NOW)
    assert res["entries"] == {}


def test_mixed_upgrade_db_steps_and_genmetadata_disjoint(monkeypatch, tmp_path):
    # A DB spanning the upgrade: steps carries the old 24-record; gen_metadata carries BOTH a
    # duplicate 24 and the new 26. Expect the steps "<stem>:<idx>" entry AND the gen "<stem>@<idx>"
    # 26 entry, with the gen-side 24 ignored (no double count).
    conv = tmp_path / "convs"
    conv.mkdir()
    _make_db(conv / "span.db",
             steps=[(0, _usage_blob(24, model=1016, u=100, o=50, c=0))],
             gen=[(0, _usage_blob(24, model=1016, u=100, o=50, c=0)),   # dup of steps -> ignored
                  (1, _usage_blob(26, model=1026, u=200, o=80, c=30))])
    _wire(monkeypatch, tmp_path, conv)
    res = costmod.update_antigravity_token_ledger(now=_FIXED_NOW)
    e = res["entries"]
    assert set(e) == {"span:0", "span@1.0"}   # steps row + the lone 26 record; gen-side 24 dropped
    assert e["span:0"]["me"] == 1016 and e["span@1.0"]["me"] == 1026


def test_steps_only_db_unaffected_by_genmetadata_pass(monkeypatch, tmp_path):
    # Pre-2.0 DB whose gen_metadata table is empty: the steps entries must be exactly as before.
    conv = tmp_path / "convs"
    conv.mkdir()
    _make_db(conv / "pre.db", steps=[(0, _usage_blob(24, model=1133, u=100, o=50, c=0))])
    _set_mtime(conv / "pre.db", "2026-06-09")  # steps first-scan backfills to DB mtime, so pin it
    _wire(monkeypatch, tmp_path, conv)
    res = costmod.update_antigravity_token_ledger(now=_FIXED_NOW)
    assert set(res["entries"]) == {"pre:0"}
    assert res["entries"]["pre:0"]["d"] == "2026-06-09"


def test_genmetadata_backfill_dates_to_mtime_then_today(monkeypatch, tmp_path):
    conv = tmp_path / "convs"
    conv.mkdir()
    db = conv / "cas.db"
    _make_db(db, gen=[(0, _usage_blob(26))])
    _set_mtime(db, "2026-06-02")
    _wire(monkeypatch, tmp_path, conv)
    res = costmod.update_antigravity_token_ledger(now=_FIXED_NOW)
    assert res["entries"]["cas@0.0"]["d"] == "2026-06-02"  # first scan -> backfill to file mtime date

    # A genuinely NEW record appearing after the DB already has "@" entries gets today_iso.
    con = sqlite3.connect(str(db))
    con.execute("INSERT INTO gen_metadata (idx, data, size) VALUES (?, ?, ?)",
                (5, _usage_blob(26), len(_usage_blob(26))))
    con.commit()
    con.close()
    res2 = costmod.update_antigravity_token_ledger(now=_FIXED_NOW)
    assert res2["entries"]["cas@0.0"]["d"] == "2026-06-02"  # existing entry unchanged
    assert res2["entries"]["cas@5.0"]["d"] == "2026-06-09"  # new record -> today, not mtime


def test_rpc_takeover_purges_at_keys(monkeypatch, tmp_path):
    # Once the live RPC reports a cascade, its on-disk-sourced entries — both legacy ":" (steps)
    # AND new "@" (gen_metadata, "<stem>@<idx>.<gi>") — are dropped in favour of the authoritative RPC "#" entries.
    conv = tmp_path / "convs"
    conv.mkdir()
    _make_db(conv / "cas.db", gen=[(0, _usage_blob(26))])
    _wire(monkeypatch, tmp_path, conv)
    # First run: no RPC -> the @ entry is captured from disk.
    first = costmod.update_antigravity_token_ledger(now=_FIXED_NOW)
    assert "cas@0.0" in first["entries"]

    # Second run: RPC now owns "cas" -> the @ key is purged, replaced by the "#" entry.
    rpc = {"cas": [{"stepKey": "0", "date": "2026-06-09", "hour": 10,
                    "u": 1, "c": 0, "o": 1, "model_placeholder": "MODEL_PLACEHOLDER_M26",
                    "api_provider": "ANTHROPIC_VERTEX"}]}
    monkeypatch.setattr(agmod, "collect_antigravity_rpc_usage", lambda *a, **k: (rpc, {"cas": "t1"}))
    second = costmod.update_antigravity_token_ledger(now=_FIXED_NOW)
    assert "cas@0.0" not in second["entries"]    # disk @ key purged on RPC takeover
    assert "cas#0" in second["entries"]


def test_rpc_covered_stem_skips_genmetadata_scan(monkeypatch, tmp_path):
    # A stem the RPC already owns must skip the DB entirely (steps AND gen_metadata), so no @
    # key is ever created for it.
    conv = tmp_path / "convs"
    conv.mkdir()
    _make_db(conv / "cas.db", gen=[(0, _usage_blob(26))])
    _wire(monkeypatch, tmp_path, conv)
    rpc = {"cas": [{"stepKey": "0", "date": "2026-06-09", "hour": 10,
                    "u": 1, "c": 0, "o": 1, "model_placeholder": "MODEL_PLACEHOLDER_M26",
                    "api_provider": "ANTHROPIC_VERTEX"}]}
    monkeypatch.setattr(agmod, "collect_antigravity_rpc_usage", lambda *a, **k: (rpc, {"cas": "t1"}))
    res = costmod.update_antigravity_token_ledger(now=_FIXED_NOW)
    assert "cas@0.0" not in res["entries"] and "cas#0" in res["entries"]


def test_genmetadata_survives_bak_round_trip(monkeypatch, tmp_path):
    conv = tmp_path / "convs"
    conv.mkdir()
    _make_db(conv / "cas.db", gen=[(0, _usage_blob(26))])
    _set_mtime(conv / "cas.db", "2026-06-05")
    _wire(monkeypatch, tmp_path, conv)
    costmod.update_antigravity_token_ledger(now=_FIXED_NOW)
    led = tmp_path / "ledger.json"
    bak = led.with_name(led.name + ".bak")
    assert led.exists() and bak.exists() and led.read_text() == bak.read_text()
    import json
    assert "cas@0.0" in json.loads(bak.read_text())["entries"]


def test_genmetadata_deadline_abandons_scan(monkeypatch, tmp_path):
    import time
    conv = tmp_path / "convs"
    conv.mkdir()
    _make_db(conv / "cas.db", gen=[(i, _usage_blob(26)) for i in range(5)])
    _wire(monkeypatch, tmp_path, conv)
    res = costmod.update_antigravity_token_ledger(now=_FIXED_NOW, deadline=time.time() - 100)
    assert res["entries"] == {}  # expired deadline -> outer dir guard abandons before any scan


# ---------------------------------------------------------------------------
# Antigravity CLI conversation DBs (~/.gemini/antigravity-cli/conversations)
# ---------------------------------------------------------------------------

def test_cli_conversations_dir_in_scan_roots():
    # The agy CLI writes plaintext trajectory DBs since 2026-06-02 — its dir must be scanned
    # (before this, ~99% of CLI token volume was invisible: only the statusLine push captured).
    assert any(p.parent.name == "antigravity-cli" for p in costmod.ANTIGRAVITY_CONVERSATION_DIRS)


def test_cli_db_steps_ingested_gen_duplicates_skipped(monkeypatch, tmp_path):
    """CLI trajectory DBs are a hybrid: REAL usage lives in steps (marker-24), while
    gen_metadata holds marker-24 records that are EXACT 2x duplicates of the steps usage
    (verified against the live agy RPC 2026-06-09) and no marker-26 records. The scan must
    ingest the steps records and never ingest gen_metadata for these DBs — ingesting the
    duplicates would double-count every CLI generation."""
    conv = tmp_path / "antigravity-cli" / "conversations"
    conv.mkdir(parents=True)
    _make_db(conv / "sess.db",
             steps=[(0, _usage_blob(24, model=1132, u=100, o=40, c=20))],
             gen=[(0, _usage_blob(24, model=1132, u=100, o=40, c=20)),
                  (1, _usage_blob(24, model=1132, u=100, o=40, c=20))])
    _set_mtime(conv / "sess.db", "2026-06-05")
    _wire(monkeypatch, tmp_path, conv)
    res = costmod.update_antigravity_token_ledger(now=_FIXED_NOW)
    e = res["entries"]
    assert (e["sess:0"]["u"], e["sess:0"]["c"], e["sess:0"]["o"], e["sess:0"]["me"]) == (100, 20, 40, 1132)
    assert e["sess:0"]["d"] == "2026-06-05"      # backlog dated to DB mtime, not today
    assert not any("@" in k for k in e)           # gen_metadata duplicates skipped entirely


def test_steps_backlog_dated_to_mtime_then_new_records_today(monkeypatch, tmp_path):
    """The steps pass mirrors the gen pass's backlog dating: the FIRST scan of a stem dates
    its records to the DB file mtime (a backlog ingest predates this run — stamping it today
    would inflate "Today"); once the stem has ':' entries, newly appearing records are
    genuinely new and get today. The gate is stem coverage, NOT mtime."""
    conv = tmp_path / "convs"
    conv.mkdir()
    _make_db(conv / "cas.db", steps=[(0, _usage_blob(24, u=50, o=10, c=0))])
    _set_mtime(conv / "cas.db", "2026-06-02")
    _wire(monkeypatch, tmp_path, conv)
    res = costmod.update_antigravity_token_ledger(now=_FIXED_NOW)
    assert res["entries"]["cas:0"]["d"] == "2026-06-02"
    con = sqlite3.connect(str(conv / "cas.db"))
    con.execute("INSERT INTO steps (idx, metadata) VALUES (1, ?)",
                (_usage_blob(24, u=7, o=3, c=0),))
    con.commit()
    con.close()
    # mtime stays OLD (a different old day, so the scan memo sees the change) — the
    # dating gate must use stem coverage, not mtime: the new record stamps today.
    _set_mtime(conv / "cas.db", "2026-06-03")
    res = costmod.update_antigravity_token_ledger(now=_FIXED_NOW)
    assert res["entries"]["cas:0"]["d"] == "2026-06-02"   # backlog dating preserved
    assert res["entries"]["cas:1"]["d"] == "2026-06-09"   # new record stamped today


def test_stale_backlog_past_prune_horizon_never_ingested(monkeypatch, tmp_path):
    """A never-scanned DB whose mtime is already past the 35-day prune horizon must be
    skipped outright: ingesting it would add entries the same run's prune deletes —
    leaving the stem entry-less so EVERY refresh re-ingests, re-prunes, and re-writes
    the ledger (double atomic write churn). Both passes guard on the cutoff."""
    conv = tmp_path / "convs"
    conv.mkdir()
    _make_db(conv / "old.db",
             steps=[(0, _usage_blob(24, u=50, o=10, c=0))],
             gen=[(0, _usage_blob(26, u=20, o=5, c=0))])
    _set_mtime(conv / "old.db", "2026-04-20")    # 50 days before _FIXED_NOW
    _wire(monkeypatch, tmp_path, conv)
    res = costmod.update_antigravity_token_ledger(now=_FIXED_NOW)
    assert not any(k.startswith("old") for k in res["entries"])
    # And no write churn: nothing changed, so the ledger file was never created.
    assert not (tmp_path / "ledger.json").exists()


def test_cli_stem_flips_back_to_disk_when_agy_gone(monkeypatch, tmp_path):
    """A CLI cascade RPC-harvested while agy was live keeps growing on disk after agy
    exits (the in-process RPC dies with the session). With no live LS listing the
    cascade, the scan must purge the '#' entries and re-own the stem from the DB's
    steps rows (complete usage for CLI DBs) — capturing the post-exit tail instead of
    suppressing it forever, and never letting '#' and ':' coexist."""
    import json
    conv = tmp_path / "antigravity-cli" / "conversations"
    conv.mkdir(parents=True)
    _make_db(conv / "sess.db",
             steps=[(0, _usage_blob(24, model=1132, u=100, o=40, c=20)),
                    (1, _usage_blob(24, model=1132, u=50, o=20, c=10))])  # post-exit tail
    _set_mtime(conv / "sess.db", "2026-06-08")
    _wire(monkeypatch, tmp_path, conv)
    # Ledger state from the live-session era: RPC harvested generation 0 only.
    (tmp_path / "ledger.json").write_text(json.dumps({
        "trackingStarted": "2026-06-08",
        "entries": {"sess#0": {"d": "2026-06-08", "u": 100, "c": 20, "o": 40,
                               "model": "Gemini 3.5 Flash (High)", "me": 1132}},
        "rpcWatermarks": {"sess": "2026-06-08T10:00:00Z"},
    }))
    res = costmod.update_antigravity_token_ledger(now=_FIXED_NOW)
    e = res["entries"]
    assert not any("#" in k for k in e)            # RPC-era keys purged
    assert {"sess:0", "sess:1"} <= set(e)          # full session re-owned from disk
    assert e["sess:1"]["u"] == 50                  # the tail is captured
    assert res.get("rpcWatermarks", {}) == {}      # stale watermark dropped


def test_cli_stem_stays_rpc_owned_while_listed_live(monkeypatch, tmp_path):
    """While ANY live language server still lists the cascade (live_marks — even a
    watermark-skipped unchanged one), RPC ownership holds: no flip, no ':' keys."""
    import json
    conv = tmp_path / "antigravity-cli" / "conversations"
    conv.mkdir(parents=True)
    _make_db(conv / "sess.db", steps=[(0, _usage_blob(24, model=1132, u=100, o=40, c=20))])
    _set_mtime(conv / "sess.db", "2026-06-08")
    _wire(monkeypatch, tmp_path, conv)
    (tmp_path / "ledger.json").write_text(json.dumps({
        "trackingStarted": "2026-06-08",
        "entries": {"sess#0": {"d": "2026-06-08", "u": 100, "c": 20, "o": 40,
                               "model": "Gemini 3.5 Flash (High)", "me": 1132}},
        "rpcWatermarks": {"sess": "2026-06-08T10:00:00Z"},
    }))
    # agy alive: cascade listed (watermark re-confirmed), no new usage harvested.
    monkeypatch.setattr(agmod, "collect_antigravity_rpc_usage",
                        lambda *a, **k: ({}, {"sess": "2026-06-08T10:00:00Z"}))
    res = costmod.update_antigravity_token_ledger(now=_FIXED_NOW)
    assert "sess#0" in res["entries"]
    assert not any(":" in k for k in res["entries"])
    assert res["rpcWatermarks"] == {"sess": "2026-06-08T10:00:00Z"}


# ---------------------------------------------------------------------------
# Per-DB scan memo (``dbScanned`` ledger key)
# ---------------------------------------------------------------------------

def _counting_pb(monkeypatch):
    calls = {"n": 0}
    real = costmod._pb_find_usage

    def wrapper(buf, *a, **k):
        calls["n"] += 1
        return real(buf, *a, **k)

    monkeypatch.setattr(costmod, "_pb_find_usage", wrapper)
    return calls


def test_scan_memo_skips_unchanged_db(monkeypatch, tmp_path):
    """Rows whose parse yields no usage never create entries, so the key-in-entries
    dedup re-parsed them on EVERY refresh (~35k blobs/run live). The dbScanned memo
    must skip an unchanged DB entirely — and rescan as soon as its content changes."""
    conv = tmp_path / "convs"
    conv.mkdir()
    _make_db(conv / "cas.db",
             steps=[(0, _usage_blob(24, u=50, o=10, c=0)), (1, b"\x08\x01")],  # 1 usage + 1 noise row
             gen=[(0, _usage_blob(26, u=20, o=5, c=0))])
    _set_mtime(conv / "cas.db", "2026-06-05")
    _wire(monkeypatch, tmp_path, conv)
    res = costmod.update_antigravity_token_ledger(now=_FIXED_NOW)
    assert "cas:0" in res["entries"] and "cas@0.0" in res["entries"]
    assert "cas" in res["dbScanned"]                      # memo persisted

    calls = _counting_pb(monkeypatch)
    res = costmod.update_antigravity_token_ledger(now=_FIXED_NOW)
    assert calls["n"] == 0                                # unchanged -> zero blob parses
    assert "cas:0" in res["entries"]                      # data intact

    con = sqlite3.connect(str(conv / "cas.db"))
    con.execute("INSERT INTO steps (idx, metadata) VALUES (2, ?)",
                (_usage_blob(24, u=7, o=3, c=0),))
    con.commit()
    con.close()                                           # mtime/size move -> sig changes
    res = costmod.update_antigravity_token_ledger(now=_FIXED_NOW)
    assert calls["n"] > 0                                 # rescanned
    assert "cas:2" in res["entries"]                      # new row captured


def test_scan_memo_wal_append_invalidates(monkeypatch, tmp_path):
    """SQLite WAL appends land in the -wal companion without touching the main file
    until checkpoint — the signature must include the -wal stats or real new rows
    would be skipped while the memo claims 'unchanged'."""
    import os as _os
    conv = tmp_path / "convs"
    conv.mkdir()
    db = conv / "cas.db"
    # The noise row is what makes the rescan observable: the usage row is already
    # ingested (key-in-entries dedup), so only the noise row re-parses on a rescan.
    _make_db(db, steps=[(0, _usage_blob(24, u=50, o=10, c=0)), (1, b"\x08\x01")])
    wal = conv / "cas.db-wal"
    wal.write_bytes(b"w")
    _set_mtime(db, "2026-06-05")
    _set_mtime(wal, "2026-06-05")
    _wire(monkeypatch, tmp_path, conv)
    costmod.update_antigravity_token_ledger(now=_FIXED_NOW)

    calls = _counting_pb(monkeypatch)
    costmod.update_antigravity_token_ledger(now=_FIXED_NOW)
    assert calls["n"] == 0                                # both files unchanged -> skip

    _os.utime(wal, None)                                  # WAL touched -> sig changes
    costmod.update_antigravity_token_ledger(now=_FIXED_NOW)
    assert calls["n"] > 0                                 # rescanned


def test_scan_memo_pre20_db_without_gen_table(monkeypatch, tmp_path):
    """'no such table: gen_metadata' on a pre-2.0 DB is a stable content fact, not a
    read failure — it must NOT block the memo (else every pre-2.0 DB rescans forever)."""
    conv = tmp_path / "convs"
    conv.mkdir()
    con = sqlite3.connect(str(conv / "old.db"))
    con.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, metadata BLOB)")
    con.execute("INSERT INTO steps (idx, metadata) VALUES (0, ?)",
                (_usage_blob(24, u=50, o=10, c=0),))
    con.commit()
    con.close()
    _set_mtime(conv / "old.db", "2026-06-05")
    _wire(monkeypatch, tmp_path, conv)
    res = costmod.update_antigravity_token_ledger(now=_FIXED_NOW)
    assert "old:0" in res["entries"]
    assert "old" in res["dbScanned"]

    calls = _counting_pb(monkeypatch)
    costmod.update_antigravity_token_ledger(now=_FIXED_NOW)
    assert calls["n"] == 0


def test_scan_memo_does_not_block_ownership_flip(monkeypatch, tmp_path):
    """The flip back to disk ownership (agy gone) must rescan even when the DB content
    is unchanged since a disk-owned-era memo: the ':' keys were purged at RPC takeover,
    so honoring the memo would silently lose the whole stem."""
    import json
    conv = tmp_path / "antigravity-cli" / "conversations"
    conv.mkdir(parents=True)
    db = conv / "sess.db"
    _make_db(db, steps=[(0, _usage_blob(24, model=1132, u=100, o=40, c=20))])
    _set_mtime(db, "2026-06-08")
    _wire(monkeypatch, tmp_path, conv)
    sig = costmod._db_signature(db)
    (tmp_path / "ledger.json").write_text(json.dumps({
        "trackingStarted": "2026-06-08",
        "entries": {"sess#0": {"d": "2026-06-08", "u": 100, "c": 20, "o": 40, "me": 1132}},
        "rpcWatermarks": {"sess": "2026-06-08T10:00:00Z"},
        "dbScanned": {"sess": sig},                       # stale memo from the disk era
    }))
    res = costmod.update_antigravity_token_ledger(now=_FIXED_NOW)
    assert "sess:0" in res["entries"]                     # flip rescanned despite memo
    assert not any("#" in k for k in res["entries"])


def test_dominant_enum_picks_max_token_record():
    # Guard: a single blob is one model in practice (verified 748/748 on-disk), so
    # this equals found[0]. But IF records ever mixed models, the summed entry attributes to
    # the DOMINANT (max-token) model, not whichever parsed first.
    assert costmod._dominant_enum([]) == 0
    assert costmod._dominant_enum([{1: 1016, 2: 100, 3: 50, 5: 0}]) == 1016
    mixed = [{1: 1016, 2: 5, 3: 1, 5: 0},        # tiny stray record
             {1: 1133, 2: 900, 3: 300, 5: 100}]  # dominant
    assert costmod._dominant_enum(mixed) == 1133
    # Order-independent: dominant wins regardless of parse order.
    assert costmod._dominant_enum(list(reversed(mixed))) == 1133


# --- Undated gen_metadata blobs must not be double-counted ---

def _ld(fn: int, payload: bytes) -> bytes:
    """A length-delimited (wiretype 2) protobuf field."""
    return _varint((fn << 3) | 2) + _varint(len(payload)) + payload


def _undated_generation_blob(**usage) -> bytes:
    """Real gen_metadata shape: root.field1 = generation; the generation's usage record sits
    at field4 AND is duplicated at field17.2; no field9 timestamp (the undated case)."""
    rec = _usage_blob(26, **usage)
    return _ld(1, _ld(4, rec) + _ld(17, _ld(2, rec)))


def test_undated_generation_record_counted_once_unit():
    from proto_wire import _dedupe_usage_records, _pb_find_usage, _pb_generations
    blob = _undated_generation_blob(model=1026, u=200, o=80, c=30)
    assert len(_pb_find_usage(blob, marker=26)) == 2          # field4 + its field17.2 copy
    assert len(_dedupe_usage_records(_pb_find_usage(blob, marker=26))) == 1
    assert _pb_generations(blob) == []                         # undated -> the fallback path
    [g] = _pb_generations(blob, require_timestamp=False)       # structural read, field4 only
    assert (g["u"], g["c"], g["o"], g["me"], g["mk"], g["secs"]) == (200, 30, 80, 1026, 26, None)


def test_undated_generation_blob_ingested_once(monkeypatch, tmp_path):
    """End to end: the undated fallback used to take _pb_find_usage's two copies and store
    u=400/o=160 for a 200/80 generation."""
    conv = tmp_path / "convs"
    conv.mkdir()
    _make_db(conv / "cas.db", gen=[(0, _undated_generation_blob(model=1026, u=200, o=80, c=30))])
    _set_mtime(conv / "cas.db", "2026-06-01")
    _wire(monkeypatch, tmp_path, conv)
    e = costmod.update_antigravity_token_ledger(now=_FIXED_NOW)["entries"]
    assert (e["cas@0.0"]["u"], e["cas@0.0"]["c"], e["cas@0.0"]["o"]) == (200, 30, 80)


def test_steps_row_repeating_one_record_counts_once(monkeypatch, tmp_path):
    """A steps row holding one request's usage at field 9 AND an exact copy at field 28.2 is ONE
    call: single values, no `n`. A row with two DISTINCT records still sums and carries n == 2."""
    rec = _usage_blob(24, model=1016, u=100, o=50, c=10)
    other = _usage_blob(24, model=1016, u=7, o=3, c=1)
    conv = tmp_path / "convs"
    conv.mkdir()
    _make_db(conv / "cas.db", steps=[(0, _ld(9, rec) + _ld(28, _ld(2, rec))),
                                     (1, _ld(9, rec) + _ld(28, _ld(2, other)))])
    _set_mtime(conv / "cas.db", "2026-06-01")
    _wire(monkeypatch, tmp_path, conv)
    e = costmod.update_antigravity_token_ledger(now=_FIXED_NOW)["entries"]
    assert (e["cas:0"]["u"], e["cas:0"]["c"], e["cas:0"]["o"]) == (100, 10, 50)
    assert "n" not in e["cas:0"]
    assert (e["cas:1"]["u"], e["cas:1"]["c"], e["cas:1"]["o"]) == (107, 11, 53)
    assert e["cas:1"]["n"] == 2


def test_unknown_layout_fallback_dedupes_exact_copies(monkeypatch, tmp_path):
    """A layout neither structural read recognizes still falls to the recursive scan —
    with exact duplicate records dropped."""
    rec = _usage_blob(26, model=1026, u=200, o=80, c=30)
    blob = _ld(7, rec) + _ld(8, rec)
    conv = tmp_path / "convs"
    conv.mkdir()
    _make_db(conv / "cas.db", gen=[(0, blob)])
    _set_mtime(conv / "cas.db", "2026-06-01")
    _wire(monkeypatch, tmp_path, conv)
    e = costmod.update_antigravity_token_ledger(now=_FIXED_NOW)["entries"]
    assert e["cas@0.0"]["u"] == 200


@pytest.mark.parametrize("layout", ["one-generation-each", "unknown-layout-fallback"])
def test_undated_blob_with_several_records_gets_one_entry_each(monkeypatch, tmp_path, layout):
    """An undated gen row holding 3 requests of ~90K prompt writes one "@<idx>.<gi>" entry per
    record (not one summed 270K entry), so each bills at the base rates instead of the
    long-prompt tier. Covers the structural read and the recursive fallback."""
    recs = [_usage_blob(26, model=1016, u=60_000 + i, o=1000, c=30_000) for i in range(3)]
    if layout == "one-generation-each":
        blob = b"".join(_ld(1, _ld(4, r)) for r in recs)
    else:
        blob = b"".join(_ld(7 + i, r) for i, r in enumerate(recs))
    conv = tmp_path / "convs"
    conv.mkdir()
    _make_db(conv / "cas.db", gen=[(0, blob)])
    _set_mtime(conv / "cas.db", dt.date.today().isoformat())
    _wire(monkeypatch, tmp_path, conv)
    e = costmod.update_antigravity_token_ledger(now=_FIXED_NOW)["entries"]
    assert set(e) == {"cas@0.0", "cas@0.1", "cas@0.2"}
    assert [e[f"cas@0.{i}"]["u"] for i in range(3)] == [60_000, 60_001, 60_002]
    assert all("n" not in v and v["me"] == 1016 for v in e.values())

    monkeypatch.setattr(costmod, "model_pricing", lambda name, *a, **k: (
        {"input": 2.0, "output": 12.0, "cache_read": 0.2, "long_context_threshold": 200_000,
         "long_input": 4.0, "long_output": 18.0, "long_cache_read": 0.4}
        if name == "gemini-3.1-pro" else {}))
    # Base rates per call: (60_000+i)*2 + 30_000*0.2 + 1000*12 = 138_000 + 2i; summed prompt (270K)
    # would have taken the long tier.
    cost = costmod.antigravity_ledger_cost_summary()["costToday"]
    assert cost == pytest.approx(3 * 0.138 + 6e-6, abs=1e-6)


def test_rpc_takeover_purges_dated_at_keys(monkeypatch, tmp_path):
    """A pre-seeded "<id>@<idx>.<gi>" entry (the dated gen shape) is dropped when the RPC
    owns the cascade, so it is not billed alongside the "#" entries."""
    conv = tmp_path / "convs"
    conv.mkdir()
    _wire(monkeypatch, tmp_path, conv)
    (tmp_path / "ledger.json").write_text(json.dumps({
        "trackingStarted": "2026-06-08",
        "entries": {"cas@0.0": {"d": "2026-06-08", "u": 5, "c": 0, "o": 5, "me": 1026},
                    "cas@12.3": {"d": "2026-06-08", "u": 5, "c": 0, "o": 5, "me": 1026},
                    "casx@0.0": {"d": "2026-06-08", "u": 5, "c": 0, "o": 5, "me": 1026}}}))
    rpc = {"cas": [{"stepKey": "0", "date": "2026-06-09", "hour": 10,
                    "u": 1, "c": 0, "o": 1, "model_placeholder": "MODEL_PLACEHOLDER_M26",
                    "api_provider": "ANTHROPIC_VERTEX"}]}
    monkeypatch.setattr(agmod, "collect_antigravity_rpc_usage", lambda *a, **k: (rpc, {"cas": "t1"}))
    e = costmod.update_antigravity_token_ledger(now=_FIXED_NOW)["entries"]
    assert "cas@0.0" not in e and "cas@12.3" not in e
    assert "cas#0" in e and "casx@0.0" in e   # another stem is untouched
