"""What the widget shows for stale, failed, signed-out and boundary states.

Pure helpers (lib/ui_helpers.js, lib/format.js) run under `node`; the QML functions that
combine them run through tools/preview/probe.qml, which loads the real component against a
snapshot and prints what it WOULD show. Both skip where their tool is missing (CI has no Qt
modules), like the offscreen render tests.
"""

import copy
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
CODE_DIR = REPO / "io.github.dlansama.tallybar" / "contents" / "code"
UI = REPO / "io.github.dlansama.tallybar" / "contents" / "ui"
sys.path.insert(0, str(CODE_DIR))

import accounting  # noqa: E402
import parsers  # noqa: E402

UI_HELPERS_JS = UI / "lib" / "ui_helpers.js"
FORMAT_JS = UI / "lib" / "format.js"
MOCK = REPO / "tools" / "preview" / "mock-telemetry.json"


def _node(module: Path, script: str, *args):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    proc = subprocess.run([node, "-e", "const m=require(process.argv[1]);" + script, str(module),
                           *[json.dumps(a) for a in args]],
                          capture_output=True, text=True, timeout=20, check=True)
    return json.loads(proc.stdout)


def _js(fn, *args):
    return _node(UI_HELPERS_JS, f"const a=process.argv.slice(2).map(JSON.parse);"
                                f"console.log(JSON.stringify(m['{fn}'](...a)))", *args)


# ---- compact counts: a "T" tier in BOTH formatters -------------------------------------

T_GOLDEN = [
    (999_499_999_999, "999B"), (999_500_000_000, "1T"), (10**12, "1T"), (1_500_000_000_000, "1.5T"),
    (5 * 10**12, "5T"), (12_340_000_000_000, "12.3T"), (10**15, "1000T"),
]


def test_python_compact_count_has_a_trillion_tier():
    for value, expected in T_GOLDEN:
        assert accounting.compact_token_count(value) == expected, value


def test_qml_compact_count_has_a_trillion_tier_and_matches_python():
    # Before the fix 999.5e9 recursed forever in format.js.
    got = _node(FORMAT_JS, "const xs=JSON.parse(process.argv[2]);"
                           "console.log(JSON.stringify(xs.map(m.compactCount)))", [v for v, _ in T_GOLDEN])
    assert got == [e for _, e in T_GOLDEN]


# ---- compactUsd carries after rounding -------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    (999.5, "$1K"), (1000, "$1K"), (999.4, "$999"), (99.5, "$100"), (99.99, "$100"),
    (99.4, "$99.40"), (1500, "$1.5K"), (0.004, "<$0.01"), (12.34, "$12.34"),
])
def test_compact_usd_applies_thresholds_after_rounding(value, expected):
    assert _js("compactUsd", value) == expected


# ---- one bad-status set ----------------------------------------------------------------

BAD = ["missing-cookies", "missing-cli", "unauthorized", "api-error", "api-empty", "partial", "error",
       "timeout", "wallet-locked", "wallet-state-unknown", "not-running", "no-port", "missing-oauth",
       "oauth-expired", "oauth-unavailable"]


@pytest.mark.parametrize("status", BAD)
def test_bad_statuses(status):
    assert _js("statusIsBad", status) is True


@pytest.mark.parametrize("status", ["ok", "loading", "idle", "cookies-ready", ""])
def test_healthy_or_in_progress_statuses_are_not_bad(status):
    assert _js("statusIsBad", status) is False


def test_tab_status_bad_delegates_to_the_one_set():
    src = (UI / "FullRepresentation.qml").read_text(encoding="utf-8")
    body = re.search(r"function tabStatusBad\(.*?\n    \}", src, re.S).group(0)
    assert "root.statusIsBad(" in body and 's === "' not in body


# ---- percent shown by the QML itself ---------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    (99.6, 99), (99.5, 99), (99.49, 99), (100, 100), (0.4, 0), (57.5, 58), (34, 34), (150, 150),
    (None, 0), ("x", 0),
])
def test_display_percent_never_rounds_up_to_100(value, expected):
    assert _js("displayPercent", value) == expected


def test_qml_rounds_percent_through_the_helper():
    for name, needle in (("main.qml", "toFixed(0)"), ("CompactRepresentation.qml", "Math.round(Number(l.percent"),
                         ("CompactRepresentation.qml", "pct.toFixed(0)")):
        assert needle not in (UI / name).read_text(encoding="utf-8"), (name, needle)


# ---- reset countdown: parity with parsers.relative_reset -------------------------------

NOW = dt.datetime(2026, 3, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)


