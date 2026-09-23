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

from io_helpers import atomic_write_text
from parsers import as_dict
from .formatting import parse_timestamp
from .pricing import (
    DEFAULT_MODEL_FOR_PROVIDER,
    slim_usage,
    usage_cost_usd,
    usage_token_total,
    usage_token_total_and_breakdown,
)
from .buckets import (
    bucket_add,
    empty_hourly_token_buckets,
    empty_monthly_token_buckets,
    empty_weekly_token_buckets,
    hourly_token_usage,
    monthly_token_usage,
    token_summary,
    weekly_token_usage,
)

_UNSET = object()

_PARSE_CACHE_DIR = Path.home() / ".tallybar" / "cache"
_CACHE_SCHEMA_VERSION = 1          # bump to invalidate ALL parse caches at once
_CLAUDE_PARSE_VERSION = 3          # bump when _parse_claude_file's output shape changes
_CODEX_PARSE_VERSION = 3           # bump when _parse_codex_file's output shape changes
_GROK_PARSE_VERSION = 3            # bump when _parse_grok_file's output shape changes
_GEMINI_PARSE_VERSION = 2          # bump when _parse_gemini_file's output shape changes


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
    """Extend a cached file's records with only the complete lines appended since the last
    parse, i.e. the bytes ``[prev["off"], end)``.

    Returns the full record list, or None when anything looks off — the caller then falls
    back to a full reparse. FAIL-CLOSED by design: every bail-out costs one ordinary full
    parse, whereas a wrong incremental result would silently corrupt the cost history
    until the cache version is next bumped.

    Bails out when the previous entry lacks a usable offset/fingerprint/records list, when
    the file SHRANK (truncated or rotated — the old offset now points into unrelated
    bytes), when the size is unchanged but the mtime moved (an in-place rewrite), and when
    the bytes just before the old offset no longer match their fingerprint (rewritten AND
    grown). Append-only growth is the only case it accepts.
    """
    prev_off = prev.get("off")
    prev_fp = prev.get("fp")
    prev_recs = prev.get("records")
    prev_size = prev.get("size")
    if (not isinstance(prev_off, int) or not isinstance(prev_fp, int)
            or not isinstance(prev_recs, list) or not isinstance(prev_size, int)):
        return None
    if size <= prev_size:              # shrank, or unchanged-size rewrite
        return None
    if prev_off < 0 or prev_off > end:
        return None
    if _offset_fingerprint(path, prev_off) != prev_fp:
        return None
    if end == prev_off:                # grew, but only by a still-unterminated line
        return list(prev_recs)
    tail = parser(path, prev_off, end)
    if tail is None:
        return None
    return prev_recs + tail


def _cached_log_records(root: Path, pattern: str, cache_path: Path | None,
                        parser, parse_version: int, walker=None,
                        deadline: float | None = None,
                        incremental: bool = False) -> list[dict[str, Any]]:
    old_files = _load_parse_cache(cache_path, parse_version) if cache_path is not None else {}
    new_files: dict[str, dict[str, Any]] = {}
    records: list[dict[str, Any]] = []
    changed = False
    complete = True
    for path in (walker(root) if walker is not None else root.rglob(pattern)):
        if deadline is not None and time.time() >= deadline:
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
        offset: int | None = None
        fingerprint: int | None = None
        recs: list[dict[str, Any]] | None = None
        if (prev is not None and prev.get("mtime") == st.st_mtime_ns
                and prev.get("size") == st.st_size):
            recs = prev.get("records") or []
            off, fp = prev.get("off"), prev.get("fp")
            if isinstance(off, int) and isinstance(fp, int):
                offset, fingerprint = off, fp
        elif not incremental:
            recs = parser(path)
            if recs is None:
                continue
            changed = True
        else:
            # The parse window's END is fixed BEFORE reading and the parser is bounded to
            # it, so the cached records are exactly the parse of bytes [0, off) no matter
            # what the agent appends meanwhile. (Parsing to EOF and measuring the offset
            # afterwards double-counted a final line whose "\n" had not landed yet.)
            end = _safe_resume_offset(path)
            if end is None:
                continue
            if prev is not None:
                # An active session's JSONL is appended to constantly, so "changed" is the
                # common case during use and a full reparse of a large file was the single
                # most expensive thing a refresh did.
                recs = _tail_parse(path, prev, parser, st.st_size, end)
            if recs is None:
                recs = parser(path, 0, end)
                if recs is None:
                    continue
            offset, fingerprint = end, _offset_fingerprint(path, end)
            changed = True
        entry: dict[str, Any] = {"mtime": st.st_mtime_ns, "size": st.st_size, "records": recs}
        if offset is not None and fingerprint is not None:
            entry["off"] = offset
            entry["fp"] = fingerprint
        new_files[key] = entry
        records.extend(recs)
    if cache_path is not None and complete and (changed or len(new_files) != len(old_files)):
        _save_parse_cache(cache_path, parse_version, new_files)
    return records


