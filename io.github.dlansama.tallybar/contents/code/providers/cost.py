"""Cost & token-ledger aggregation for the providers package.

Not a fetch path: this module folds local token usage into provider cost
summaries. It owns ``compute_local_cost_summaries`` / ``apply_cost_summaries``,
``update_cost_archive``, and the whole Antigravity token-capture pipeline —
``update_antigravity_token_ledger`` (persists new generations to
``~/.tallybar/antigravity_token_ledger.json``; its only network I/O is the
loopback RPC to the local language server) and ``antigravity_ledger_cost_summary``
(prices each ledger entry per model).
"""
from __future__ import annotations

import datetime as dt
import functools
import json
import os
import re
import sqlite3
import time
import urllib.parse
from pathlib import Path
from typing import Any

from accounting import (
    UsageSplit,
    breakdown_from_split,
    compact_token_count,
    cost_breakdown_line,
    default_provider,
    bucket_add,
    buckets_model_costs,
    empty_hourly_token_buckets,
    empty_monthly_token_buckets,
    empty_type_split,
    empty_weekly_token_buckets,
    exact_usd,
    hourly_token_usage,
    model_breakdown_rows,
    model_pricing,
    monthly_token_usage,
    prompt_tier_prices,
    trailing_week_days,
    type_breakdown,
    type_split_add,
    weekly_token_usage,
)
from io_helpers import (
    deadline_passed,
    flock_with_timeout,
    load_json_with_backup,
    save_json_with_backup,
)


ANTIGRAVITY_CONVERSATION_DIRS = (
    Path.home() / ".gemini" / "antigravity" / "conversations",
    Path.home() / ".gemini" / "antigravity-ide" / "conversations",
    # The Antigravity CLI (agy): plaintext trajectory DBs (its older .pb history is
    # encrypted and unrecoverable). A hybrid — see the antigravity-cli gate in the gen pass.
    Path.home() / ".gemini" / "antigravity-cli" / "conversations",
)
ANTIGRAVITY_LEDGER_PATH = Path.home() / ".tallybar" / "antigravity_token_ledger.json"
# Token usage logged by the Antigravity CLI (agy/gemini) via its statusLine push — written by
# integrations/antigravity_cli/cli_statusline_capture.py. Same {trackingStarted, entries} shape as
# the IDE ledger; keys namespaced "cli:<session>" so they never collide with the IDE's
# "<db_stem>:<idx>" keys when the two are merged for costing.
ANTIGRAVITY_CLI_USAGE_PATH = Path.home() / ".tallybar" / "antigravity_cli_usage.json"
# Append-only per-month per-provider cost/token rollup. Survives the Antigravity
# ledger's retention prune — only Antigravity is lossy; the other providers' parse caches
# are all-time. Past months freeze once written so the prune can't erode them.
COST_ARCHIVE_PATH = Path.home() / ".tallybar" / "cost_archive.json"
# Bump when the disk-scan dating logic changes in a way that needs existing entries rebuilt.
# On a version mismatch the per-DB scan memo (``dbScanned``) is cleared so every conversation
# is re-scanned once.
ANTIGRAVITY_DISK_DATING_VERSION = 3
# How long a ledger entry is kept; older ones are pruned, and DBs past it are not ingested.
ANTIGRAVITY_RETENTION_DAYS = 35


# ---------------------------------------------------------------------------
# Google AI subscription tier (shared by Gemini + Antigravity)
# ---------------------------------------------------------------------------

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
    _dedupe_usage_records,
    _dominant_enum,
    _pb_fields,
    _pb_find_usage,
    _pb_generations,
    _pb_read_varint,
    USAGE_CACHED,
    USAGE_INPUT,
    USAGE_MODEL,
    USAGE_OUTPUT,
    USAGE_RESPONSE,
    USAGE_THOUGHTS,
)


def _local_iso(secs: int) -> str:
    """Local-time ``YYYY-MM-DD`` for a unix timestamp (seconds). Used to date disk-scanned
    generations by their embedded protobuf timestamp."""
    return dt.datetime.fromtimestamp(secs).date().isoformat()


