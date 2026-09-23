"""Cost & token-ledger aggregation for the providers package.

Not a fetch path: this module folds local token usage into provider cost
summaries. It owns ``apply_local_cost_summaries`` (token/tier propagation) and
the whole Antigravity token-capture pipeline — ``_pb_find_usage`` (scans the
trajectory DBs for protobuf usage records), ``update_antigravity_token_ledger``
(persists new generations to ``~/.tallybar/antigravity_token_ledger.json``), and
``antigravity_ledger_cost_summary`` (prices each ledger entry per model). Reads
local on-disk state only; performs no network I/O.
"""
from __future__ import annotations

import datetime as dt
import functools
import json
import os
import re
import sqlite3
import urllib.parse
from pathlib import Path
from typing import Any

from accounting import (
    compact_token_count,
    cost_breakdown_line,
    default_provider,
    bucket_add,
    empty_hourly_token_buckets,
    empty_monthly_token_buckets,
    empty_weekly_token_buckets,
    exact_usd,
    hourly_token_usage,
    model_breakdown_rows,
    model_pricing,
    monthly_token_usage,
    trailing_week_days,
    weekly_token_usage,
)
from io_helpers import atomic_write_text, flock_with_timeout


ANTIGRAVITY_CONVERSATION_DIRS = (
    Path.home() / ".gemini" / "antigravity" / "conversations",
    Path.home() / ".gemini" / "antigravity-ide" / "conversations",
    # The Antigravity CLI (agy) switched to the same plaintext trajectory-DB format on
    # 2026-06-02 (its older .pb history is encrypted and stays unrecoverable). CLI DBs
    # are a hybrid: REAL usage lives in `steps` as marker-24 records (the normal steps
    # scan ingests it), while `gen_metadata` holds marker-24 records that are EXACT 2x
    # duplicates of the steps usage (verified against the live agy RPC 2026-06-09:
    # cache tokens matched steps to the digit, gen was precisely double) and no
    # marker-26 records at all — see the antigravity-cli gate in the gen pass below.
    Path.home() / ".gemini" / "antigravity-cli" / "conversations",
)
ANTIGRAVITY_LEDGER_PATH = Path.home() / ".tallybar" / "antigravity_token_ledger.json"
# Token usage logged by the Antigravity CLI (agy/gemini) via its statusLine push — written by
# integrations/antigravity_cli/cli_statusline_capture.py. Same {trackingStarted, entries} shape as
# the IDE ledger; keys namespaced "cli:<session>" so they never collide with the IDE's
# "<db_stem>:<idx>" keys when the two are merged for costing.
ANTIGRAVITY_CLI_USAGE_PATH = Path.home() / ".tallybar" / "antigravity_cli_usage.json"
# Append-only per-month per-provider cost/token rollup. Survives the 35-day
# Antigravity ledger prune — only Antigravity is lossy; the other providers' parse caches
# are all-time. Past months freeze once written so the prune can't erode them.
COST_ARCHIVE_PATH = Path.home() / ".tallybar" / "cost_archive.json"
# Bumped when the disk-scan dating logic changes in a way that needs existing entries rebuilt.
# v2: generations dated by EMBEDDED protobuf timestamp instead of DB file mtime.
# v3: usage-record gate accepts Antigravity-2.0 marker (field6 == 26) alongside pre-2.0 (24),
# so 2.0-format generations are no longer silently dropped.
# On a version mismatch we clear the per-DB scan cache (``dbScanned``) so every conversation
# is re-scanned once and its entries re-derived correctly.
ANTIGRAVITY_DISK_DATING_VERSION = 3
# Per-tick cap on the number of changed DBs scanned, so a first-run backlog (e.g. right after
# a version-bump cache reset) can't blow the cost-scan budget. Candidates are scanned
# newest-first, so the cap only defers the oldest, least-relevant DBs to later ticks.
ANTIGRAVITY_MAX_DBS_PER_TICK = 64


# ---------------------------------------------------------------------------
# Google AI subscription tier (shared by Gemini + Antigravity)
# ---------------------------------------------------------------------------

# --- Honest summary for flat-rate subscription providers (Gemini, Antigravity) -------------------
# These have no locally-measurable per-token cost: the Gemini web app is not metered per token,
def google_ai_subscription_tier(providers: dict[str, dict[str, Any]]) -> str | None:
    """The Google AI subscription tier (e.g. "Google AI Ultra") shared by Gemini
    and Antigravity. Only Antigravity's GetUserStatus exposes the exact name."""
    ag = providers.get("antigravity") or {}
    tier = str(ag.get("tier") or "").strip()
    return tier if tier.lower().startswith("google ai") else None



# ---------------------------------------------------------------------------
# Antigravity persistent token ledger
# ---------------------------------------------------------------------------

from proto_wire import (  # noqa: F401
    _dominant_enum,
    _pb_fields,
    _pb_find_usage,
    _pb_generations,
    _pb_read_varint,
)


def _local_iso(secs: int) -> str:
    """Local-time ``YYYY-MM-DD`` for a unix timestamp (seconds). Used to date disk-scanned
    generations by their embedded protobuf timestamp."""
    return dt.datetime.fromtimestamp(secs).date().isoformat()


def _drop_stem_entries(entries: dict[str, Any], stem: str) -> None:
    """Delete every entry belonging to a conversation stem, whatever separator it used
    (``':'`` / ``'#'`` / ``'@'``). Used to re-derive a conversation from scratch on re-scan so a
    re-scan is idempotent and disk is the single source of truth for any convo on disk."""
    for key in list(entries.keys()):
        if key == stem:
            del entries[key]
            continue
        if key.startswith(stem) and len(key) > len(stem) and key[len(stem)] in (":", "#", "@"):
            del entries[key]


def _load_antigravity_ledger() -> dict[str, Any]:
    """Load the persistent token ledger, recovering from the last-good ``.bak`` on failure.

    A transient read/parse failure on the main file used to fall straight through to an
    *empty* ledger — which silently discarded the entire per-day history, because the next
    scan re-stamped every generation with *today* (the disk path dates by first-seen). To
    make that non-destructive, ``_save_antigravity_ledger`` mirrors every good write to a
    ``.bak``; if the main file is missing or corrupt we recover from it — and quarantine the
    corrupt main as ``.corrupt`` for forensics — instead of resetting tracking to today.
    """
    main = ANTIGRAVITY_LEDGER_PATH
    bak = main.with_name(main.name + ".bak")
    for idx, path in enumerate((main, bak)):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("entries"), dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
        if idx == 0:
            # Main was unreadable/invalid: set it aside (best-effort) so the next save
            # doesn't overwrite the evidence, then try to recover from the backup.
            try:
                main.replace(main.with_name(main.name + ".corrupt"))
            except OSError:
                pass
    return {"trackingStarted": None, "entries": {}}


