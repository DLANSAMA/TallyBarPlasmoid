"""Guards against the FullRepresentation component split silently emptying the UI.

Regression: ``delegate: UsageLimitCard { root: root }``. A Repeater delegate is its own
component scope, so the right-hand ``root`` resolved to the card's OWN ``root`` property
(undefined) instead of the outer ``id: root``. Every binding in the card is guarded with
``(root && ...) ? ... : default``, so nothing errored — the limit cards just rendered as
collapsed "Usage" rows, and the README screenshots were regenerated from that.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
UI = REPO / "io.github.dlansama.tallybar" / "contents" / "ui"
# The expected renders of the mock fixture (`make render-baselines`). The README's
# docs/screenshots/ are rendered from a live snapshot instead, so they can't be compared.
SHOTS = REPO / "tests" / "render-baselines"
# Mirrors the Makefile's SHOT_ENV + SHOT_DAY. Both are what make a render byte-stable: the
# day pins where the fixture's week/month buckets land (rebasing onto the REAL today broke
# the week/month comparisons every day after the screenshots were taken), and the repo-local
# XDG_CONFIG_HOME pins the icon theme (the kde platform theme otherwise follows the
# developer's desktop kdeglobals, so switching desktop icon themes broke the widget shots).
SHOT_DAY = re.search(r"^SHOT_DAY := (\S+)$", (REPO / "Makefile").read_text(), re.M).group(1)
CURSOR_PX = re.search(r"^CURSOR_PX := (\d+)", (REPO / "Makefile").read_text(), re.M).group(1)
SHOT_ENV = dict(QML_XHR_ALLOW_FILE_READ="1", QT_FORCE_STDERR_LOGGING="1",
                QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software",
                XDG_ICON_THEME="breeze-dark", QT_QPA_PLATFORMTHEME="kde",
                XDG_CONFIG_HOME=str(REPO / "tools" / "preview" / "xdg-config"))


def _delegate_blocks(src: str):
    """Yield the brace-delimited body of every ``delegate: Type { ... }`` in ``src``."""
    for m in re.finditer(r"\bdelegate\s*:\s*[A-Za-z_][\w.]*\s*\{", src):
        depth, i = 1, m.end()
        while i < len(src) and depth:
            depth += {"{": 1, "}": -1}.get(src[i], 0)
            i += 1
        yield src[m.end():i - 1]


def test_no_delegate_binds_a_property_to_its_own_name():
    # Static, so it runs in CI (which has no QtQuick modules and cannot render).
    offenders = []
    for path in sorted(UI.rglob("*.qml")):
        for body in _delegate_blocks(path.read_text(encoding="utf-8")):
            top = re.sub(r"\{[^{}]*\}", "", body)  # drop nested objects' own bindings
            for name, value in re.findall(r"^\s*(\w+)\s*:\s*(\w+)\s*(?://.*)?$", top, re.M):
                if name == value:
                    offenders.append(f"{path.relative_to(REPO)}: `{name}: {value}` inside a delegate")
    assert not offenders, (
        "self-shadowing delegate binding (resolves to the delegate's own undefined "
        "property, not the outer id) — pass the outer item under another name:\n"
        + "\n".join(offenders)
    )


def test_delegate_scanner_catches_the_original_bug():
    bad = "Repeater {\n  delegate: UsageLimitCard {\n    root: root\n  }\n}\n"
    good = "Repeater {\n  delegate: UsageLimitCard {\n    root: host   // ok\n  }\n}\n"
    hit = lambda s: [b for b in _delegate_blocks(s) if re.search(r"^\s*(\w+)\s*:\s*\1\s*(?://.*)?$", b, re.M)]
    assert hit(bad) and not hit(good)


@pytest.mark.parametrize("provider", ["claude", "antigravity"])
def test_offscreen_render_matches_committed_screenshot(provider, tmp_path):
    # Local-only (needs qml6 + the QtQuick modules). The committed baselines ARE the
    # expected render of the mock fixture, so a UI change that alters them must be a
    # deliberate `make render-baselines` that a human has LOOKED at — not a silent side effect.
    qml6 = shutil.which("qml6")
    if qml6 is None or os.environ.get("CI"):
        pytest.skip("qml6/QtQuick not available; offscreen render test skipped")
    out = tmp_path / f"{provider}.png"
    env = dict(os.environ, **SHOT_ENV)
    proc = subprocess.run([qml6, "tools/preview/screenshot.qml", "--", f"today={SHOT_DAY}",
                           f"provider={provider}", f"out={out}"],
                          cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    if not out.is_file():
        pytest.skip(f"offscreen render unavailable here: {proc.stderr[-200:]}")
    assert "TypeError" not in proc.stderr and "ReferenceError" not in proc.stderr, proc.stderr[-600:]
    assert out.read_bytes() == (SHOTS / f"widget-{provider}.png").read_bytes(), (
        f"offscreen render of '{provider}' no longer matches tests/render-baselines/widget-{provider}.png. "
        "Open BOTH images and compare before regenerating."
    )


@pytest.mark.parametrize("mode", ["day", "week", "month"])
def test_cost_popout_render_matches_committed_screenshot(mode, tmp_path):
    """Same guard as above for the cost flyout's three tabs.

    The flyout is a separate PopupPlasmaWindow and used to be uncapturable offscreen, so it
    was the one large piece of UI with no render regression test at all — the component that
    would most easily go silently empty. It is capturable once the window is actually made
    visible through the widget's own costDrawerOpen state; see tools/preview/screenshot.qml.
    """
    qml6 = shutil.which("qml6")
    if qml6 is None or os.environ.get("CI"):
        pytest.skip("qml6/QtQuick not available; offscreen render test skipped")
    out = tmp_path / f"cost-{mode}.png"
    env = dict(os.environ, **SHOT_ENV)
    proc = subprocess.run([qml6, "tools/preview/screenshot.qml", "--", f"today={SHOT_DAY}", "component=CostPopout",
                           f"graphMode={mode}", f"out={out}"],
                          cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    if not out.is_file():
        pytest.skip(f"offscreen render unavailable here: {proc.stderr[-200:]}")
    assert "TypeError" not in proc.stderr and "ReferenceError" not in proc.stderr, proc.stderr[-600:]
    assert out.read_bytes() == (SHOTS / f"cost-{mode}.png").read_bytes(), (
        f"offscreen render of the cost flyout '{mode}' tab no longer matches "
        f"tests/render-baselines/cost-{mode}.png. Open BOTH images and compare before regenerating."
    )


def test_cost_popout_tooltip_shows_after_a_jump_between_bars(tmp_path):
    """The busiest Day bar's tooltip shows after the pointer jumps onto it from the next bar.

    When one move event carries the pointer from a bar straight into its neighbour (past the
    2 px gap), Qt delivers the new bar's enter BEFORE the old bar's exit. The late exit used to
    stop the tooltip timer the new bar had just started, so the tip never showed. The pointer
    is driven through Qt's own test events (`walk=hover` in screenshot.qml) and the baseline
    shows the tip; a render without it differs.
    """
    qml6 = shutil.which("qml6")
    if qml6 is None or os.environ.get("CI"):
        pytest.skip("qml6/QtQuick not available; offscreen render test skipped")
    cursors = tmp_path / "cursors"
    made = subprocess.run([sys.executable, "tools/preview/demo_gif.py", "cursors", str(cursors), CURSOR_PX],
                          cwd=REPO, capture_output=True, text=True)
    if made.returncode != 0:
        pytest.skip(f"no Breeze cursor theme here: {made.stderr[-200:]}")
    out = tmp_path / "cost-day-hover.png"
    env = dict(os.environ, **SHOT_ENV)
    proc = subprocess.run([qml6, "tools/preview/screenshot.qml", "--", f"today={SHOT_DAY}", "component=CostPopout",
                           "graphMode=day", "walk=hover", f"cursors={cursors}", f"out={out}"],
                          cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    if not out.is_file():
        pytest.skip(f"offscreen render unavailable here: {proc.stderr[-200:]}")
    assert "TypeError" not in proc.stderr and "ReferenceError" not in proc.stderr, proc.stderr[-600:]
    assert out.read_bytes() == (SHOTS / "cost-day-hover.png").read_bytes(), (
        "the hovered Day bar no longer renders like tests/render-baselines/cost-day-hover.png "
        "(the tooltip over the busiest bar). Open BOTH images and compare before regenerating."
    )


def test_extracted_components_do_not_size_themselves_from_parent():
    # Inside a layout the parent's size is DERIVED from its children, so a component sizing
    # itself from `parent.height` collapses to its floor.
    # A component's ROOT object must take such sizes from the host via a property.
    offenders = []
    for path in sorted((UI / "components").glob("*.qml")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.match(r"^ {4}(Layout\.\w+|width|height|implicitWidth|implicitHeight)\s*:.*\bparent\.(width|height)\b", line):
                offenders.append(f"{path.relative_to(REPO)}:{n}: {line.strip()}")
    assert not offenders, "component root sized from parent:\n" + "\n".join(offenders)


def test_render_env_is_pinned():
    """The render must not depend on the day or the developer's desktop icon theme."""
    kdeglobals = REPO / "tools" / "preview" / "xdg-config" / "kdeglobals"
    assert "Theme=breeze-dark" in kdeglobals.read_text()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", SHOT_DAY)
    shot_qml = (REPO / "tools" / "preview" / "screenshot.qml").read_text()
    assert 'argValue("today"' in shot_qml