def _resolve_cache_path(explicit_dir, provided_root, name: str) -> Path | None:
    if explicit_dir is _UNSET:
        return None if provided_root is not None else _PARSE_CACHE_DIR / name
    if explicit_dir is None:
        return None
    return Path(explicit_dir) / name


def _parse_claude_file(path: Path, start: int = 0,
                       end: int | None = None) -> list[dict[str, Any]] | None:
    """Parse a Claude session JSONL — the BYTE range ``[start, end)`` of it when given
    (both must sit on line boundaries; ``end=None`` reads to EOF).

    Safe to resume because this parser is stateless across lines — each record is decided
    entirely by its own line. ``_parse_codex_file`` (carries ``current_model``) and
    ``_parse_grok_file`` (carry ``model_by_sid``/``last_global_model``) are NOT, which is
    why only this one is wired for incremental parsing in ``_cached_log_records``.

    Opened in binary so ``start`` is a real byte offset: text-mode ``seek()`` only accepts
    opaque cookies from ``tell()``. ``json.loads`` takes bytes directly, and decoding a
    mangled line now raises UnicodeDecodeError, which is skipped alongside JSONDecodeError
    rather than escaping the way it used to in text mode.
    """
    try:
        handle = path.open("rb")
    except OSError:
        return None
    out: list[dict[str, Any]] = []
    with handle:
        if start > 0:
            handle.seek(start)
        remaining = None if end is None else end - start
        for line in handle:
            if remaining is not None:
                remaining -= len(line)
                if remaining < 0:      # past the window: appended after ``end`` was fixed
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
            out.append({
                "t": record.get("timestamp"),
                "m": message.get("model") or record.get("model"),
                "u": slim_usage(usage),
                "r": str(record.get("requestId") or record.get("uuid") or ""),
            })
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
    increase over the previous total, never the whole running sum."""
    try:
        handle = path.open("r", encoding="utf-8")
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
                continue  # repeated token_count: the session's cumulative total hasn't moved
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
        handle = path.open("r", encoding="utf-8")
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


def local_claude_token_summary(
    projects_dir: Path | None = None,
    now: dt.datetime | None = None,
    tier: str | None = None,
    cache_dir: Any = _UNSET,
    deadline: float | None = None,
) -> dict[str, str] | None:
    root = projects_dir or (Path.home() / ".claude" / "projects")
    if not root.is_dir():
        return None
    cache_path = _resolve_cache_path(cache_dir, projects_dir, "claude_logs.json")

    current = now.astimezone() if now is not None else dt.datetime.now(dt.timezone.utc).astimezone()
    today = current.date()
    thirty_days_ago = (current - dt.timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
    current_month_start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    history_start = min(thirty_days_ago, current_month_start)
    today_tokens = 0
    month_tokens = 0
    today_cost = 0.0
    month_cost = 0.0
    month_in = month_out = month_cached = 0
    model_costs: dict[str, dict[str, Any]] = {}
    daily = empty_weekly_token_buckets(current)
    monthly_buckets = empty_monthly_token_buckets(current)
    hourly = empty_hourly_token_buckets(current)
    best: dict[str, dict[str, Any]] = {}
    keyless: list[dict[str, Any]] = []

    def _out_tokens(u: Any) -> int:
        if isinstance(u, dict):
            v = u.get("output_tokens")
            if isinstance(v, (int, float)) and v > 0:
                return int(v)
        return 0

    for record in _cached_log_records(root, "*.jsonl", cache_path,
                                       _parse_claude_file, _CLAUDE_PARSE_VERSION,
                                       deadline=deadline, incremental=True):
        timestamp = parse_timestamp(record["t"])
        if timestamp is None or timestamp < history_start:
            continue
        usage = record["u"]
        model = record["m"] or DEFAULT_MODEL_FOR_PROVIDER["claude"]
        entry = {"usage": usage, "model": model, "timestamp": timestamp, "out": _out_tokens(usage)}
        request_key = record["r"]
        if not request_key:
            keyless.append(entry)
            continue
        prev = best.get(request_key)
        if prev is None or entry["out"] > prev["out"]:
            best[request_key] = entry

    for entry in (*best.values(), *keyless):
        usage = entry["usage"]
        model = entry["model"]
        timestamp = entry["timestamp"]
        tokens, (bi, bo, bc) = usage_token_total_and_breakdown(usage)
        cost = usage_cost_usd(usage, model)
        day_key = timestamp.date().isoformat()
        if timestamp >= thirty_days_ago:
            month_tokens += tokens
            month_cost += cost
            month_in += bi
            month_out += bo
            month_cached += bc
            slot = model_costs.setdefault(str(model), {"cost": 0.0, "tokens": 0})
            slot["cost"] += cost
            slot["tokens"] += tokens
        if day_key in daily:
            bucket_add(daily[day_key], tokens, cost, model)
        if day_key in monthly_buckets and monthly_buckets[day_key].get("inMonth"):
            bucket_add(monthly_buckets[day_key], tokens, cost, model)
        if timestamp.date() == today:
            today_tokens += tokens
            today_cost += cost
            bucket_add(hourly[timestamp.hour], tokens, cost, model)

    return token_summary(today_tokens, month_tokens, "local-claude-logs", today_cost, month_cost, tier,
                         breakdown=(month_in, month_out, month_cached),
                         daily=weekly_token_usage(daily),
                         monthly=monthly_token_usage(monthly_buckets),
                         hourly=hourly_token_usage(hourly),
                         model_costs=model_costs)


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
    thirty_days_ago = (current - dt.timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
    current_month_start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    history_start = min(thirty_days_ago, current_month_start)
    today_tokens = 0
    month_tokens = 0
    today_cost = 0.0
    month_cost = 0.0
    month_in = month_out = month_cached = 0
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
        usage = record["u"]
        tokens, (bi, bo, bc) = usage_token_total_and_breakdown(usage)
        if tokens <= 0:
            continue
        model = record["m"] or DEFAULT_MODEL_FOR_PROVIDER["codex"]
        cost = usage_cost_usd(usage, model)
        day_key = timestamp.date().isoformat()
        if timestamp >= thirty_days_ago:
            month_tokens += tokens
            month_cost += cost
            month_in += bi
            month_out += bo
            month_cached += bc
            slot = model_costs.setdefault(str(model), {"cost": 0.0, "tokens": 0})
            slot["cost"] += cost
            slot["tokens"] += tokens
        if day_key in daily:
            bucket_add(daily[day_key], tokens, cost, model)
        if day_key in monthly_buckets and monthly_buckets[day_key].get("inMonth"):
            bucket_add(monthly_buckets[day_key], tokens, cost, model)
        if timestamp.date() == today:
            today_tokens += tokens
            today_cost += cost
            bucket_add(hourly[timestamp.hour], tokens, cost, model)

    return token_summary(today_tokens, month_tokens, "local-codex-logs", today_cost, month_cost, tier,
                         breakdown=(month_in, month_out, month_cached),
                         daily=weekly_token_usage(daily),
                         monthly=monthly_token_usage(monthly_buckets),
                         hourly=hourly_token_usage(hourly),
                         model_costs=model_costs)


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
    thirty_days_ago = (current - dt.timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
    current_month_start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    history_start = min(thirty_days_ago, current_month_start)
    today_tokens = 0
    month_tokens = 0
    today_cost = 0.0
    month_cost = 0.0
    month_in = month_out = month_cached = 0
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
        usage = record["u"]
        tokens, (bi, bo, bc) = usage_token_total_and_breakdown(usage)
        if tokens <= 0:
            continue
        raw_model = record.get("m")
        if not raw_model and request_key:
            sid = request_key.split(":", 1)[0]
            raw_model = session_models.get(sid)
        display_model = _display_grok_model(raw_model)
        cost = usage_cost_usd(usage, _grok_cli_pricing_model(raw_model or display_model))
        day_key = timestamp.date().isoformat()
        if timestamp >= thirty_days_ago:
            month_tokens += tokens
            month_cost += cost
            month_in += bi
            month_out += bo
            month_cached += bc
            slot = model_costs.setdefault(str(display_model), {"cost": 0.0, "tokens": 0})
            slot["cost"] += cost
            slot["tokens"] += tokens
        lrec = live_days.setdefault(day_key, {"tokens": 0, "cost": 0.0, "in": 0, "out": 0,
                                              "cached": 0, "models": {}})
        lrec["tokens"] += tokens
        lrec["cost"] += cost
        lrec["in"] += bi
        lrec["out"] += bo
        lrec["cached"] += bc
        lslot = lrec["models"].setdefault(str(display_model), {"cost": 0.0, "tokens": 0})
        lslot["cost"] += cost
        lslot["tokens"] += tokens
        if day_key in daily:
            bucket_add(daily[day_key], tokens, cost, display_model)
        if day_key in monthly_buckets and monthly_buckets[day_key].get("inMonth"):
            bucket_add(monthly_buckets[day_key], tokens, cost, display_model)
        if timestamp.date() == today:
            today_tokens += tokens
            today_cost += cost
            bucket_add(hourly[timestamp.hour], tokens, cost, display_model)
        if week_start is not None and timestamp >= week_start and (week_end is None or timestamp < week_end):
            week_tokens_billing += tokens
            week_cost_billing += cost

    fixture_driven = logs_dir is not None or cache_dir is not _UNSET
    if not fixture_driven:
        try:
            for day, rec in merge_grok_archive(live_days, today.isoformat()).items():
                if day in live_days:
                    continue
                try:
                    dday = dt.date.fromisoformat(day)
                except ValueError:
                    continue
                d_tokens = int(rec.get("tokens") or 0)
                d_cost = float(rec.get("cost") or 0.0)
                if d_tokens <= 0 and d_cost <= 0.0:
                    continue
                models = as_dict(rec.get("models"))
                if dday >= thirty_days_ago.date():
                    month_tokens += d_tokens
                    month_cost += d_cost
                    month_in += int(rec.get("in") or 0)
                    month_out += int(rec.get("out") or 0)
                    month_cached += int(rec.get("cached") or 0)
                    for mname, mrec in models.items():
                        slot = model_costs.setdefault(str(mname), {"cost": 0.0, "tokens": 0})
                        slot["cost"] += float(mrec.get("cost") or 0.0)
                        slot["tokens"] += int(mrec.get("tokens") or 0)
                for bucket_map in (daily, monthly_buckets):
                    if day not in bucket_map:
                        continue
                    if bucket_map is monthly_buckets and not bucket_map[day].get("inMonth"):
                        continue
                    for mname, mrec in (models or {"Grok Build": {"cost": d_cost,
                                                                  "tokens": d_tokens}}).items():
                        bucket_add(bucket_map[day], int(mrec.get("tokens") or 0),
                                   float(mrec.get("cost") or 0.0), str(mname))
        except Exception:
            pass

    summary = token_summary(today_tokens, month_tokens, "local-grok-logs", today_cost, month_cost, tier,
                            breakdown=(month_in, month_out, month_cached),
                            daily=weekly_token_usage(daily),
                            monthly=monthly_token_usage(monthly_buckets),
                            hourly=hourly_token_usage(hourly),
                            model_costs=model_costs)
    if summary is not None and week_start is not None:
        summary["billingWeekTokens"] = int(week_tokens_billing)
        summary["billingWeekCost"] = round(float(week_cost_billing), 6)
    return summary


GROK_ARCHIVE_PATH = Path.home() / ".tallybar" / "grok_archive.json"
_GROK_ARCHIVE_KEEP_DAYS = 120


def _get_grok_archive_path() -> Path:
    import sys
    acct = sys.modules.get("accounting")
    if acct is not None and hasattr(acct, "GROK_ARCHIVE_PATH"):
        return Path(acct.GROK_ARCHIVE_PATH)
    return GROK_ARCHIVE_PATH


def _load_grok_archive() -> dict[str, Any]:
    main = _get_grok_archive_path()
    bak = main.with_name(main.name + ".bak")
    for idx, path in enumerate((main, bak)):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("days"), dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
        if idx == 0:
            try:
                main.replace(main.with_name(main.name + ".corrupt"))
            except OSError:
                pass
    return {"version": 1, "days": {}}


def _save_grok_archive(archive: dict[str, Any]) -> None:
    main = _get_grok_archive_path()
    payload = json.dumps(archive)
    try:
        atomic_write_text(main, payload)
    except OSError:
        return
    try:
        atomic_write_text(main.with_name(main.name + ".bak"), payload)
    except OSError:
        pass


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
            if fn.startswith("session-") and fn.endswith(".json"):
                yield Path(dirpath) / fn


def _parse_gemini_file(path: Path) -> list[dict[str, Any]] | None:
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        return None
    with handle:
        try:
            session = json.load(handle)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return []
    if not isinstance(session, dict) or not isinstance(session.get("messages"), list):
        return []
    out: list[dict[str, Any]] = []
    for msg in session["messages"]:
        if not isinstance(msg, dict) or msg.get("type") != "gemini":
            continue
        ts = msg.get("timestamp")
        tokens_data = msg.get("tokens")
        if not isinstance(ts, str) or not isinstance(tokens_data, dict):
            continue
        if usage_token_total(tokens_data) <= 0:
            continue
        out.append({"t": ts, "m": msg.get("model"), "u": slim_usage(tokens_data)})
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
    thirty_days_ago = (current - dt.timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
    current_month_start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    history_start = min(thirty_days_ago, current_month_start)
    today_tokens = 0
    month_tokens = 0
    today_cost = 0.0
    month_cost = 0.0
    month_in = month_out = month_cached = 0
    model_costs: dict[str, dict[str, Any]] = {}
    daily = empty_weekly_token_buckets(current)
    monthly_buckets = empty_monthly_token_buckets(current)
    hourly = empty_hourly_token_buckets(current)

    for record in _cached_log_records(root, "session-*.json", cache_path,
                                       _parse_gemini_file, _GEMINI_PARSE_VERSION,
                                       walker=_gemini_session_files, deadline=deadline):
        timestamp = parse_timestamp(record.get("t") or "")
        if timestamp is None or timestamp < history_start:
            continue
        tokens_data = record.get("u")
        if not isinstance(tokens_data, dict):
            continue
        tokens, (bi, bo, bc) = usage_token_total_and_breakdown(tokens_data)
        if tokens <= 0:
            continue

        model = record.get("m") or DEFAULT_MODEL_FOR_PROVIDER["gemini"]
        cost = usage_cost_usd(tokens_data, model)

        day_key = timestamp.date().isoformat()
        if timestamp >= thirty_days_ago:
            month_tokens += tokens
            month_cost += cost
            month_in += bi
            month_out += bo
            month_cached += bc
            slot = model_costs.setdefault(str(model), {"cost": 0.0, "tokens": 0})
            slot["cost"] += cost
            slot["tokens"] += tokens
        if day_key in daily:
            bucket_add(daily[day_key], tokens, cost, model)
        if day_key in monthly_buckets and monthly_buckets[day_key].get("inMonth"):
            bucket_add(monthly_buckets[day_key], tokens, cost, model)
        if timestamp.date() == today:
            today_tokens += tokens
            today_cost += cost
            bucket_add(hourly[timestamp.hour], tokens, cost, model)

    return token_summary(today_tokens, month_tokens, "local-gemini-logs", today_cost, month_cost, tier,
                         breakdown=(month_in, month_out, month_cached),
                         daily=weekly_token_usage(daily),
                         monthly=monthly_token_usage(monthly_buckets),
                         hourly=hourly_token_usage(hourly),
                         model_costs=model_costs)
