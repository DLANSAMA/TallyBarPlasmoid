"""The Claude parse cache: per-file folded states in a hot/cold store.

The summary must be exactly what folding every record would give (the reference below is
that record-level fold), and a refresh must write about as much as changed.
"""
import datetime as dt
import hashlib
import json
import os
import random
from pathlib import Path
from unittest.mock import patch

import pytest

import accounting
from accounting import log_parsers as lp
from accounting.buckets import (
    bucket_add, buckets_model_costs, empty_hourly_token_buckets, empty_monthly_token_buckets,
    empty_type_split, empty_weekly_token_buckets, hourly_token_usage, monthly_token_usage,
    token_summary, type_split_add, weekly_token_usage,
)
from accounting.formatting import parse_timestamp
from accounting.pricing import DEFAULT_MODEL_FOR_PROVIDER, usage_split

_NOW = dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.timezone.utc)
_PRICES = {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_write_1h": 6.0,
           "cache_read": 0.3, "long_context_threshold": 150_000, "long_input": 6.0,
           "long_output": 22.5, "long_cache_read": 0.6, "long_cache_write": 7.5,
           "long_cache_write_1h": 12.0}


def _reference_summary(root: Path, now: dt.datetime):
    """local_claude_token_summary as a fold over every record of every file, in walk
    order: the definition the cached states must reproduce."""
    current = now.astimezone()
    today = current.date()
    thirty_days_ago = dt.datetime.combine(current.date() - dt.timedelta(days=29), dt.time.min).astimezone()
    month_start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    history_start = min(thirty_days_ago, month_start)
    today_tokens = month_tokens = 0
    today_cost = month_cost = 0.0
    split_today, split_week, split_month = empty_type_split(), empty_type_split(), empty_type_split()
    model_costs: dict = {}
    daily = empty_weekly_token_buckets(current)
    monthly_buckets = empty_monthly_token_buckets(current)
    hourly = empty_hourly_token_buckets(current)
    best: dict = {}
    keyless: list = []
    final_requests: set = set()
    visible_chars: dict = {}
    estimated_output = 0

    def out_tokens(u):
        v = u.get("output_tokens") if isinstance(u, dict) else None
        return int(v) if isinstance(v, (int, float)) and v > 0 else 0

    for path in root.rglob("*.jsonl"):
        for record in lp._parse_claude_file(path) or []:
            timestamp = parse_timestamp(record["t"])
            if timestamp is None or timestamp < history_start:
                continue
            usage = record["u"]
            entry = {"usage": usage, "model": record["m"] or DEFAULT_MODEL_FOR_PROVIDER["claude"],
                     "timestamp": timestamp, "out": out_tokens(usage)}
            key = record["r"]
            if not key:
                keyless.append(entry)
                continue
            if record.get("f"):
                final_requests.add(key)
            if record.get("v"):
                visible_chars.setdefault(key, {})[record.get("i") or ""] = record["v"]
            prev = best.get(key)
            if prev is None or entry["out"] > prev["out"]:
                best[key] = entry
    for key, chars in visible_chars.items():
        if key in final_requests:
            continue
        entry = best[key]
        estimate = sum(chars.values()) // 4
        if estimate <= entry["out"]:
            continue
        if entry["timestamp"] >= thirty_days_ago:
            estimated_output += estimate - entry["out"]
        entry["usage"] = {**entry["usage"], "output_tokens": estimate}
        entry["out"] = estimate
    for entry in (*best.values(), *keyless):
        model, timestamp = entry["model"], entry["timestamp"]
        split = usage_split(entry["usage"], model)
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
    return token_summary(today_tokens, month_tokens, "local-claude-logs", today_cost, month_cost,
                         None, now=current,
                         type_split={"today": split_today, "7d": split_week, "30d": split_month},
                         daily=weekly_token_usage(daily), monthly=monthly_token_usage(monthly_buckets),
                         hourly=hourly_token_usage(hourly), model_costs=model_costs,
                         model_costs_7d=buckets_model_costs(daily),
                         model_costs_1d=daily.get(today.isoformat(), {}).get("models"),
                         estimated_output_tokens=estimated_output)


