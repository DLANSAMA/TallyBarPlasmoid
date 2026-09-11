"""The Grok daily archive.

~/.grok/logs/unified.jsonl is a single rolling log that xAI truncates in place
(~2 days observed). Sessions persist ~30 days but carry no per-call billing, so
history has to be accumulated forward rather than re-read. These tests pin the
one rule that makes truncation harmless.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent
                       / "io.github.dlansama.tallybar" / "contents" / "code"))
import accounting  # noqa: E402


def _day(tokens, cost, models=None):
    return {"tokens": tokens, "cost": cost, "in": 0, "out": 0, "cached": 0,
            "models": models or {"Grok 4.6": {"cost": cost, "tokens": tokens}}}


def _redirect(monkeypatch, tmp_path):
    p = tmp_path / "grok_archive.json"
    monkeypatch.setattr(accounting, "GROK_ARCHIVE_PATH", p)
    return p


def test_days_that_scroll_out_of_the_log_survive(monkeypatch, tmp_path):
    """The whole point: a day the log no longer contains is still reported."""
    _redirect(monkeypatch, tmp_path)
    accounting.merge_grok_archive({"2026-09-01": _day(100, 1.0)}, "2026-09-01")
    # Next refresh: the log has rotated and only knows about a later day.
    out = accounting.merge_grok_archive({"2026-09-03": _day(300, 3.0)}, "2026-09-03")
    assert out["2026-09-01"]["tokens"] == 100      # survived the rotation
    assert out["2026-09-03"]["tokens"] == 300


def test_a_truncated_day_never_erodes_the_archived_one(monkeypatch, tmp_path):
    """Truncation can only REMOVE records, so a lower live figure means the log lost
    part of that day — not that usage shrank. Replacing downward would let the rolling
    log eat its own history one day at a time."""
    _redirect(monkeypatch, tmp_path)
    accounting.merge_grok_archive({"2026-09-01": _day(1000, 10.0)}, "2026-09-01")
    out = accounting.merge_grok_archive({"2026-09-01": _day(400, 4.0)}, "2026-09-02")
    assert out["2026-09-01"]["tokens"] == 1000
    assert out["2026-09-01"]["cost"] == 10.0


def test_a_growing_day_is_replaced_upward(monkeypatch, tmp_path):
    """Within a day usage is monotonic, so a higher live figure is the truth."""
    _redirect(monkeypatch, tmp_path)
    accounting.merge_grok_archive({"2026-09-01": _day(100, 1.0)}, "2026-09-01")
    out = accounting.merge_grok_archive({"2026-09-01": _day(900, 9.0)}, "2026-09-01")
    assert out["2026-09-01"]["tokens"] == 900
    assert out["2026-09-01"]["cost"] == 9.0        # the record moves as a unit


def test_the_day_record_moves_as_a_unit(monkeypatch, tmp_path):
    """Fields are not max'd individually — that would blend two readings into a day
    that never existed. Tokens decide, and the rest of the record follows."""
    _redirect(monkeypatch, tmp_path)
    accounting.merge_grok_archive(
        {"2026-09-01": _day(100, 99.0, {"Old": {"cost": 99.0, "tokens": 100}})}, "2026-09-01")
    out = accounting.merge_grok_archive(
        {"2026-09-01": _day(500, 5.0, {"New": {"cost": 5.0, "tokens": 500}})}, "2026-09-01")
    assert out["2026-09-01"]["cost"] == 5.0        # NOT the higher 99.0
    assert set(out["2026-09-01"]["models"]) == {"New"}


def test_corrupt_archive_recovers_from_bak_rather_than_starting_empty(monkeypatch, tmp_path):
    """Never-load-into-empty: returning {} on a read error would let the next write
    replace real history with whatever two days the log happens to hold."""
    p = _redirect(monkeypatch, tmp_path)
    accounting.merge_grok_archive({"2026-09-01": _day(100, 1.0)}, "2026-09-01")
    assert p.with_name(p.name + ".bak").is_file()
    p.write_text("{ this is not json")
    out = accounting.merge_grok_archive({"2026-09-02": _day(200, 2.0)}, "2026-09-02")
    assert out["2026-09-01"]["tokens"] == 100                 # recovered from .bak
    assert p.with_name(p.name + ".corrupt").is_file()         # main quarantined


def test_write_is_change_gated(monkeypatch, tmp_path):
    """An unchanged union must not rewrite the file — a quiet refresh costs no I/O."""
    p = _redirect(monkeypatch, tmp_path)
    accounting.merge_grok_archive({"2026-09-01": _day(100, 1.0)}, "2026-09-01")
    before = p.stat().st_mtime_ns
    accounting.merge_grok_archive({"2026-09-01": _day(100, 1.0)}, "2026-09-01")
    assert p.stat().st_mtime_ns == before


def test_archive_is_owner_only(monkeypatch, tmp_path):
    p = _redirect(monkeypatch, tmp_path)
    accounting.merge_grok_archive({"2026-09-01": _day(1, 0.1)}, "2026-09-01")
    assert oct(p.stat().st_mode & 0o777) == "0o600"


def test_archive_is_bounded_keeping_the_newest_days(monkeypatch, tmp_path):
    """Unbounded growth would be the other failure mode of an append-only file."""
    _redirect(monkeypatch, tmp_path)
    many = {f"2026-{m:02d}-{d:02d}": _day(1, 0.1) for m in (1, 2, 3, 4, 5) for d in range(1, 29)}
    out = accounting.merge_grok_archive(many, "2026-05-28", keep_days=10)
    assert len(out) == 10
    assert sorted(out)[-1] == "2026-05-28"      # newest kept
    assert sorted(out)[0] == "2026-05-19"       # oldest pruned


def test_a_fixture_driven_summarise_never_touches_the_shared_archive(tmp_path, monkeypatch):
    """Regression: the first cut of this banked TEST data into the user's real
    ~/.tallybar/grok_archive.json — four invented days at a tidy $0.01/1k tokens
    landed in live history. An injected log_path means fixture data; never bank it.
    """
    import json as _json
    real = tmp_path / "must_not_be_written.json"
    monkeypatch.setattr(accounting, "GROK_ARCHIVE_PATH", real)

    log = tmp_path / "unified.jsonl"
    log.write_text(_json.dumps({
        "ts": "2026-05-28T12:00:00Z", "msg": "shell.turn.inference_done",
        "sid": "s1", "ctx": {"model": "grok-build-0.1", "prompt_tokens": 100_000,
                             "cached_prompt_tokens": 0, "completion_tokens": 10_000},
    }) + "\n", encoding="utf-8")

    accounting.local_grok_token_summary(logs_dir=log.parent)
    assert not real.exists(), "fixture data was banked into the shared archive"