def _load_usage_file(path: Path) -> dict[str, Any]:
    """Read a {trackingStarted, entries} token-usage file (the Antigravity CLI statusLine push).

    Read-only here: the writer process (CLI statusLine command) owns the writes; the backend only
    folds the entries into the cost summary, so there is no read-modify-write race.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("entries"), dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"trackingStarted": None, "entries": {}}


def _save_antigravity_ledger(ledger: dict[str, Any]) -> None:
    # Atomic 0600 write + ``.bak`` mirror. io_helpers.atomic_write_text is the ONE shared
    # recipe (unique 0600 mkstemp, fsync, replace, dir-fsync, parent chmod 0700) — the
    # ledger holds the user's AI usage stats. The same payload is mirrored to a ``.bak``
    # so a later corruption of the main file is recoverable (see _load_antigravity_ledger)
    # rather than silently resetting the per-day history to today.
    payload = json.dumps(ledger)
    try:
        atomic_write_text(ANTIGRAVITY_LEDGER_PATH, payload)
    except OSError:
        return
    # Best-effort last-good mirror; a bak failure must never break the main write.
    try:
        atomic_write_text(ANTIGRAVITY_LEDGER_PATH.with_name(ANTIGRAVITY_LEDGER_PATH.name + ".bak"), payload)
    except OSError:
        pass


def _db_signature(db_path: Path) -> str | None:
    """Content signature for the per-DB scan memo: main-file mtime_ns+size PLUS the
    ``-wal`` companion's (SQLite WAL appends land in the -wal without touching the
    main file until checkpoint — main-file stats alone would skip real new rows).
    ``None`` on any stat failure (caller then scans unconditionally — fail open)."""
    try:
        st = db_path.stat()
        sig = f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        return None
    try:
        w = db_path.with_name(db_path.name + "-wal").stat()
        return f"{sig}:{w.st_mtime_ns}:{w.st_size}"
    except OSError:
        return f"{sig}:0:0"


def update_antigravity_token_ledger(now: dt.datetime | None = None, deadline: float | None = None) -> dict[str, Any]:
    """Fold any newly-seen Antigravity generations into the persistent ledger.

    Two sources feed the ledger, the live language-server RPC taking precedence over the on-disk
    scan (Antigravity changes the trajectory-DB protobuf layout between releases, so the RPC is the
    stable source of truth):

    1. **Language-server RPC** (``collect_antigravity_rpc_usage``) — authoritative per-model usage for
       conversations currently loaded in LS memory. Keyed ``<cascadeId>#<stepIndex>`` and carries the
       real per-model pricing key. Because the RPC supersedes the DB for a conversation, any legacy
       on-disk-sourced entries for that conversation (``<cascadeId>:<int>``) are dropped in its favour.
    2. **Trajectory DBs** (``_pb_find_usage``) — on-disk fallback for conversations NOT in LS memory
       (history / LS not running); skipped for any conversation the RPC already covers.

    Each entry is keyed per generation (dedup-safe) and stamped with the date the generation occurred
    (RPC ``createdAt``) or, for the disk scan, the date first seen — so re-scanning never double-counts.

    An exclusive file lock (fcntl.flock) protects the read-modify-write cycle against concurrent
    executions (e.g. overlapping widget refreshes).
    """
    import fcntl
    import time

    current = now.astimezone() if now is not None else dt.datetime.now(dt.timezone.utc).astimezone()
    today_iso = current.date().isoformat()

    # Acquire exclusive lock on the ledger to prevent concurrent read-modify-write races.
    ANTIGRAVITY_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        ANTIGRAVITY_LEDGER_PATH.parent.chmod(0o700)  # match the other ~/.tallybar writers
    except OSError:
        pass
    lock_path = ANTIGRAVITY_LEDGER_PATH.with_suffix(".lock")
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    # Acquire NON-blocking with a bounded retry. A plain blocking LOCK_EX is unsafe here:
    # this runs inside a to_daemon_thread worker, so if another process holds the lock the outer
    # asyncio.wait_for cancels the awaiting task but CANNOT unblock this thread. The daemon worker
    # is abandoned at interpreter exit (so it won't hang the --once process the way the old
    # asyncio.to_thread non-daemon pool would), but parking a thread on a held lock is still pure
    # waste and a never-released lock would stall every future refresh. Bound the wait to the
    # cost-scan deadline (short default if none); on contention timeout, skip the read-modify-write
    # and return the last-known ledger read-only (one slightly-stale refresh) instead of blocking.
    lock_budget = max(0.0, min((deadline - time.time()) if deadline is not None else 2.0, 5.0))
    if not flock_with_timeout(lock_fd, lock_budget):
        os.close(lock_fd)
        return _load_antigravity_ledger()
    try:
        ledger = _load_antigravity_ledger()
        if ledger.get("trackingStarted") is None:
            ledger["trackingStarted"] = today_iso
        entries = ledger["entries"]
        changed = False

        # Version migration: if the stored disk-dating version is older than the current
        # ANTIGRAVITY_DISK_DATING_VERSION, clear the per-DB scan memo so every conversation
        # is re-scanned once with the corrected dating logic. The entries themselves are kept
        # (re-scanning produces new @ keys; old : keys survive until the RPC or prune drops them).
        # Only set changed when there is an actual dbScanned memo to clear; a fresh ledger has no
        # memo so the migration is a no-op and must not force a pointless ledger write.
        stored_version = ledger.get("diskDatingVersion", 0)
        if stored_version < ANTIGRAVITY_DISK_DATING_VERSION:
            ledger["diskDatingVersion"] = ANTIGRAVITY_DISK_DATING_VERSION
            if ledger.pop("dbScanned", None) is not None:
                changed = True

        # 0) Normalize legacy model strings to exact picker display names (idempotent;
        # runs under the lock so it can't lose a race with a concurrent refresh).
        for e in entries.values():
            legacy = _LEGACY_MODEL_STRINGS.get(e.get("model"))
            if legacy is not None:
                e["model"] = legacy
                changed = True

        # 1) Live RPC — authoritative for conversations loaded in the language server's memory.
        # Incremental harvest: load persisted lastModifiedTime watermarks so unchanged cascades
        # are skipped (see the ``rpcWatermarks`` contract in CLAUDE.md).
        marks = ledger.get("rpcWatermarks")
        if not isinstance(marks, dict):
            marks = {}
        learned_placeholders: dict[str, str] = {}
        try:
            from .antigravity import collect_antigravity_rpc_usage
            rpc_usage, live_marks = collect_antigravity_rpc_usage(
                4.0, known=marks, deadline=deadline, learned_names=learned_placeholders,
            )
        except Exception:
            rpc_usage, live_marks = {}, {}
        rpc_covered: set[str] = set()
        for cascade_id, records in rpc_usage.items():
            rpc_covered.add(cascade_id)
            # The RPC supersedes the on-disk scan for this conversation: drop any legacy DB-sourced
            # entries (plain ``<cascadeId>:<int>`` keys) so RPC + disk never double-count it.
            # Use strict prefix_len slicing instead of split(":", 1) to handle colons in cascade IDs.
            # NB the RPC is authoritative PER CONVERSATION and counts per-generation, while the disk
            # scan counts per-step — so the RPC legitimately having FEWER records than the disk did is
            # the normal case (one generation spans several steps), NOT a data-loss signal. Gating the
            # purge on a record-count comparison is therefore WRONG: it would block
            # superseding in the common case and reintroduce double-counting. Disk entries are an
            # approximation the authoritative RPC replaces wholesale.
            prefix_len = len(cascade_id) + 1
            for key in [k for k in entries
                        if (k.startswith(f"{cascade_id}:") or k.startswith(f"{cascade_id}@"))
                        and k[prefix_len:].isdigit()]:
                del entries[key]
                changed = True
            for rec in records:
                key = f"{cascade_id}#{rec['stepKey']}"
                rec_hour = rec.get("hour")
                if key in entries:
                    # Backfill the local hour onto an entry captured before hour-tracking
                    # existed (or one still missing it) so today's already-seen generations
                    # show in the Day view — without re-pricing or re-dating them.
                    if rec_hour is not None and entries[key].get("h") is None:
                        entries[key]["h"] = rec_hour
                        changed = True
                    # Re-resolve the model when the mapping has improved: a placeholder that
                    # was unknown at capture time fell back to a family default (e.g. M132 ->
                    # gemini-3-pro before 1132 was mapped). The RPC is authoritative, so when
                    # this cascade is re-queried anyway, heal the stale attribution in place
                    # (keeps date/hour/tokens — only the pricing/breakdown name changes).
                    # UPGRADE-ONLY: with no live display name this refresh (GetAvailableModels
                    # failed, or the id was retired from the picker) never DOWNGRADE a stored
                    # name to the family fallback — only fill a missing one.
                    fixed = rec.get("model_display") or ""
                    if not fixed and not entries[key].get("model"):
                        fixed = _rpc_model_pricing_key(rec["model_placeholder"], rec["api_provider"])
                    if fixed and entries[key].get("model") != fixed:
                        entries[key]["model"] = fixed
                        changed = True
                    enum = _placeholder_enum(rec["model_placeholder"])
                    if enum is not None and entries[key].get("me") != enum:
                        entries[key]["me"] = enum
                        changed = True
                    continue
                entry = {
                    "d": rec["date"] or today_iso,
                    "u": rec["u"],          # uncached input
                    "c": rec["c"],          # cached input (read)
                    "o": rec["o"],          # total output (thinking + response)
                    # Exact picker name when the live GetAvailableModels lookup had it;
                    # enum-map / family-fallback pricing key otherwise.
                    "model": (rec.get("model_display")
                              or _rpc_model_pricing_key(rec["model_placeholder"], rec["api_provider"])),
                }
                # Keep the RAW enum too: when "model" is a family fallback (placeholder
                # unknown at capture), a later _MODEL_ENUM_NAMES addition re-attributes
                # this entry at summary time — no re-query needed (enum map wins there).
                enum = _placeholder_enum(rec["model_placeholder"])
                if enum is not None:
                    entry["me"] = enum
                if rec_hour is not None:
                    entry["h"] = rec_hour   # local hour-of-day for the Day view
                entries[key] = entry
                changed = True

        # 2) On-disk trajectory DBs — fallback for conversations the RPC didn't cover. Skip any
        # conversation the RPC owns now or owned on a prior run (its ``<id>#…`` entries persist).
        # rsplit, not split: keys are "<cascadeId>#<stepKey>" and stepKey never contains
        # '#', so the LAST '#' is always the real separator — rsplit recovers the full cascade id
        # even in the (pathological) case where a cascade id itself contains a '#'.
        rpc_stems = rpc_covered | {k.rsplit("#", 1)[0] for k in entries if "#" in k}
        # First-scan backdating gates, snapshotted once per run (the per-DB ``any()``
        # alternative is O(DBs x entries)): a stem with NO prior entries of a shape is a
        # backlog ingest — its records predate this run, so stamping them ``today`` would
        # inflate "Today". Date those to the DB file mtime instead (cheap, far closer to
        # truth); once a stem has entries of that shape, newly appearing records are
        # genuinely new and get today_iso.
        colon_stems = {k.rsplit(":", 1)[0] for k in entries if ":" in k}
        at_stems = {k.rsplit("@", 1)[0] for k in entries if "@" in k}
        # Prune horizon, needed DURING the scan too: a backlog whose mtime date is already
        # past the prune would be ingested and deleted in the same run — and, with the stem
        # left entry-less, re-ingested + re-pruned on EVERY refresh (a permanent double
        # atomic write + full blob re-parse per refresh). Skip such DBs outright. (The old
        # today_iso dating had it worse: pruned conversations resurrected onto "Today"
        # every 35 days.) A stale DB that gets new writes moves its mtime inside the
        # horizon and is scanned normally.
        cutoff = (current.date() - dt.timedelta(days=35)).isoformat()
        # Per-DB scan memo (additive ledger key, rides the .bak mirror): skip BOTH passes
        # for a DB whose content signature (_db_signature: main + -wal mtime_ns/size)
        # matches its last COMPLETE scan. Without it ~35k no-usage blobs were re-parsed
        # on EVERY refresh (rows that never create entries can't be skipped by the
        # key-in-entries dedup) — ~3s/refresh of pure waste and real deadline pressure.
        # Fail-open by design: stat/sqlite errors, deadline expiry, and ownership flips
        # all leave the DB un-memoized, so the failure direction is always "rescan".
        old_memo = ledger.get("dbScanned")
        old_memo = old_memo if isinstance(old_memo, dict) else {}
        seen_memo: dict[str, str] = {}
        for conv_dir in ANTIGRAVITY_CONVERSATION_DIRS:
            if deadline is not None and time.time() > deadline:
                break  # budget spent — abandon the remaining conversation dirs
            if not conv_dir.is_dir():
                continue
            for db_path in conv_dir.glob("*.db"):
                # Budget check BEFORE opening a connection: the inner chunk-loop break
                # only abandons the current DB's blob reads; without this the outer loop
                # would still pay a connect + index query for every remaining DB after
                # the deadline (defeating the documented "abandon mid-scan" contract).
                if deadline is not None and time.time() > deadline:
                    break
                stem = db_path.stem
                force_scan = False
                if stem in rpc_stems:
                    if (conv_dir.parent.name != "antigravity-cli"
                            or stem in live_marks or stem in rpc_covered):
                        continue  # RPC is authoritative for this conversation
                    # ...but a CLI cascade NOT listed by any live language server can
                    # never be re-harvested: agy's RPC server is in-process and died
                    # with the session, while the session's tail generations (anything
                    # after the last in-session refresh) kept landing in this DB's
                    # steps rows — which for CLI DBs carry COMPLETE usage, unlike
                    # desktop 2.0. Flip ownership back to disk: purge the '#' entries
                    # and rescan fresh below ('#' and ':' must never coexist for a stem
                    # — double-count). The stale watermark drops in the merge below
                    # (stem no longer '#'-entry-bearing). If agy later resumes this
                    # exact conversation, the RPC re-harvest purges ':' and takes the
                    # stem back — ownership follows liveness.
                    for key in [k for k in entries if k.startswith(f"{stem}#")]:
                        del entries[key]
                        changed = True
                    # The ledger's ':' keys were purged when the RPC took over, so a
                    # scan memo from the disk-owned era may still match the unchanged
                    # file — honoring it would skip the rescan and lose the stem.
                    force_scan = True
                sig = _db_signature(db_path)
                if sig and not force_scan and old_memo.get(stem) == sig:
                    seen_memo[stem] = sig
                    continue  # content unchanged since the last complete scan
                db_ok = True   # any sqlite failure below blocks this run's memo
                # Backlog dating for the steps pass (mirrors the gen pass below): the
                # first scan of a DB ingests records that may be days old — e.g. the CLI
                # conversations backlog — so date them to the DB mtime, not today.
                if stem in colon_stems:
                    steps_date = today_iso
                else:
                    try:
                        steps_date = dt.date.fromtimestamp(db_path.stat().st_mtime).isoformat()
                    except OSError:
                        steps_date = today_iso
                    if steps_date < cutoff:
                        continue  # whole-DB backlog already past the prune horizon (see above)
                # SQLite read-lock contention (see CLAUDE.md "SQLite Read Lock Contention"): the widget
                # polls these DBs while live Antigravity agents are writing them. Holding a read lock
                # during the slow protobuf parse — or re-reading every step's blob on every poll — blocks
                # the agent from checkpointing its WAL and crashes it with SQLITE_BUSY. So keep the lock
                # window minimal: read only step indices first (cheap, no blobs), drop the ones already
                # captured, fetch ONLY the unseen blobs, then close BEFORE parsing. busy_timeout lets us
                # wait out a transient writer lock instead of failing immediately.
                new_idx = []
                try:
                    con = sqlite3.connect(f"file:{urllib.parse.quote(str(db_path))}?mode=ro", uri=True)
                    con.execute("PRAGMA busy_timeout = 3000")
                    new_idx = [i for (i,) in con.execute(
                        "SELECT idx FROM steps WHERE metadata IS NOT NULL")
                        if f"{stem}:{i}" not in entries]
                except sqlite3.Error:
                    db_ok = False  # couldn't read -> never memo a possibly-partial scan
                finally:
                    try: con.close()
                    except Exception: pass

                for j in range(0, len(new_idx), 400):  # chunk to stay under SQLite's variable limit
                    if deadline is not None and time.time() > deadline:
                        break
                    chunk = new_idx[j:j + 400]
                    placeholders = ",".join("?" * len(chunk))
                    chunk_rows = []
                    try:
                        con = sqlite3.connect(f"file:{urllib.parse.quote(str(db_path))}?mode=ro", uri=True)
                        con.execute("PRAGMA busy_timeout = 3000")
                        chunk_rows = con.execute(
                            f"SELECT idx, metadata FROM steps WHERE idx IN ({placeholders})", chunk
                        ).fetchall()
                    except sqlite3.Error:
                        db_ok = False  # couldn't read -> never memo a possibly-partial scan
                    finally:
                        try: con.close()
                        except Exception: pass

                    # Parse without lock
                    for idx, blob in chunk_rows:
                        if not isinstance(blob, (bytes, bytearray)):
                            continue
                        found = _pb_find_usage(bytes(blob))
                        if not found:
                            continue
                        entries[f"{stem}:{idx}"] = {
                            "d": steps_date,
                            "u": sum(vd.get(2, 0) for vd in found),   # uncached input
                            "c": sum(vd.get(5, 0) for vd in found),   # cached input
                            "o": sum(vd.get(3, 0) for vd in found),   # output (candidates)
                            "t": sum(vd.get(9, 0) for vd in found),   # thoughts
                            "x": sum(vd.get(10, 0) for vd in found),  # tool
                            "me": _dominant_enum(found),  # model enum (for per-model pricing)
                        }
                        changed = True

                # CLI DBs: STOP after the steps pass. Their gen_metadata rows hold
                # marker-24 records that are EXACT 2x duplicates of the steps usage
                # (verified against the live agy RPC 2026-06-09 — ingesting them would
                # double-count every CLI generation) and no marker-26 records at all.
                # Skipping also keeps the scan cheap: with no "@" entries ever written
                # for these stems, the pass below would re-fetch and re-parse every
                # gen blob of every CLI DB on every refresh.
                if conv_dir.parent.name == "antigravity-cli":
                    if db_ok and sig and (deadline is None or time.time() <= deadline):
                        seen_memo[stem] = sig  # steps pass IS the complete scan here
                    continue

                # Post-2.0 ``gen_metadata`` pass for the SAME DB. Marker-26 usage records
                # live ONLY here (after 2.0 the steps rows carry no usage); the 24-marked records
                # that also appear here duplicate the steps scan above, so we gate strictly on
                # marker 26. Keys "<stem>@<idx>.<gi>" (idx.generation-index) are disjoint from steps
                # "<stem>:<idx>" and RPC "<stem>#…". Same lock-window discipline: read indices first,
                # drop seen, fetch only unseen blobs, close BEFORE parsing, busy_timeout, deadline
                # checks per chunk.
                # Dating: uses EMBEDDED protobuf timestamps (via _pb_generations) so re-scanning a
                # file never re-stamps old generations as "today". Generations missing a timestamp
                # fall back to DB file mtime (old pre-2.0 blobs, expected to be rare).
                gnew_idx = []
                try:
                    con = sqlite3.connect(f"file:{urllib.parse.quote(str(db_path))}?mode=ro", uri=True)
                    con.execute("PRAGMA busy_timeout = 3000")
                    stem_prefix = f"{stem}@"
                    stem_prefix_len = len(stem_prefix)
                    seen_gen_idx = {
                        k[stem_prefix_len:].split(".", 1)[0]
                        for k in entries
                        if k.startswith(stem_prefix)
                    }
                    gnew_idx = [i for (i,) in con.execute(
                        "SELECT idx FROM gen_metadata WHERE data IS NOT NULL")
                        if str(i) not in seen_gen_idx]
                except sqlite3.Error as err:
                    # Table absent on pre-2.0 DBs -> nothing to ingest here (a stable
                    # fact about the content, safe to memo). Any OTHER failure (e.g.
                    # SQLITE_BUSY) means a possibly-partial read -> block the memo.
                    if "no such table" not in str(err):
                        db_ok = False
                finally:
                    try: con.close()
                    except Exception: pass

                try:
                    mtime_fallback_date = dt.date.fromtimestamp(db_path.stat().st_mtime).isoformat()
                except OSError:
                    mtime_fallback_date = today_iso

                for j in range(0, len(gnew_idx), 400):
                    if deadline is not None and time.time() > deadline:
                        break
                    chunk = gnew_idx[j:j + 400]
                    placeholders = ",".join("?" * len(chunk))
                    chunk_rows = []
                    try:
                        con = sqlite3.connect(f"file:{urllib.parse.quote(str(db_path))}?mode=ro", uri=True)
                        con.execute("PRAGMA busy_timeout = 3000")
                        chunk_rows = con.execute(
                            f"SELECT idx, data FROM gen_metadata WHERE idx IN ({placeholders})", chunk
                        ).fetchall()
                    except sqlite3.Error:
                        db_ok = False  # couldn't read -> never memo a possibly-partial scan
                    finally:
                        try: con.close()
                        except Exception: pass

                    for idx, blob in chunk_rows:
                        if not isinstance(blob, (bytes, bytearray)):
                            continue
                        # Use _pb_generations to extract per-generation usage WITH embedded timestamps
                        # (the accuracy fix: date by embedded secs, not file mtime).
                        gens = _pb_generations(bytes(blob))
                        if not gens:
                            # Fall back to _pb_find_usage for blobs _pb_generations can't parse.
                            found = _pb_find_usage(bytes(blob), marker=26)
                            if not found:
                                continue
                            # Use today_iso for new records in a known stem (the stem already has
                            # "@" entries from a prior scan so this blob is genuinely new); use
                            # mtime_fallback_date for first-scan backlog ingests (mirrors the
                            # at_stems backfill logic for the _pb_generations path below).
                            gen_date = today_iso if stem in at_stems else mtime_fallback_date
                            if gen_date >= cutoff:
                                entries[f"{stem}@{idx}"] = {
                                    "d": gen_date,
                                    "u": sum(vd.get(2, 0) for vd in found),
                                    "c": sum(vd.get(5, 0) for vd in found),
                                    "o": sum(vd.get(3, 0) for vd in found),
                                    "me": _dominant_enum(found),
                                }
                                changed = True
                            continue
                        for gi, gen in enumerate(gens):
                            gen_date = _local_iso(gen["secs"])
                            if gen_date < cutoff:
                                continue  # skip generations already past the prune horizon
                            key = f"{stem}@{idx}.{gi}"
                            if key in entries:
                                continue
                            entries[key] = {
                                "d": gen_date,
                                "u": gen["u"],
                                "c": gen["c"],
                                "o": gen["o"],
                                "me": gen["me"],
                            }
                            changed = True

                # Both passes completed cleanly within budget -> memo this content state.
                if db_ok and sig and (deadline is None or time.time() <= deadline):
                    seen_memo[stem] = sig

        # Persist the scan memo (change-gated like rpcWatermarks). After a COMPLETE scan,
        # seen_memo is authoritative (memos of deleted DBs and RPC-owned stems drop out);
        # after a deadline-cut partial scan, carry unvisited DBs' old memos forward — their
        # signatures still describe their last complete scan, and dropping them would force
        # a pointless rescan AND churn a ledger write on every partial run.
        scan_complete = deadline is None or time.time() <= deadline
        new_memo = seen_memo if scan_complete else {**old_memo, **seen_memo}
        if new_memo != old_memo:
            ledger["dbScanned"] = new_memo
            changed = True

        # Prune persistent ledger entries older than 35 days (``cutoff`` computed above,
        # before the scan — the scan skips backlogs already past it).
        for key in list(entries.keys()):
            e = entries[key]
            if e.get("d", "") < cutoff:
                del entries[key]
                changed = True

        # Merge + prune watermarks.  Run AFTER the entry prune so ``stems_with_entries``
        # reflects the post-prune set.  Prune rule: drop marks for cascades that are neither
        # live this run (not in ``live_marks``) nor have persisted ``#`` entries — so stale
        # marks for long-gone conversations don't accumulate forever.  Marks for entry-bearing
        # cascades survive the LS going down (one-LS-restart grace), and die naturally once
        # their entries age past 35 days.  The ``new_marks != marks`` gate is load-bearing —
        # don't drop it: without it every refresh pays the double atomic write (main + .bak)
        # even when nothing changed.  All watermark mutation is inside the flock'd section;
        # the flock-contention early-return (above) skips this block entirely.
        stems_with_entries = {k.rsplit("#", 1)[0] for k in entries if "#" in k}
        new_marks = {k: v for k, v in marks.items() if k in stems_with_entries}
        new_marks.update(live_marks)
        if new_marks != marks:
            ledger["rpcWatermarks"] = new_marks
            changed = True

        # Learned model display names (additive, change-gated like rpcWatermarks/dbScanned):
        # GetAvailableModels' displayName is the exact picker wording for a model enum this
        # codebase hasn't been hand-taught yet (_MODEL_ENUM_NAMES) — remembering it here means
        # a disk-scan entry with that enum heals from "Model M<N>" to the real name on the very
        # next refresh after Antigravity was open, with no code update required. Once learned,
        # a name is never pruned (unlike watermarks/scan-memos, there's no staleness concern for
        # a handful of small strings); a later run CAN overwrite one if the picker's wording
        # itself changes. Placeholder-string keys are converted to the same enum space
        # _MODEL_ENUM_NAMES uses (1000 + N) so both sources are looked up identically.
        old_learned = ledger.get("learnedModelNames")
        if not isinstance(old_learned, dict):
            old_learned = {}
        new_learned = dict(old_learned)
        for placeholder, display_name in learned_placeholders.items():
            enum = _placeholder_enum(placeholder)
            if enum is not None and display_name:
                new_learned[str(enum)] = display_name
        if new_learned != old_learned:
            ledger["learnedModelNames"] = new_learned
            changed = True

        if changed:
            _save_antigravity_ledger(ledger)

        return ledger
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


# Per-MTok prices for models the LiteLLM catalog doesn't carry. ESTIMATES — tune as needed.
# gpt-oss is an open-weights model whose hosted rates are far below the Pro fallback, so without
# this it would be badly overstated. Keys are normalised (lowercase, hyphenated) pricing keys.
_EXTRA_MODEL_PRICES: dict[str, dict[str, float]] = {
    "gpt-oss-120b": {"input": 0.15, "cache_read": 0.075, "output": 0.60},
    "gpt-oss-20b": {"input": 0.05, "cache_read": 0.025, "output": 0.20},
    "gpt-oss": {"input": 0.15, "cache_read": 0.075, "output": 0.60},
    # Google's published rate (2026-07): $1.50 in / $7.50 out per MTok; cache read at
    # the standard Gemini 10%-of-input discount. LiteLLM has no gemini-3.6 key yet —
    # _prices_for tries the live catalog first, so this estimate self-heals to the
    # catalog rate the moment LiteLLM adds one. Without it, 3.6 Flash usage billed
    # at the Gemini 3.1 Pro default ($2/$12) — ~60% high on output.
    "gemini-3.6-flash": {"input": 1.50, "cache_read": 0.15, "output": 7.50},
}

# Desktop/IDE ledger entries identify the model only by an integer enum (protobuf field 1).
# Mapping derived by correlating the enum with co-located model strings in the gen_metadata blobs
# (gen_metadata .1.19) and the language-server RPC's `model`/`apiProvider`. The RPC reports the same
# model as MODEL_PLACEHOLDER_M<N> where enum == 1000 + N (M133->1133, M26->1026, M16->1016).
# Unmapped enums fall through to the Gemini 3.1 Pro default. Extend as new enums are observed.
def _placeholder_enum(placeholder: str) -> "int | None":
    """``MODEL_PLACEHOLDER_M<N>`` -> protobuf model enum ``1000 + N`` (None if unparseable)."""
    if "_M" in str(placeholder):
        try:
            return 1000 + int(str(placeholder).rsplit("_M", 1)[1])
        except ValueError:
            return None
    return None


# EXACT Antigravity model-picker display names, verified live via the language server's
# GetAvailableModels RPC (2026-06-09) — displayName + MODEL_PLACEHOLDER_M<N> pairs. The
# breakdown shows these verbatim; pricing strips the effort parenthetical and normalizes
# (_normalize_model_name), so "Gemini 3.5 Flash (High)" still prices as gemini-3.5-flash.
# NOTE: there is NO "Gemini 3 Pro" in the picker — enum 1016 (M16) is Gemini 3.1 Pro
# (High); the old "gemini-3-pro" name was our mislabel (ledger migrated 2026-06-09).
# Legacy ledger model strings -> exact picker names. Entries written before 2026-06-09
# stored normalized pricing keys (and "gemini-3-pro" was an outright mislabel of M16 =
# Gemini 3.1 Pro (High) — the picker has no "Gemini 3 Pro"). Applied on every ledger
# update so old ledgers / .bak restores / racing writers all converge; me-bearing
# entries would resolve via the enum map anyway, this also fixes the me-less ones.
# NOT here: "gemini-3.1-pro" — it is the CURRENT unknown-Gemini fallback string
# (_rpc_model_pricing_key), so migrating it would morph honest unknowns into a
# specific effort claim; its old enum-1036 entries resolve via "me" regardless.
# ("claude-opus-4-6" doubles as the unknown-Anthropic fallback, but every variant
# groups/prices as Claude either way, and the mapping has real pre-06-09 targets.)
_LEGACY_MODEL_STRINGS: dict[str, str] = {
    "gemini-3-pro": "Gemini 3.1 Pro (High)",
    "gemini-3.5-flash": "Gemini 3.5 Flash",
    "gemini-3-flash": "Gemini 3 Flash",
    "claude-opus-4-6": "Claude Opus 4.6 (Thinking)",
}

_MODEL_ENUM_NAMES: dict[int, str] = {
    # Sub-1000 enums: raw protobuf model ids from older / utility API paths (not the
    # 1000+N MODEL_PLACEHOLDER_M<N> convention). 330 is observed as a flash-lite-tier
    # utility model (lighter than Flash, no effort variants); named to mirror the
    # flash-lite naming used for 1050 so the breakdown groups them consistently.
    330:  "Gemini Flash Lite",
    1016: "Gemini 3.1 Pro (High)",
    1018: "Gemini 3 Flash",
    1020: "Gemini 3.5 Flash (Medium)",
    1021: "Gemini 3.1 Flash Image",
    1026: "Claude Opus 4.6 (Thinking)",
    1035: "Claude Sonnet 4.6 (Thinking)",
    1036: "Gemini 3.1 Pro (Low)",
    1037: "Gemini 3.1 Pro (High)",
    1050: "Gemini 3.1 Flash Lite",
    1132: "Gemini 3.5 Flash (High)",
    1133: "Gemini 3.5 Flash",  # retired pre-effort-split 3.5 Flash id; effort unknowable
    1187: "Gemini 3.5 Flash (Low)",
    # Gemini 3.6 Flash effort trio (M264/M265/M266), confirmed live via
    # GetAvailableModels 2026-07 (matches the ledger's learnedModelNames). Static
    # entries make _prices_for/_rpc_model_pricing_key resolve them even for the
    # me-only disk entries captured before the names were learned.
    1264: "Gemini 3.6 Flash (High)",
    1265: "Gemini 3.6 Flash (Medium)",
    1266: "Gemini 3.6 Flash (Low)",
}


def _resolve_model_display(e: dict[str, Any], learned: "dict[int, str] | None" = None) -> str:
    """Best-effort DISPLAY name for a ledger entry (pricing is resolved separately
    by _prices_for and is unaffected by this).

    Resolution order: the static, hand-curated ``_MODEL_ENUM_NAMES`` table first;
    then ``learned`` (names auto-captured live from GetAvailableModels and persisted
    as the ledger's ``learnedModelNames`` — see ``update_antigravity_token_ledger``);
    then a live-captured display-name string already stored on the entry itself
    (CLI/RPC entries carry one). If none of those resolve but "me" still fits the
    MODEL_PLACEHOLDER_M<N> convention (enum = 1000 + N) this codebase uses
    everywhere else, return a self-documenting "Model M<N>" placeholder instead of
    an opaque "Other" — so a model Antigravity has started using ahead of both the
    static table AND a live-learned name is still identifiable rather than silently
    disappearing into the generic bucket. Only a genuinely unresolvable entry (no
    "me", or an "me" outside that convention — e.g. a legacy pre-2026-06 raw
    protobuf enum) falls all the way to "Unknown".
    """
    me = e.get("me")
    # Both tables are int-keyed (the ledger persists learnedModelNames with JSON
    # string keys and the caller converts them back), so a non-int "me" simply
    # misses both — same outcome as the old unguarded .get, now stated outright.
    me_key = me if isinstance(me, int) else None
    name = None
    if me_key is not None:
        name = _MODEL_ENUM_NAMES.get(me_key) or (learned or {}).get(me_key)
    name = name or e.get("model")
    if name:
        return name
    if isinstance(me, int) and me >= 1000:
        return f"Model M{me - 1000}"
    return "Unknown"


def _rpc_model_pricing_key(placeholder: str, api_provider: str) -> str:
    """Resolve a language-server RPC generation's pricing key from its
    ``MODEL_PLACEHOLDER_M<N>`` id and ``apiProvider``.

    The placeholder number maps to the protobuf model enum as ``enum = 1000 + N``,
    so a known placeholder resolves through ``_MODEL_ENUM_NAMES``. When the
    placeholder is new/unknown, fall back by provider family so the model is still
    priced in the right ballpark (and is never silently treated as Gemini Pro when
    it is actually Claude/GPT). Extend ``_MODEL_ENUM_NAMES`` as new ids appear.
    """
    enum = _placeholder_enum(placeholder)
    if enum is not None and enum in _MODEL_ENUM_NAMES:
        return _MODEL_ENUM_NAMES[enum]
    ap = str(api_provider).upper()
    if "ANTHROPIC" in ap:
        return "claude-opus-4-6"
    if "OPENAI" in ap or "GPT" in ap:
        return "gpt-oss"
    # "Gemini 3 Pro" never existed in the picker — the honest unknown-Gemini ballpark
    # is the Pro family key (prices as Pro, displays as "Gemini 3.1 Pro").
    return "gemini-3.1-pro"


@functools.lru_cache(maxsize=128)
def _normalize_model_name(name: str) -> str:
    """Map a CLI statusLine model *display* name to a pricing-catalog key.

    "Gemini 3.5 Flash (High)" -> "gemini-3.5-flash"; "Gemini 3.1 Pro (High)" -> "gemini-3.1-pro".
    Drops the reasoning-effort parenthetical, lowercases, and hyphenates spaces.

    Memoized via lru_cache: called per ledger entry during cost calculations;
    caching eliminates redundant regex operations across thousands of entries.
    """
    s = re.sub(r"\(.*?\)", "", str(name)).strip().lower()
    return re.sub(r"\s+", "-", s)


def _model_family(name: Any) -> str:
    """Collapse a resolved model name to its breakdown DISPLAY family.

    The cost breakdown and per-bucket tooltips group effort/thinking variants
    together: "Gemini 3.1 Pro (High)" and "(Low)" both read "Gemini 3.1 Pro",
    every Claude variant reads "Claude", every GPT/OpenAI variant reads "GPT".
    Pricing is unaffected — cost_of/_prices_for resolve the exact per-entry
    name BEFORE this grouping, so Opus vs Sonnet (etc.) still bill correctly.
    """
    s = re.sub(r"\(.*?\)", "", str(name or "")).strip()
    low = s.lower()
    if not s or low == "unknown":
        return "Unknown"
    if "claude" in low:
        return "Claude"
    if "gpt" in low or "openai" in low:
        return "GPT"
    if "gemini" in low:
        # Hyphenated pricing-key fallbacks read like the picker: "gemini-3.1-pro"
        # -> "Gemini 3.1 Pro" (version dots survive, words title-case).
        words = low.replace("-", " ").split()
        return " ".join(w if w[0].isdigit() else w.capitalize() for w in words)
    return s


def antigravity_ledger_cost_summary(now: dt.datetime | None = None, deadline: float | None = None) -> dict[str, str] | None:
    # NOTE: no `tier` parameter — Antigravity ledger costing is per-entry per-model
    # (see _prices_for) and tier-independent, so the caller's tier never affected the
    # result. The dead arg was dropped to stop implying tier-aware pricing here.
    ledger = update_antigravity_token_ledger(now, deadline)
    # Live-learned model names (see update_antigravity_token_ledger) — keys are the enum-as-str
    # form persisted to JSON; convert back to int to match _MODEL_ENUM_NAMES / entry["me"].
    # Malformed keys (should never happen — we write them ourselves) are skipped, not fatal.
    learned_raw = ledger.get("learnedModelNames")
    learned_names: dict[int, str] = {}
    if isinstance(learned_raw, dict):
        for k, v in learned_raw.items():
            try:
                learned_names[int(k)] = str(v)
            except (TypeError, ValueError):
                continue
    # Merge both local Antigravity token sources: the IDE/desktop-app/CLI trajectory ledger and
    # the Antigravity CLI (agy) statusLine push. Key namespaces are disjoint ("<db_stem>:<idx>" vs
    # "cli:<session>"), so a plain dict merge is a safe union — neither source clobbers the other.
    # BUT a statusLine entry is the session's CUMULATIVE total: once the same session is covered
    # per-generation by the ledger (its trajectory DB got scanned, or a live agy answered the
    # RPC — the CLI writes plaintext trajectory DBs since 2026-06-02), keeping the "cli:" entry
    # would count the whole session twice. Ledger coverage supersedes; statusLine remains the
    # only record for the encrypted pre-06-02 .pb era and any session whose DB never appears.
    ledger_entries = ledger.get("entries", {})
    covered_stems = {k.rsplit(sep, 1)[0] for k in ledger_entries
                     for sep in ("#", "@", ":") if sep in k}
    cli = _load_usage_file(ANTIGRAVITY_CLI_USAGE_PATH)
    cli_entries = {k: v for k, v in cli.get("entries", {}).items()
                   if not (k.startswith("cli:") and k[4:] in covered_stems)}
    entries = {**ledger_entries, **cli_entries}
    if not entries:
        return None
    current = now.astimezone() if now is not None else dt.datetime.now(dt.timezone.utc).astimezone()
    today_iso = current.date().isoformat()
    m30_iso = (current - dt.timedelta(days=30)).date().isoformat()
    # Per-entry pricing. IDE/desktop trajectory entries carry no model id — price them at Gemini
    # 3.1 Pro (the model that desktop usage actually runs on). CLI entries carry a "model" display
    # name (e.g. "Gemini 3.5 Flash (High)") which we normalise to a pricing key, so Flash/Claude/etc.
    # CLI sessions are priced at their real rate instead of the Pro default.
    default_prices = model_pricing("gemini-3.1-pro")
    _price_cache: dict[str, dict[str, Any]] = {}

    def _prices_for(e: dict[str, Any]) -> dict[str, Any]:
        # Enum map FIRST: it's the freshest knowledge. An RPC entry whose placeholder was
        # unknown at capture stores a family-fallback "model" string plus the raw "me"
        # enum — once the enum gets mapped, pricing/attribution heal retroactively.
        # CLI entries (display-name string, no "me") and disk entries ("me" only) are
        # unaffected by the order.
        me_key = e.get("me")
        m = _MODEL_ENUM_NAMES.get(me_key) if isinstance(me_key, int) else None
        if not m:
            m = e.get("model")  # CLI entries carry a display-name string
        if not m:
            return default_prices
        key = _normalize_model_name(m)
        if key not in _price_cache:
            # Try the key as-is, then with version dots -> hyphens. Families differ: Gemini keeps
            # the dot ("gemini-3.5-flash") while Claude uses hyphens ("claude-opus-4-6"), so a single
            # spelling can't match both. First candidate with a real input rate wins; else default.
            chosen = default_prices
            for cand in (key, key.replace(".", "-")):
                p = model_pricing(cand)
                if p.get("input"):
                    chosen = p
                    break
                if cand in _EXTRA_MODEL_PRICES:  # catalog miss -> our estimate (e.g. gpt-oss)
                    chosen = _EXTRA_MODEL_PRICES[cand]
                    break
            _price_cache[key] = chosen
        return _price_cache[key]

    def cost_of(e: dict[str, int]) -> float:
        p = _prices_for(e)
        in_rate = p.get("input", 0.0) or 0.0
        cache_rate = p.get("cache_read", 0.0) or 0.0
        out_rate = p.get("output", 0.0) or 0.0
        # "o" is the TOTAL output (thinking + response). The desktop ledger's "t"/"x"
        # (thinking / response output) are SUBSETS of "o", not additive components —
        # verified o == t + x across every ledger entry and every GetCascadeTrajectory-
        # GeneratorMetadata RPC record (outputTokens == thinkingOutputTokens +
        # responseOutputTokens). So bill "o" once at the output rate; the old
        # "o + t" (+ "x" at input rate) double-counted thinking and re-billed the
        # response. CLI entries already store o=total with t=x=0, so they were correct.
        #
        # "c" is billed as recorded on EVERY entry, whatever its key namespace. Each
        # ':' (disk steps), '@' (gen_metadata) and '#' (RPC) entry is one API call, and
        # each call pays for the cache it re-reads — so a conversation's cache cost is
        # the SUM over its calls, exactly as usage_cost_usd bills Claude/Codex/Gemini per
        # request. Do NOT collapse a conversation to its peak "c": the ':' and '#' records
        # of the same agy session carry identical per-call values (the RPC and the steps
        # scan agree to the digit), so any per-namespace deflation makes a conversation's
        # cost depend on which path captured it — and change when CLI ownership flips.
        # → tests/test_cli_usage.py::test_cache_read_billed_per_call_*
        return (e.get("u", 0) * in_rate + e.get("c", 0) * cache_rate
                + e.get("o", 0) * out_rate) / 1_000_000.0

    today_cost = month_cost = 0.0
    today_tok = month_tok = 0
    month_in = month_out = month_cached = 0
    model_costs: dict[str, dict[str, Any]] = {}  # per-model 30-day cost/tokens for the breakdown
    daily = empty_weekly_token_buckets(current)
    monthly_buckets = empty_monthly_token_buckets(current)
    hourly = empty_hourly_token_buckets(current)
    # Today's tokens/cost whose entry carries no local hour (disk first-seen / CLI / a
    # generation captured before hour-tracking and not yet re-seen by the RPC). Folded
    # into the current hour after the loop so the Day bars still sum to "Today".
    hourless_tok = 0
    hourless_cost = 0.0
    hourless_models: dict[str, dict[str, Any]] = {}
    # Display-name + family resolution memo. Only ~18 distinct (me, model) pairs exist
    # across the ~47k ledger entries, yet _resolve_model_display's lookup chain plus
    # _model_family's two regex subs ran per entry. Cache is keyed on the ONLY two entry
    # fields the resolution reads (_resolve_model_display uses e["me"]/e["model"]) and is
    # LOCAL to this call — a module-global would answer with a stale name after
    # learned_names changes between refreshes (learned_names is per-call input here).
    family_cache: dict[tuple[Any, Any], str] = {}
    for key, e in entries.items():
        d = e.get("d", "")
        cached = e.get("c", 0)
        c = cost_of(e)
        # Token VOLUME = total tokens processed = uncached input + cached re-read + output
        # (industry standard: volume includes cache reads; cost bills them discounted).
        tok = e.get("u", 0) + cached + e.get("o", 0)  # o = total output (see cost_of)
        # Enum map first (mirrors _prices_for): heals fallback-attributed entries
        # retroactively once a new placeholder enum gets mapped. Then collapse to the
        # display FAMILY — the single choke point that keeps the 30-day breakdown and
        # every per-bucket tooltip grouped the same way (pricing already happened above).
        fk = (e.get("me"), e.get("model"))
        mname = family_cache.get(fk)
        if mname is None:
            mname = _model_family(_resolve_model_display(e, learned=learned_names))
            family_cache[fk] = mname
        if d >= m30_iso:
            month_cost += c
            month_tok += tok
            # input lane = uncached input; output lane = total output; cached = cache reads
            month_in += e.get("u", 0)
            month_out += e.get("o", 0)
            month_cached += cached
            slot = model_costs.setdefault(str(mname), {"cost": 0.0, "tokens": 0})
            slot["cost"] += c
            slot["tokens"] += tok
        if d in daily:
            bucket_add(daily[d], tok, c, mname)
        if d in monthly_buckets and monthly_buckets[d].get("inMonth"):
            bucket_add(monthly_buckets[d], tok, c, mname)
        if d == today_iso:
            today_cost += c
            today_tok += tok
            h = e.get("h")
            if isinstance(h, int) and 0 <= h <= 23:
                bucket_add(hourly[h], tok, c, mname)
            else:
                hourless_tok += tok
                hourless_cost += c
                slot = hourless_models.setdefault(str(mname), {"cost": 0.0, "tokens": 0})
                slot["cost"] += c
                slot["tokens"] += tok
    if hourless_tok or hourless_cost:
        hnow = current.hour
        hourly[hnow]["tokens"] += hourless_tok
        hourly[hnow]["cost"] += hourless_cost
        for name, v in hourless_models.items():
            slot = hourly[hnow]["models"].setdefault(name, {"cost": 0.0, "tokens": 0})
            slot["cost"] += v["cost"]
            slot["tokens"] += v["tokens"]
    # "Last 7 days" = the daily buckets summed, so it matches the Week graph's bars exactly.
    week_tok = sum(b["tokens"] for b in daily.values())
    week_cost = sum(b["cost"] for b in daily.values())
    burn_rate = week_cost / trailing_week_days()  # trailing-7-day average $/day (unbiased)
    result = {
        "title": "Cost (if pay-per-use)",
        "today": f"Today: {exact_usd(today_cost)} · {compact_token_count(today_tok)} tok",
        "last7Days": f"Last 7 days: {exact_usd(week_cost)} · {compact_token_count(week_tok)} tok",
        "last30Days": f"Last 30 days: {exact_usd(month_cost)} · {compact_token_count(month_tok)} tok",
        "source": "antigravity-token-ledger",
        "weeklyTokenUsage": weekly_token_usage(daily),
        "monthlyTokenUsage": monthly_token_usage(monthly_buckets),
        "hourlyTokenUsage": hourly_token_usage(hourly),
        # Raw numeric fields (mirror accounting.token_summary) for the panel display
        # modes, burn-rate line, and `--cost` export.
        "costToday": round(today_cost, 6),
        "cost7d": round(week_cost, 6),
        "cost30d": round(month_cost, 6),
        "tokensToday": int(today_tok),
        "tokens7d": int(week_tok),
        "tokens30d": int(month_tok),
        "burnRatePerDay": round(burn_rate, 6),
        "projectedMonthlyCost": round(burn_rate * 30.0, 6),
        "modelBreakdown": model_breakdown_rows(model_costs),
    }
    line = cost_breakdown_line(month_in, month_out, month_cached)
    if line:
        result["breakdown"] = line
    return result


# ---------------------------------------------------------------------------
# Monthly cost-rollup archive
# ---------------------------------------------------------------------------

def _load_cost_archive() -> dict[str, Any]:
    """Load the append-only cost archive, recovering from ``.bak`` on failure and
    quarantining a corrupt main as ``.corrupt`` — the same never-load-into-empty discipline
    as the Antigravity ledger. A transient failure must not silently drop frozen history."""
    main = COST_ARCHIVE_PATH
    bak = main.with_name(main.name + ".bak")
    for idx, path in enumerate((main, bak)):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("months"), dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
        if idx == 0:
            try:
                main.replace(main.with_name(main.name + ".corrupt"))
            except OSError:
                pass
    return {"version": 1, "months": {}}


def _save_cost_archive(archive: dict[str, Any]) -> None:
    """Atomic 0600 write + ``.bak`` mirror via the shared io_helpers recipe (mirrors
    ``_save_antigravity_ledger``)."""
    payload = json.dumps(archive)
    try:
        atomic_write_text(COST_ARCHIVE_PATH, payload)
    except OSError:
        return
    try:
        atomic_write_text(COST_ARCHIVE_PATH.with_name(COST_ARCHIVE_PATH.name + ".bak"), payload)
    except OSError:
        pass


def _summary_month_totals(summary: Any) -> dict[str, Any] | None:
    """Sum a provider cost summary's in-month buckets to ``{cost, tokens}``; ``None`` when
    the provider has no summary this run (so it isn't recorded as a zero)."""
    if not isinstance(summary, dict):
        return None
    cost = 0.0
    tokens = 0
    for b in summary.get("monthlyTokenUsage") or []:
        if isinstance(b, dict) and b.get("inMonth"):
            try:
                cost += float(b.get("cost") or 0.0)
            except (TypeError, ValueError):
                pass
            try:
                tokens += int(b.get("tokens") or 0)
            except (TypeError, ValueError):
                pass
    return {"cost": round(cost, 6), "tokens": int(tokens)}


def update_cost_archive(summaries: dict[str, Any] | None,
                        now: dt.datetime | None = None,
                        deadline: float | None = None) -> None:
    """Upsert the CURRENT month's per-provider totals into the append-only archive.

    Past months freeze once present (we only ever rewrite ``months[<current>]``), so the
    35-day Antigravity prune can't erode them. Change-gated exactly like the ledger's
    ``new_marks != marks`` gate — when the recomputed current-month dict equals what's
    stored, we skip the write entirely (no double atomic write per refresh). The first
    month the archive ever records is flagged ``"partial": true`` (a mid-month install has
    incomplete data for that month). Best-effort: enrichment must never break the snapshot,
    so callers wrap this in try/except; here we also bound the flock by the cost-scan deadline.
    """
    import fcntl
    import time as _time

    current = now.astimezone() if now is not None else dt.datetime.now(dt.timezone.utc).astimezone()
    month_key = current.strftime("%Y-%m")
    cur: dict[str, Any] = {}
    for prov, summary in (summaries or {}).items():
        tot = _summary_month_totals(summary)
        if tot is not None:
            cur[prov] = tot

    try:
        COST_ARCHIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
        COST_ARCHIVE_PATH.parent.chmod(0o700)
    except OSError:
        return
    lock_path = COST_ARCHIVE_PATH.with_suffix(".lock")
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    # Bound the wait by the cost-scan deadline (short default if none) — same daemon-thread
    # reasoning as the ledger lock: never park on a held lock past our budget.
    lock_budget = max(0.0, min((deadline - _time.time()) if deadline is not None else 2.0, 5.0))
    if not flock_with_timeout(lock_fd, lock_budget):
        os.close(lock_fd)
        return
    try:
        archive = _load_cost_archive()
        months = archive.setdefault("months", {})
        existing = months.get(month_key)
        target: dict[str, Any] = dict(cur)
        # Partial-month flag: set on the very first month the archive records, and preserved
        # thereafter for that month (a mid-month install never becomes "complete").
        if not months or (isinstance(existing, dict) and existing.get("partial")):
            target["partial"] = True
        if existing == target:
            return  # change-gate: nothing changed for the current month
        months[month_key] = target
        _save_cost_archive(archive)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def compute_local_cost_summaries(deadline: float | None = None) -> dict[str, dict[str, Any] | None]:
    """Compute every provider's local cost summary from disk — the slow half of the cost
    scan, deliberately split out so ``build_snapshot`` can run it CONCURRENTLY with the
    network provider fetches (it reads only ~/.claude, ~/.codex, ~/.gemini and the
    Antigravity ledger — never the live ``providers`` dict, so it needs no provider results
    and can start before they exist). ``tier`` is a dead passthrough in ``token_summary``
    (it stores no tier field), so omitting it here changes nothing. Returns a fresh
    ``{provider: summary|None}`` map for ``apply_cost_summaries`` to merge once the fetches
    finish — keeping the summaries OUT of ``providers`` until then is what makes a scan
    timeout a clean no-op (no torn write to roll back). The ``deadline`` is threaded into
    the Antigravity ledger's per-DB scan loop exactly as before, AND into each local-log
    summarizer's file walk (accounting._cached_log_records) — a slow cold parse checks in
    against the same budget instead of running unbounded and self-perpetuating across
    refreshes with no incremental checkpoint."""
    from accounting import (local_claude_token_summary, local_codex_token_summary,
                            local_gemini_token_summary, local_grok_token_summary)
    from .grok import grok_billing_period
    # Grok billing period: read the current billing week from the unified log so
    # local_grok_token_summary can accumulate billingWeekTokens/billingWeekCost.
    # Best-effort — an exception (e.g. disk error) must never block the rest of the scan.
    grok_week_start = grok_week_end = None
    try:
        bp = grok_billing_period()
        if bp is not None:
            grok_week_start, grok_week_end = bp
    except Exception:
        pass
    summaries: dict[str, dict[str, Any] | None] = {
        "codex": local_codex_token_summary(deadline=deadline),
        "claude": local_claude_token_summary(deadline=deadline),
        "gemini": local_gemini_token_summary(deadline=deadline),
        # Grok Build CLI: REAL per-turn tokens parsed from ~/.grok/logs/unified.jsonl.
        # None when there's no local Grok CLI data, so the tab stays hidden.
        "grok": local_grok_token_summary(
            deadline=deadline,
            week_start=grok_week_start,
            week_end=grok_week_end,
        ),
        # Antigravity: REAL per-generation tokens from the persistent ledger (IDE/desktop
        # trajectory DBs + the agy CLI statusLine push). None when there's no local token
        # data, so the cost section stays off rather than showing the subscription price.
        "antigravity": antigravity_ledger_cost_summary(deadline=deadline),
    }
    # Roll the current month's per-provider totals into the append-only archive.
    # Best-effort — enrichment must never break the snapshot.
    try:
        update_cost_archive(summaries, deadline=deadline)
    except Exception:
        pass
    return summaries


def apply_cost_summaries(providers: dict[str, dict[str, Any]],
                         summaries: dict[str, dict[str, Any] | None]) -> None:
    """Merge precomputed cost summaries into ``providers`` — the fast, provider-DEPENDENT
    half of the cost scan. Runs only AFTER the provider fetches finish (it reads the
    Antigravity tier) and AFTER a clean await of the concurrent scan (so a timeout skips
    it entirely, leaving the snapshot untouched)."""
    # Gemini and Antigravity share one Google AI subscription; only Antigravity's
    # userTier exposes the exact tier (Ultra/Pro/Free). Propagate it to Gemini.
    google_tier = google_ai_subscription_tier(providers)
    if google_tier and (providers.get("gemini") or {}).get("status") == "ok":
        providers["gemini"]["tier"] = google_tier

    for provider, label, source in (
        ("codex", "Codex", "json-rpc"),
        ("claude", "Claude", "browser-api"),
        ("gemini", "Gemini", "gemini-web"),
        # Grok is local-only this pass (no live fetch); setdefault creates the provider
        # entry from its costSummary so the tab appears whenever ~/.grok logs exist.
        ("grok", "Grok", "local-grok-logs"),
    ):
        summary = (summaries or {}).get(provider)
        if summary is not None:
            providers.setdefault(provider, default_provider(label, source)).setdefault(
                "costSummary",
                summary,
            )

    # We no longer gate Antigravity on the provider being "ok": CLI token data can exist
    # while the IDE is closed (status != ok), and that usage should still surface.
    ag_provider = providers.get("antigravity")
    if ag_provider is not None and "costSummary" not in ag_provider:
        ledger_summary = (summaries or {}).get("antigravity")
        if ledger_summary is not None:
            ag_provider["costSummary"] = ledger_summary

    # Gemini: the cost section is pay-per-use only (from local gemini-cli token logs).
    # gemini.google.com isn't token-metered, so when there are no local logs we show no
    # cost section — deliberately NOT the monthly-subscription price.


def apply_local_cost_summaries(providers: dict[str, dict[str, Any]], deadline: float | None = None) -> None:
    """Compute + merge the local cost summaries in one synchronous call (the original
    sequential API, kept for direct callers/tests). ``build_snapshot`` instead calls the
    two halves separately so the compute can overlap the network fetches."""
    apply_cost_summaries(providers, compute_local_cost_summaries(deadline))