def _line(rng, ts, key=None, out=None, final=None, model=None):
    usage = {"input_tokens": rng.randint(1, 50), "output_tokens": out if out is not None else rng.randint(1, 900),
             "cache_read_input_tokens": rng.choice([0, rng.randint(1_000, 190_000)]),
             "cache_creation_input_tokens": rng.randint(0, 9_000)}
    message = {"model": model or rng.choice(["claude-opus-4-5", "claude-haiku-4-5", None]),
               "usage": usage, "content": [{"type": "text", "text": "x" * rng.randint(0, 9_000)}]}
    if key is not None:
        message["id"] = key
    if final:
        message["stop_reason"] = "end_turn"
    return json.dumps({"type": "assistant", "timestamp": ts.isoformat().replace("+00:00", "Z"),
                       "uuid": f"u{rng.randrange(10 ** 9)}", "message": message})


def _write_history(root: Path, seed: int, window_edge: dt.datetime) -> None:
    """Sessions with subagents, messages repeated across files, streamed lines with ties,
    keyless lines, unreadable lines, and messages whose lines straddle ``window_edge``."""
    rng = random.Random(seed)
    keys = [f"msg_{i}" for i in range(60)]
    for s in range(4):
        sess = root / f"proj{s % 2}"
        sub = sess / f"sess{s}" / "subagents"
        sub.mkdir(parents=True, exist_ok=True)
        for path, is_sub in ((sess / f"sess{s}.jsonl", False), (sub / "agent-a.jsonl", True)):
            lines = []
            for _ in range(rng.randint(10, 30)):
                base = _NOW - dt.timedelta(seconds=rng.randint(0, 50 * 86400))
                if rng.random() < 0.15:
                    base = window_edge - dt.timedelta(seconds=rng.randint(1, 3))
                key = rng.choice(keys + [None])
                out = rng.choice([None, 7, 7])
                for n in range(rng.randint(1, 3)):
                    lines.append(_line(rng, base + dt.timedelta(seconds=2 * n), key=key, out=out,
                                       final=is_sub and rng.random() < 0.3))
                if rng.random() < 0.1:
                    lines.append("{not json")
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _summary(root, cache=None):
    return accounting.local_claude_token_summary(projects_dir=root, now=_NOW,
                                                 cache_dir=cache)


@pytest.mark.parametrize("seed", range(6))
def test_cached_states_reproduce_the_record_level_fold(tmp_path, seed):
    root = tmp_path / "projects"
    current = _NOW.astimezone()
    edge = min(dt.datetime.combine(current.date() - dt.timedelta(days=29), dt.time.min).astimezone(),
               current.replace(day=1, hour=0, minute=0, second=0, microsecond=0))
    _write_history(root, seed, edge)
    cache = tmp_path / "cache"
    with patch("accounting.pricing.model_pricing", return_value=_PRICES):
        truth = _reference_summary(root, _NOW)
        assert truth is not None
        assert _summary(root) == truth
        assert _summary(root, cache) == truth             # cold
        assert _summary(root, cache) == truth             # warm
        # One file grows by a line, another by lines of a message it already holds.
        rng = random.Random(seed + 100)
        main = root / "proj0" / "sess0.jsonl"
        with main.open("a", encoding="utf-8") as handle:
            handle.write(_line(rng, _NOW, key="msg_1", out=5000) + "\n")
            handle.write(_line(rng, _NOW, key=None) + "\n")
        os.utime(main, ns=(main.stat().st_atime_ns, main.stat().st_mtime_ns + 10 ** 9))
        assert _summary(root, cache) == _reference_summary(root, _NOW)


