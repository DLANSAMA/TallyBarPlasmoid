"""Every QML ``Text`` that renders a DYNAMIC string must pin ``textFormat: Text.PlainText``.

Qt's default is ``Text.AutoText``, which sniffs the string and renders it as rich text when
it looks like markup. A backend-derived value containing ``<img src="http://…">`` would then
become an outbound network fetch from the widget — a tracking/exfil beacon — and ``<b>``/
``<table>`` would let a provider response garble the panel. Backend strings reach the UI from
provider API responses, from local log files, and (for Antigravity model names) from the
remote ``GetAvailableModels`` RPC via ``learnedModelNames`` in the on-disk ledger.

Annotating by hand does not hold: a Text added later, or moved during a refactor, silently
reverts to AutoText. This test holds the invariant instead.

Static bindings — a bare string literal or an ``i18n("…")`` of literals — are exempt: they are
developer/translator-authored, not attacker-reachable, and annotating every Text would bury
the ones that matter. Same family as
``test_io_durability.py::test_no_duplicate_atomic_write_definitions``: pin the invariant in a
test rather than trusting a convention.
"""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
UI = REPO / "io.github.dlansama.tallybar" / "contents" / "ui"

# Calls whose result is developer/translator-authored rather than backend-derived.
_STATIC_CALLS = {"i18n", "i18nc", "i18np", "i18ncp", "qsTr", "qsTrId"}
_IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")


def _mask(src: str) -> str:
    """Blank out comments and string-literal CONTENTS, preserving length and newlines.

    Brace matching and identifier scanning both have to ignore a ``"{"`` or a ``// {`` that
    isn't real structure. Keeping the length identical means offsets into the masked copy
    still index the original.
    """
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in "\"'":
            quote, j = c, i + 1
            while j < n and src[j] != quote:
                if src[j] == "\\":
                    out[j] = " "
                    j += 1
                    if j < n:
                        out[j] = " "
                else:
                    out[j] = " "
                j += 1
            i = j + 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                out[i] = " "
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
                if src[i] != "\n":
                    out[i] = " "
                i += 1
            for k in range(i, min(i + 2, n)):
                out[k] = " "
            i += 2
            continue
        i += 1
    return "".join(out)


def _own_level_slices(masked: str, start: int):
    """Yield (a, b) spans of the block opening at ``start`` that are at ITS OWN nesting
    level — i.e. excluding every nested child block. A `textFormat` set on a nested
    MouseArea, or a `text:` belonging to a child Text, must not count as this block's."""
    depth, i, n = 0, start, len(masked)
    seg_start = None
    while i < n:
        ch = masked[i]
        if ch == "{":
            depth += 1
            if depth == 1:
                seg_start = i + 1
            elif depth == 2 and seg_start is not None:
                yield (seg_start, i)
                seg_start = None
        elif ch == "}":
            depth -= 1
            if depth == 1:
                seg_start = i + 1
            elif depth == 0:
                if seg_start is not None:
                    yield (seg_start, i)
                return
        i += 1


def _is_static_binding(expr: str) -> bool:
    """True when the expression can only produce developer/translator-authored text."""
    masked = _mask(expr)
    for ident in _IDENTIFIER.findall(masked):
        if ident not in _STATIC_CALLS:
            return False
    return True


def find_unpinned_texts(path: Path):
    """Return [(line, expr)] for every ``Text`` binding dynamic text without a textFormat."""
    src = path.read_text()
    masked = _mask(src)
    findings = []
    for m in re.finditer(r"\bText\s*\{", masked):
        brace = masked.index("{", m.start())
        spans = list(_own_level_slices(masked, brace))
        own_masked = "".join(masked[a:b] for a, b in spans)
        if "textFormat" in own_masked:
            continue
        # Locate `text:` at this block's own level, then read the expression from the
        # ORIGINAL source so the failure message shows the real binding.
        hit = None
        for a, b in spans:
            rel = re.search(r"(?<![\w.])text\s*:", masked[a:b])
            if rel:
                hit = a + rel.end()
                break
        if hit is None:
            continue
        line_end = src.find("\n", hit)
        expr = src[hit:line_end if line_end != -1 else len(src)].strip()
        if expr.startswith("{"):          # multi-line expression block — dynamic by nature
            expr = "{ … }"
        elif _is_static_binding(expr):
            continue
        findings.append((src[:hit].count("\n") + 1, expr))
    return findings


QML_FILES = sorted(UI.rglob("*.qml"))


def test_qml_files_were_found():
    # A path typo would otherwise make every test below pass vacuously.
    assert len(QML_FILES) >= 10, f"expected the UI tree, found {len(QML_FILES)} qml files"


@pytest.mark.parametrize("qml", QML_FILES, ids=lambda p: p.name)
def test_dynamic_text_pins_plaintext(qml):
    findings = find_unpinned_texts(qml)
    assert not findings, (
        f"{qml.relative_to(REPO)} has {len(findings)} Text element(s) rendering dynamic "
        f"text without `textFormat: Text.PlainText`, so Qt falls back to AutoText and will "
        f"render embedded markup as rich text:\n"
        + "\n".join(f"  line {ln}: text: {expr}" for ln, expr in findings)
    )


def test_checker_flags_a_dynamic_binding(tmp_path):
    """The checker must actually bite — a green suite from a broken parser is worthless."""
    f = tmp_path / "Bad.qml"
    f.write_text('import QtQuick\nItem {\n  Text {\n    text: someBackend.value\n  }\n}\n')
    assert find_unpinned_texts(f) == [(4, "someBackend.value")]


def test_checker_accepts_pinned_and_literal_bindings(tmp_path):
    f = tmp_path / "Good.qml"
    f.write_text(
        'import QtQuick\n'
        'Item {\n'
        '  Text { textFormat: Text.PlainText; text: backend.value }\n'   # pinned
        '  Text { text: i18n("Settings") }\n'                            # translator literal
        '  Text { text: "×" }\n'                                         # bare glyph
        '}\n'
    )
    assert find_unpinned_texts(f) == []


def test_checker_ignores_nested_children(tmp_path):
    """A textFormat on a nested child must not satisfy the parent, and a child's `text:`
    must not be mistaken for the parent's."""
    f = tmp_path / "Nested.qml"
    f.write_text(
        'import QtQuick\n'
        'Item {\n'
        '  Text {\n'
        '    text: outer.value\n'
        '    Rectangle { Text { textFormat: Text.PlainText; text: inner.value } }\n'
        '  }\n'
        '}\n'
    )
    assert find_unpinned_texts(f) == [(4, "outer.value")]