def _load_antigravity_ledger() -> dict[str, Any]:
    """Load the persistent token ledger, recovering from the last-good ``.bak`` on failure.

    ``_save_antigravity_ledger`` mirrors every good write to a ``.bak``; if the main file is
    missing or corrupt we recover from it — and quarantine the corrupt main as ``.corrupt``
    for forensics — rather than returning an empty ledger.
    """
    return load_json_with_backup(
        ANTIGRAVITY_LEDGER_PATH,
        lambda data: isinstance(data, dict) and isinstance(data.get("entries"), dict),
        {"trackingStarted": None, "entries": {}},
    )


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
    save_json_with_backup(ANTIGRAVITY_LEDGER_PATH, ledger)


def _lock_budget(deadline: float | None) -> float:
    """Seconds a ledger/archive flock may wait: the time left to the cost-scan deadline
    (2s when there is none), capped at 5s."""
    return max(0.0, min((deadline - time.time()) if deadline is not None else 2.0, 5.0))


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

    Each entry is keyed per generation (dedup-safe), so re-scanning never double-counts. RPC entries
    are dated by ``createdAt``; steps-pass entries get today for a stem already holding ``:`` entries,
    else the DB mtime; the ``gen_metadata`` pass (``<stem>@<idx>.<gi>``) uses embedded protobuf timestamps.

    An exclusive file lock (fcntl.flock) protects the read-modify-write cycle against concurrent
    executions (e.g. overlapping widget refreshes).
    """
    import fcntl

    current = now.astimezone() if now is not None else dt.datetime.now(dt.timezone.utc).astimezone()
    today_iso = current.date().isoformat()

    ANTIGRAVITY_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        ANTIGRAVITY_LEDGER_PATH.parent.chmod(0o700)
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
    lock_budget = _lock_budget(deadline)
    if not flock_with_timeout(lock_fd, lock_budget):
        os.close(lock_fd)
        return _load_antigravity_ledger()
    try:
        ledger = _load_antigravity_ledger()
        if ledger.get("trackingStarted") is None:
            ledger["trackingStarted"] = today_iso
        entries = ledger["entries"]
        changed = False

        # A stale disk-dating version clears the scan memo (entries are kept). Only mark
        # changed when there was a memo to clear, so a fresh ledger doesn't force a write.
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
            # The RPC counts per generation, the disk per step, so fewer RPC records than disk
            # entries is normal: never gate this purge on a record-count comparison.
            prefix_len = len(cascade_id) + 1
            for key in [k for k in entries
                        if (k.startswith(f"{cascade_id}:") or k.startswith(f"{cascade_id}@"))
                        and (k[prefix_len:].isdigit()
                             or (k.startswith(f"{cascade_id}@")
                                 and re.fullmatch(r"\d+\.\d+", k[prefix_len:]))
                             )]:
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
                    # Re-resolve the model when the mapping has improved: a placeholder unknown
                    # at capture time fell back to a family default. Heal it in place (keeps
                    # date/hour/tokens — only the pricing/breakdown name changes).
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
                    entry["h"] = rec_hour 
                entries[key] = entry
                changed = True

        # 2) On-disk trajectory DBs — fallback for conversations the RPC didn't cover. Skip any
        # conversation the RPC owns now or owned on a prior run (its ``<id>#…`` entries persist).
        # rsplit, not split: keys are "<cascadeId>#<stepKey>" and stepKey never contains
        # '#', so the LAST '#' is always the real separator — rsplit recovers the full cascade id
        # even in the (pathological) case where a cascade id itself contains a '#'.
        rpc_stems = rpc_covered | {k.rsplit("#", 1)[0] for k in entries if "#" in k}
        # First-scan backdating gates, snapshotted once per run: a stem with NO prior entries
        # of a shape is a backlog ingest, dated to the DB file mtime (stamping ``today``
        # would inflate "Today"); once it has some, new records are genuinely new.
        colon_stems = {k.rsplit(":", 1)[0] for k in entries if ":" in k}
        at_stems = {k.rsplit("@", 1)[0] for k in entries if "@" in k}
        # Needed DURING the scan too: a backlog already past the horizon would be ingested
        # and pruned in the same run, then re-ingested on every refresh. Such DBs are skipped
        # outright; a stale DB that gets new writes moves inside the horizon.
        cutoff = (current.date() - dt.timedelta(days=ANTIGRAVITY_RETENTION_DAYS)).isoformat()
        # Per-DB scan memo (additive ledger key, rides the .bak mirror): skip BOTH passes
        # for a DB whose content signature (_db_signature: main + -wal mtime_ns/size)
        # matches its last COMPLETE scan (rows that never create entries can't be skipped
        # by the key-in-entries dedup). Fail-open by design: stat/sqlite errors, deadline expiry, and ownership flips
        # all leave the DB un-memoized, so the failure direction is always "rescan".
        old_memo = ledger.get("dbScanned")
        old_memo = old_memo if isinstance(old_memo, dict) else {}
        seen_memo: dict[str, str] = {}
        for conv_dir in ANTIGRAVITY_CONVERSATION_DIRS:
            if deadline_passed(deadline):
                break
            if not conv_dir.is_dir():
                continue
            for db_path in conv_dir.glob("*.db"):
                if deadline_passed(deadline):
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
                    continue
                db_ok = True   # any sqlite failure below blocks this run's memo
                # Backlog dating for the steps pass (mirrors the gen pass below).
                if stem in colon_stems:
                    steps_date = today_iso
                else:
                    try:
                        steps_date = dt.date.fromtimestamp(db_path.stat().st_mtime).isoformat()
                    except OSError:
                        steps_date = today_iso
                    if steps_date < cutoff:
                        continue  # whole-DB backlog already past the prune horizon (see above)
                # SQLite read-lock discipline (CLAUDE.md "SQLite scans"): the widget polls DBs live
                # agents are writing, and a held read lock blocks their WAL checkpoint with
                # SQLITE_BUSY. Read indices only, drop seen keys, fetch ONLY unseen blobs, close
                # BEFORE parsing.
                new_idx = []
                try:
                    con = sqlite3.connect(f"file:{urllib.parse.quote(str(db_path))}?mode=ro", uri=True)
                    con.execute("PRAGMA busy_timeout = 3000")
                    new_idx = [i for (i,) in con.execute(
                        "SELECT idx FROM steps WHERE metadata IS NOT NULL")
                        if f"{stem}:{i}" not in entries]
                except sqlite3.Error:
                    db_ok = False
                finally:
                    try: con.close()
                    except Exception: pass

                for j in range(0, len(new_idx), 400):  # chunk to stay under SQLite's variable limit
                    if deadline_passed(deadline):
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

                    for idx, blob in chunk_rows:
                        if not isinstance(blob, (bytes, bytearray)):
                            continue
                        # A steps row can repeat one request's usage record (field 9 plus an
                        # exact copy at 28.2); dedupe first so it counts once and stays one call.
                        found = _dedupe_usage_records(_pb_find_usage(bytes(blob)))
                        if not found:
                            continue
                        entries[f"{stem}:{idx}"] = {
                            "d": steps_date,
                            "u": sum(vd.get(USAGE_INPUT, 0) for vd in found),
                            "c": sum(vd.get(USAGE_CACHED, 0) for vd in found),
                            "o": sum(vd.get(USAGE_OUTPUT, 0) for vd in found),
                            "t": sum(vd.get(USAGE_THOUGHTS, 0) for vd in found),
                            "x": sum(vd.get(USAGE_RESPONSE, 0) for vd in found),  # o = t + x
                            "me": _dominant_enum(found),  # model enum (for per-model pricing)
                        }
                        if len(found) > 1:
                            # Several distinct records summed into one entry: no single call's
                            # size is known, so the cost summary bills it at the base rates.
                            entries[f"{stem}:{idx}"]["n"] = len(found)
                        changed = True

                # CLI DBs: STOP after the steps pass (their gen_metadata rows are exact 2x
                # duplicates of the steps usage — see CLAUDE.md "Antigravity CLI (agy) capture").
                if conv_dir.parent.name == "antigravity-cli":
                    if db_ok and sig and not deadline_passed(deadline):
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
                    if deadline_passed(deadline):
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
                        db_ok = False
                    finally:
                        try: con.close()
                        except Exception: pass

                    for idx, blob in chunk_rows:
                        if not isinstance(blob, (bytes, bytearray)):
                            continue
                        gens = _pb_generations(bytes(blob))
                        if not gens:
                            # Undated blob. Prefer the same structural read without the
                            # timestamp requirement (field-4 records only, marker 26 — the
                            # 24-records here duplicate the steps scan). Only an unknown layout
                            # falls to the recursive scan, which sees every record TWICE (field 4
                            # + its field-17.2 copy), so exact duplicates are dropped first.
                            undated = [g for g in _pb_generations(bytes(blob), require_timestamp=False)
                                       if g.get("mk") == 26]
                            if undated:
                                found = [{1: g["me"], 2: g["u"], 3: g["o"], 5: g["c"]} for g in undated]
                            else:
                                found = _dedupe_usage_records(_pb_find_usage(bytes(blob), marker=26))
                            if not found:
                                continue
                            # today_iso for a stem that already has "@" entries (genuinely new
                            # blob); the DB mtime for a first-scan backlog ingest.
                            gen_date = today_iso if stem in at_stems else mtime_fallback_date
                            if gen_date >= cutoff:
                                # One entry per record (same key shape as the dated path) so
                                # each is one call that can pick its own prompt-length tier.
                                for gi, vd in enumerate(found):
                                    key = f"{stem}@{idx}.{gi}"
                                    if key in entries:
                                        continue
                                    entries[key] = {
                                        "d": gen_date,
                                        "u": vd.get(USAGE_INPUT, 0),
                                        "c": vd.get(USAGE_CACHED, 0),
                                        "o": vd.get(USAGE_OUTPUT, 0),
                                        "me": vd.get(USAGE_MODEL, 0),
                                    }
                                    changed = True
                            continue
                        for gi, gen in enumerate(gens):
                            gen_date = _local_iso(gen["secs"])
                            if gen_date < cutoff:
                                continue
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
                if db_ok and sig and not deadline_passed(deadline):
                    seen_memo[stem] = sig

        # Persist the scan memo (change-gated like rpcWatermarks). After a COMPLETE scan,
        # seen_memo is authoritative (memos of deleted DBs and RPC-owned stems drop out);
        # after a deadline-cut partial scan, carry unvisited DBs' old memos forward — their
        # signatures still describe their last complete scan, and dropping them would force
        # a pointless rescan AND churn a ledger write on every partial run.
        scan_complete = not deadline_passed(deadline)
        new_memo = seen_memo if scan_complete else {**old_memo, **seen_memo}
        if new_memo != old_memo:
            ledger["dbScanned"] = new_memo
            changed = True

        # Prune entries past the retention horizon (``cutoff``, computed before the scan).
        for key in list(entries.keys()):
            e = entries[key]
            if e.get("d", "") < cutoff:
                del entries[key]
                changed = True

        # Merge + prune watermarks AFTER the entry prune: keep marks only for cascades live
        # this run or with persisted ``#`` entries. The ``new_marks != marks`` gate is
        # load-bearing (tests/test_rpc_watermarks.py::test_no_write_churn_when_marks_identical).
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


# Per-MTok prices for models neither the LiteLLM catalog nor pricing_data's embedded
# _FALLBACK_PRICING carries. gpt-oss is an open-weights model whose hosted rates are
# far below the Pro fallback, so without this it would be badly overstated. Keys are normalised (lowercase, hyphenated) pricing keys. A published rate
# belongs in _FALLBACK_PRICING instead: its rows are always merged into the active
# pricing, so the same key here would never be read.
_EXTRA_MODEL_PRICES: dict[str, dict[str, float]] = {
    "gpt-oss-120b": {"input": 0.15, "cache_read": 0.075, "output": 0.60},
    "gpt-oss-20b": {"input": 0.05, "cache_read": 0.025, "output": 0.20},
    "gpt-oss": {"input": 0.15, "cache_read": 0.075, "output": 0.60},
}

# The RPC reports a model as MODEL_PLACEHOLDER_M<N>, where the protobuf model enum == 1000 + N
# (M133->1133, M26->1026, M16->1016).
def _placeholder_enum(placeholder: str) -> "int | None":
    """``MODEL_PLACEHOLDER_M<N>`` -> protobuf model enum ``1000 + N`` (None if unparseable)."""
    if "_M" in str(placeholder):
        try:
            return 1000 + int(str(placeholder).rsplit("_M", 1)[1])
        except ValueError:
            return None
    return None


# Legacy ledger model strings -> exact picker names ("gemini-3-pro" mislabels M16 =
# Gemini 3.1 Pro (High); the picker has no "Gemini 3 Pro"). Applied on every ledger update
# so old ledgers / .bak restores converge; me-bearing entries resolve via the enum map anyway.
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

# EXACT Antigravity model-picker display names keyed by protobuf model enum. The breakdown
# shows these verbatim; pricing strips the effort parenthetical (_normalize_model_name), so
# "Gemini 3.5 Flash (High)" still prices as gemini-3.5-flash.
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
    1071: "Gemini 3.6 Flash (High)",
    1072: "Gemini 3.6 Flash (Medium)",
    1073: "Gemini 3.6 Flash (Low)",
    1132: "Gemini 3.5 Flash (High)",
    1133: "Gemini 3.5 Flash",  # retired pre-effort-split 3.5 Flash id; effort unknowable
    1187: "Gemini 3.5 Flash (Low)",
    1198: "Gemini 3.5 Flash Lite",
    # Gemini 3.6 Flash effort trio (M264/M265/M266).
    1264: "Gemini 3.6 Flash (High)",
    1265: "Gemini 3.6 Flash (Medium)",
    1266: "Gemini 3.6 Flash (Low)",
    1277: "Gemini 3.5 Flash Lite",
    # Gemini 3.7 Flash (M298-M300) and 3.8 Flash (M318-M320) effort trios.
    1298: "Gemini 3.7 Flash (High)",
    1299: "Gemini 3.7 Flash (Medium)",
    1300: "Gemini 3.7 Flash (Low)",
    1318: "Gemini 3.8 Flash (High)",
    1319: "Gemini 3.8 Flash (Medium)",
    1320: "Gemini 3.8 Flash (Low)",
    1322: "Gemini 3.8 Flash",  # TieredModelConfig MODEL_TIER_FLASH subagent model (M322 -> gemini-3.8-flash-tiered)
    1400: "Claude Opus 5.5 (Low)",
    1401: "Claude Opus 5.5 (Medium)",
    1402: "Claude Opus 5.5 (High)",
    1403: "Claude Sonnet 5.5 (Low)",
    1404: "Claude Sonnet 5.5 (Medium)",
    1405: "Claude Sonnet 5.5 (High)",
    1417: "Gemini 4 Argon (High)",
    1418: "Gemini 4 Argon (Medium)",
    1419: "Gemini 4 Argon (Low)",
}


def _resolve_model_key(e: dict[str, Any], learned: "dict[int, str] | None" = None) -> str | None:
    """The model name a ledger entry resolves to, shared by display and pricing so the
    breakdown never names one model while the cost is computed for another.

    Order: the static, hand-curated ``_MODEL_ENUM_NAMES`` (freshest knowledge: an RPC entry
    whose placeholder was unknown at capture stores a family-fallback ``model`` plus the raw
    ``me``, and heals once the enum is mapped); then ``learned`` (names captured live from
    GetAvailableModels, persisted as the ledger's ``learnedModelNames``); then the entry's
    own ``model`` string (CLI/RPC entries carry one). ``None`` when none resolve. Both tables
    are int-keyed, so a non-int ``me`` simply misses them.
    """
    me = e.get("me")
    name = None
    if isinstance(me, int):
        name = _MODEL_ENUM_NAMES.get(me) or (learned or {}).get(me)
    return name or e.get("model") or None


def _resolve_model_display(e: dict[str, Any], learned: "dict[int, str] | None" = None) -> str:
    """DISPLAY name for a ledger entry: ``_resolve_model_key``, else a self-documenting
    "Model M<N>" when ``me`` fits the MODEL_PLACEHOLDER_M<N> convention (enum = 1000 + N) —
    a model used ahead of both the static table and a live-learned name stays identifiable
    instead of vanishing into a generic bucket — else "Unknown" (no ``me``, or one outside
    that convention, e.g. a legacy pre-2026-06 raw protobuf enum)."""
    name = _resolve_model_key(e, learned)
    if name:
        return name
    me = e.get("me")
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
    """Map a model *display* name to a pricing-catalog key.

    "Gemini 3.5 Flash (High)" -> "gemini-3.5-flash"; "Gemini 3.1 Pro (High)" -> "gemini-3.1-pro".
    Drops the reasoning-effort parenthetical, lowercases, and hyphenates spaces.
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
    # Merge the trajectory ledger with the agy statusLine push; key namespaces are disjoint
    # ("<db_stem>:<idx>" vs "cli:<session>"). A statusLine entry is the session's CUMULATIVE
    # total, so ledger coverage of the same stem supersedes it (else the session counts twice);
    # it stays the only record for the encrypted .pb era and sessions whose DB never appears.
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
    # Per-entry pricing: entries carry a model enum ("me") and/or a "model" display name;
    # the Gemini 3.1 Pro default applies only when nothing resolves.
    default_prices = model_pricing("gemini-3.1-pro")
    _price_cache: dict[str, dict[str, Any]] = {}

    def _prices_for(e: dict[str, Any]) -> dict[str, Any]:
        m = _resolve_model_key(e, learned_names)
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

    def cost_of(e: dict[str, int], per_call: bool = True) -> tuple[float, tuple[float, float, float]]:
        p = _prices_for(e)
        if per_call:
            # Prompt-length tier (Gemini 3.1 Pro bills prompts over 200K higher): one call's
            # prompt is its uncached input + cache reads ("o" is output). Only a per-call
            # entry can pick one — see the caller for the cumulative "cli:" entries.
            p = prompt_tier_prices(p, e.get("u", 0) + e.get("c", 0))
        in_rate = p.get("input", 0.0) or 0.0
        cache_rate = p.get("cache_read", 0.0) or 0.0
        out_rate = p.get("output", 0.0) or 0.0
        # "o" is the TOTAL output (thinking + response). The ledger's "t"/"x" (thinking /
        # response output) are SUBSETS of "o" (o == t + x), so bill "o" once at the output rate.
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
        #
        # Returns the cost and its (input, output, cache-read) parts; the ledger records
        # no cache writes.
        u_cost = e.get("u", 0) * in_rate
        c_cost = e.get("c", 0) * cache_rate
        o_cost = e.get("o", 0) * out_rate
        return ((u_cost + c_cost + o_cost) / 1_000_000.0,
                (u_cost / 1_000_000.0, o_cost / 1_000_000.0, c_cost / 1_000_000.0))

    today_cost = month_cost = 0.0
    today_tok = month_tok = 0
    split_today, split_week, split_month = empty_type_split(), empty_type_split(), empty_type_split()
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
    # Display-name + family memo keyed on the only two fields the resolution reads. LOCAL to
    # this call: learned_names changes between refreshes, so a module-global would go stale.
    family_cache: dict[tuple[Any, Any], str] = {}
    for key, e in entries.items():
        d = e.get("d", "")
        cached = e.get("c", 0)
        # A ':' / '@' / '#' entry is one API call (gen_metadata stores one entry per
        # generation), so it picks its own prompt-length tier. A steps row holding several
        # distinct records is marked "n" and bills at the base rates, like a "cli:" entry:
        # a sum can't pick a tier. An entry without "n" is one call.
        # A "cli:" entry is the statusLine's CUMULATIVE session total: its u + c adds up
        # every call's prompt and says nothing about any one call's size, and the capture
        # keeps no per-call record to tier from — so it bills at the base rates.
        c, (u_cost, o_cost, c_cost) = cost_of(e, per_call=not key.startswith("cli:") and e.get("n", 1) == 1)
        # Token VOLUME = total tokens processed = uncached input + cached re-read + output
        # (industry standard: volume includes cache reads; cost bills them discounted).
        tok = e.get("u", 0) + cached + e.get("o", 0)  # o = total output (see cost_of)
        # By type: input lane = uncached input; output lane = total output; cache reads.
        split = UsageSplit(tok, c, (e.get("u", 0), e.get("o", 0), cached, 0),
                           (u_cost, o_cost, c_cost, 0.0))
        # Collapse to the display FAMILY: the single choke point that keeps the 30-day
        # breakdown and every per-bucket tooltip grouped the same way (pricing already ran).
        fk = (e.get("me"), e.get("model"))
        mname = family_cache.get(fk)
        if mname is None:
            mname = _model_family(_resolve_model_display(e, learned=learned_names))
            family_cache[fk] = mname
        if d >= m30_iso:
            month_cost += c
            month_tok += tok
            type_split_add(split_month, split)
            slot = model_costs.setdefault(str(mname), {"cost": 0.0, "tokens": 0})
            slot["cost"] += c
            slot["tokens"] += tok
        if d in daily:
            bucket_add(daily[d], tok, c, mname)
            type_split_add(split_week, split)
        if d in monthly_buckets and monthly_buckets[d].get("inMonth"):
            bucket_add(monthly_buckets[d], tok, c, mname)
        if d == today_iso:
            today_cost += c
            today_tok += tok
            type_split_add(split_today, split)
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
    burn_rate = week_cost / trailing_week_days(current)  # trailing-7-day average $/day (unbiased)
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
        "modelBreakdown7d": model_breakdown_rows(buckets_model_costs(daily)),
        "modelBreakdown1d": model_breakdown_rows(daily.get(today_iso, {}).get("models")),
        "typeBreakdown": type_breakdown(split_today, split_week, split_month),
    }
    line = cost_breakdown_line(*breakdown_from_split(split_month))
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
    return load_json_with_backup(
        COST_ARCHIVE_PATH,
        lambda data: isinstance(data, dict) and isinstance(data.get("months"), dict),
        {"version": 1, "months": {}},
    )