def test_window_straddling_message_counts_only_its_lines_inside_the_window(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    current = _NOW.astimezone()
    edge = min(dt.datetime.combine(current.date() - dt.timedelta(days=29), dt.time.min).astimezone(),
               current.replace(day=1, hour=0, minute=0, second=0, microsecond=0))
    rng = random.Random(1)
    # The line before the window start has the most output; only the one after it counts.
    (root / "s.jsonl").write_text(
        _line(rng, edge - dt.timedelta(seconds=1), key="m", out=900, model="claude-opus-4-5") + "\n"
        + _line(rng, edge + dt.timedelta(seconds=1), key="m", out=10, model="claude-opus-4-5") + "\n",
        encoding="utf-8")
    cache = tmp_path / "cache"
    with patch("accounting.pricing.model_pricing", return_value=_PRICES):
        truth = _reference_summary(root, _NOW)
        assert _summary(root, cache) == truth
        assert _summary(root, cache) == truth
    assert truth["typeBreakdown"]["30d"]["output"]["tokens"] == 10


def _project(tmp_path, sessions=8):
    root = tmp_path / "projects"
    rng = random.Random(7)
    for s in range(sessions):
        d = root / "proj" / f"sess{s}" / "subagents"
        d.mkdir(parents=True)
        lines = [_line(rng, _NOW - dt.timedelta(minutes=i), key=f"m{s}-{i}") for i in range(200)]
        (root / "proj" / f"sess{s}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (d / "agent-a.jsonl").write_text(_line(rng, _NOW, key=f"m{s}-0", final=True) + "\n",
                                          encoding="utf-8")
    return root


def _append(path, rng, n=1):
    with path.open("a", encoding="utf-8") as handle:
        for i in range(n):
            handle.write(_line(rng, _NOW, key=f"new-{rng.randrange(10 ** 9)}") + "\n")
    os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 10 ** 9))


def _stamps(cache):
    return {p.name: (p.stat().st_mtime_ns, p.stat().st_size) for p in cache.glob("claude_logs*")}


def test_a_refresh_after_one_append_rewrites_only_the_small_hot_file(tmp_path, monkeypatch):
    root = _project(tmp_path)
    cache = tmp_path / "cache"
    monkeypatch.setattr(lp, "_CLAUDE_HOT_LIMIT", 1)       # first run folds everything cold
    with patch("accounting.pricing.model_pricing", return_value=_PRICES):
        _summary(root, cache)
        monkeypatch.setattr(lp, "_CLAUDE_HOT_LIMIT", 256 * 1024)
        before = _stamps(cache)
        cold_bytes = sum(size for name, (_m, size) in before.items() if name != "claude_logs.hot.json")
        _append(root / "proj" / "sess3.jsonl", random.Random(1))
        writes = []
        real_write = lp.atomic_write_text
        monkeypatch.setattr(lp, "atomic_write_text",
                            lambda path, text: (writes.append((Path(path).name, len(text))),
                                                real_write(path, text)))
        assert _summary(root, cache) == _reference_summary(root, _NOW)
    assert [name for name, _ in writes] == ["claude_logs.hot.json"]
    assert writes[0][1] < cold_bytes / 20                    # the new line, not the history
    hot = json.loads((cache / "claude_logs.hot.json").read_text())["files"]
    [(key, entry)] = hot.items()
    assert key.endswith("sess3.jsonl") and "base" in entry and len(entry["s"]["g"]) == 1


def test_an_unchanged_refresh_writes_nothing(tmp_path, monkeypatch):
    root = _project(tmp_path, sessions=3)
    cache = tmp_path / "cache"
    with patch("accounting.pricing.model_pricing", return_value=_PRICES):
        _summary(root, cache)
        before = _stamps(cache)
        _summary(root, cache)
    assert _stamps(cache) == before


def test_a_full_hot_file_folds_into_only_the_touched_shard(tmp_path, monkeypatch):
    root = _project(tmp_path)
    cache = tmp_path / "cache"
    monkeypatch.setattr(lp, "_CLAUDE_HOT_LIMIT", 1)
    with patch("accounting.pricing.model_pricing", return_value=_PRICES):
        _summary(root, cache)
        before = _stamps(cache)
        _append(root / "proj" / "sess5" / "subagents" / "agent-a.jsonl", random.Random(2))
        _append(root / "proj" / "sess5.jsonl", random.Random(3))
        assert _summary(root, cache) == _reference_summary(root, _NOW)
    after = _stamps(cache)
    changed = {name for name in after if after[name] != before.get(name)}
    shard = f"claude_logs.s{zlib_shard('proj/sess5'):02d}.json"
    assert changed == {shard, "claude_logs.hot.json"}       # a session shares one shard
    assert json.loads((cache / "claude_logs.hot.json").read_text())["files"] == {}


