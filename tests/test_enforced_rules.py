"""Source-scanning guards for rules CLAUDE.md states in prose."""

import re
import sys
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
APPLET = REPO / "io.github.dlansama.tallybar" / "contents"
CODE_DIR = APPLET / "code"
UI = APPLET / "ui"
sys.path.insert(0, str(CODE_DIR))

import pricing_data  # noqa: E402


def _strip_qml_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


def _function_body(src: str, name: str) -> str:
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", src)
    assert m, f"function {name} not found"
    depth, i = 1, m.end()
    while depth:
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        i += 1
    return src[m.end():i - 1]


# 1 -------------------------------------------------------------------------
def test_no_plasma5_theme_or_plasmoid_enum_constants_in_ui():
    bad = []
    for f in UI.rglob("*"):
        if f.suffix not in (".qml", ".js"):
            continue
        code = _strip_qml_comments(f.read_text())
        for m in re.finditer(r"\b(?:Kirigami|PlasmaCore)\.Theme\b|\bPlasmoid\.[A-Z]\w*", code):
            bad.append(f"{f.relative_to(REPO)}: {m.group(0)}")
    assert not bad, "Plasma 6 API traps (use PlasmaCore.Types / the fixed palette): " + ", ".join(bad)


# 2 -------------------------------------------------------------------------
def test_usage_bars_never_recolour_by_percentage():
    compact = _strip_qml_comments((UI / "CompactRepresentation.qml").read_text())
    full = _strip_qml_comments((UI / "FullRepresentation.qml").read_text())
    for src, name, target in ((compact, "compactFillColor", "accentColor()"),
                              (full, "tabUsageColor", "tabAccentColor(")):
        body = _function_body(src, name)
        assert target in body, f"{name} must resolve to the provider accent"
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b|Qt\.rgba|\"(?:red|orange|amber|yellow)\"|\?|\bif\b|warning|critical",
                             body, re.I), f"{name} must not recolour by percentage"
    card = _strip_qml_comments((UI / "components" / "UsageLimitCard.qml").read_text())
    assert re.search(r"color:\s*\(root && root\.accentColor\) \? root\.accentColor\(\) : \"#[0-9a-fA-F]+\"", card), \
        "metric bar fill must bind to root.accentColor()"


# 3 -------------------------------------------------------------------------
def test_qml_provider_accents_match_backend():
    body = _function_body((UI / "lib" / "format.js").read_text(), "providerAccent")
    qml = dict(re.findall(r'providerKey === "(\w+)"\) return "(#[0-9a-fA-F]{6})"', body))
    qml["codex"] = re.findall(r'return "(#[0-9a-fA-F]{6})";', body)[-1]  # the default row is Codex's
    backend = dict(re.findall(r'"(\w+)": \{\*\*default_provider\([^)]*\), "accentColor": "(#[0-9a-fA-F]{6})"\}',
                              (CODE_DIR / "backend.py").read_text()))
    assert qml == backend, f"Fmt.providerAccent and backend accents differ: {qml} vs {backend}"


# 4 -------------------------------------------------------------------------
def test_python_exec_is_a_constant():
    src = (UI / "main.qml").read_text()
    m = re.search(r"property\s+string\s+pythonExec\s*:\s*(.+)", src)
    assert m
    assert re.fullmatch(r'"[^"$`]*"', m.group(1).strip()), "pythonExec must be a plain string literal"


# 7 -------------------------------------------------------------------------
@pytest.mark.parametrize("model,end", sorted(pricing_data.INTRO_RATE_ENDS.items()))
def test_introductory_price_has_not_expired(model, end):
    assert any(k == model for k, _ in pricing_data._FALLBACK_PRICING), f"{model} row missing"
    assert date.today() <= date.fromisoformat(end), (
        f"{model}: introductory rate ended {end}. Update its row in pricing_data._FALLBACK_PRICING "
        "to the list rate and fix/remove its INTRO_RATE_ENDS entry (master-plan B-5).")


# 8 -------------------------------------------------------------------------
def test_legacy_model_strings_never_map_gemini_3_1_pro():
    from providers import cost
    assert "gemini-3.1-pro" not in cost._LEGACY_MODEL_STRINGS


# 14 ------------------------------------------------------------------------
def test_qml_render_reads_shot_env_from_makefile():
    src = (REPO / "tests" / "test_qml_render.py").read_text()
    assert "SHOT_ENV := " in src, "tests/test_qml_render.py must read SHOT_ENV from the Makefile"
    assert not re.search(r"SHOT_ENV\s*=\s*dict\(", src), "SHOT_ENV must not be a hand-copied literal"


# 16 ------------------------------------------------------------------------
def test_atomic_write_recipe_lives_only_in_io_helpers():
    bad = []
    for f in CODE_DIR.rglob("*.py"):
        if f.name == "io_helpers.py":
            continue
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if re.search(r"\bos\.fsync\b|\bmkstemp\s*\(", line.split("#")[0]):
                bad.append(f"{f.relative_to(REPO)}:{n}")
    assert not bad, "use io_helpers.atomic_write_text: " + ", ".join(bad)
