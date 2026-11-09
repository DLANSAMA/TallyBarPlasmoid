"""Local provider log parsers (Claude, Codex, Grok, Gemini) with incremental disk caching."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import stat
import time
import zlib
from pathlib import Path
from typing import Any

from io_helpers import atomic_write_text, load_json_with_backup, save_json_with_backup
from parsers import as_dict
from . import pricing as _pricing
from .formatting import parse_timestamp
from .pricing import (
    DEFAULT_MODEL_FOR_PROVIDER,
    has_catalog_price,
    slim_usage,
    usage_split,
    usage_token_total,
)
from .buckets import (
    bucket_add,
    buckets_model_costs,
    empty_hourly_token_buckets,
    empty_monthly_token_buckets,
    empty_type_split,
    empty_weekly_token_buckets,
    hourly_token_usage,
    monthly_token_usage,
    token_summary,
    type_split_add,
    type_split_merge,
    weekly_token_usage,
)

_UNSET = object()

_PARSE_CACHE_DIR = Path.home() / ".tallybar" / "cache"
_CACHE_SCHEMA_VERSION = 1          # bump to invalidate ALL parse caches at once
# A cached record is only valid for the output shape that wrote it, so a shape change
# bumps its version. → tests/test_accounting.py::test_parse_cache_versions_track_output_shape
_CLAUDE_PARSE_VERSION = 6
_CODEX_PARSE_VERSION = 3
_GROK_PARSE_VERSION = 3
_GEMINI_PARSE_VERSION = 4
# The shape of a Claude file's folded state (_claude_state) in the Claude store; bump it
# on any change to that shape. → tests/test_claude_log_store.py::test_state_shape_is_pinned
_CLAUDE_STATE_VERSION = 1
# A deadline-cut walk stops this long BEFORE the deadline so its checkpoint save lands
# while the process is still alive (the scan runs on a daemon thread that dies with the
# one-shot backend). A checkpoint writes one file: a 45 MB cache saves in ~0.2 s, and
# 1.5 s leaves room for a larger one and for GIL contention with the provider fetches.
_CHECKPOINT_MARGIN_SECONDS = 1.5
# The Claude store (_ClaudeLogStore): a session's transcripts hash to one of this many
# cold shards, and the hot file is folded into them once it passes _CLAUDE_HOT_LIMIT. Each
# atomic write costs two fsyncs (~25 ms per 1 MB shard on an NVMe disk), so fewer, larger
# shards keep a full rewrite (first run, a version bump) near 1.5 s, while the limit keeps
# the per-refresh write to the hot file small.
_CLAUDE_SHARDS = 64
_CLAUDE_HOT_LIMIT = 256 * 1024


class LogScanIncomplete(TimeoutError):
    """A parse-cache walk ran out of time. Its progress is checkpointed to the cache, but
    the records are partial, so no summary may be built from them. A TimeoutError, so the
    cost scan reports ``cost_summary_timeout`` exactly like the outer timeout."""


def _load_parse_cache(cache_path: Path, parse_version: int) -> dict[str, dict[str, Any]]:
    try:
        with cache_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if (not isinstance(data, dict)
            or data.get("schema") != _CACHE_SCHEMA_VERSION
            or data.get("version") != parse_version
            or not isinstance(data.get("files"), dict)):
        return {}
    return data["files"]


def _save_parse_cache(cache_path: Path, parse_version: int,
                      files: dict[str, dict[str, Any]]) -> None:
    try:
        body = json.dumps({"schema": _CACHE_SCHEMA_VERSION, "version": parse_version,
                            "files": files}, separators=(",", ":"))
    except (TypeError, ValueError):
        return
    try:
        atomic_write_text(cache_path, body)
    except OSError:
        pass


def _safe_resume_offset(path: Path) -> int | None:
    """Byte offset just past the file's LAST newline — i.e. the end of the last COMPLETE
    line (0 when there is none; None when the file cannot be read). Resuming here can never
    land mid-record: a partially written trailing line (the agent is still streaming into
    this file) is excluded, so it gets parsed whole on the next refresh instead of being
    split across two parses and lost. Scans backwards from EOF in 8 KiB chunks, so it costs
    a couple of reads rather than a full file scan."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            pos = handle.tell()
            while pos > 0:
                start = max(0, pos - 8192)
                handle.seek(start)
                buf = handle.read(pos - start)
                idx = buf.rfind(b"\n")
                if idx != -1:
                    return start + idx + 1
                pos = start
    except OSError:
        return None
    return 0


def _offset_fingerprint(path: Path, offset: int) -> int | None:
    """CRC32 of the (up to) 256 bytes that END at ``offset``. Stored beside ``off`` so a
    later tail parse can prove the bytes it is about to skip are still the bytes it parsed:
    a file REWRITTEN to a larger size passes the grew-only size check, but its content
    before the old offset differs, so the fingerprint does not match."""
    try:
        with path.open("rb") as handle:
            start = max(0, offset - 256)
            handle.seek(start)
            buf = handle.read(offset - start)
    except OSError:
        return None
    if len(buf) != offset - start:
        return None
    return zlib.crc32(buf)


def _tail_parse(path: Path, prev: dict[str, Any], parser, size: int,
                end: int) -> list[dict[str, Any]] | None:
    """The records of only the complete lines appended since a cached parse, i.e. the
    bytes ``[prev["off"], end)``; the caller folds them onto what it already holds.

    Returns None when anything looks off — the caller then falls back to a full reparse.
    FAIL-CLOSED by design: every bail-out costs one ordinary full parse, whereas a wrong
    incremental result would silently corrupt the cost history until the cache version is
    next bumped.

    Bails out when the previous entry lacks a usable offset/fingerprint/size, when the
    file SHRANK (truncated or rotated — the old offset now points into unrelated bytes),
    when the size is unchanged but the mtime moved (an in-place rewrite), and when the
    bytes just before the old offset no longer match their fingerprint (rewritten AND
    grown). Append-only growth is the only case it accepts.
    """
    prev_off = prev.get("off")
    prev_fp = prev.get("fp")
    prev_size = prev.get("size")
    if (not isinstance(prev_off, int) or not isinstance(prev_fp, int)
            or not isinstance(prev_size, int)):
        return None
    if size <= prev_size:
        return None
    if prev_off < 0 or prev_off > end:
        return None
    if _offset_fingerprint(path, prev_off) != prev_fp:
        return None
    if end == prev_off:
        return []
    return parser(path, prev_off, end)


def _cached_log_records(root: Path, pattern: str, cache_path: Path | None,
                        parser, parse_version: int, walker=None,
                        deadline: float | None = None) -> list[dict[str, Any]]:
    """Every record under ``root``, re-parsing only files whose bytes changed since the
    cached parse. Each changed file is parsed whole: these parsers carry per-line state
    (Codex's current model, Grok's model per session), so resuming mid-file would
    mis-attribute models. The Claude logs, whose parser is stateless, go through
    _cached_claude_states instead.

    A walk cut by ``deadline`` CHECKPOINTS what it parsed and raises LogScanIncomplete. It
    stops ``_CHECKPOINT_MARGIN_SECONDS`` early so the save lands before the backend exits.
    Without the checkpoint, a cold cache (first run, or a parse-version bump) whose full
    parse doesn't fit one refresh would restart from zero every time and never finish. A
    partial cache is safe because each entry is reused only while its file's mtime and size
    match, and a file missing from the cache is simply parsed. Partial records are not
    safe, hence the raise."""
    old_files = _load_parse_cache(cache_path, parse_version) if cache_path is not None else {}
    new_files: dict[str, dict[str, Any]] = {}
    records: list[dict[str, Any]] = []
    changed = False
    complete = True
    stop_at = None if deadline is None else deadline - _CHECKPOINT_MARGIN_SECONDS
    for path in (walker(root) if walker is not None else root.rglob(pattern)):
        if stop_at is not None and time.time() >= stop_at:
            complete = False
            break
        try:
            st = path.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        key = str(path)
        prev = old_files.get(key)
        if (prev is not None and prev.get("mtime") == st.st_mtime_ns
                and prev.get("size") == st.st_size):
            recs = prev.get("records") or []
        else:
            parsed = parser(path)
            if parsed is None:
                continue
            recs = parsed
            changed = True
        new_files[key] = {"mtime": st.st_mtime_ns, "size": st.st_size, "records": recs}
        records.extend(recs)
    if cache_path is not None and complete and (changed or len(new_files) != len(old_files)):
        _save_parse_cache(cache_path, parse_version, new_files)
    elif cache_path is not None and not complete and changed:
        # Unvisited files keep their old (still-valid) entries; a later COMPLETE walk
        # rebuilds the map from scratch, which prunes deleted files.
        _save_parse_cache(cache_path, parse_version, {**old_files, **new_files})
    if not complete:
        raise LogScanIncomplete(f"log scan of {root.name or root} ran out of time; progress saved")
    return records