def zlib_shard(session):
    import zlib
    return zlib.crc32(session.encode()) % lp._CLAUDE_SHARDS


def test_a_crash_between_the_shard_and_hot_writes_loses_nothing(tmp_path, monkeypatch):
    root = _project(tmp_path, sessions=2)
    cache = tmp_path / "cache"
    with patch("accounting.pricing.model_pricing", return_value=_PRICES):
        monkeypatch.setattr(lp, "_CLAUDE_HOT_LIMIT", 1)
        _summary(root, cache)
        monkeypatch.setattr(lp, "_CLAUDE_HOT_LIMIT", 256 * 1024)
        rng = random.Random(4)
        _append(root / "proj" / "sess1.jsonl", rng)
        _summary(root, cache)                                  # a delta in the hot file
        stale_hot = (cache / "claude_logs.hot.json").read_text()
        _append(root / "proj" / "sess1.jsonl", rng)
        monkeypatch.setattr(lp, "_CLAUDE_HOT_LIMIT", 1)
        _summary(root, cache)                                  # folded into the shard
        (cache / "claude_logs.hot.json").write_text(stale_hot)  # as if the hot write was lost
        monkeypatch.setattr(lp, "_CLAUDE_HOT_LIMIT", 256 * 1024)
        assert _summary(root, cache) == _reference_summary(root, _NOW)
        # A delta whose base is gone is dropped, never applied to the wrong cold entry.
        _append(root / "proj" / "sess1.jsonl", rng)
        assert _summary(root, cache) == _reference_summary(root, _NOW)


def test_an_unreadable_shard_is_rebuilt(tmp_path, monkeypatch):
    root = _project(tmp_path, sessions=2)
    cache = tmp_path / "cache"
    monkeypatch.setattr(lp, "_CLAUDE_HOT_LIMIT", 1)
    with patch("accounting.pricing.model_pricing", return_value=_PRICES):
        truth = _summary(root, cache)
        shard = next(p for p in cache.glob("claude_logs.s*.json"))
        shard.write_text("}{")
        assert _summary(root, cache) == truth
    json.loads(shard.read_text())


def _write_legacy(cache: Path, root: Path, tamper=None):
    """A cache in the single-file format, as the previous release wrote it."""
    files = {}
    for path in root.rglob("*.jsonl"):
        st = path.stat()
        end = lp._safe_resume_offset(path)
        records = lp._parse_claude_file(path, 0, end)
        if tamper:
            records = [tamper(r) for r in records]
        files[str(path)] = {"mtime": st.st_mtime_ns, "size": st.st_size, "records": records,
                            "off": end, "fp": lp._offset_fingerprint(path, end)}
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "claude_logs.json").write_text(json.dumps(
        {"schema": lp._CACHE_SCHEMA_VERSION, "version": lp._CLAUDE_PARSE_VERSION, "files": files}))


def _double_output(record):
    return {**record, "u": {**record["u"], "output_tokens": record["u"]["output_tokens"] * 2}}


def test_a_single_file_cache_is_migrated_without_reparsing(tmp_path, monkeypatch):
    root = _project(tmp_path, sessions=3)
    cache = tmp_path / "cache"
    # The old cache's records differ from the files, so a summary that matches them came
    # from the migrated records, not from a reparse.
    _write_legacy(cache, root, tamper=_double_output)
    parsed = []
    real_parse = lp._parse_claude_file
    monkeypatch.setattr(lp, "_parse_claude_file", lambda *a: parsed.append(a) or real_parse(*a))
    with patch("accounting.pricing.model_pricing", return_value=_PRICES):
        migrated = _summary(root, cache)
        monkeypatch.setattr(lp, "_parse_claude_file", real_parse)
        honest = _summary(root)
    assert parsed == []
    assert migrated["typeBreakdown"]["30d"]["output"]["tokens"] == \
        2 * honest["typeBreakdown"]["30d"]["output"]["tokens"]
    assert not (cache / "claude_logs.json").exists()        # taken in whole, then removed
    with patch("accounting.pricing.model_pricing", return_value=_PRICES):
        assert _summary(root, cache) == migrated               # the store now holds it


