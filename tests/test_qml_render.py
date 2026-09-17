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
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
UI = REPO / "io.github.dlansama.tallybar" / "contents" / "ui"
SHOTS = REPO / "docs" / "screenshots"


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
    # Local-only (needs qml6 + the QtQuick modules). The committed README screenshots ARE
    # the expected render of the mock fixture, so a UI change that alters them must be a
    # deliberate `make screenshots` that a human has LOOKED at — not a silent side effect.
    qml6 = shutil.which("qml6")
    if qml6 is None or os.environ.get("CI"):
        pytest.skip("qml6/QtQuick not available; offscreen render test skipped")
    out = tmp_path / f"{provider}.png"
    env = dict(os.environ, QML_XHR_ALLOW_FILE_READ="1", QT_FORCE_STDERR_LOGGING="1",
               QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software",
               XDG_ICON_THEME="breeze-dark", QT_QPA_PLATFORMTHEME="kde")  # mirrors Makefile SHOT_ENV
    proc = subprocess.run([qml6, "tools/preview/screenshot.qml", "--", f"provider={provider}", f"out={out}"],
                          cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    if not out.is_file():
        pytest.skip(f"offscreen render unavailable here: {proc.stderr[-200:]}")
    assert "TypeError" not in proc.stderr and "ReferenceError" not in proc.stderr, proc.stderr[-600:]
    assert out.read_bytes() == (SHOTS / f"widget-{provider}.png").read_bytes(), (
        f"offscreen render of '{provider}' no longer matches docs/screenshots/widget-{provider}.png. "
        "Open BOTH images and compare before regenerating."
    )


def test_extracted_components_do_not_size_themselves_from_parent():
    # Regression: EmptyStateSection was extracted with
    # `Layout.preferredHeight: Math.max(120, parent.height)` where the original read
    # `metricsScroller.height`. Inside a layout the parent's size is DERIVED from its
    # children, so the block collapsed to its floor and the sign-in button floated mid-body.
    # A component's ROOT object must take such sizes from the host via a property.
    offenders = []
    for path in sorted((UI / "components").glob("*.qml")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.match(r"^ {4}(Layout\.\w+|width|height|implicitWidth|implicitHeight)\s*:.*\bparent\.(width|height)\b", line):
                offenders.append(f"{path.relative_to(REPO)}:{n}: {line.strip()}")
    assert not offenders, "component root sized from parent:\n" + "\n".join(offenders)