# --- Claude: per-file folded state, hot/cold store ------------------------------------
#
# The Claude history is the big one (tens of thousands of transcripts, over half a million
# usage lines), and its summary needs nearly all of it on every refresh, so the cache
# stores each file's records already FOLDED the way the summarizer folds them
# (_claude_state) and keeps that fold in a store whose refresh writes scale with what
# changed (_ClaudeLogStore). Both stay pure functions of file bytes: the state holds token
# counts and timestamps, never a price.

_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


def _epoch_seconds(value: Any) -> int | None:
    """parse_timestamp's instant as whole seconds since the epoch, rounded down. Every
    window and day boundary the summary compares against is a whole second, so the
    rounded value lands on the same side of each as the exact instant."""
    parsed = parse_timestamp(value)
    if parsed is None:
        return None
    delta = parsed - _EPOCH
    return delta.days * 86400 + delta.seconds


def _out_tokens(usage: Any) -> int:
    if isinstance(usage, dict):
        v = usage.get("output_tokens")
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    return 0


def _claude_state(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold one file's Claude records the way local_claude_token_summary folds them.

    ``g`` maps each request key ``r`` (first-seen order) to ``[t, m, u, lo, hi, f, vd]``:
    the FIRST record with the most output tokens (its epoch second, model and usage), the
    earliest and latest epoch second of all the key's records, whether any of them is a
    final-usage line, and — while none is — the visible output chars by line uuid. ``k``
    holds the keyless records as ``[t, m, u]`` in order; ``lo``/``hi`` bound every epoch
    second in the file. Records with no readable timestamp are dropped, as the summary
    drops them.

    A group only stands in for its records when ALL of them fall inside the summary's
    window; local_claude_token_summary re-folds a file whose group straddles the window
    start from its records."""
    groups: dict[str, list[Any]] = {}
    keyless: list[list[Any]] = []
    lo: int | None = None
    hi: int | None = None
    for record in records:
        t = _epoch_seconds(record["t"])
        if t is None:
            continue
        if lo is None or t < lo:
            lo = t
        if hi is None or t > hi:
            hi = t
        usage = record["u"]
        model = record["m"]
        key = record["r"]
        if not key:
            keyless.append([t, model, usage])
            continue
        group = groups.get(key)
        if group is None:
            group = groups[key] = [t, model, usage, t, t, 0, None]
        else:
            if _out_tokens(usage) > _out_tokens(group[2]):
                group[0], group[1], group[2] = t, model, usage
            if t < group[3]:
                group[3] = t
            if t > group[4]:
                group[4] = t
        if record.get("f"):
            group[5], group[6] = 1, None
        elif not group[5]:
            visible = record.get("v")
            if visible:
                if group[6] is None:
                    group[6] = {}
                group[6][record.get("i") or ""] = visible
    return {"g": groups, "k": keyless, "lo": lo, "hi": hi}


def _merge_claude_state(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """The state of a file's records ``A + B`` from the states of ``A`` and ``B``:
    exactly ``_claude_state(A + B)``, which is what lets a tail parse extend a state.
    Neither input is modified."""
    if not b["g"] and not b["k"]:
        return a
    groups = dict(a["g"])
    for key, row in b["g"].items():
        prev = groups.get(key)
        if prev is None:
            groups[key] = row
            continue
        win = prev if _out_tokens(prev[2]) >= _out_tokens(row[2]) else row
        final = 1 if prev[5] or row[5] else 0
        visible = None
        if not final:
            visible = {**prev[6], **row[6]} if prev[6] and row[6] else (prev[6] or row[6])
        groups[key] = [win[0], win[1], win[2], min(prev[3], row[3]), max(prev[4], row[4]),
                       final, visible]
    bounds = [x for x in (a["lo"], a["hi"], b["lo"], b["hi"]) if x is not None]
    return {"g": groups, "k": a["k"] + b["k"],
            "lo": min(bounds) if bounds else None, "hi": max(bounds) if bounds else None}


_EMPTY_CLAUDE_STATE: dict[str, Any] = {"g": {}, "k": [], "lo": None, "hi": None}


def _valid_claude_entry(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    state = entry.get("s")
    return (isinstance(entry.get("mtime"), int) and isinstance(entry.get("size"), int)
            and isinstance(entry.get("off"), int)
            and (entry.get("fp") is None or isinstance(entry.get("fp"), int))
            and isinstance(state, dict) and isinstance(state.get("g"), dict)
            and isinstance(state.get("k"), list) and "lo" in state and "hi" in state)


class _ClaudeLogStore:
    """Where the Claude parse cache lives, laid out so a refresh writes about as much as
    changed instead of the whole history.

    ``claude_logs.sNN.json`` (cold shards): full entries ``{mtime, size, off, fp, s}``,
    a session's transcripts (the main JSONL and its ``subagents/``) all in one shard.
    ``claude_logs.hot.json``: the entries that changed since the shards were last
    written — a whole entry for a new or reparsed file, or, for a file that only grew, a
    DELTA: the state of the bytes appended since its cold entry, tagged with that entry's
    ``off`` and ``fp`` (``base``/``bfp``). An active session therefore costs one rewrite
    of the hot file per refresh, sized by its new lines. Once the hot file passes
    _CLAUDE_HOT_LIMIT, a complete walk folds it into the shards its entries belong to.

    Every file goes through atomic_write_text. Shards are written before the hot file,
    and a delta whose base no longer matches its cold entry is dropped on load (that
    file's tail is simply parsed again), so a crash between any two writes leaves a cache
    whose every entry still describes its file. Unreadable or other-version files read as
    empty. A cache in the single-file format (``claude_logs.json``, records not states) is
    folded in entry by entry as the walk reaches each file, and deleted once a complete
    walk has taken it all in."""

    def __init__(self, legacy_path: Path | None, root: Path, extra_roots: tuple[Path, ...] = ()):
        self.legacy_path = legacy_path
        self.root = root
        self.roots = (root, *extra_roots)
        self.cold: list[dict[str, dict[str, Any]]] = [{} for _ in range(_CLAUDE_SHARDS)]
        self.found_in: dict[str, int] = {}
        self.hot_keys: set[str] = set()
        self.hot_dirty = False
        self.bad_shards: set[int] = set()
        self.legacy: dict[str, dict[str, Any]] = {}

    def _path(self, label: str) -> Path:
        assert self.legacy_path is not None
        return self.legacy_path.with_name(f"{self.legacy_path.stem}.{label}.json")

    @staticmethod
    def _read(path: Path) -> tuple[dict[str, Any], bool]:
        """A store file's entries, and whether the file exists but can't be used."""
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return {}, False
        except (OSError, ValueError):
            return {}, True
        if (not isinstance(data, dict)
                or data.get("schema") != _CACHE_SCHEMA_VERSION
                or data.get("version") != _CLAUDE_PARSE_VERSION
                or data.get("state") != _CLAUDE_STATE_VERSION
                or not isinstance(data.get("files"), dict)):
            return {}, True
        return data["files"], False

    def _shard_of(self, key: str) -> int:
        parts = Path(key).parts
        for root in self.roots:
            try:
                parts = Path(key).relative_to(root).parts
                break
            except ValueError:
                continue
        session = "/".join(parts[:2]).removesuffix(".jsonl")
        return zlib.crc32(session.encode("utf-8", "surrogateescape")) % _CLAUDE_SHARDS

    def load(self) -> dict[str, dict[str, Any]]:
        """Every stored entry, resolved: ``{mtime, size, off, fp, s, cold, delta, hot}``,
        where ``s`` is the file's whole state, ``cold`` the shard entry it builds on (if
        any), ``delta`` the hot state on top of it, and ``hot`` marks a whole entry that
        lives in the hot file."""
        if self.legacy_path is None:
            return {}
        for index in range(_CLAUDE_SHARDS):
            files, bad = self._read(self._path(f"s{index:02d}"))
            if bad:
                self.bad_shards.add(index)
            for key, entry in files.items():
                if _valid_claude_entry(entry):
                    self.cold[index][key] = entry
                    self.found_in[key] = index
        resolved: dict[str, dict[str, Any]] = {}
        hot, self.hot_dirty = self._read(self._path("hot"))
        self.hot_keys = set(hot)
        for key, entry in hot.items():
            if not _valid_claude_entry(entry):
                self.hot_dirty = True
                continue
            meta = {k: entry[k] for k in ("mtime", "size", "off", "fp")}
            if "base" not in entry:
                resolved[key] = {**meta, "s": entry["s"], "cold": None, "delta": None, "hot": True}
                continue
            cold = self.cold[self.found_in[key]].get(key) if key in self.found_in else None
            if cold is None or cold["off"] != entry.get("base") or cold["fp"] != entry.get("bfp"):
                self.hot_dirty = True
                continue
            resolved[key] = {**meta, "s": _merge_claude_state(cold["s"], entry["s"]),
                             "cold": cold, "delta": entry["s"], "hot": False}
        for shard in self.cold:
            for key, cold in shard.items():
                if key not in resolved:
                    resolved[key] = {**{k: cold[k] for k in ("mtime", "size", "off", "fp")},
                                     "s": cold["s"], "cold": cold, "delta": None, "hot": False}
        if self.legacy_path.is_file():
            self.legacy = _load_parse_cache(self.legacy_path, _CLAUDE_PARSE_VERSION)
        return resolved

    def from_legacy(self, key: str) -> dict[str, Any] | None:
        """The single-file cache's entry for ``key``, folded into a whole hot entry."""
        old = self.legacy.get(key)
        if not isinstance(old, dict):
            return None
        records, off, fp = old.get("records"), old.get("off"), old.get("fp")
        if (not isinstance(records, list) or not isinstance(off, int)
                or not isinstance(fp, int)):
            return None
        return {"mtime": old.get("mtime"), "size": old.get("size"), "off": off, "fp": fp,
                "s": _claude_state(records), "cold": None, "delta": None, "hot": True}

    def save(self, entries: dict[str, dict[str, Any]], complete: bool, changed: bool) -> None:
        """Persist ``entries`` (every live file after a complete walk; visited plus carried
        ones after a cut walk). Writes only the shards whose entries moved, then the hot
        file if anything in it changed."""
        if self.legacy_path is None:
            return
        dirty: set[int] = set()
        hot_dirty = self.hot_dirty or changed
        if complete:
            # Deleted transcripts leave the store; an unreadable shard is rewritten whole.
            dirty |= self.bad_shards
            for key, index in self.found_in.items():
                if key not in entries:
                    del self.cold[index][key]
                    dirty.add(index)
            hot_dirty = hot_dirty or not self.hot_keys.issubset(entries)
        hot: dict[str, dict[str, Any]] = {}
        for key, entry in entries.items():
            meta = {k: entry[k] for k in ("mtime", "size", "off", "fp")}
            if entry["hot"]:
                hot[key] = {**meta, "s": entry["s"]}
            elif entry["delta"] is not None:
                hot[key] = {**meta, "s": entry["delta"], "base": entry["cold"]["off"],
                            "bfp": entry["cold"]["fp"]}
        body = self._dump(hot) if hot_dirty else None
        if complete and body is not None and len(body) > _CLAUDE_HOT_LIMIT:
            for key in hot:
                entry = entries[key]
                index = self.found_in.get(key, self._shard_of(key))
                self.cold[index][key] = {**{k: entry[k] for k in ("mtime", "size", "off", "fp")},
                                         "s": entry["s"]}
                dirty.add(index)
            body = self._dump({})
        try:
            for index in sorted(dirty):
                atomic_write_text(self._path(f"s{index:02d}"), self._dump(self.cold[index]))
            if body is not None:
                atomic_write_text(self._path("hot"), body)
            if complete and self.legacy_path.exists():
                self.legacy_path.unlink()
        except OSError:
            pass

    @staticmethod
    def _dump(files: dict[str, dict[str, Any]]) -> str:
        return json.dumps({"schema": _CACHE_SCHEMA_VERSION, "version": _CLAUDE_PARSE_VERSION,
                           "state": _CLAUDE_STATE_VERSION, "files": files},
                          separators=(",", ":"))


def _cached_claude_states(root: Path, legacy_path: Path | None,
                          deadline: float | None = None,
                          extra_roots: tuple[Path, ...] = ()) -> list[tuple[Path, int, dict[str, Any]]]:
    """``(path, off, state)`` for every Claude transcript under ``root`` and
    ``extra_roots``, in walk order (a file's key is its absolute path, so roots share one
    store without colliding),
    where ``state`` is _claude_state of the parse of bytes ``[0, off)``. Files whose mtime
    and size match their stored entry are not read; a file that grew is extended from
    ``off`` (tail-incremental) and anything else is parsed whole.

    The window END is fixed before reading: ``end = _safe_resume_offset(path)`` first, and
    the parse is bounded to it, so a line the agent appends meanwhile is left for the next
    refresh instead of being stored twice.

    A walk cut by ``deadline`` checkpoints and raises LogScanIncomplete, as
    _cached_log_records does."""
    store = _ClaudeLogStore(legacy_path, root, extra_roots)
    stored = store.load()
    visited: dict[str, dict[str, Any]] = {}
    out: list[tuple[Path, int, dict[str, Any]]] = []
    changed = False
    complete = True
    stop_at = None if deadline is None else deadline - _CHECKPOINT_MARGIN_SECONDS
    for path in (p for r in (root, *extra_roots) if r.is_dir() for p in r.rglob("*.jsonl")):
        if stop_at is not None and time.time() >= stop_at:
            complete = False
            break
        try:
            st = path.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        key = str(path)
        if key in visited:
            continue
        prev = stored.get(key)
        if prev is None and store.legacy:
            prev = store.from_legacy(key)
            if prev is not None:
                changed = True
        if (prev is not None and prev["mtime"] == st.st_mtime_ns
                and prev["size"] == st.st_size):
            entry = prev
        else:
            # The window end is fixed before any read.
            # → tests/test_accounting.py::test_append_during_the_parse_is_neither_lost_nor_doubled
            end = _safe_resume_offset(path)
            if end is None:
                continue
            tail = _tail_parse(path, prev, _parse_claude_file, st.st_size, end) if prev else None
            if tail is not None and prev is not None:
                tail_state = _claude_state(tail)
                entry = {**prev, "s": _merge_claude_state(prev["s"], tail_state)}
                if not prev["hot"]:
                    entry["delta"] = _merge_claude_state(prev["delta"] or _EMPTY_CLAUDE_STATE,
                                                         tail_state)
            else:
                records = _parse_claude_file(path, 0, end)
                if records is None:
                    continue
                entry = {"s": _claude_state(records), "cold": None, "delta": None, "hot": True}
            entry.update(mtime=st.st_mtime_ns, size=st.st_size, off=end,
                         fp=_offset_fingerprint(path, end))
            changed = True
        visited[key] = entry
        out.append((path, entry["off"], entry["s"]))
    store.save(visited if complete else {**stored, **visited}, complete, changed)
    if not complete:
        raise LogScanIncomplete(f"log scan of {root.name or root} ran out of time; progress saved")
    return out


# Directories under the Claude desktop app's local-agent-mode-sessions tree that hold
# plugins, mounts or bulk files and never a transcript root.
_COWORK_SKIP_DIRS = frozenset({"skills-plugin", "cowork_plugins", "node_modules", "rpm",
                               "backups", "sessions", "mnt", "outputs", "uploads"})
_COWORK_MAX_DEPTH = 8


def _claude_extra_roots(primary: Path, deadline: float | None = None) -> tuple[Path, ...]:
    """Claude transcript roots besides ``primary`` (``~/.claude/projects``) that exist:
    ``$CLAUDE_CONFIG_DIR/projects``, ``<config>/claude/projects`` and every
    ``.claude/projects`` the Claude desktop app keeps under ``<config>/Claude/
    local-agent-mode-sessions`` (Cowork and agent sessions, at varying depth), where
    ``<config>`` is ``$XDG_CONFIG_HOME`` or ``~/.config``. A ``.jsonl`` elsewhere in that
    tree (an audit log) is not a transcript. Missing directories are normal.

    The walk stops descending where it finds a root and skips bulk directories, because it
    runs on every refresh inside the cost-scan deadline; one that runs out of time raises
    LogScanIncomplete, since a partial root list would make the store drop the transcripts
    of the roots it did not reach."""
    config = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    candidates: list[Path] = []
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        candidates.append(Path(override) / "projects")
    candidates.append(config / "claude" / "projects")
    base = config / "Claude" / "local-agent-mode-sessions"
    stop_at = None if deadline is None else deadline - _CHECKPOINT_MARGIN_SECONDS
    if base.is_dir():
        for dirpath, dirnames, _files in os.walk(base):
            if stop_at is not None and time.time() >= stop_at:
                raise LogScanIncomplete("transcript root discovery ran out of time")
            here = Path(dirpath)
            if ".claude" in dirnames and (here / ".claude" / "projects").is_dir():
                candidates.append(here / ".claude" / "projects")
                dirnames[:] = []
                continue
            depth = len(here.relative_to(base).parts)
            dirnames[:] = [] if depth >= _COWORK_MAX_DEPTH else sorted(
                d for d in dirnames if d not in _COWORK_SKIP_DIRS
                and (not d.startswith(".") or d == ".claude"))
    seen = {primary.resolve()}
    roots: list[Path] = []
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        real = candidate.resolve()
        if real in seen:
            continue
        seen.add(real)
        roots.append(candidate)
    return tuple(roots)


def _resolve_cache_path(explicit_dir, provided_root, name: str) -> Path | None:
    if explicit_dir is _UNSET:
        return None if provided_root is not None else _PARSE_CACHE_DIR / name
    if explicit_dir is None:
        return None
    return Path(explicit_dir) / name


def _visible_output_chars(content: Any) -> int:
    """Characters of model output a transcript line shows: text blocks plus the compact
    JSON of each tool_use input. Thinking is excluded (current models log it empty), so
    this undercounts what the model generated — callers use it only as a lower bound."""
    if isinstance(content, str):
        return len(content)
    if not isinstance(content, list):
        return 0
    total = 0
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            text = block.get("text")
            if isinstance(text, str):
                total += len(text)
        elif kind == "tool_use":
            try:
                total += len(json.dumps(block.get("input"), ensure_ascii=False,
                                        separators=(",", ":")))
            except (TypeError, ValueError):
                pass
    return total


def _parse_claude_file(path: Path, start: int = 0,
                       end: int | None = None) -> list[dict[str, Any]] | None:
    """Parse a Claude session JSONL — the BYTE range ``[start, end)`` of it when given
    (both must sit on line boundaries; ``end=None`` reads to EOF).

    Safe to resume because this parser is stateless across lines — each record is decided
    entirely by its own line. ``_parse_codex_file`` (carries ``current_model``) and
    ``_parse_grok_file`` (carry ``model_by_sid``/``last_global_model``) are NOT, which is
    why only this one is parsed incrementally (``_cached_claude_states``).

    Opened in binary so ``start`` is a real byte offset: text-mode ``seek()`` only accepts
    opaque cookies from ``tell()``. ``json.loads`` takes bytes directly, and decoding a
    mangled line raises UnicodeDecodeError, which is skipped alongside JSONDecodeError.

    The dedup key ``r`` is the API message id, falling back to ``requestId`` and then the
    line uuid. Every line of one message (one per content block, each carrying the full
    usage) shares that id, and so does a copy of the message in another transcript. Some
    transcripts carry no ``requestId`` on any line (they have no ``version`` field either,
    and some repeat another session's messages).

    Subagent transcripts (any file under a ``subagents/`` directory) carry two extra
    per-line fields for the summarizer's output estimate: ``f`` marks a line that holds
    final usage (a non-null ``stop_reason`` or ``usage.output_tokens_details``), and a
    non-final line records its visible output size ``v`` keyed by its line uuid ``i``.
    Several Claude Code versions log only the start-of-message usage for many subagent
    turns, so no line of such a request ever
    holds the real ``output_tokens`` and the max-output fold has nothing to recover.
    Main-session lines never carry these fields. The flag derives from the path, which is
    the parse cache's key, so the cache stays a pure function of each entry's file.
    """
    try:
        handle = path.open("rb")
    except OSError:
        return None
    subagent = "subagents" in path.parts
    out: list[dict[str, Any]] = []
    with handle:
        if start > 0:
            handle.seek(start)
        remaining = None if end is None else end - start
        for line in handle:
            if remaining is not None:
                remaining -= len(line)
                if remaining < 0:
                    break
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(record, dict):
                continue
            if record.get("type") != "assistant":
                continue
            message = as_dict(record.get("message"))
            usage = message.get("usage") or record.get("usage")
            if usage_token_total(usage) <= 0:
                continue
            rec: dict[str, Any] = {
                "t": record.get("timestamp"),
                "m": message.get("model") or record.get("model"),
                "u": slim_usage(usage),
                "r": str(message.get("id") or record.get("requestId") or record.get("uuid") or ""),
            }
            if subagent:
                if (message.get("stop_reason") is not None
                        or (isinstance(usage, dict) and "output_tokens_details" in usage)):
                    rec["f"] = 1
                else:
                    visible = _visible_output_chars(message.get("content"))
                    if visible > 0:
                        rec["v"] = visible
                        rec["i"] = str(record.get("uuid") or "")
            out.append(rec)
    return out


def _usage_delta(total: dict[str, Any], prev: dict[str, Any] | None) -> dict[str, Any]:
    """Per-field increase of a cumulative usage dict over the previous one (numeric fields)."""
    if not prev:
        return dict(total)
    out: dict[str, Any] = {}
    for key, value in total.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            before = prev.get(key)
            out[key] = max(0, value - before) if isinstance(before, (int, float)) else value
    return out


def _parse_codex_file(path: Path) -> list[dict[str, Any]] | None:
    """Per-turn usage records from a Codex session JSONL.

    Each ``token_count`` event carries the turn's ``last_token_usage`` and the session's
    cumulative ``total_token_usage``. Codex re-emits the event without new usage (e.g. a
    rate-limit refresh) — same cumulative total, same ``last_token_usage`` — so an event
    whose total hasn't moved since the previous one is a REPEAT and is skipped (observed:
    100 of 13,809 real events). An event with only the cumulative total contributes its
    increase over the previous total, never the whole running sum.

    Each record is ONE model request (Codex emits a token_count after every response),
    which is what lets usage_cost_usd pick its prompt-length tier. A cumulative-only delta
    spans several requests only when Codex skipped events in between; the log then holds
    nothing to split it by, so it is priced as one request."""
    # errors="replace": one stray non-UTF-8 byte in a log must cost its line, not the file.
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return None
    out: list[dict[str, Any]] = []
    with handle:
        current_model: str | None = None
        prev_total: dict[str, Any] | None = None
        for line in handle:
            if '"model"' in line:
                try:
                    meta = json.loads(line)
                except json.JSONDecodeError:
                    meta = None
                if isinstance(meta, dict):
                    payload = as_dict(meta.get("payload"), meta)
                    m = payload.get("model") if isinstance(payload, dict) else None
                    if isinstance(m, str) and m:
                        current_model = m
            if "last_token_usage" not in line and "total_token_usage" not in line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = as_dict(record.get("payload"))
            info = as_dict(payload.get("info"))
            last = info.get("last_token_usage")
            total = info.get("total_token_usage")
            total = total if isinstance(total, dict) and total else None
            if total is not None and prev_total is not None and total == prev_total:
                continue
            if isinstance(last, dict) and last:
                usage: Any = last
            elif total is not None:
                usage = _usage_delta(total, prev_total)
            else:
                usage = {}
            if total is not None:
                prev_total = total
            if usage_token_total(usage) <= 0:
                continue
            out.append({"t": record.get("timestamp"), "m": current_model, "u": slim_usage(usage)})
    return out


def _display_grok_model(model: str | None) -> str:
    if not model or not str(model).strip():
        return "Grok Build"
    m = str(model).strip()
    if " " in m:
        return m
    low = m.lower().replace("_", "-")

    if "composer" in low:
        ver = re.search(r"composer-?(\d+(?:\.\d+)*)", low)
        return f"Composer {ver.group(1)}" if ver else "Composer"

    if "build" in low:
        return "Grok Build"

    ver = re.match(r"grok-?(\d+(?:\.\d+)*)", low)
    if ver:
        return f"Grok {ver.group(1)}"

    if low.startswith("grok-"):
        return "Grok " + m[5:].replace("-", " ").strip().title()
    return m


def _grok_cli_pricing_model(model: str | None) -> str:
    key = _grok_pricing_model(model)
    if re.fullmatch(r"grok-\d+(?:\.\d+)*", key):
        return "grok-build-0.1"
    return key


def _grok_pricing_model(model: str | None) -> str:
    if not model or not str(model).strip():
        return DEFAULT_MODEL_FOR_PROVIDER["grok"]
    raw = str(model).strip()
    low = raw.lower().replace("_", "-")

    if "composer" in low:
        ver = re.search(r"composer-?(\d+(?:\.\d+)*)", low) or re.search(
            r"composer\s+(\d+(?:\.\d+)*)", low
        )
        if ver:
            return f"grok-composer-{ver.group(1)}-fast"
        return "grok-composer-2.5-fast"
    if "build" in low:
        return "grok-build-0.1"
    ver = re.search(r"(?:grok[-\s]?)(\d+(?:\.\d+)*)", low)
    if ver:
        return f"grok-{ver.group(1)}"
    if low.startswith("grok-"):
        return raw
    return raw


def _parse_grok_file(path: Path) -> list[dict[str, Any]] | None:
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return None
    out: list[dict[str, Any]] = []
    model_by_sid: dict[str, str] = {}
    last_global_model: str | None = None

    def _remember(model: Any, sid_key: str) -> None:
        nonlocal last_global_model
        if not isinstance(model, str) or not model.strip():
            return
        name = model.strip()
        last_global_model = name
        if sid_key:
            model_by_sid[sid_key] = name

    with handle:
        for line in handle:
            if (
                "shell.turn.inference_done" not in line
                and "model changed" not in line
                and "model switch" not in line
                and "current_model_id" not in line
                and "new_model" not in line
                and "session_model_id" not in line
            ):
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            msg = record.get("msg")
            ctx = as_dict(record.get("ctx"))
            sid = record.get("sid")
            sid_key = str(sid) if isinstance(sid, str) and sid else ""

            if msg == "model changed":
                _remember(ctx.get("model"), sid_key)
                continue

            if msg == "backend_search: model switch":
                _remember(ctx.get("new_model") or ctx.get("model"), sid_key)
                continue

            if msg != "shell.turn.inference_done":
                for key in (
                    "current_model_id",
                    "session_model_id",
                    "global_model_id",
                    "new_model",
                    "model_id",
                    "canonical_model",
                    "effective_model",
                ):
                    if key in ctx:
                        _remember(ctx.get(key), sid_key)
                        break
                continue

            prompt = ctx.get("prompt_tokens")
            completion = ctx.get("completion_tokens")
            if not isinstance(prompt, (int, float)) and not isinstance(completion, (int, float)):
                continue
            prompt_i = int(prompt) if isinstance(prompt, (int, float)) and prompt > 0 else 0
            completion_i = int(completion) if isinstance(completion, (int, float)) and completion > 0 else 0
            cached = ctx.get("cached_prompt_tokens")
            cached_i = int(cached) if isinstance(cached, (int, float)) and cached > 0 else 0
            if cached_i > prompt_i:
                cached_i = prompt_i

            usage = {
                "input_tokens": prompt_i,
                "cached_input_tokens": cached_i,
                "output_tokens": completion_i,
            }
            if usage_token_total(usage) <= 0:
                continue

            model = model_by_sid.get(sid_key) or last_global_model
            loop = ctx.get("loop_index")
            dedup = f"{sid_key}:{loop}:{record.get('ts')}"
            out.append({
                "t": record.get("ts"),
                "m": model,
                "u": slim_usage(usage),
                "r": dedup,
            })
    return out


def _claude_state_in_window(path: Path, off: int, state: dict[str, Any],
                            window_start: int) -> dict[str, Any]:
    """``state`` as if the file held only its records from ``window_start`` on. The
    summary keeps or skips a group lying wholly on one side of the start as a unit; one
    that straddles it (a message whose lines span that instant) needs its in-window
    records folded again, from a re-read of the file's ``[0, off)``. Rare: a message's
    lines are seconds apart."""
    if not any(row[3] < window_start <= row[4] for row in state["g"].values()):
        return state
    records = _parse_claude_file(path, 0, off)
    if records is None:
        return state
    return _claude_state([r for r in records
                          if (t := _epoch_seconds(r["t"])) is not None and t >= window_start])


def local_claude_token_summary(
    projects_dir: Path | None = None,
    now: dt.datetime | None = None,
    tier: str | None = None,
    cache_dir: Any = _UNSET,
    deadline: float | None = None,
) -> dict[str, str] | None:
    root = projects_dir or (Path.home() / ".claude" / "projects")
    extra_roots = () if projects_dir is not None else _claude_extra_roots(root, deadline)
    if not root.is_dir() and not extra_roots:
        return None
    cache_path = _resolve_cache_path(cache_dir, projects_dir, "claude_logs.json")

    current = now.astimezone() if now is not None else dt.datetime.now(dt.timezone.utc).astimezone()
    today = current.date()
    # "Last 30 days" = 30 local calendar dates including today. Counting dates rather than
    # subtracting a timedelta from now keeps it 30 across a DST change.
    # → tests/test_log_windows.py
    thirty_days_ago = dt.datetime.combine(today - dt.timedelta(days=29), dt.time.min).astimezone()
    current_month_start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    history_start = min(thirty_days_ago, current_month_start)
    today_tokens = 0
    month_tokens = 0
    today_cost = 0.0
    month_cost = 0.0
    split_today, split_week, split_month = empty_type_split(), empty_type_split(), empty_type_split()
    model_costs: dict[str, dict[str, Any]] = {}
    daily = empty_weekly_token_buckets(current)
    monthly_buckets = empty_monthly_token_buckets(current)
    hourly = empty_hourly_token_buckets(current)
    window_start = int(history_start.timestamp())
    month_window_start = int(thirty_days_ago.timestamp())
    default_model = DEFAULT_MODEL_FOR_PROVIDER["claude"]
    # Each request kept as [usage, model, epoch second, output tokens].
    best: dict[str, list[Any]] = {}
    keyless: list[list[Any]] = []
    # Subagent output estimate (see _parse_claude_file): requests that have a final-usage
    # line, and the visible output of the others' non-final lines keyed by line uuid, so a
    # line seen twice is counted once — the same idempotence the max-output fold has.
    final_requests: set[str] = set()
    visible_chars: dict[str, dict[str, int]] = {}
    estimated_output = 0

    for path, off, state in _cached_claude_states(root, cache_path, deadline=deadline,
                                                    extra_roots=extra_roots):
        if state["hi"] is None or state["hi"] < window_start:
            continue
        if state["lo"] < window_start:
            state = _claude_state_in_window(path, off, state, window_start)
        for request_key, (t, model, usage, _lo, hi, final, visible) in state["g"].items():
            if hi < window_start:
                continue
            if final:
                final_requests.add(request_key)
            if visible:
                visible_chars.setdefault(request_key, {}).update(visible)
            out = _out_tokens(usage)
            prev = best.get(request_key)
            if prev is None or out > prev[3]:
                best[request_key] = [usage, model or default_model, t, out]
        for t, model, usage in state["k"]:
            if t >= window_start:
                keyless.append([usage, model or default_model, t, _out_tokens(usage)])

    # A subagent request with no final-usage line bills max(logged output, visible
    # characters // 4): a lower bound, since thinking tokens are not visible. Main-session
    # requests never reach here (they carry no "v"); a final request keeps its logged output.
    for request_key, chars in visible_chars.items():
        if request_key in final_requests:
            continue
        entry = best[request_key]
        estimate = sum(chars.values()) // 4
        if estimate <= entry[3]:
            continue
        if entry[2] >= month_window_start:
            estimated_output += estimate - entry[3]
        entry[0] = {**entry[0], "output_tokens": estimate}
        entry[3] = estimate

    # Local day and hour per epoch MINUTE: every zone's offset is a whole number of
    # minutes, so all seconds of a minute share them.
    local_slots: dict[int, tuple[str, int, bool]] = {}
    for usage, model, t, _out in (*best.values(), *keyless):
        split = usage_split(usage, model)
        tokens, cost = split.tokens, split.cost
        slot = local_slots.get(t // 60)
        if slot is None:
            local = dt.datetime.fromtimestamp(t // 60 * 60)
            slot = local_slots[t // 60] = (local.date().isoformat(), local.hour,
                                           local.date() == today)
        day_key, hour, is_today = slot
        if t >= month_window_start:
            month_tokens += tokens
            month_cost += cost
            type_split_add(split_month, split)
            model_slot = model_costs.setdefault(str(model), {"cost": 0.0, "tokens": 0})
            model_slot["cost"] += cost
            model_slot["tokens"] += tokens
        if day_key in daily:
            bucket_add(daily[day_key], tokens, cost, model)
            type_split_add(split_week, split)
        if day_key in monthly_buckets and monthly_buckets[day_key].get("inMonth"):
            bucket_add(monthly_buckets[day_key], tokens, cost, model)
        if is_today:
            today_tokens += tokens
            today_cost += cost
            type_split_add(split_today, split)
            bucket_add(hourly[hour], tokens, cost, model)

    summary = token_summary(today_tokens, month_tokens, "local-claude-logs", today_cost, month_cost, tier,
                            now=current,
                            type_split={"today": split_today, "7d": split_week, "30d": split_month},
                            daily=weekly_token_usage(daily),
                            monthly=monthly_token_usage(monthly_buckets),
                            hourly=hourly_token_usage(hourly),
                            model_costs=model_costs,
                            model_costs_7d=buckets_model_costs(daily),
                            model_costs_1d=daily.get(today.isoformat(), {}).get("models"),
                            estimated_output_tokens=estimated_output)
    # A model with no price of its own (one newer than both the live catalog and the
    # embedded fallback) is billed at the claude-sonnet-4 family default. Name it, so
    # build_snapshot can surface it as a diagnostic rather than mis-price it silently.
    unpriced = sorted(m for m in model_costs if not has_catalog_price(m))
    if summary is not None and unpriced:
        summary["unpricedModels"] = unpriced
    return summary


def local_codex_token_summary(
    sessions_dir: Path | None = None,
    now: dt.datetime | None = None,
    tier: str | None = None,
    cache_dir: Any = _UNSET,
    deadline: float | None = None,
) -> dict[str, str] | None:
    root = sessions_dir or (Path.home() / ".codex" / "sessions")
    if not root.is_dir():
        return None
    cache_path = _resolve_cache_path(cache_dir, sessions_dir, "codex_logs.json")

    current = now.astimezone() if now is not None else dt.datetime.now(dt.timezone.utc).astimezone()
    today = current.date()
    # "Last 30 days" = 30 local calendar dates including today. Counting dates rather than
    # subtracting a timedelta from now keeps it 30 across a DST change.
    # → tests/test_log_windows.py
    thirty_days_ago = dt.datetime.combine(today - dt.timedelta(days=29), dt.time.min).astimezone()
    current_month_start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    history_start = min(thirty_days_ago, current_month_start)
    today_tokens = 0
    month_tokens = 0
    today_cost = 0.0
    month_cost = 0.0
    split_today, split_week, split_month = empty_type_split(), empty_type_split(), empty_type_split()
    model_costs: dict[str, dict[str, Any]] = {}
    daily = empty_weekly_token_buckets(current)
    monthly_buckets = empty_monthly_token_buckets(current)
    hourly = empty_hourly_token_buckets(current)

    for record in _cached_log_records(root, "*.jsonl", cache_path,
                                       _parse_codex_file, _CODEX_PARSE_VERSION,
                                       deadline=deadline):
        timestamp = parse_timestamp(record["t"])
        if timestamp is None or timestamp < history_start:
            continue
        model = record["m"] or DEFAULT_MODEL_FOR_PROVIDER["codex"]
        split = usage_split(record["u"], model)
        if split.tokens <= 0:
            continue
        tokens, cost = split.tokens, split.cost
        day_key = timestamp.date().isoformat()
        if timestamp >= thirty_days_ago:
            month_tokens += tokens
            month_cost += cost
            type_split_add(split_month, split)
            slot = model_costs.setdefault(str(model), {"cost": 0.0, "tokens": 0})
            slot["cost"] += cost
            slot["tokens"] += tokens
        if day_key in daily:
            bucket_add(daily[day_key], tokens, cost, model)
            type_split_add(split_week, split)
        if day_key in monthly_buckets and monthly_buckets[day_key].get("inMonth"):
            bucket_add(monthly_buckets[day_key], tokens, cost, model)
        if timestamp.date() == today:
            today_tokens += tokens
            today_cost += cost
            type_split_add(split_today, split)
            bucket_add(hourly[timestamp.hour], tokens, cost, model)

    return token_summary(today_tokens, month_tokens, "local-codex-logs", today_cost, month_cost, tier,
                         now=current,
                         type_split={"today": split_today, "7d": split_week, "30d": split_month},
                         daily=weekly_token_usage(daily),
                         monthly=monthly_token_usage(monthly_buckets),
                         hourly=hourly_token_usage(hourly),
                         model_costs=model_costs,
                         model_costs_7d=buckets_model_costs(daily),
                         model_costs_1d=daily.get(today.isoformat(), {}).get("models"))


def _load_grok_session_models(sessions_dir: Path | None = None) -> dict[str, str]:
    root = sessions_dir or (Path.home() / ".grok" / "sessions")
    if not root.is_dir():
        return {}
    out: dict[str, str] = {}
    try:
        paths = root.rglob("summary.json")
    except OSError:
        return {}
    for path in paths:
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        model = data.get("current_model_id")
        if not isinstance(model, str) or not model.strip():
            continue
        info = as_dict(data.get("info"))
        sid = info.get("id") if isinstance(info, dict) else None
        if not isinstance(sid, str) or not sid:
            sid = path.parent.name
        if isinstance(sid, str) and sid:
            out[sid] = model.strip()
    return out


def local_grok_token_summary(
    logs_dir: Path | None = None,
    sessions_dir: Path | None = None,
    now: dt.datetime | None = None,
    tier: str | None = None,
    cache_dir: Any = _UNSET,
    deadline: float | None = None,
    week_start: dt.datetime | None = None,
    week_end: dt.datetime | None = None,
) -> dict[str, str] | None:
    root = logs_dir or (Path.home() / ".grok" / "logs")
    if not root.is_dir():
        return None
    cache_path = _resolve_cache_path(cache_dir, logs_dir, "grok_logs.json")

    current = now.astimezone() if now is not None else dt.datetime.now(dt.timezone.utc).astimezone()
    today = current.date()
    # "Last 30 days" = 30 local calendar dates including today. Counting dates rather than
    # subtracting a timedelta from now keeps it 30 across a DST change.
    # → tests/test_log_windows.py
    thirty_days_ago = dt.datetime.combine(today - dt.timedelta(days=29), dt.time.min).astimezone()
    current_month_start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    history_start = min(thirty_days_ago, current_month_start)
    first_day = thirty_days_ago.date()
    today_tokens = 0
    month_tokens = 0
    today_cost = 0.0
    month_cost = 0.0
    split_today, split_week, split_month = empty_type_split(), empty_type_split(), empty_type_split()
    model_costs: dict[str, dict[str, Any]] = {}
    daily = empty_weekly_token_buckets(current)
    monthly_buckets = empty_monthly_token_buckets(current)
    hourly = empty_hourly_token_buckets(current)
    week_tokens_billing = 0
    week_cost_billing = 0.0
    live_days: dict[str, dict[str, Any]] = {}

    seen: set[str] = set()
    if sessions_dir is not None:
        sessions_root = sessions_dir
    elif logs_dir is not None:
        sibling = logs_dir.parent / "sessions"
        sessions_root = sibling if sibling.is_dir() else Path.home() / ".grok" / "sessions"
    else:
        sessions_root = Path.home() / ".grok" / "sessions"
    session_models = _load_grok_session_models(sessions_root)

    rows: list[tuple[dt.datetime, str, Any, str]] = []
    live_week: dict[str, list[Any]] = {}
    for record in _cached_log_records(root, "unified.jsonl*", cache_path,
                                       _parse_grok_file, _GROK_PARSE_VERSION,
                                       deadline=deadline):
        timestamp = parse_timestamp(record["t"])
        if timestamp is None or timestamp < history_start:
            continue
        request_key = str(record.get("r") or "")
        if request_key:
            if request_key in seen:
                continue
            seen.add(request_key)
        raw_model = record.get("m")
        if not raw_model and request_key:
            sid = request_key.split(":", 1)[0]
            raw_model = session_models.get(sid)
        display_model = _display_grok_model(raw_model)
        split = usage_split(record["u"], _grok_cli_pricing_model(raw_model or display_model))
        if split.tokens <= 0:
            continue
        tokens, cost = split.tokens, split.cost
        day_key = timestamp.date().isoformat()
        rows.append((timestamp, day_key, split, display_model))
        lrec = live_days.setdefault(day_key, {"tokens": 0, "cost": 0.0, "in": 0, "out": 0,
                                              "cached": 0, "types": empty_type_split(),
                                              "models": {}})
        lrec["tokens"] += tokens
        lrec["cost"] += cost
        lrec["in"] += split.type_tokens[0]
        lrec["out"] += split.type_tokens[1]
        lrec["cached"] += split.type_tokens[2] + split.type_tokens[3]
        type_split_add(lrec["types"], split)
        lslot = lrec["models"].setdefault(str(display_model), {"cost": 0.0, "tokens": 0})
        lslot["cost"] += cost
        lslot["tokens"] += tokens
        if week_start is not None and timestamp >= week_start and (week_end is None or timestamp < week_end):
            week_tokens_billing += tokens
            week_cost_billing += cost
            wk = live_week.setdefault(day_key, [0, 0.0])
            wk[0] += tokens
            wk[1] += cost

    # unified.jsonl rotates, so a day can sit in both the log and the archive with the
    # log holding only its tail. Per day the larger figure wins, tokens and cost together
    # from ONE source; merge_grok_archive keeps the live record when it is the larger.
    # Archived days hold cost already priced per request, at the rates in force when they
    # were archived, while their lines were in unified.jsonl: only the per-day total
    # survives a rotation, and a total can't pick a tier.
    archived: dict[str, dict[str, Any]] = {}
    fixture_driven = logs_dir is not None or cache_dir is not _UNSET
    if not fixture_driven:
        try:
            for day, rec in merge_grok_archive(live_days, today.isoformat()).items():
                if rec is not live_days.get(day):
                    archived[day] = rec
        except Exception:
            archived = {}
    archive_wins = {day for day in archived if day in live_days}

    for timestamp, day_key, split, display_model in rows:
        tokens, cost = split.tokens, split.cost
        if timestamp.date() == today:
            bucket_add(hourly[timestamp.hour], tokens, cost, display_model)
        if day_key in archive_wins:
            continue
        if timestamp.date() >= first_day:
            month_tokens += tokens
            month_cost += cost
            type_split_add(split_month, split)
            slot = model_costs.setdefault(str(display_model), {"cost": 0.0, "tokens": 0})
            slot["cost"] += cost
            slot["tokens"] += tokens
        if day_key in daily:
            bucket_add(daily[day_key], tokens, cost, display_model)
            type_split_add(split_week, split)
        if day_key in monthly_buckets and monthly_buckets[day_key].get("inMonth"):
            bucket_add(monthly_buckets[day_key], tokens, cost, display_model)
        if timestamp.date() == today:
            today_tokens += tokens
            today_cost += cost
            type_split_add(split_today, split)

    try:
        for day, rec in archived.items():
            try:
                dday = dt.date.fromisoformat(day)
            except ValueError:
                continue
            d_tokens = int(rec.get("tokens") or 0)
            d_cost = float(rec.get("cost") or 0.0)
            if d_tokens <= 0 and d_cost <= 0.0:
                continue
            models = as_dict(rec.get("models"))
            day_split = _grok_archive_split(rec, d_tokens, d_cost)
            if dday >= first_day:
                month_tokens += d_tokens
                month_cost += d_cost
                type_split_merge(split_month, day_split)
                for mname, mrec in models.items():
                    slot = model_costs.setdefault(str(mname), {"cost": 0.0, "tokens": 0})
                    slot["cost"] += float(mrec.get("cost") or 0.0)
                    slot["tokens"] += int(mrec.get("tokens") or 0)
            if day in daily:
                type_split_merge(split_week, day_split)
            if dday == today:
                today_tokens += d_tokens
                today_cost += d_cost
                type_split_merge(split_today, day_split)
            for bucket_map in (daily, monthly_buckets):
                if day not in bucket_map:
                    continue
                if bucket_map is monthly_buckets and not bucket_map[day].get("inMonth"):
                    continue
                for mname, mrec in (models or {"Grok Build": {"cost": d_cost,
                                                              "tokens": d_tokens}}).items():
                    bucket_add(bucket_map[day], int(mrec.get("tokens") or 0),
                               float(mrec.get("cost") or 0.0), str(mname))
            if week_start is not None:
                # A whole local day inside the billing week is replaced by the archived
                # figure; a day the week starts or ends in can't be split from a total.
                day_start = dt.datetime.combine(dday, dt.time.min).astimezone()
                day_end = dt.datetime.combine(dday + dt.timedelta(days=1), dt.time.min).astimezone()
                if day_start >= week_start and (week_end is None or day_end <= week_end):
                    live_tokens, live_cost = live_week.get(day, (0, 0.0))
                    week_tokens_billing += d_tokens - live_tokens
                    week_cost_billing += d_cost - live_cost
    except Exception:
        pass

    summary = token_summary(today_tokens, month_tokens, "local-grok-logs", today_cost, month_cost, tier,
                            now=current,
                            type_split={"today": split_today, "7d": split_week, "30d": split_month},
                            daily=weekly_token_usage(daily),
                            monthly=monthly_token_usage(monthly_buckets),
                            hourly=hourly_token_usage(hourly),
                            model_costs=model_costs,
                            model_costs_7d=buckets_model_costs(daily),
                            model_costs_1d=daily.get(today.isoformat(), {}).get("models"))
    if summary is not None and week_start is not None:
        summary["billingWeekTokens"] = int(week_tokens_billing)
        summary["billingWeekCost"] = round(float(week_cost_billing), 6)
    return summary


def _grok_archive_split(rec: dict[str, Any], tokens: int, cost: float) -> dict[str, dict[str, Any]]:
    """An archived Grok day's type split (empty_type_split shape), adding up to the
    day's archived ``tokens`` and ``cost``.

    A day archived since the split existed carries it under "types". An older day kept
    only its in/out/cached token counts and one cost. Its tokens split exactly (xAI
    reports no cache writes, so cached is all reads). Its cost can't be re-priced per
    request once the log has rotated, so it is shared out by each type's cost at
    grok-build's rates, the rates the CLI's models are priced at; the day's total stays
    exactly what was archived."""
    acc = empty_type_split()
    stored = rec.get("types")
    if isinstance(stored, dict):
        type_split_merge(acc, stored)
        return acc
    typed = {"input": int(rec.get("in") or 0), "output": int(rec.get("out") or 0),
             "cacheRead": int(rec.get("cached") or 0)}
    prices = _pricing.model_pricing(DEFAULT_MODEL_FOR_PROVIDER["grok"])
    rates = {"input": prices.get("input", 0.0), "output": prices.get("output", 0.0),
             "cacheRead": prices.get("cache_read", prices.get("input", 0.0))}
    weights = {key: typed[key] * rates[key] for key in typed}
    weight_sum = sum(weights.values()) or sum(typed.values())
    if weight_sum <= 0:
        weights = {}
    elif not sum(weights.values()):
        weights = dict(typed)
    for key, count in typed.items():
        acc[key]["tokens"] += count
        if weights:
            acc[key]["cost"] += cost * weights[key] / weight_sum
    extra = tokens - sum(typed.values())
    if extra > 0:
        acc["unsplit"]["tokens"] += extra
    if not weights:
        acc["unsplit"]["cost"] += cost
    return acc


GROK_ARCHIVE_PATH = Path.home() / ".tallybar" / "grok_archive.json"
_GROK_ARCHIVE_KEEP_DAYS = 120


def _get_grok_archive_path() -> Path:
    import sys
    acct = sys.modules.get("accounting")
    if acct is not None and hasattr(acct, "GROK_ARCHIVE_PATH"):
        return Path(acct.GROK_ARCHIVE_PATH)
    return GROK_ARCHIVE_PATH


def _load_grok_archive() -> dict[str, Any]:
    return load_json_with_backup(
        _get_grok_archive_path(),
        lambda data: isinstance(data, dict) and isinstance(data.get("days"), dict),
        {"version": 1, "days": {}},
    )


def _save_grok_archive(archive: dict[str, Any]) -> None:
    save_json_with_backup(_get_grok_archive_path(), archive)


def merge_grok_archive(live_days: dict[str, dict[str, Any]],
                       today_iso: str,
                       keep_days: int = _GROK_ARCHIVE_KEEP_DAYS) -> dict[str, dict[str, Any]]:
    archive = _load_grok_archive()
    days: dict[str, Any] = dict(archive.get("days") or {})
    before = json.dumps(days, sort_keys=True)

    for day, rec in (live_days or {}).items():
        prev = days.get(day)
        if not isinstance(prev, dict) or int(rec.get("tokens") or 0) >= int(prev.get("tokens") or 0):
            days[day] = rec

    if len(days) > keep_days:
        for stale in sorted(days)[:-keep_days]:
            days.pop(stale, None)

    if json.dumps(days, sort_keys=True) != before:
        archive["days"] = days
        archive["version"] = 1
        archive["updatedAt"] = today_iso
        _save_grok_archive(archive)
    return days


def _gemini_session_files(base: Path):
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if not d.startswith("antigravity")]
        for fn in filenames:
            if fn.startswith("session-") and fn.endswith((".json", ".jsonl")):
                yield Path(dirpath) / fn


def _gemini_session_messages(path: Path) -> list[Any] | None:
    """The messages of a Gemini CLI session: one JSON document (``.json``), or an
    append-only JSON-lines log (``.jsonl``) that carries a header line, ``$set`` updates
    and each message again whenever it is amended."""
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return None
    with handle:
        if path.suffix == ".jsonl":
            messages: list[Any] = []
            for line in handle:
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            return messages
        try:
            session = json.load(handle)
        except json.JSONDecodeError:
            return []
    if not isinstance(session, dict) or not isinstance(session.get("messages"), list):
        return []
    return session["messages"]


def _parse_gemini_file(path: Path) -> list[dict[str, Any]] | None:
    """Per-message usage records. A message re-logged under the same ``id`` (the CLI
    rewrites it as its tool calls land) keeps one record, the one with the most tokens;
    ``r`` carries the id so the summary can drop a copy held by another file."""
    messages = _gemini_session_messages(path)
    if messages is None:
        return None
    out: list[dict[str, Any]] = []
    by_id: dict[str, int] = {}
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("type") != "gemini":
            continue
        ts = msg.get("timestamp")
        tokens_data = msg.get("tokens")
        if not isinstance(ts, str) or not isinstance(tokens_data, dict):
            continue
        if usage_token_total(tokens_data) <= 0:
            continue
        rec: dict[str, Any] = {"t": ts, "m": msg.get("model"), "u": slim_usage(tokens_data)}
        msg_id = msg.get("id")
        if isinstance(msg_id, str) and msg_id:
            rec["r"] = msg_id
            at = by_id.get(msg_id)
            if at is not None:
                if usage_token_total(rec["u"]) > usage_token_total(out[at]["u"]):
                    out[at] = rec
                continue
            by_id[msg_id] = len(out)
        out.append(rec)
    return out


def local_gemini_token_summary(
    gemini_dir: Path | None = None,
    now: dt.datetime | None = None,
    tier: str | None = None,
    cache_dir: Any = _UNSET,
    deadline: float | None = None,
) -> dict[str, str] | None:
    root = gemini_dir or (Path.home() / ".gemini")
    if not root.is_dir():
        return None
    cache_path = _resolve_cache_path(cache_dir, gemini_dir, "gemini_logs.json")

    current = now.astimezone() if now is not None else dt.datetime.now(dt.timezone.utc).astimezone()
    today = current.date()
    # "Last 30 days" = 30 local calendar dates including today. Counting dates rather than
    # subtracting a timedelta from now keeps it 30 across a DST change.
    # → tests/test_log_windows.py
    thirty_days_ago = dt.datetime.combine(today - dt.timedelta(days=29), dt.time.min).astimezone()
    current_month_start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    history_start = min(thirty_days_ago, current_month_start)
    today_tokens = 0
    month_tokens = 0
    today_cost = 0.0
    month_cost = 0.0
    split_today, split_week, split_month = empty_type_split(), empty_type_split(), empty_type_split()
    model_costs: dict[str, dict[str, Any]] = {}
    daily = empty_weekly_token_buckets(current)
    monthly_buckets = empty_monthly_token_buckets(current)
    hourly = empty_hourly_token_buckets(current)

    seen_ids: set[str] = set()
    for record in _cached_log_records(root, "session-*.json", cache_path,
                                       _parse_gemini_file, _GEMINI_PARSE_VERSION,
                                       walker=_gemini_session_files, deadline=deadline):
        timestamp = parse_timestamp(record.get("t") or "")
        if timestamp is None or timestamp < history_start:
            continue
        msg_id = record.get("r")
        if msg_id:
            if msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
        tokens_data = record.get("u")
        if not isinstance(tokens_data, dict):
            continue
        model = record.get("m") or DEFAULT_MODEL_FOR_PROVIDER["gemini"]
        split = usage_split(tokens_data, model)
        if split.tokens <= 0:
            continue
        tokens, cost = split.tokens, split.cost

        day_key = timestamp.date().isoformat()
        if timestamp >= thirty_days_ago:
            month_tokens += tokens
            month_cost += cost
            type_split_add(split_month, split)
            slot = model_costs.setdefault(str(model), {"cost": 0.0, "tokens": 0})
            slot["cost"] += cost
            slot["tokens"] += tokens
        if day_key in daily:
            bucket_add(daily[day_key], tokens, cost, model)
            type_split_add(split_week, split)
        if day_key in monthly_buckets and monthly_buckets[day_key].get("inMonth"):
            bucket_add(monthly_buckets[day_key], tokens, cost, model)
        if timestamp.date() == today:
            today_tokens += tokens
            today_cost += cost
            type_split_add(split_today, split)
            bucket_add(hourly[timestamp.hour], tokens, cost, model)

    return token_summary(today_tokens, month_tokens, "local-gemini-logs", today_cost, month_cost, tier,
                         now=current,
                         type_split={"today": split_today, "7d": split_week, "30d": split_month},
                         daily=weekly_token_usage(daily),
                         monthly=monthly_token_usage(monthly_buckets),
                         hourly=hourly_token_usage(hourly),
                         model_costs=model_costs,
                         model_costs_7d=buckets_model_costs(daily),
                         model_costs_1d=daily.get(today.isoformat(), {}).get("models"))