def _python_relative_reset(value: str) -> str:
    class Fixed(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(NOW.timestamp(), tz)

    shim = types.SimpleNamespace(datetime=Fixed, timezone=dt.timezone, timedelta=dt.timedelta)
    real = parsers.dt
    parsers.dt = shim
    try:
        return parsers.relative_reset(value)
    finally:
        parsers.dt = real


def _reset_vectors():
    out = []
    for seconds in (-100, -1, 0, 1, 59, 60, 61, 3599, 3600, 3661, 86399, 86400, 90061, 4 * 86400 + 7 * 3600 + 59 * 60,
                    30 * 86400):
        at = NOW + dt.timedelta(seconds=seconds)
        out += [at.isoformat(), at.isoformat().replace("+00:00", "Z"),
                at.astimezone(dt.timezone(dt.timedelta(hours=-4))).isoformat(),
                at.astimezone(dt.timezone(dt.timedelta(hours=5, minutes=30))).isoformat(),
                at.replace(tzinfo=None).isoformat()]
    out += ["2026-03-15T12:00:59.900Z", "2026-03-15T12:01:00.999999+00:00", "2026-03-15T12:00:00.0001Z",
            "2026-03-16", "2026-03-15T14:00", "2026-03-15 14:00:00+00:00", "2026-03-15T14:00:00+0000",
            "soon", "", "2026-13-01T00:00:00Z", "2026-02-30T00:00:00Z", "Resets in 3h"]
    return out


def test_relative_reset_js_matches_python_word_for_word():
    vectors = _reset_vectors()
    expected = [_python_relative_reset(v) for v in vectors]
    # An unparseable value comes back as itself from Python; the JS helper returns its
    # `fallback` (the baked string), which the widget sets to the same text.
    got = _node(UI_HELPERS_JS,
                "const [vs, now]=process.argv.slice(2).map(JSON.parse);"
                "console.log(JSON.stringify(vs.map(v=>m.relativeReset(v, now, v))))", vectors, NOW_MS)
    mismatches = [(v, e, g) for v, e, g in zip(vectors, expected, got) if e != g]
    assert not mismatches, mismatches[:5]


def test_reset_text_counts_down_from_reset_at():
    now = NOW_MS
    limit = {"reset": "Resets in 9h 0m", "resetAt": (NOW + dt.timedelta(hours=2, minutes=5)).isoformat()}
    assert _js("resetText", limit, now) == "Resets in 2h 5m"
    # the frozen string stays when there is no usable timestamp, or when it isn't a countdown
    assert _js("resetText", {"reset": "Resets in 9h 0m"}, now) == "Resets in 9h 0m"
    assert _js("resetText", {"reset": "Resets in 9h 0m", "resetAt": "garbage"}, now) == "Resets in 9h 0m"
    assert _js("resetText", {"reset": "Monthly credit pool", "resetAt": limit["resetAt"]}, now) == "Monthly credit pool"
    assert _js("resetText", {"reset": "", "resetAt": limit["resetAt"]}, now) == "Resets in 2h 5m"
    assert _js("resetText", None, now) == ""


def test_limit_card_and_panel_read_the_live_countdown():
    assert "limitResetText(modelData)" in (UI / "components" / "UsageLimitCard.qml").read_text(encoding="utf-8")
    assert "UIHelpers.resetText(" in (UI / "CompactRepresentation.qml").read_text(encoding="utf-8")
    main = (UI / "main.qml").read_text(encoding="utf-8")
    assert "UIHelpers.resetText(" in main and 'limit.reset || ""' not in main


# ---- stale readings --------------------------------------------------------------------

def test_staleness_comes_from_the_providers_own_fetch_time():
    snap = (NOW - dt.timedelta(seconds=10)).isoformat()
    day3 = (NOW - dt.timedelta(days=3)).isoformat()
    h2 = (NOW - dt.timedelta(hours=2)).isoformat()
    age = lambda p: _js("readingAgeSeconds", p, snap, NOW_MS)  # noqa: E731
    assert age({"stale": True, "fetchedAt": day3}) == 3 * 86400
    assert age({"stale": True, "staleAsOf": h2, "fetchedAt": day3}) == 7200  # carried reading wins
    assert age({"fetchedAt": day3}) == 10  # a fresh reading ignores fetchedAt
    assert age({"stale": True}) == 10  # no stamp of its own: the snapshot's
    assert age({"stale": True, "staleAsOf": "junk", "fetchedAt": h2}) == 7200
    assert _js("readingAgeSeconds", {}, "", NOW_MS) == -1


def test_age_parts_units():
    assert _js("ageParts", 10) == {"unit": "now", "n": 0}
    assert _js("ageParts", 150) == {"unit": "m", "n": 3}
    assert _js("ageParts", 7200) == {"unit": "h", "n": 2}
    assert _js("ageParts", 3 * 86400) == {"unit": "d", "n": 3}


def test_unpriced_models_reads_the_selected_providers_list():
    diag = {"unpriced_models": {"claude": ["a-1", " ", "b-2"], "codex": ["c"]}}
    assert _js("unpricedModels", diag, "claude") == ["a-1", "b-2"]
    assert _js("unpricedModels", diag, "grok") == []
    assert _js("unpricedModels", None, "claude") == []


def test_tooltip_reads_staleness_age_and_countdown():
    main = (UI / "main.qml").read_text(encoding="utf-8")
    body = re.search(r"function tooltipText\(\).*?\n    \}", main, re.S).group(0)
    assert "provider.stale === true" in body and "readingAgeSeconds" in body and "root.nowTick" in body
    assert "nowTick: root.nowTick" in main


# ---- the QML functions, run against the real component ---------------------------------

QML6 = shutil.which("qml6")


def _snapshot(**providers):
    snap = json.loads(MOCK.read_text())
    snap["timestamp"] = dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")
    for key, patch in providers.items():
        snap["providers"][key].update(copy.deepcopy(patch))
    return snap


def _probe(tmp_path, snap, exprs, provider="claude", component="FullRepresentation", **flags):
    if QML6 is None or os.environ.get("CI"):
        pytest.skip("qml6/QtQuick not available; probe skipped")
    path = tmp_path / "snap.json"
    path.write_text(json.dumps(snap))
    mk = (REPO / "Makefile").read_text()
    shot = re.search(r"^SHOT_ENV := ((?:.*\\\n)*.*)$", mk, re.M).group(1)
    shot = shot.replace("\\\n", " ").replace("$(QML_ENV)", re.search(r"^QML_ENV := (.+)$", mk, re.M).group(1))
    env = dict(os.environ, **dict(t.split("=", 1) for t in shot.replace("$(CURDIR)", str(REPO)).split()))
    args = [QML6, "tools/preview/probe.qml", "--", f"snapshot={path}", f"provider={provider}",
            f"component={component}", f"expr={json.dumps(exprs)}"]
    args += [f"{k}={v}" for k, v in flags.items()]
    proc = subprocess.run(args, cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    lines = [ln for ln in proc.stderr.splitlines() + proc.stdout.splitlines() if "PROBE " in ln]
    if "probe: expression failed" in proc.stderr:
        pytest.fail(proc.stderr[-400:])
    if not lines:
        pytest.skip(f"probe unavailable here: {proc.stderr[-200:]}")
    assert "TypeError" not in proc.stderr, proc.stderr[-600:]
    return json.loads(lines[-1].split("PROBE ", 1)[1])


def _iso(delta):
    return (dt.datetime.now(dt.timezone.utc) + delta).astimezone().isoformat(timespec="seconds")


def test_stale_provider_subtitle_ages_from_its_own_time(tmp_path):
    snap = _snapshot(grok={"stale": True, "fetchedAt": _iso(-dt.timedelta(days=3))},
                     claude={"stale": True, "staleAsOf": _iso(-dt.timedelta(hours=2))})
    grok, claude, fresh = (_probe(tmp_path, snap, ["item.updatedText()"], provider=p)[0]
                           for p in ("grok", "claude", "codex"))
    assert "3d ago" in grok and "(cached)" in grok
    assert "2h ago" in claude and "(cached)" in claude
    assert "just now" in fresh and "(cached)" not in fresh


def test_cost_title_marks_stale_totals(tmp_path):
    snap = _snapshot(codex={"costSummary": {**json.loads(MOCK.read_text())["providers"]["codex"]["costSummary"],
                                            "stale": True, "staleAsOf": _iso(-dt.timedelta(hours=5))}})
    title, plain = (_probe(tmp_path, snap, ["item.costTitleText(item.costSummary())"], provider=p)[0]
                    for p in ("codex", "claude"))
    assert "as of 5h ago" in title and "as of" not in plain


def test_unpriced_models_note_is_in_the_cost_section_height(tmp_path):
    snap = _snapshot()
    snap["diagnostics"] = {"unpriced_models": {"claude": ["mystery-1", "mystery-2"]}}
    # the selected provider carries the note, another provider's cost block does not
    got = _probe(tmp_path, snap, ["item.unpricedModelsText()", "item.costSectionHeight()"], provider="claude")
    plain = _probe(tmp_path, snap, ["item.unpricedModelsText()", "item.costSectionHeight()"], provider="codex")
    assert got[0] == "Estimated rate: mystery-1, mystery-2" and plain[0] == ""
    assert got[1] == plain[1] + 20


def test_failed_refresh_is_visible_while_limits_exist(tmp_path):
    snap = _snapshot()
    shown = _probe(tmp_path, snap, ["item.degradedText()"], provider="codex", lastError="Backend did not respond in time")[0]
    assert "Refresh failed" in shown and "Backend did not respond" in shown
    assert _probe(tmp_path, snap, ["item.degradedText()"], provider="codex")[0] == ""


def test_cold_start_placeholder_status_reads_as_fetching(tmp_path):
    snap = _snapshot(gemini={"status": "loading", "limits": []})
    msg = _probe(tmp_path, snap, ["item.emptyStateMessage()"], provider="gemini", loading=1)[0]
    assert msg == "Fetching usage data…"


def test_tabs_mark_every_bad_status_but_honour_mute(tmp_path):
    snap = _snapshot(gemini={"status": "api-empty"}, antigravity={"status": "missing-cookies"},
                     grokbot={"status": "partial"}, grok={"status": "not-running"}, claude={"status": "missing-cli"})
    snap["config"]["mutedProviders"] = ["grok"]
    flags = _probe(tmp_path, snap, [f"item.tabStatusBad('{k}')" for k in
                                    ("gemini", "antigravity", "grokbot", "grok", "claude", "codex")])
    assert flags == [True, True, True, False, True, False]


def test_status_message_and_action_show_beside_cost_data(tmp_path):
    """A provider with no limits, a bad status and local cost data used to show nothing."""
    snap = _snapshot(claude={"status": "missing-cookies", "limits": []},
                     codex={"status": "not-running", "limits": []})
    claude = _probe(tmp_path, snap, ["item.emptyStateMode()", "item.emptyStateShowsSignIn()",
                                     "item.hasCostSection()"], provider="claude")
    assert claude == ["compact", True, True]
    codex = _probe(tmp_path, snap, ["item.emptyStateMode()", "item.emptyStateMessage()"], provider="codex")
    assert codex[0] == "compact" and "Codex isn't running" in codex[1]
    # unchanged: ok with limits -> none; no cost and no limits -> full body; ok with no limits -> none
    assert _probe(tmp_path, _snapshot(), ["item.emptyStateMode()"], provider="claude") == ["none"]
    assert _probe(tmp_path, _snapshot(gemini={"status": "api-empty", "limits": []}),
                  ["item.emptyStateMode()"], provider="gemini") == ["full"]
    ok_empty = _snapshot(claude={"limits": []})
    assert _probe(tmp_path, ok_empty, ["item.emptyStateMode()"], provider="claude") == ["none"]


_SCROLLER = ("(function f(o){for(let i=0;i<o.children.length;++i){const c=o.children[i];"
             "if(c.flickableDirection!==undefined)return [c.interactive,c.contentHeight,c.height];"
             "const r=f(c);if(r)return r;}return null;})(item)")


def test_compact_empty_state_keeps_the_body_free_of_a_scrollbar(tmp_path):
    snap = _snapshot(claude={"status": "missing-cookies", "limits": []})
    mirror, scroller = _probe(tmp_path, snap, ["item.metricsBodyHeight()", _SCROLLER], provider="claude")
    assert scroller is not None, "scroller not found"
    assert scroller[0] is False, scroller
    cost = 107 + 20  # section base + its token-type line
    assert mirror == 50 + 40 + 8 + cost  # message + sign-in button, one gap, the cost section


def test_compact_panel_counts_the_reset_down_and_flags_a_stale_reading(tmp_path):
    soon = _iso(dt.timedelta(hours=3, minutes=10))
    snap = _snapshot(grok={"stale": True, "fetchedAt": _iso(-dt.timedelta(days=1)),
                           "limits": [{"label": "Weekly", "percent": 99.6, "reset": "Resets in 9d 1h",
                                       "resetAt": soon}]})
    reset, stale, summary, pace = _probe(
        tmp_path, snap, ["item.panelResetText()", "item.readingStale", "item.usageSummary()", "item.panelPaceText()"],
        provider="grok", component="CompactRepresentation")
    assert reset == "3h" and stale is True and summary == "99%" and pace == "99%"
    fresh = _probe(tmp_path, _snapshot(), ["item.readingStale"], provider="codex", component="CompactRepresentation")
    assert fresh == [False]


def test_compact_empty_state_prints_a_failed_refresh_once_and_keeps_the_status_hint(tmp_path):
    snap = _snapshot(codex={"status": "not-running", "limits": []})
    err = "Backend did not respond in time"
    msg, strip, mode = _probe(tmp_path, snap, ["item.emptyStateMessage()", "item.degradedText()",
                                               "item.emptyStateMode()"], provider="codex", lastError=err)
    assert mode == "compact"
    assert "Codex isn't running" in msg and err not in msg
    assert err in strip
    # full mode: the body prints the error itself and the strip stays quiet
    full = _snapshot(codex={"status": "not-running", "limits": []})
    for key in ("costSummary",):
        full["providers"]["codex"].pop(key, None)
    msg, strip, mode = _probe(tmp_path, full, ["item.emptyStateMessage()", "item.degradedText()",
                                               "item.emptyStateMode()"], provider="codex", lastError=err)
    assert mode == "full" and msg == err and strip == ""
