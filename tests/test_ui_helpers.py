"""Unit tests for contents/ui/lib/ui_helpers.js."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

UI_HELPERS_JS = (
    Path(__file__).parent.parent
    / "io.github.dlansama.tallybar"
    / "contents"
    / "ui"
    / "lib"
    / "ui_helpers.js"
)


def _run_js_fn(fn_name: str, *args):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available; QML UI helpers test skipped")
    assert UI_HELPERS_JS.is_file(), UI_HELPERS_JS

    script = f"""
    const h = require(process.argv[1]);
    const args = JSON.parse(process.argv[2]);
    const res = h['{fn_name}'].apply(null, args);
    console.log(JSON.stringify(res));
    """
    proc = subprocess.run(
        [node, "-e", script, str(UI_HELPERS_JS), json.dumps(list(args))],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return json.loads(proc.stdout.strip())


def test_tab_label():
    assert _run_js_fn("tabLabel", "gemini") == "Gemini"
    assert _run_js_fn("tabLabel", "antigravity") == "Antigravity"
    assert _run_js_fn("tabLabel", "codex") == "Codex"
    assert _run_js_fn("tabLabel", "claude") == "Claude"
    assert _run_js_fn("tabLabel", "grok") == "Grok"
    assert _run_js_fn("tabLabel", "unknown") == "Codex"


def test_provider_short_label():
    assert _run_js_fn("providerShortLabel", "antigravity") == "Antigr."
    assert _run_js_fn("providerShortLabel", "codex") == "Codex"
    assert _run_js_fn("providerShortLabel", "custom") == "custom"


def test_provider_login_site_and_url():
    assert _run_js_fn("providerLoginSite", "claude") == "claude.ai"
    assert _run_js_fn("providerLoginUrl", "claude") == "https://claude.ai/login"
    assert _run_js_fn("providerLoginUrl", "antigravity") == ""


def test_dashboard_and_status_urls():
    assert _run_js_fn("dashboardUrl", "claude") == "https://claude.ai/settings/usage"
    assert _run_js_fn("statusUrl", "claude") == "https://status.claude.com/"


def test_status_is_bad():
    assert _run_js_fn("statusIsBad", "missing-cookies") is True
    assert _run_js_fn("statusIsBad", "wallet-locked") is True
    assert _run_js_fn("statusIsBad", "ok") is False
    assert _run_js_fn("statusIsBad", "") is False


def test_menu_money_spacing():
    assert _run_js_fn("menuMoneySpacing", "$188") == "$ 188"
    assert _run_js_fn("menuMoneySpacing", "No dollar") == "No dollar"


def test_normalized_cost_line():
    assert _run_js_fn("normalizedCostLine", "$10 today", "Today") == "Today: $ 10"
    assert _run_js_fn("normalizedCostLine", "$50 last 30 days", "Last 30 days") == "Last 30 days: $ 50"
    assert _run_js_fn("normalizedCostLine", "Today: $5", "Today") == "Today: $ 5"


def test_compact_usd():
    assert _run_js_fn("compactUsd", 1500) == "$1.5K"
    assert _run_js_fn("compactUsd", 250) == "$250"
    assert _run_js_fn("compactUsd", 0.004) == "<$0.01"
    assert _run_js_fn("compactUsd", 12.34) == "$12.34"


def test_pretty_model_name():
    assert _run_js_fn("prettyModelName", "claude-opus-4-7") == "Claude Opus 4.7"
    assert _run_js_fn("prettyModelName", "gemini-3.5-flash") == "Gemini 3.5 Flash"
    assert _run_js_fn("prettyModelName", "Unknown") == "Other"
    assert _run_js_fn("prettyModelName", "Already Spaced Model") == "Already Spaced Model"


@pytest.mark.parametrize("text, dp, gs, expected", [
    ("12,50", ",", ".", 12.5),        # de_DE decimal comma — used to save as 1250
    ("1.234,56", ",", ".", 1234.56),  # de_DE with grouping
    ("1.234", ",", ".", 1234.0),      # de_DE group separator (3 digits) stays a group
    ("12.50", ",", ".", 12.5),        # C-style decimal typed in a comma locale
    ("1 234,5", ",", " ", 1234.5),  # fr_FR narrow no-break space grouping
    ("1,234.56", ".", ",", 1234.56),  # en_US
    ("1,234", ".", ",", 1234.0),      # en_US grouping (the case the old code handled)
    ("250", ".", ",", 250.0),
    ("", ".", ",", None),
    ("abc", ".", ",", None),
    ("-5", ".", ",", None),
    ("1.2.3", ".", ",", None),
])
def test_parse_locale_amount(text, dp, gs, expected):
    assert _run_js_fn("parseLocaleAmount", text, dp, gs) == expected