def test_a_cut_migration_keeps_the_old_cache_until_a_walk_completes(tmp_path, monkeypatch):
    root = _project(tmp_path, sessions=3)
    cache = tmp_path / "cache"
    _write_legacy(cache, root)
    clock = [1000.0]

    class _Clock:
        @staticmethod
        def time():
            clock[0] += 1.0
            return clock[0]

    monkeypatch.setattr(lp, "time", _Clock)
    with patch("accounting.pricing.model_pricing", return_value=_PRICES):
        with pytest.raises(lp.LogScanIncomplete):
            accounting.local_claude_token_summary(projects_dir=root, now=_NOW, cache_dir=cache,
                                                  deadline=clock[0] + 3 + lp._CHECKPOINT_MARGIN_SECONDS)
        assert (cache / "claude_logs.json").exists()
        saved = json.loads((cache / "claude_logs.hot.json").read_text())["files"]
        assert 0 < len(saved) < 6
        assert _summary(root, cache) == _reference_summary(root, _NOW)
    assert not (cache / "claude_logs.json").exists()


def test_a_cut_walk_checkpoints_and_the_next_walk_parses_only_the_rest(tmp_path, monkeypatch):
    root = _project(tmp_path, sessions=2)
    cache = tmp_path / "cache"
    clock = [1000.0]
    parsed = []
    real_parse = lp._parse_claude_file

    def slow_parse(path, start=0, end=None):
        parsed.append(path.name)
        clock[0] += 10.0
        return real_parse(path, start, end)

    class _Clock:
        @staticmethod
        def time():
            return clock[0]

    monkeypatch.setattr(lp, "time", _Clock)
    monkeypatch.setattr(lp, "_parse_claude_file", slow_parse)
    with patch("accounting.pricing.model_pricing", return_value=_PRICES):
        with pytest.raises(lp.LogScanIncomplete):
            accounting.local_claude_token_summary(projects_dir=root, now=_NOW, cache_dir=cache,
                                                  deadline=clock[0] + 15 + lp._CHECKPOINT_MARGIN_SECONDS)
        assert len(parsed) == 2
        parsed.clear()
        resumed = _summary(root, cache)
        assert len(parsed) == 2                              # only the two not yet parsed
        assert resumed == _reference_summary(root, _NOW)


def test_merging_states_equals_folding_the_joined_records(tmp_path):
    rng = random.Random(3)
    sub = tmp_path / "s" / "subagents"
    sub.mkdir(parents=True)
    path = sub / "agent.jsonl"
    lines = []
    for i in range(80):
        lines.append(_line(rng, _NOW - dt.timedelta(seconds=rng.randint(0, 999)),
                           key=rng.choice(["a", "b", "c", None]), out=rng.choice([3, 3, 9, None]),
                           final=rng.random() < 0.2))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    records = lp._parse_claude_file(path)
    for cut in range(0, len(records) + 1, 7):
        merged = lp._merge_claude_state(lp._claude_state(records[:cut]), lp._claude_state(records[cut:]))
        assert merged == lp._claude_state(records)


def test_state_shape_is_pinned(tmp_path):
    rng = random.Random(5)
    sub = tmp_path / "s" / "subagents"
    sub.mkdir(parents=True)
    path = sub / "agent.jsonl"
    path.write_text("\n".join(_line(rng, _NOW, key=k, final=f) for k, f in
                              (("a", False), ("a", False), ("b", True), (None, False))) + "\n")
    state = json.loads(json.dumps(lp._claude_state(lp._parse_claude_file(path))))
    digest = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()[:16]
    assert (lp._CLAUDE_STATE_VERSION, digest) == (1, "e57387ed17d1f223"), (
        f"_claude_state output changed (now {digest}): bump _CLAUDE_STATE_VERSION and repin")