def _save_cost_archive(archive: dict[str, Any]) -> None:
    """Atomic 0600 write + ``.bak`` mirror (see ``save_json_with_backup``)."""
    save_json_with_backup(COST_ARCHIVE_PATH, archive)


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
    lock_budget = _lock_budget(deadline)
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
            return
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
    and can start before they exist). Returns a fresh
    ``{provider: summary|None}`` map for ``apply_cost_summaries`` to merge once the fetches
    finish — keeping the summaries OUT of ``providers`` until then is what makes a scan
    timeout a clean no-op (no torn write to roll back). The ``deadline`` is threaded into
    the Antigravity ledger's per-DB scan loop AND into each local-log summarizer's file walk (accounting._cached_log_records). A walk the deadline cuts
    checkpoints its parse cache and raises LogScanIncomplete (a TimeoutError), so a slow
    cold parse finishes over several refreshes instead of restarting each time, and no
    summary — nor the cost archive below — is ever built from partial records."""
    from accounting import (local_claude_token_summary, local_codex_token_summary,
                            local_gemini_token_summary, local_grok_token_summary)
    from .grok import grok_billing_period
    # Best-effort: a failure here must never block the rest of the scan.
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
        # None when there's no local data, so the tab stays hidden.
        "grok": local_grok_token_summary(
            deadline=deadline,
            week_start=grok_week_start,
            week_end=grok_week_end,
        ),
        "antigravity": antigravity_ledger_cost_summary(deadline=deadline),
    }
    # Best-effort: enrichment must never break the snapshot.
    try:
        update_cost_archive(summaries, deadline=deadline)
    except Exception:
        pass
    return summaries


def _attach_summary(providers: dict[str, dict[str, Any]], provider: str,
                    summaries: dict[str, dict[str, Any] | None] | None,
                    label: str | None = None, source: str | None = None) -> None:
    """ASSIGN this run's summary (never setdefault): the scan is the only source of a cost
    summary, so any costSummary already on the provider is stale — e.g. one riding a
    carried-forward last-good entry. A scan that reported no data for the provider (None)
    clears a stale one; a provider absent from ``summaries`` is left untouched."""
    if not summaries or provider not in summaries:
        return
    summary = summaries[provider]
    if summary is not None:
        target = providers.get(provider)
        if target is None:
            if label is None or source is None:
                return
            target = providers[provider] = default_provider(label, source)
        target["costSummary"] = summary
    elif isinstance(providers.get(provider), dict):
        providers[provider].pop("costSummary", None)


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
        # _attach_summary creates the provider entry from its costSummary, so the tab
        # appears whenever ~/.grok logs exist.
        ("grok", "Grok", "local-grok-logs"),
    ):
        _attach_summary(providers, provider, summaries, label, source)

    # CLI token data can exist while the IDE is closed (status != ok).
    if providers.get("antigravity") is not None:
        _attach_summary(providers, "antigravity", summaries)
